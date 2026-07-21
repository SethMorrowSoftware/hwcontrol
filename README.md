# Facility Thermostat Dashboard (Honeywell Home / Resideo)

A self-hosted web dashboard to monitor and control many Honeywell Home / Resideo
thermostats across a facility, with three capabilities layered on top of the
Resideo API:

1. **Control** — see every zone's temperature, humidity, and online status; change
   mode, heat/cool setpoints, holds, and fan from one screen.
2. **Scheduling** — application-level **daily programs** with as many timed periods
   as you like ("6am → 70°, noon → 66°, 6pm → 70°, 10pm → Off") that work across all
   your thermostats regardless of their own onboard schedules. One-tap ON / OFF /
   Set-temperature presets, day-of-week selection, and in-place editing. **Sole
   Controller mode** (on by default) holds every zone under the app continuously so
   the onboard/Resideo schedules never override it.
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
poll fetches all of your locations in **a single call** — the `/locations`
response already embeds every device's full state, so the poller reads thermostats
straight from it instead of making a separate call per location — and a
client-side rate limiter (minimum spacing + rolling hourly cap) sits underneath as
a safety net. If you have more than ~20 devices or
want faster polling, email **developerinfo@resideo.com** to request a higher limit,
then lower `POLL_INTERVAL_SECONDS`. Automations and rotations also issue control
calls, so keep rotation intervals sane (the app enforces a 5-minute minimum, which
also protects compressors from short-cycling).

---

## Quick start

The fastest path — clone the repo and run the guided installer:

```bash
git clone https://github.com/SethMorrowSoftware/hwcontrol.git
cd hwcontrol
sudo ./install.sh            # recommended: also installs an always-on service
```

`install.sh` walks you through setup:

- Creates the virtualenv and installs dependencies.
- **Prompts for your Honeywell client ID / secret** and a few settings (web port,
  MQTT, dashboard token), each with a sensible default — press Enter to keep it.
  Defaults come from your existing `.env` if present, otherwise the placeholders,
  so re-running never loses your answers. It writes `.env` with `600` permissions.
- **When run with `sudo`, installs a `systemd` service** (`hwcontrol.service`) that
  starts on boot and restarts on failure (`Restart=always`) — the reliable,
  always-available setup. Run it **without** root to skip the service and just
  build the venv + `.env` (add the service later with `sudo ./install.sh`).

It's idempotent and safe to re-run. Flags: `--no-service`, `--yes`
(non-interactive, accept defaults), `--user NAME`, `--name NAME`, `--help`.

After it finishes: register the redirect URI it prints on the developer portal,
then authorize once (`./.venv/bin/python authorize.py`).

### Manual setup

If you'd rather not use the installer:

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

- **Zones** — a search filter and live fleet summary (online/offline, how many
  zones are **actively heating / cooling right now**, and mode counts) above a
  card per thermostat: current temp/humidity, online dot, a live activity chip
  ("Heating to 66°" with a pulse while equipment runs, "Idle · Heat 66°" while it
  holds — straight from the thermostat's reported equipment state, with a
  commanded-target fallback on models that don't report it), mode chips,
  heat/cool steppers (press-and-hold to repeat), fan mode (when the device
  reports fan capability), hold selector, and Apply. Changes you're typing are
  preserved across background refreshes until you Apply or discard.
  **Bulk control:** tick the checkbox on any cards — or use **Select shown**,
  which pairs with the search filter ("arcade" → Select shown) — and a floating
  bar applies one mode/setpoint/hold change to every selected zone at once
  (`POST /api/devices/set`). Zones under an active outage rotation are skipped
  automatically so a bulk change can't re-energize shed zones mid-outage.
- **Automations** — the generator load-shed setup, a live status strip (active
  rotations, saved snapshots, MQTT connection), the list of rules (editable), and a
  custom multi-condition (AND/OR) rule builder. Details below.
- **Schedules** — **zone groups** (below) plus daily programs: pick any group of
  zones (one, several, or all) and days, then add timed periods (ON / OFF /
  temperature changes) with one-tap presets. Programs are editable.
- **Alerts** — a feed of offline/again events, out-of-range temperatures,
  equipment faults (a zone actively heating/cooling while set to Off — checked
  against the live equipment state, debounced across two polls), and a log of
  every automation action taken, filterable by severity; the tab badge turns
  red/amber while critical/warning alerts are live. The offline/again events can
  also be pushed to **Slack** — see [Slack notifications](#slack-notifications-unit-offline--online).

### Zone groups

Name a set of zones once — e.g. **"Arcade"** for the arcade thermostats — under
**Schedules → Zone groups**, then **quick-pick it anywhere you choose zones**: the
bulk-control bar, a program's target picker, and the outage plan's "keep running"
/ "can switch off" pickers all show a **Groups:** chip row. Click a chip to
select (or clear) that group's zones in one tap.

Groups are a **selection shortcut**, not a live reference: they're stored on the
server (`/api/groups`, in `groups.json`) so they persist and are shared across
browsers, and a selection expands to concrete zones the moment you use it. That
means editing a group changes what it selects *next time* — it does **not**
rewrite programs or automations you've already saved, and it never touches the
safety-critical schedule/rotation/apply path. A member that's temporarily offline
or missing is simply skipped when you pick the group.

### Optional access gate

Set `DASHBOARD_TOKEN` in `.env` to require `?token=...` in the URL (or an `X-Token`
header) for the dashboard and API. This is a **light guard, not real
authentication** — for production, put the app behind a VPN or a reverse proxy that
handles auth/TLS.

---

## Slack notifications (unit offline / online)

Get a Slack message the moment a thermostat drops off the network, and another
when it comes back — **one message per transition**, never a repeat while the unit
stays down. This rides on the same edge-triggered offline/again detection that
already powers the Alerts feed, so a flapping unit doesn't spam the channel: you
get exactly one "🔴 … went offline" when it goes down and one "🟢 … is back online"
when it returns.

### Setup

1. **Create a Slack app** at <https://api.slack.com/apps> → *Create New App* →
   *From scratch*, pick your workspace.
2. **Add the bot scope.** Under *OAuth & Permissions* → *Scopes* → *Bot Token
   Scopes*, add **`chat:write`**.
3. **Install** the app to the workspace (*OAuth & Permissions* → *Install to
   Workspace*) and copy the **Bot User OAuth Token** — it starts with `xoxb-`.
4. **Invite the bot** to the channel you want the alerts in: in that channel,
   `/invite @your-app-name`. (A bot can only post to channels it's a member of;
   skipping this gives a `not_in_channel` error in the logs.)
5. **Configure `.env`:**

   ```bash
   SLACK_ENABLED=true
   SLACK_BOT_TOKEN=xoxb-your-bot-token
   SLACK_CHANNEL=C0123456789        # the channel ID, or a #channel-name
   ```

   The channel **ID** (from the channel's *View channel details*) is the most
   robust; a `#name` also works. Restart the app; the startup log prints
   `Slack alerts enabled (channel …)`.

### Behavior & reliability

- **One notification per state change.** A unit that's offline across many polls
  alerts once; the next alert for it is the "back online" when it recovers. A unit
  that's already offline when the app starts alerts once too (so a zone that's dead
  at boot isn't silently unmonitored); healthy units at boot stay quiet.
- **Off the hot path.** Messages are delivered on a background worker thread, so a
  slow or unreachable Slack never stalls polling or thermostat control — the design
  mirrors the MQTT bridge.
- **Best-effort, never fatal.** Transient failures (rate-limit `429` honoring
  `Retry-After`, `5xx`, network blips) are retried with capped backoff; a
  misconfiguration (bad token, wrong channel, bot not invited) is logged once,
  clearly, and the message is dropped rather than retried forever. A Slack outage
  can never take down the control loop.
- **In-app alerts are unaffected.** Slack is an *additional* sink; the dashboard
  Alerts feed and the MQTT `honeywell/alerts` topic still carry everything.

> Requires no new dependency (it uses the `requests` library the app already
> ships with) and no inbound network — the app calls out to `slack.com`.

---

## Power-outage plan (generator) — walkthrough

This is the turnkey path for the main use case, designed so anyone can set it up
without help. Open **Automations → Power-outage plan (generator)** and work through
three plain-language steps:

- **Step 1 — Which zones matter most?** Pick the critical zones to **keep running
  (they take turns)** and the non-critical zones that **can switch off** during the
  outage. (The form won't let you put the same zone in both.)
- **Step 2 — How should they share the generator?** Leave it on **Half on / half
  off** (the safe default) or choose a set number, set the **swap interval** (≥5 min),
  the **run mode** while a zone is on (**Auto** = heat or cool to hold the band,
  the year-round default; or force **Cool** / **Heat** — e.g. Cool in summer), and
  the **hold temperatures**.
- **Step 3 — How does the app know the generator is on?** It's preset to listen for
  `on` / `off` on `facility/generator/status`. The MQTT topic/payload details live
  under **"Change the message details (advanced)"** if your equipment differs.

As you fill it in, a **live preview** spells out exactly what will happen in plain
English ("When the generator turns ON: switch OFF …; keep … running half-on/half-off,
swapping every 15 min … When utility power returns: put every zone back exactly how
it was, then resume your daily programs."), so there are no surprises before you
click **Create outage plan**.

This creates two paired automations:

1. **On generator** (`facility/generator/status` = `on`):
   - **snapshots** the current settings of all involved zones as `pre_generator`,
   - **sets** the non-critical zones to `Off`,
   - **starts a rotation** on the critical group: every N minutes the "on" window
     slides by one, so each unit gets a turn while only `run_count` run at any moment.
2. **On utility** (`facility/generator/status` = `off`):
   - **stops** the rotation,
   - **restores** every zone from the `pre_generator` snapshot — exactly the mode,
     setpoints, and hold each had before the outage,
   - **re-asserts your daily programs**: the currently-active period of every
     enabled program is applied immediately, so programmed zones resume the
     regular schedule right away — not at the next period boundary, which could
     be hours off (boundaries that fired during the outage were deliberately
     skipped for rotated zones). Zones not covered by a program keep the
     restored pre-outage state.

To verify it without waiting for an outage, use **Run now** on either rule (this
executes the actions immediately, ignoring the trigger — it works even if MQTT is
disabled). To go live, make sure `MQTT_ENABLED=true` and the broker is connected
(the status strip shows this).

**Restart-safe.** Snapshots, the trigger's last-seen state, and any active
rotation are all persisted **atomically** (temp-file + rename, with a `.bak`
fallback), so a crash or power-loss mid-write can't corrupt them — important on a
system that runs on generator power. If the app restarts mid-outage: the running
rotation resumes *and reconciles* the physical zones to its last-applied window
(so it can never come back with more than `run_count` on), a *retained* "on"
message replayed by the broker will **not** re-fire the shed rule or clobber the
good snapshot, the startup schedule assertion is **deferred** while a rotation is
active (so it doesn't re-energize the shed zones and overload the generator), and
the genuine transition to "off" still stops the rotation and triggers the restore.
(For this to work across a restart, publish the generator status **retained**.)

**Outage-safe by construction.** While a rotation is active, **daily programs skip
the zones it manages** — a "6am all zones ON" boundary firing mid-outage can't
re-energize shed zones and overload the generator (an alert notes the skip; those
zones return to normal program control after the restore). If a zone **fails to
switch off** during a swap, the incoming zones are held back by the equivalent
draw, so a failed write can never push the group over its count/power cap — it's
retried at the next swap. The broker connection is opened asynchronously with
background retry, so if this app boots before Mosquitto after a site-wide power
blink, MQTT comes up on its own instead of staying dead until a restart. Inbound
MQTT handling runs on a dedicated worker (in arrival order), so a slow burst of
rate-limited Honeywell writes can't starve the MQTT keepalive mid-outage.

The bridge connects with a persistent session (`clean_session=false`) and
subscribes/publishes at **QoS 1**, so a brief broker blip during an outage won't
silently drop the "utility restored" message. It also raises an operator alert the
moment the broker link drops (and clears it on reconnect) instead of falsely
reporting "connected" — so a dead broker during an outage is visible, not hidden.

---

## Schedules & source of truth

**Schedules are daily programs.** A program targets any set of zones — one, a
custom group (e.g. all the arcade units), or all — on selected days and runs any
number of timed **periods** — each period sets a mode and/or setpoints with a
hold. Build them under **Schedules**: pick the zones with checkboxes, then use
the one-tap **Turn ON**, **Set temperature**, and **Turn OFF** presets, and edit
them in place.

**How a program overrides the thermostat's own schedule.** Each period writes a
setpoint with a *hold*:

- **Permanent hold** (the default) suspends the thermostat's onboard schedule
  indefinitely — the value stays until the next period replaces it, so between
  periods the onboard schedule never acts. This app wins.
- **Until next period** / **Resume onboard schedule** hand control back to the
  onboard schedule; use those only if you *want* the device's own schedule to take
  over.

This is enforced at the server, not just in the UI: **any** change the app pushes
without an explicit hold — a scheduled period (including "Turn OFF"), an automation
action, an MQTT command, or a hand-edited rule in `schedules.json` — is written as
a permanent hold. The only way a period hands the zone back to the onboard schedule
is to explicitly choose "Resume onboard schedule" (`NoHold`). The manual per-zone
controls still let an operator pick any hold for one-off adjustments.

**Program changes follow the schedule.** Creating, editing, enabling, or disabling
a program updates the *plan* only — zones are never driven as a side effect of
saving; each change takes effect at the program's scheduled period times. If you
*do* want the period that should be in effect right now written to the zones as
you save, tick **"Also apply the current period to the zones right now"** in the
form (or pass `apply_now: true` to the API).

Two automatic exceptions exist because they keep the schedule *accurate*: at
**startup** (after the first poll) and **after a power-outage restore**, the app
re-asserts every enabled program's currently-active period — both catch up
boundaries that fired while the app was down or the outage was in progress, so
zones aren't left at stale setpoints.

**Sole Controller mode makes it automatic (on by default).** Under **Schedules →
Control mode**, Sole Controller mode keeps *every* zone under a permanent hold
continuously: on each poll the app re-asserts a hold on any zone that isn't already
held, so the thermostats' onboard schedules and the Resideo app never change a zone
— with no per-device setup. It's on by default (`SOLE_CONTROLLER=true`) and can be
toggled live from the dashboard (the choice is remembered across restarts). Per
zone you can **Hold now** to take control immediately, or (with the mode off)
**Release** a zone back to its onboard schedule.

Takeover uses a permanent hold — a normal setpoint write that works on every unit —
rather than editing the device's onboard schedule via Resideo's `/devices/schedule`
endpoint, which returns 404 on LCC thermostats. A permanent hold suspends the
onboard schedule just as effectively, so there's nothing to "disable" or restore.

> This doesn't stop someone changing a thermostat directly in the Resideo app or at
> the wall *between* polls — but with Sole Controller mode on, the next poll takes
> the zone back (and a program, if one covers it, re-imposes its own setpoints).

**Enforce schedules (anti-tamper).** A switch at the top of the dashboard's **main
(Zones) page** — mirrored by `SCHEDULE_ENFORCE` — makes the app, on **every update**,
put any zone a program covers back to what its schedule says right now, undoing a
temperature or mode change made at the thermostat or in the Resideo app. It's
**drift-aware**: a zone already on its program costs no API calls, so this is cheap
in steady state and only writes when something actually changed. Each correction
raises an alert naming the zones (and what each was set to, and which program),
giving you a tamper log. Zones under an active generator rotation are left alone
(same guard as programs), so it can't re-energize shed zones mid-outage.

**When more than one program covers a zone**, enforcement (and the startup /
post-outage re-assertion) picks the program whose schedule **most recently took
effect** — so on a Monday a *weekday* program's this-morning setting beats a
*weekend* program that merely carried over from Sunday night. This is deterministic,
not a guess. Only when two programs land on the **same boundary time** but set the
zone **differently** is there a real conflict: that zone is left alone and an alert
asks you to fix the overlap, rather than the two programs fighting each other every
update.

*Enforce schedules vs. Sole Controller:* Sole Controller keeps a zone under a
permanent hold so the onboard schedule can't act, but leaves whatever setpoint is
currently there; Enforce schedules forces the **program's** setpoints back. They're
complementary — turn on both to fully lock zones to your programs. Note that with
enforcement on, a manual one-off change to a program-covered zone (from the
dashboard, the wall, or Resideo) is reverted on the next update; disable enforcement
or the program if you want a manual override to stick.

**Schedule readout (which schedule is in effect).** Every zone card on the main page
shows a line naming the program governing that zone right now, what it should be set
to, and whether the zone is actually following it — computed with the **same
arbitration** the enforcer uses, so what you see is exactly what enforcement would
act on:

- **Following *Program* · Cool 68°** (green) — the zone matches its program.
- **Off‑schedule** (amber) — the zone was changed at the wall or in Resideo; with
  enforcement on the line reads "correcting to …" and the next update sets it back.
- **No program covers this zone** (grey) — nothing is scheduled for it right now.
- **Schedule conflict: *A, B*** (red) — two programs set it differently at the same
  time, so the app leaves it alone; fix the overlap in Schedules.
- **Outage control — schedule paused** (blue) — under an active generator rotation;
  its program resumes when utility power returns.

A summary above the grid tallies how many zones are **on schedule**, **off‑schedule**,
and **in conflict**. The same data is available programmatically at
`GET /api/schedule_status`.

---

## Automations — the rule model

An automation is: **a trigger** (one or more conditions combined with AND/OR) and
**an ordered list of actions**. Rules are stored in `automations.json`, and can be
edited in place from the dashboard (Automations → **Edit**).

### Trigger matching

A trigger has a `mode` (`all` = AND, `any` = OR) and a list of `conditions`. Each
condition watches one topic:

| Match type   | Fires when…                                              |
|--------------|----------------------------------------------------------|
| `equals`     | payload equals the value (case-insensitive by default)   |
| `not_equals` | payload does not equal the value                         |
| `contains`   | payload contains the value as a substring                |
| `regex`      | payload matches the regular expression                   |
| `gt` / `lt`  | payload (as a number) is greater / less than the value   |
| `between`    | payload (as a number) is within `value`…`value2` (inclusive) |
| `any`        | any message on the topic                                 |

Each condition remembers the last message seen on its topic, so a multi-topic
rule (e.g. "generator is on **AND** load > 85%") fires when the whole combination
is true — even though the two facts arrive in separate messages. If **`field`** is
set, the payload is parsed as JSON and that dot-path is extracted first (e.g.
`field: "load.pct"`, type `gt`, value `85`).

**`retrigger`** controls repeats:
- `on_change` (default) — fire only on the *rising edge* into the true state.
  Republished/retained duplicates are ignored, and it re-arms once the combination
  goes false again. This is what you want for a generator status that gets
  re-announced periodically.
- `every_message` — fire on every matching message.

> Single-condition rules from earlier versions (`trigger.topic` + `trigger.match`)
> are migrated to the one-condition shape automatically on load.

### Action types

| Action          | What it does                                                                 |
|-----------------|------------------------------------------------------------------------------|
| `set`           | Apply `values` (mode / heatSetpoint / coolSetpoint / hold / fan) to targets. |
| `snapshot`      | Save targets' current settings under `name` for later restore. **Non-clobbering**: if a snapshot of that name already exists (an outage in progress) it's kept, so a retained "on" replayed after a restart can't overwrite the good pre-outage capture with the already-shed state. |
| `restore`       | Restore every device saved in snapshot `name`, then clear the snapshot on full success so the next outage captures fresh. Zones that fail to restore keep the snapshot for a retry. On full success the app re-asserts every daily program's active period, so programmed zones resume the regular schedule immediately. |
| `rotate`        | Duty-cycle a group: keep a safe subset running, slide the window every `interval_minutes`; on/off units get `on_values` / `off_values`. |
| `stop_rotation` | Stop the rotation with `rotation_id`.                                         |

**Targets** are `"all"`, a single `"deviceID"`, or a list `["id1","id2"]`.

**How many run at once** — set it whichever way matches how you think about your
limit (they compose; give at least one):

| Field | Meaning |
|-------|---------|
| `run_count` | A fixed number of units on at a time (e.g. `2`). |
| `on_fraction` | A fraction of the group — `0.5` = **half on / half off**. Auto-adjusts if you add or remove zones, so you never have to recompute "half". |
| `max_power` + `power` | A power budget. `power` is a map `{ "deviceID": kW }` (any consistent unit; default `1` each). The on-set is trimmed so its total draw stays **under `max_power`** — the right tool when your units draw *different* amounts and "half the units" wouldn't guarantee "under the limit". |

Each rotation tick drives the **full** desired state — the window to `on_values`,
every other member to `off_values` — rather than only the members it believes
changed. That's a deliberate safety choice: it guarantees the group never exceeds
its count/power cap even if a zone drifted on (at the wall, via the Resideo app, or
from an earlier failed write) or the window re-entered from a new outage. Swaps are
**break-before-make** (outgoing units off *before* incoming on) so a swap never
transiently exceeds the cap, and incoming units start one at a time to stagger
compressor inrush. Keep the interval at/above the 5-minute minimum so the extra
writes stay well within the rate limit.

**Heating zones are never cycled.** A zone set to **`Heat`** runs on **natural gas**,
so it draws no generator power — duty-cycling it off would save the generator
nothing and just let the space get cold. When a rotation starts, the app splits its
targets **once**: any zone in **`Heat`** mode at that moment is left running and
dropped from the rotation; only the electrically-taxing **cooling** zones are
cycled, and the `run_count` / `max_power` cap applies to that cooling set. The split
is fixed for the outage (it isn't re-evaluated each tick), survives a restart, and
the exempt zones are listed on the Automations status stripe (`🔥 … left running
(gas)`) and in `/api/automations` (`status.rotations[].exempt_heating`). If *every*
target is in Heat, nothing is cycled and no relief is needed.

> **Only `Heat` mode is exempt — not `Auto`.** The split is made once, at outage
> start, and holds for the whole outage, so a zone is only safe to exempt if it can
> *never* draw cooling load. `Heat` mode can't run the AC compressor; `Auto` can —
> an Auto zone whose furnace is firing at outage start could autonomously switch to
> cooling later, and an exempted zone's compressor would then run **uncounted** and
> overload the generator. So `Auto` (and `Cool`, and `Off`) zones stay cyclable, and
> a zone that can't be read is treated as cyclable — the safe default. If you want a
> zone left running during an outage, set it to **`Heat`**, not `Auto`.

```jsonc
// Half on / half off, break-before-make, every 15 min:
{ "type": "rotate", "rotation_id": "critical", "targets": ["Z1","Z2","Z3","Z4"],
  "on_fraction": 0.5, "interval_minutes": 15,
  "on_values": {"mode":"Heat","heatSetpoint":66,"thermostatSetpointStatus":"PermanentHold"},
  "off_values": {"mode":"Off"} }

// Or cap by power when units differ (keep total under 20, whatever fits):
{ "type": "rotate", "rotation_id": "critical", "targets": ["Z1","Z2","Z3","Z4"],
  "max_power": 20, "power": {"Z1":5,"Z2":5,"Z3":10,"Z4":10}, "interval_minutes": 15,
  "on_values": {"mode":"Heat","heatSetpoint":66,"thermostatSetpointStatus":"PermanentHold"},
  "off_values": {"mode":"Off"} }
```

### Custom rules

**Automations → Build a custom rule** exposes all of the above: choose AND/OR, add
one or more conditions (each with its own topic, matcher, and optional JSON field),
then add actions one at a time (each with its own zone picker and parameters). Use
this to react to anything your broker publishes — a load percentage crossing a
threshold, a demand-response signal combined with a temperature band, a BMS mode
change, etc. Existing rules can be edited in place with **Edit**.

### Example rule (JSON)

```json
{
  "name": "Generator on AND high load: shed warehouse",
  "enabled": true,
  "trigger": {
    "mode": "all",
    "conditions": [
      { "topic": "facility/generator/status", "type": "equals", "value": "on" },
      { "topic": "facility/power/load", "type": "gt", "value": 85, "field": "pct" }
    ],
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
| `groups.py`          | Named, reusable zone groups (picker convenience; expanded to deviceIDs before use). |
| `mqtt_bridge.py`     | paho-mqtt bridge: publishes state, routes command & trigger topics.  |
| `slack_notifier.py`  | Optional Slack notifications for unit offline/online (chat.postMessage, off-thread). |
| `config.py`          | Loads config from environment / `.env`.                              |
| `authorize.py`       | Standalone CLI to complete OAuth and list devices without the server. |
| `static/index.html`  | The entire dashboard (HTML/CSS/JS, no build step).                   |

### Runtime files (created automatically, safe to delete to reset)

`tokens.json` (OAuth tokens), `schedules.json`, `automations.json`,
`snapshots.json` (saved zone states for restore), `trigger_state.json` (last-seen
trigger values, for restart-safe edge detection), `rotations.json` (active
duty-cycle rotations, so they resume after a restart), `groups.json` (named zone
groups), `schedule_enforce.json` (the schedule-enforcement toggle),
`sole_control.json` (the Sole Controller toggle).

These are written to the current working directory. A manual run keeps them next
to the code; the systemd service (below) puts them in `/var/lib/hwcontrol` so the
repo directory stays untouched and owned by your login user.

---

## Running as a service (systemd example)

`sudo ./install.sh` generates and enables this unit for you (pointing at wherever
you cloned the repo, using the port from your `.env`). The equivalent unit, if
you'd rather write it by hand — assuming the repo is cloned at `/opt/hwcontrol`:

```ini
# /etc/systemd/system/hwcontrol.service
[Unit]
Description=Facility Thermostat Dashboard (hwcontrol)
After=network-online.target
Wants=network-online.target

[Service]
User=hwcontrol
Group=hwcontrol
# Runtime state (tokens.json, *.json) lives here, owned by the service account.
# systemd creates /var/lib/hwcontrol automatically.
StateDirectory=hwcontrol
WorkingDirectory=/var/lib/hwcontrol
Environment=PYTHONPATH=/opt/hwcontrol
EnvironmentFile=/opt/hwcontrol/.env
ExecStart=/opt/hwcontrol/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8010
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Why the code and the state are separate.** The service runs from its own
`/var/lib/hwcontrol` (owned by the `hwcontrol` account) and imports the code via
`PYTHONPATH`, so the **repo directory stays owned by whoever cloned it** and
`git pull` keeps working. Don't `chown` the repo to the service user — that's what
triggers git's "detected dubious ownership" error. systemd reads `.env` as root
before dropping privileges, so the service account never needs to read it.

Because the service's runtime dir differs from the repo, authorize it through the
dashboard's **Connect account** button (the running service writes the tokens),
not by running `authorize.py` from the repo.

> **Note on `EnvironmentFile`.** systemd parses `.env` more strictly than
> `python-dotenv` does: no `export`, and values with spaces or `#` may need
> quoting. If a value doesn't take, check `journalctl -u hwcontrol`.

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
- **429 / rate limit** — you're polling too many devices too often. The client
  **automatically retries** a 429 (honoring the server's `Retry-After`), plus 5xx
  and network blips, for both reads and writes — so a control action rides through
  a short throttle instead of failing (bounded by `RL_MAX_RETRIES` /
  `RL_RETRY_MAX_SLEEP`, and each retry still goes through the rate limiter). If you
  see *sustained* 429s, raise `POLL_INTERVAL_SECONDS`, reduce rotation frequency,
  and/or request a higher limit from Resideo. When a poll is ultimately throttled
  the dashboard says so in place of the zone grid; zones return on their own once
  the window clears, so there's no need to restart the service (restarting only
  adds more calls and prolongs the throttle).
- **Automations don't fire live** — `MQTT_ENABLED` must be `true` and the broker
  reachable (check the Automations status strip). Confirm your equipment is
  actually publishing to the exact topic in the rule. Use **Run now** to test the
  actions independently of MQTT.
- **A zone shows offline** — `isAlive` came back false from Resideo (Wi-Fi/power at
  the thermostat). The dashboard reflects device reachability, not the app's.
- **Schedules fire at the wrong time (e.g. hours early/late)** — almost always a
  timezone issue. If `SCHEDULE_TZ` is blank the app uses the **server's** local
  timezone, and most servers run in **UTC**, so a "22:00 Off" program fires at
  22:00 UTC — 6pm Eastern in summer. Set `SCHEDULE_TZ` to your IANA zone (e.g.
  `America/New_York`, **not** a bare `EST` — the IANA name handles EST/EDT for
  you) and restart. An **invalid** `SCHEDULE_TZ` doesn't stop the app: it falls
  back to server-local time and raises a critical alert until fixed. Confirm the
  effective zone with `GET /api/status` (`schedule_timezone` / `server_time`) or
  in the startup log line ("Scheduler started … Timezone=…, local time now=…").

---

## Tests

The safety-critical behaviors (rotation window math and break-before-make under
failure, trigger edge latching and retry, schedule walkback and the
rotation-skip guard, MQTT worker dispatch, timezone fallback) have regression
tests. They use only the standard library test runner:

```bash
python -m unittest discover -s tests
```

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
