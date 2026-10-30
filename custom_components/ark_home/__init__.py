"""ARK Home Monitor — reports system health to ARK Home."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import time
from collections import deque
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, __version__ as HA_VERSION
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import (
    API_BASE,
    CHECKIN_INTERVAL_SECONDS,
    CONF_ALLOW_RESTART,
    CONF_SECRET,
    CONF_SYSTEM_ID,
    DOMAIN,
    LOW_BATTERY_REPORT_PCT,
    MAX_ERROR_LINES,
    MONITORED_DOMAINS,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

SECURITY_BINARY = {"tamper": "tamper", "smoke": "smoke", "moisture": "water_leak", "safety": "panic"}
CLEARABLE_BINARY = {"moisture": "water_leak", "smoke": "smoke"}
POWER_UNITS = {"W", "kW", "A", "mA"}


def _to_amps(value: Any, unit: str | None) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if unit == "A":
        return v
    if unit == "mA":
        return v / 1000
    if unit == "W":
        return v / 230
    if unit == "kW":
        return v * 1000 / 230
    return None


class _ErrorCollector(logging.Handler):
    """Keeps the most recent ERROR lines (message only, truncated)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.lines: deque[str] = deque(maxlen=MAX_ERROR_LINES)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(f"custom_components.{DOMAIN}"):
            return
        try:
            self.count += 1
            self.lines.append(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(record.created))} {record.name}: {record.getMessage()}"[:500])
        except Exception:  # noqa: BLE001
            pass


class ArkMonitor:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.errors = _ErrorCollector()
        self.events: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._unsubs: list[Any] = []
        self._pending_send = None
        self.watch: dict[str, Any] | None = None
        self._last_power_push = 0.0

    @property
    def allow_restart(self) -> bool:
        return bool(self.entry.options.get(CONF_ALLOW_RESTART, False))

    async def start(self) -> None:
        logging.getLogger().addHandler(self.errors)
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(seconds=CHECKIN_INTERVAL_SECONDS)))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed))
        self._unsubs.append(async_call_later(self.hass, 30, self._tick))

    async def stop(self) -> None:
        logging.getLogger().removeHandler(self.errors)
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def _tick(self, _now=None) -> None:
        await self.checkin()

    @callback
    def _state_changed(self, event: Event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or (old is not None and old.state == new.state):
            return
        if self.watch and new.entity_id == self.watch.get("entity_id"):
            amps = _to_amps(new.state, new.attributes.get("unit_of_measurement"))
            if amps is not None and amps >= float(self.watch.get("warn_amps") or 0):
                # High use: report at most once a minute so ARK can time how long it lasts.
                if time.monotonic() - self._last_power_push >= 60:
                    self._last_power_push = time.monotonic()
                    self._schedule_send()
            return
        if new.domain == "binary_sensor" and new.state == "off" and old is not None and old.state == "on" \
                and new.attributes.get("device_class") in CLEARABLE_BINARY:
            # Leak/smoke cleared: report so the customer gets an "all clear".
            self._schedule_send()
            return
        kind = None
        if new.domain == "alarm_control_panel" and new.state == "triggered":
            kind = "alarm_triggered"
        elif new.domain == "binary_sensor" and new.state == "on":
            kind = SECURITY_BINARY.get(new.attributes.get("device_class"))
        if not kind:
            return
        self.events.append({
            "type": kind,
            "entity_id": new.entity_id,
            "name": str(new.attributes.get("friendly_name") or new.entity_id)[:255],
            "at": new.last_changed.isoformat(),
        })
        # Urgent: report within seconds, batching bursts.
        self._schedule_send()

    def _schedule_send(self) -> None:
        if self._pending_send is None:
            self._pending_send = async_call_later(self.hass, 5, self._send_now)

    async def _send_now(self, _now=None) -> None:
        self._pending_send = None
        await self.checkin()

    # ---- data collection -------------------------------------------------

    def _system_stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cpu_count": os.cpu_count()}
        try:
            du = shutil.disk_usage(self.hass.config.config_dir)
            out["disk_used_pct"] = round(du.used / du.total * 100, 1)
        except Exception:  # noqa: BLE001
            pass
        try:
            info = {}
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    k, v = line.split(":", 1)
                    info[k] = int(v.strip().split()[0])
            out["mem_used_pct"] = round((1 - info["MemAvailable"] / info["MemTotal"]) * 100, 1)
        except Exception:  # noqa: BLE001
            pass
        try:
            out["load_1m"] = round(os.getloadavg()[0], 2)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _collect(self) -> dict[str, Any]:
        hass = self.hass
        registry = er.async_get(hass)
        unavailable, batteries, updates, power, active = [], [], [], [], []
        total = 0
        backup_last = None
        for state in hass.states.async_all():
            entry = registry.async_get(state.entity_id)
            if entry and (entry.disabled_by or entry.hidden_by):
                continue
            if state.domain == "update":
                if state.state == "on":
                    updates.append({
                        "entity_id": state.entity_id,
                        "title": str(state.attributes.get("title") or state.attributes.get("friendly_name") or state.entity_id)[:255],
                        "installed": state.attributes.get("installed_version"),
                        "latest": state.attributes.get("latest_version"),
                    })
                continue
            if state.entity_id.startswith("sensor.backup_last_successful") and state.state not in (STATE_UNAVAILABLE, "unknown"):
                backup_last = state.state
            if state.domain == "sensor" and state.attributes.get("device_class") in ("power", "current"):
                unit = state.attributes.get("unit_of_measurement")
                if unit in POWER_UNITS:
                    try:
                        power.append({
                            "entity_id": state.entity_id,
                            "name": str(state.attributes.get("friendly_name") or state.entity_id)[:255],
                            "value": round(float(state.state), 3),
                            "unit": unit,
                        })
                    except ValueError:
                        pass
            if state.domain == "binary_sensor" and state.state == "on" and state.attributes.get("device_class") in CLEARABLE_BINARY:
                active.append({
                    "type": CLEARABLE_BINARY[state.attributes.get("device_class")],
                    "entity_id": state.entity_id,
                    "name": str(state.attributes.get("friendly_name") or state.entity_id)[:255],
                })
            if state.domain not in MONITORED_DOMAINS:
                continue
            if entry and entry.entity_category is not None:
                continue
            total += 1
            if state.state == STATE_UNAVAILABLE:
                unavailable.append({
                    "entity_id": state.entity_id,
                    "name": str(state.attributes.get("friendly_name") or state.entity_id)[:255],
                    "domain": state.domain,
                    "since": state.last_changed.isoformat(),
                })
            elif state.domain == "sensor" and state.attributes.get("device_class") == "battery":
                try:
                    level = float(state.state)
                except ValueError:
                    continue
                if 0 <= level <= LOW_BATTERY_REPORT_PCT:
                    batteries.append({"entity_id": state.entity_id, "name": str(state.attributes.get("friendly_name") or state.entity_id)[:255], "level": level})
        repairs = []
        for issue in ir.async_get(hass).issues.values():
            if issue.active and not issue.dismissed_version:
                repairs.append({"domain": issue.domain, "issue_id": issue.issue_id[:255], "severity": str(issue.severity)})
        return {
            "entities": {"total": total, "unavailable": unavailable[:500]},
            "batteries": batteries[:500],
            "updates": updates[:200],
            "repairs": repairs[:200],
            "backup": {"last_success": backup_last},
            "power_sensors": power[:50],
            "active_safety": active[:100],
        }

    # ---- transport -------------------------------------------------------

    async def checkin(self) -> None:
        async with self._lock:
            from homeassistant.helpers import instance_id

            payload: dict[str, Any] = {
                "version": VERSION,
                "ha_version": HA_VERSION,
                "instance_id": await instance_id.async_get(self.hass),
                "allow_restart": self.allow_restart,
                "system": await self.hass.async_add_executor_job(self._system_stats),
                **self._collect(),
                "errors": {"count": self.errors.count, "recent": list(self.errors.lines)[-10:]},
                "events": self.events[:50],
                "command_results": self.results[:20],
            }
            body = json.dumps(payload, separators=(",", ":"), default=str)
            ts = str(int(time.time()))
            sig = hmac.new(self.entry.data[CONF_SECRET].encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
            session = async_get_clientsession(self.hass)
            try:
                async with session.post(
                    f"{API_BASE}/checkin",
                    data=body,
                    headers={
                        "content-type": "application/json",
                        "x-ark-system": self.entry.data[CONF_SYSTEM_ID],
                        "x-ark-timestamp": ts,
                        "x-ark-signature": sig,
                    },
                    timeout=30,
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("ARK Home check-in refused (%s)", resp.status)
                        return
                    data = await resp.json(content_type=None)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("ARK Home check-in failed: %s", err)
                return
            # Delivered: clear what was sent.
            self.events = self.events[len(payload["events"]):]
            self.results = self.results[len(payload["command_results"]):]
            self.errors.count = 0
            commands = data.get("commands") or []
            watch = data.get("watch")
            self.watch = watch if isinstance(watch, dict) and watch.get("entity_id") else None
        if commands:
            await self._run_commands(commands)

    async def _run_commands(self, commands: list[dict[str, Any]]) -> None:
        restart = False
        for cmd in commands:
            kind, cid = cmd.get("kind"), cmd.get("id")
            try:
                if kind == "health_check":
                    self.results.append({"id": cid, "ok": True, "result": {"checked": True}})
                elif kind == "logs":
                    self.results.append({"id": cid, "ok": True, "result": {"recent_errors": list(self.errors.lines)}})
                elif kind == "backup":
                    if self.hass.services.has_service("backup", "create_automatic"):
                        await self.hass.services.async_call("backup", "create_automatic", {}, blocking=False)
                    elif self.hass.services.has_service("hassio", "backup_full"):
                        await self.hass.services.async_call("hassio", "backup_full", {}, blocking=False)
                    else:
                        raise RuntimeError("No backup service available")
                    self.results.append({"id": cid, "ok": True, "result": {"started": True}})
                elif kind == "restart":
                    if not self.allow_restart:
                        raise RuntimeError("Remote restart not allowed on this system")
                    self.results.append({"id": cid, "ok": True, "result": {"restarting": True}})
                    restart = True
                else:
                    raise RuntimeError(f"Unknown command {kind}")
            except Exception as err:  # noqa: BLE001
                self.results.append({"id": cid, "ok": False, "result": {"error": str(err)[:300]}})
        # Report results straight away (before any restart).
        await self.checkin()
        if restart:
            await self.hass.services.async_call("homeassistant", "restart", {}, blocking=False)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    monitor = ArkMonitor(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = monitor
    await monitor.start()
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.data[DOMAIN][entry.entry_id].checkin()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    monitor: ArkMonitor = hass.data[DOMAIN].pop(entry.entry_id)
    await monitor.stop()
    return True
