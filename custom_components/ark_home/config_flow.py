"""Config flow: pair with ARK Home using a one-time code from the ARK workspace."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import instance_id

from .const import API_BASE, CONF_ALLOW_RESTART, CONF_SECRET, CONF_SYSTEM_ID, DOMAIN, VERSION

CODE_RE = re.compile(r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")


class ArkHomeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle pairing."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input["code"].strip().upper()
            if len(code) == 8 and "-" not in code:
                code = f"{code[:4]}-{code[4:]}"
            if not CODE_RE.match(code):
                errors["code"] = "invalid_code"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    async with session.post(
                        f"{API_BASE}/pair",
                        json={"code": code, "instance_id": await instance_id.async_get(self.hass), "version": VERSION},
                        timeout=20,
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status == 404:
                            errors["code"] = "invalid_code"
                        elif resp.status != 200:
                            errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    errors["base"] = "cannot_connect"
                if not errors:
                    await self.async_set_unique_id(data["system_id"])
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"ARK Home – {data.get('name', 'Monitor')}",
                        data={CONF_SYSTEM_ID: data["system_id"], CONF_SECRET: data["secret"]},
                        options={CONF_ALLOW_RESTART: False},
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ArkHomeOptionsFlow()


class ArkHomeOptionsFlow(config_entries.OptionsFlow):
    """Local opt-in for remote restart (agreed with the customer)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_ALLOW_RESTART, False)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_ALLOW_RESTART, default=current): bool}),
        )
