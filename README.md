# ARK Home Monitor

Home Assistant integration by ARK Home (arkhome.uk). Reports system health to the ARK Home workspace so our team can spot problems early.

## What it sends (every 5 minutes, signed with a per-system key)
- Heartbeat, Home Assistant version, disk/memory/load
- Devices that are unavailable, batteries at or below 25%
- Pending updates, active repair issues, last successful backup
- Recent error-log lines (message text only)
- Security events straight away: alarm triggered, tamper, smoke, water leak, safety

Never sent: camera images, locations, passwords or tokens.

## Install (HACS)
1. HACS → ⋮ → Custom repositories → add this repository, type **Integration**.
2. Install **ARK Home Monitor**, restart Home Assistant.
3. Settings → Devices & services → Add integration → **ARK Home Monitor**, enter the pairing code from the ARK workspace.

## Updates
Publish a GitHub release with a new tag (e.g. `v1.0.1`) and bump `version` in `manifest.json` and `const.py`. HACS shows it in each system's Updates panel.

## Remote restart
Off by default. Turn on in the integration's **Configure** options only if agreed with the customer.
