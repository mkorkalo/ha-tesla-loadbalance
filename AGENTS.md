# Instructions for AI agents

You are most likely helping a **non-technical owner** install this system on
their own computer. [INSTALL.md](INSTALL.md) is the procedure; this file
tells you how to carry it out with them.

## How to work with the owner

- Use plain language and no jargon. One step at a time, and say *why* before
  *what*.
- You run the commands. Ask the owner only for what you cannot do: physical
  actions, logins in a browser, values only they know.
- When they must act, say exactly what to do and what they should see, e.g.
  *"Sit in the car, tap your key card on the reader behind the cup holders,
  then press Confirm on the screen."*
- Confirm before anything hard to undo: deleting data, overwriting their
  files, changing router or system settings.
- Keep a short checklist of the steps below and tell them where you are.

## Safety rules (never break these)

1. `ev_house_max_current` must be **at most the main fuse rating** and
   `ev_shared_max_current` **at most the EV circuit's breaker rating**. Use
   the numbers the owner reads from their fuse box or electrician. Never guess,
   never raise them to "make it charge faster". If they are unsure, set lower
   values (e.g. 16 and 10) and say why.
2. Never instruct the owner to open the electrical panel or install the meter
   themselves. That is an electrician's job.
3. Do not expose any port to the internet, set up port forwarding, or
   disable the firewall.
4. Never commit or print secrets: `.env`, `appdaemon/secrets.yaml`, tokens,
   TeslaMate login. VINs belong only in the git-ignored `apps.yaml`.
5. Do not change the control logic (`appdaemon/apps/*.py`) as part of an
   installation. If something seems to need a code change, explain and stop.
6. Before calling the install done, the **meter check** (INSTALL.md step 7.5)
   must pass. If the meter does not see the car's current, the system can
   over-allocate. Tell the owner plainly and do not hand off.

## Step 0: interview

Ask these first, a few at a time, and record the answers:

1. How many Teslas (1 or 2), which models, and their VINs?
2. Where do they park, and how far is that from the computer? Any walls between?
3. Main fuse size (e.g. 3×25 A)? EV circuit breaker size (e.g. 16 A)? Do both
   cars charge from the same circuit?
4. Which charger(s)? (Wall Connector, Mobile Connector, other.)
5. Which energy meter (Shelly Pro 3EM, Shelly 3EM, other)? Is it installed?
   Does it measure the whole house?
6. Is Home Assistant, MQTT or TeslaMate already running somewhere? (See
   "Existing installations" below.)
7. Country / spot price area, and roughly what they pay on top of the spot
   price per kWh (transfer, tax, margin)? If unknown, leave the default.
8. When must the cars be ready in the morning (weekdays / weekends)?

If a required piece of hardware is missing (no Bluetooth, no meter), stop
and explain what to get. Use the README's hardware table.

## Steps 1–7

Follow [INSTALL.md](INSTALL.md) in order. Notes for you:

- **Prerequisites:** check `docker compose version`, `bluetoothctl list`
  (one controller), `hostname -I`, and free disk space (`df -h`, need ~10 GB).
- **`.env`:** generate both secrets yourself with `openssl rand -base64 32`.
  Tell the owner to save `TESLAMATE_ENCRYPTION_KEY` in their password manager.
- **Home Assistant onboarding, TeslaMate sign-in and the BLE key pairing**
  need the owner in a browser or in the car. Guide them and wait.
- **Prefixes:** choose short lowercase names with the owner (e.g. `model3`,
  `modely`, or the cars' nicknames, `[a-z0-9]` only). Entity ids come from
  the MQTT `name:` fields, so `name: "Model3 Battery Level"` →
  `sensor.model3_battery_level`. Keep names, ids and `prefix:` consistent.
- **Phase sensors:** find the real entity ids rather than guessing:

  ```bash
  TOKEN=$(grep ha_token appdaemon/secrets.yaml | awk '{print $2}')
  curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8123/api/states \
    | python3 -c "import json,sys; [print(s['entity_id'], s['state'], s['last_updated']) for s in json.load(sys.stdin) if 'current' in s['entity_id']]"
  ```

  Run it twice a few seconds apart: the phase sensors must change. Data
  older than ~10 s makes the controller fall back to safe mode.
- **Config check:** after `docker compose restart homeassistant appdaemon`,
  check Home Assistant → Settings → System → Repairs or
  `docker compose logs homeassistant | grep -i error` for YAML errors. Then wait
  for `All required entities present` in `docker compose logs appdaemon`.
- **Live test (step 7):** have the owner start charging; watch
  `sensor.ev_charge_allocation` and the phase currents together. Read state
  with:

  ```bash
  curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8123/api/states/sensor.ev_charge_allocation | python3 -m json.tool
  ```

## Existing installations

If the owner already has Home Assistant, MQTT or TeslaMate, reuse them:

- Remove the duplicate services from `docker-compose.yml` (keep at least
  `tesla-ble-http-proxy` and `appdaemon`, unless AppDaemon is already
  available too, e.g. the Home Assistant OS add-on).
- **Home Assistant OS:** use the official *Mosquitto broker* and *AppDaemon*
  add-ons. Copy `appdaemon/apps/*.py` and `apps.yaml` into the add-on's apps
  folder, and put the package in `/config/packages/`. The BLE proxy still
  needs a Linux machine with Docker and Bluetooth near the cars; a Raspberry Pi
  is enough. Point `ble_proxy_url` at it.
- Set `ha_url` in `appdaemon/appdaemon.yaml`, `ble_proxy_url` and
  `teslamate_url` in `apps.yaml` to wherever those services actually run.
- Existing TeslaMate MQTT sensors may already exist under other names. Either
  add the package's sensors alongside them, or use the existing names by
  choosing a matching `prefix` (the apps expect `sensor.<prefix>_battery_level`,
  `_charging_state`, `_charger_actual_current`, `_charge_current_request_max`,
  `_charge_limit_soc`, `_state`, `_outside_temp` and
  `binary_sensor.<prefix>_plugged_in`).

## Repository map

| Path | What |
|---|---|
| `appdaemon/apps/charge_amps_limit.py` | 1 Hz current balancer. `decide()` is pure. |
| `appdaemon/apps/charge_timing.py` | Nightly cheapest-window scheduler. |
| `examples/apps.yaml` | App config template → `appdaemon/apps/apps.yaml` |
| `examples/ev_charging.yaml` | HA package template → `homeassistant/packages/` |
| `examples/dashboard.yaml` | Lovelace card |
| `docker-compose.yml`, `.env.example`, `mosquitto/` | All-in-one deployment |
| `tests/` | Offline tests: `python3 tests/test_charge_amps_limit.py && python3 tests/test_charge_timing.py` |
| `docs/DESIGN.md` | Control logic and safety model |

## Developing

Only when explicitly asked to change the code: run both test files (no
dependencies needed) before and after. Keep `decide()` free of I/O, and keep
the safety invariants in docs/DESIGN.md: reservations only fall on settle,
reductions are immediate, increases are dwelled and ramped.
