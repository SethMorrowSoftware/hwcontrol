# Facility Thermostat Dashboard (Honeywell Home / Resideo)

A self-hosted web dashboard to monitor and control many Honeywell Home / Resideo
thermostats across a facility, with three capabilities layered on top of the
Resideo API:

1. **Control** — see every zone's temperature, humidity, and online status; change
   mode, heat/cool setpoints, holds, and fan from one screen.
2. **Scheduling** — application-level schedules ("every weekday at 6pm, set the
   warehouse to 62°F") that work across all your thermostats regardless of their
   own onboard schedules.
3. **Automations** — an event-driven rules engine that reacts to **MQTT messages
   from your own broker**. The headline use case: when your generator's transfer
   switch reports it's carrying the load, shed non-critical zones and duty-cycle
   the rest so they share the generator instead of all running at once — then
   restore everything when utility power returns.

---

## ⚠️ Read this first: protect your API credentials

Your Honeywell API key/secret pair is a password. If it has ever been pasted
into a chat, screenshot, ticket, or committed to git, treat it as **compromised**
and rotate it before deploying:

1. Go to <https://developer.honeywellhome.com> → **My Apps** → your app.
2. Regenerate the secret (or delete and recreate the app).
3. Put the **new** key/secret in your `.env`.

Never commit `.env` or `tokens.json` — both hold secrets and are already listed
in `.gitignore`. Anything pasted into a chat, screenshot, or ticket can leak.

---

## Two constraints worth understanding up front

These come from the Resideo API itself, not this app:

**There is no native MQTT and no webhook-to-your-server.** Resideo does not push
device state to an arbitrary endpoint you control. (Their only push mechanism is an
enterprise Azure Event Hub integration.) So this app is a **bridge**: it *polls*
the Resideo API on a timer, *publishes* the state it reads to your MQTT broker, and
*subscribes* to command/trigger topics on that same broker. Your generator's status
message travels: `transfer switch → your broker → this app`. This app never talks
to Resideo faster than the poll interval, so a state change on a thermostat becomes
visible within one poll cycle, not instantly.

**The rate limits are tight.** The Resideo "Basic" developer plan is sized roughly
for **polling ~20 devices every 5 minutes**. This app is built around that: each
poll fetches your locations once, then **one call per location** returns every
thermostat there — and a client-side rate limiter (minimum spacing + rolling
hourly cap) sits underneath as a safety net. If you have more than ~20 devices or
want faster polling, email **developerinfo@resideo.com** to request a higher limit,
then lower `POLL_INTERVAL_SECONDS`. Automations and rotations also issue control
calls, so keep rotation intervals sane (the app enforces a 5-minute minimum, which
also protects compressors from short-cycling).

---

## Quick start

```bash
# 1. Install dependencies (Python 3.10+)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
#   edit .env: put your NEW api key/secret in, set HONEYWELL_REDIRECT_URI to match
#   the redirect URI registered on the developer portal EXACTLY (see below).

# 3. Authorize your Resideo account (one time) — either method works:
python authorize.py          # CLI: opens the consent flow, saves tokens.json
#   ...or just start the server and click "Connect Honeywell" in the dashboard.

# 4. Run
python -m uvicorn app:app --host 0.0.0.0 --port 8010
#   open http://localhost:8010
```

> **Port 8010, not 8000.** The default is 8010 so this can share a host with the
> GenWatch generator monitor (which listens on 8000). See
> [Running alongside other services](#running-alongside-other-services) below.

Tokens are stored in `tokens.json` (chmod 600). Resideo **rotates the refresh
token on every refresh**, and this app persists the new one automatically — so you
only authorize once, and it keeps working as long as the app can write that file.

### The "redirect URL does not match" error

This is the single most common setup problem. The `HONEYWELL_REDIRECT_URI` in
your `.env` must be **byte-for-byte identical** to the Redirect URI registered on
the developer portal — same scheme (`http`/`https`), same host, same port, same
path, no trailing slash difference.

- Portal has `http://localhost:8010/auth/callback` → `.env` must be exactly that.
- `http://localhost:8010/auth/callback` ≠ `http://localhost:8010/auth/callback/`
- `http://127.0.0.1:8010/...` ≠ `http://localhost:8010/...`

If you run the app on a different port, register a redirect URI with that port
and set `HONEYWELL_REDIRECT_URI` to match.

If you change one, change the other to match.

---

## The dashboard

Served at `/`. It's a single self-contained page (no external fonts or CDNs, works
on an air-gapped LAN) that polls the local API every ~10s. Four tabs:

- **Zones** — a card per thermostat: current temp/humidity, online dot, mode chips,
  heat/cool steppers, hold selector, and Apply. Changes you're typing are preserved
  across background refreshes until you Apply or discard.
- **Automations** — the generator load-shed setup, a live status strip (active
  rotations, saved snapshots, MQTT connection), the list of rules, and a custom
  rule builder. Details below.
- **Schedules** — time-of-day rules with day-of-week pickers.
- **Alerts** — a feed of offline/again events, out-of-range temperatures, and a log
  of every automation action taken.

### Optional access gate

Set `DASHBOARD_TOKEN` in `.env` to require `?token=...` in the URL (or an `X-Token`
header) for the dashboard and API. This is a **light guard, not real
authentication** — for production, put the app behind a VPN or a reverse proxy that
handles auth/TLS.

---

## Generator load-shed — walkthrough

This is the turnkey path for the main use case. Open **Automations → Generator
load-shed** and fill in:

- **Generator status topic** — the MQTT topic your transfer switch / power monitor
  publishes to, e.g. `facility/generator/status`.
- **JSON field** (optional) — if the payload is JSON like `{"power":{"source":"generator"}}`,
  put the dot-path `power.source` here. Leave blank if the payload is a plain string
  like `on` / `off`.
- **Payload when ON generator** / **when back on utility** — the values to match,
  e.g. `on` and `off`, or `generator` and `mains`.
- **Turn these zones OFF** — non-critical zones that should simply shut off on
  generator power.
- **Rotate these zones** — critical zones that should keep *some* conditioning.
  Choose **how many run at once**, the **interval** (≥5 min), and the **setpoints**
  to hold while a zone is in its "on" slice.

Click **Create generator rules**. This creates two paired automations:

1. **On generator** (`facility/generator/status` = `on`):
   - **snapshots** the current settings of all involved zones as `pre_generator`,
   - **sets** the non-critical zones to `Off`,
   - **starts a rotation** on the critical group: every N minutes the "on" window
     slides by one, so each unit gets a turn while only `run_count` run at any moment.
2. **On utility** (`facility/generator/status` = `off`):
   - **stops** the rotation,
   - **restores** every zone from the `pre_generator` snapshot — exactly the mode,
     setpoints, and hold each had before the outage.

To verify it without waiting for an outage, use **Run now** on either rule (this
executes the actions immediately, ignoring the trigger — it works even if MQTT is
disabled). To go live, make sure `MQTT_ENABLED=true` and the broker is connected
(the status strip shows this).

**Restart-safe.** Snapshots, the trigger's last-seen state, and any active
rotation are all persisted, so if the app restarts mid-outage: the running
rotation resumes on its schedule, a *retained* "on" message replayed by the
broker will **not** re-fire the shed rule or clobber the good snapshot, and the
genuine transition to "off" still stops the rotation and triggers the restore.
(For this to work across a restart, publish the generator status **retained**.)

---

## Automations — the rule model

An automation is: **one trigger** (an MQTT topic + a match condition) and **an
ordered list of actions**. Rules are stored in `automations.json`.

### Trigger matching

| Match type   | Fires when…                                              |
|--------------|----------------------------------------------------------|
| `equals`     | payload equals the value (case-insensitive by default)   |
| `not_equals` | payload does not equal the value                         |
| `contains`   | payload contains the value as a substring                |
| `regex`      | payload matches the regular expression                   |
| `gt` / `lt`  | payload (as a number) is greater / less than the value   |
| `any`        | any message on the topic                                 |

If **`field`** is set, the payload is parsed as JSON and that dot-path is extracted
first, then the match is applied to the extracted value (e.g. `field: "load.pct"`,
type `gt`, value `85`).

**`retrigger`** controls repeats:
- `on_change` (default) — fire only on the *rising edge* into a matching value.
  Republished/retained duplicates of the same value are ignored. This is what you
  want for a generator status that gets re-announced periodically.
- `every_message` — fire on every matching message.

### Action types

| Action          | What it does                                                                 |
|-----------------|------------------------------------------------------------------------------|
| `set`           | Apply `values` (mode / heatSetpoint / coolSetpoint / hold / fan) to targets. |
| `snapshot`      | Save targets' current settings under `name` for later restore.               |
| `restore`       | Restore every device saved in snapshot `name`.                               |
| `rotate`        | Duty-cycle a group: keep `run_count` running, slide the window every `interval_minutes`; on/off units get `on_values` / `off_values`. |
| `stop_rotation` | Stop the rotation with `rotation_id`.                                         |

**Targets** are `"all"`, a single `"deviceID"`, or a list `["id1","id2"]`.

The rotation only sends commands to units whose on/off membership actually changes
each tick, to stay kind to the rate limit.

### Custom rules

**Automations → Build a custom rule** exposes all of the above: pick a topic and
match, then add actions one at a time (each with its own zone picker and
parameters). Use this to react to anything your broker publishes — a load
percentage crossing a threshold, a demand-response signal, a BMS mode change, etc.

### Example rule (JSON)

```json
{
  "name": "High load: shed warehouse",
  "enabled": true,
  "trigger": {
    "topic": "facility/power/load",
    "match": { "type": "gt", "value": 85, "field": "pct" },
    "retrigger": "on_change"
  },
  "actions": [
    { "type": "snapshot", "name": "pre_shed", "targets": ["TCC-9"] },
    { "type": "set", "targets": ["TCC-9"], "values": { "mode": "Off" } }
  ]
}
```

---

## MQTT topic reference

Base topic is configurable via `MQTT_BASE_TOPIC` (default `honeywell`).

### Published by this app (state out)

| Topic                              | Payload                                   | Retained |
|------------------------------------|-------------------------------------------|----------|
| `honeywell/<deviceID>/state`       | full JSON state of the thermostat         | yes      |
| `honeywell/<deviceID>/online`      | `true` / `false`                          | yes      |
| `honeywell/<deviceID>/event`       | JSON describing a change (mode/setpoint/…) | no      |
| `honeywell/alerts`                 | JSON alert (offline, out-of-range, automation action) | no |
| `honeywell/status/bridge`          | `online` / `offline` (last-will)          | yes      |

### Commands (control in)

Publish to these to control a thermostat from outside the dashboard:

| Topic                                | Payload                                             |
|--------------------------------------|-----------------------------------------------------|
| `honeywell/<deviceID>/set`           | JSON, e.g. `{"mode":"Heat","heatSetpoint":70}`      |
| `honeywell/<deviceID>/set/heat`      | a number, e.g. `70` (sets heat setpoint, temp hold) |
| `honeywell/<deviceID>/set/cool`      | a number, e.g. `74`                                 |
| `honeywell/<deviceID>/set/mode`      | `Heat` / `Cool` / `Auto` / `Off`                    |
| `honeywell/<deviceID>/set/fan`       | `On` / `Auto` / `Circulate`                         |

### Trigger topics (automations in)

Any topic you name in a rule's trigger is subscribed to automatically (and
unsubscribed when the rule is removed/disabled). These are **your** topics — e.g.
`facility/generator/status` — published by your equipment, not by this app.

---

## Architecture

Synchronous, thread-based, and deliberately boring for reliability:

```
                    ┌─────────────────── FastAPI (app.py) ───────────────────┐
                    │  REST API  +  serves the dashboard (static/index.html)  │
                    └───▲───────────────▲───────────────▲───────────────▲─────┘
                        │               │               │               │
              poller thread      APScheduler      AutomationEngine   MqttBridge
             (polls Resideo,   (time-of-day     (MQTT-triggered     (paho; state
              per location,     schedules)       rules: shed,        out + command
              publishes MQTT)                    rotate, restore)    & trigger in)
                        │                               │               │
                        └───────────── apply_action() ──┴───────────────┘
                                            │
                                  HoneywellClient (honeywell_client.py)
                              OAuth2 + token rotation + rate limiter + HTTP
                                            │
                                   Resideo API (api.honeywellhome.com/v2)
```

### Files

| File                 | Responsibility                                                        |
|----------------------|----------------------------------------------------------------------|
| `app.py`             | FastAPI app, REST endpoints, poller loop, wires everything together. |
| `honeywell_client.py`| Resideo OAuth (with refresh-token rotation), rate limiting, thermostat read/write. |
| `state_store.py`     | Normalizes device state, diffs polls into change events, tracks alerts. |
| `scheduler.py`       | Application-level time-of-day schedules (APScheduler cron).          |
| `automation.py`      | The MQTT-triggered rules engine (matching, snapshot/restore, rotation). |
| `mqtt_bridge.py`     | paho-mqtt bridge: publishes state, routes command & trigger topics.  |
| `config.py`          | Loads config from environment / `.env`.                              |
| `authorize.py`       | Standalone CLI to complete OAuth and list devices without the server. |
| `static/index.html`  | The entire dashboard (HTML/CSS/JS, no build step).                   |

### Runtime files (created automatically, safe to delete to reset)

`tokens.json` (OAuth tokens), `schedules.json`, `automations.json`,
`snapshots.json` (saved zone states for restore), `trigger_state.json` (last-seen
trigger values, for restart-safe edge detection), `rotations.json` (active
duty-cycle rotations, so they resume after a restart).

---

## Running as a service (systemd example)

```ini
# /etc/systemd/system/hwcontrol.service
[Unit]
Description=Facility Thermostat Dashboard (hwcontrol)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/hwcontrol
EnvironmentFile=/opt/hwcontrol/.env
ExecStart=/opt/hwcontrol/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8010
Restart=on-failure
RestartSec=5
User=hwcontrol
Group=hwcontrol

[Install]
WantedBy=multi-user.target
```

The working directory must be writable by the service user — it's where
`tokens.json` and the other runtime files live.

> **Note on `EnvironmentFile`.** systemd parses `.env` more strictly than
> `python-dotenv` does: no `export`, and values with spaces or `#` may need
> quoting. If a value doesn't take, start the app manually with `python app.py`
> once to confirm the `.env` loads cleanly.

## Running alongside other services

This is designed to coexist with the other services on the facility server:

| Service              | Port / endpoint            | Relationship                          |
|----------------------|----------------------------|---------------------------------------|
| **hwcontrol** (this) | `:8010` (HTTP)             | The dashboard + API.                  |
| **GenWatch**         | `:8000` (HTTP)             | Generator monitor — separate web app. |
| **Mosquitto broker** | `localhost:1883` (MQTT)    | Shared bus; this app is a client.     |

- **No web-port conflict.** hwcontrol defaults to `:8010`; GenWatch uses `:8000`.
  If you change `PORT`, keep it off `8000` and update `HONEYWELL_REDIRECT_URI`.
- **Shared broker.** hwcontrol connects to Mosquitto as a client (it does not run
  its own broker), so it lives happily next to anything else on `1883`. It
  connects with client id `<MQTT_BASE_TOPIC>-bridge` (default `honeywell-bridge`)
  — keep that unique among your MQTT clients.
- **Generator status comes from your PLC.** The load-shed automation triggers on
  whatever your PLC publishes to the generator status topic (e.g.
  `facility/generator/status`). Publish that message **retained** so a restart
  mid-outage re-reads the current state correctly.

---

## Troubleshooting

- **"redirect URL does not match"** — the `.env` redirect URI and the portal's
  registered URI aren't byte-identical. See the setup section above.
- **401 / "not authorized"** — tokens expired and couldn't refresh, or `tokens.json`
  isn't writable. Re-run `python authorize.py` and confirm the file's permissions.
- **429 / rate limit** — you're polling too many devices too often. Raise
  `POLL_INTERVAL_SECONDS`, reduce rotation frequency, and/or request a higher limit
  from Resideo.
- **Automations don't fire live** — `MQTT_ENABLED` must be `true` and the broker
  reachable (check the Automations status strip). Confirm your equipment is
  actually publishing to the exact topic in the rule. Use **Run now** to test the
  actions independently of MQTT.
- **A zone shows offline** — `isAlive` came back false from Resideo (Wi-Fi/power at
  the thermostat). The dashboard reflects device reachability, not the app's.

---

## Extending

Some natural next steps if you want to go further:

- **Internal triggers** — the engine is trigger-agnostic; add a source that fires
  rules on internal conditions (a zone offline for >X minutes, a temperature
  threshold) in addition to MQTT.
- **Runtime-balanced rotation** — rotate by accumulated on-time instead of a simple
  sliding window, to equalize wear across a group.
- **Per-zone criticality tiers** — shed in stages as generator load climbs (tier 3
  off at 70%, tier 2 at 85%, …) using `gt` triggers on a load topic.
- **History** — persist state/events to a small database for trend charts.
