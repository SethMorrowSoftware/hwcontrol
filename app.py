"""
app.py
------
Ties everything together and serves the dashboard.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
(or)  python app.py

Flow on startup:
  1. Build the Honeywell client (loads any stored tokens).
  2. Start MQTT bridge (if enabled) and the facility scheduler.
  3. Start a background poller thread that refreshes all devices on an interval,
     pushes state to MQTT, and raises alerts.
  4. Serve the dashboard + REST API.

If the account isn't authorized yet, everything still runs - the dashboard shows
a "Connect account" button that kicks off the OAuth flow.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from automation import AutomationEngine
from config import Config
from honeywell_client import HoneywellClient, HoneywellError, NotAuthorized
from scheduler import FacilityScheduler
from state_store import StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("honeywell.app")

STATIC_DIR = Path(__file__).parent / "static"

# ----------------------------------------------------------------- singletons

Config.require_credentials()

client = HoneywellClient(
    api_key=Config.API_KEY,
    api_secret=Config.API_SECRET,
    redirect_uri=Config.REDIRECT_URI,
    min_interval=Config.RL_MIN_INTERVAL,
    hourly_cap=Config.RL_HOURLY_CAP,
)
store = StateStore()
bridge = None          # set up in lifespan if MQTT enabled
scheduler: Optional[FacilityScheduler] = None
engine: Optional[AutomationEngine] = None
_poller_stop = threading.Event()


def notify(severity: str, kind: str, message: str) -> None:
    """Raise an operator-facing alert and mirror it to MQTT if connected."""
    alert = store.add_alert(severity, kind, message)
    if bridge and bridge.connected:
        bridge.publish_alert(alert)


def snapshot_read(device_id: str) -> Optional[dict]:
    """Current changeableValues for a device from the latest poll (for snapshots)."""
    cached = store.get(device_id)
    return cached.get("changeableValues") if cached else None


def sync_automation_topics() -> None:
    """Keep the MQTT bridge subscribed to exactly the topics our rules watch."""
    if bridge and engine:
        bridge.sync_trigger_topics(engine.subscribed_topics())


# ------------------------------------------------------------ command plumbing

def apply_action(targets: Any, action: dict) -> None:
    """Apply a control action to one device, a list, or 'all'. Used by both the
    scheduler and MQTT commands. `action` may contain a 'fan' key handled
    separately from setpoint fields."""
    if targets == "all":
        device_ids = store.all_device_ids()
    elif isinstance(targets, str):
        device_ids = [targets]
    else:
        device_ids = list(targets)

    fan_mode = action.get("fan")
    setpoint_overrides = {k: v for k, v in action.items() if k != "fan"}

    for did in device_ids:
        loc = store.location_of(did)
        if loc is None:
            log.warning("No known location for device %s; skipping.", did)
            continue
        cached = store.get(did)
        current_cv = cached.get("changeableValues") if cached else None
        try:
            if setpoint_overrides:
                client.set_thermostat(did, loc, setpoint_overrides, current_changeable=current_cv)
            if fan_mode:
                client.set_fan(did, loc, fan_mode)
            _refresh_one(did, loc)
        except HoneywellError as exc:
            log.error("Failed to apply action to %s: %s", did, exc)
            store.add_alert("critical", "control_failed",
                            f"Control failed for {did}: {exc}", did)


def handle_mqtt_command(device_id: str, command: dict) -> None:
    apply_action(device_id, command)


# ------------------------------------------------------------------- polling

def _publish_all(devices: list[dict]) -> None:
    if bridge and bridge.connected:
        for d in devices:
            bridge.publish_state(d)


def _refresh_one(device_id: str, location_id: Any) -> None:
    """Targeted refresh of a single device after a control action so the UI and
    MQTT reflect the change without waiting for the next full poll."""
    try:
        raw = client.get_thermostat(device_id, location_id)
        events = store.ingest([raw], location_id)
        d = store.get(device_id)
        if d and bridge and bridge.connected:
            wire = {k: v for k, v in d.items() if k != "changeableValues"}
            bridge.publish_state(wire)
        _emit_events(events)
    except HoneywellError as exc:
        log.error("Targeted refresh of %s failed: %s", device_id, exc)


def _emit_events(events: list[dict]) -> None:
    for ev in events:
        if bridge and bridge.connected:
            bridge.publish_event(ev)
    # push any freshly generated alerts to MQTT too
    if bridge and bridge.connected:
        for alert in store.alerts(limit=len(events) + 2):
            if time.time() - alert["ts"] < 5:
                bridge.publish_alert(alert)


def poll_once() -> None:
    """One full poll: locations -> per-location thermostats. Location-efficient."""
    if not client.is_authorized:
        return
    try:
        locations = client.get_locations()
    except NotAuthorized:
        return
    except HoneywellError as exc:
        store.mark_poll(error=str(exc))
        log.error("Poll failed at /locations: %s", exc)
        return

    all_events: list[dict] = []
    for loc in locations:
        loc_id = loc.get("locationID")
        # /locations already includes devices, but re-fetching per location via
        # /devices/thermostats guarantees full, current thermostat state.
        try:
            thermostats = client.get_thermostats(loc_id)
        except HoneywellError as exc:
            log.error("Poll failed at location %s: %s", loc_id, exc)
            continue
        all_events.extend(store.ingest(thermostats, loc_id))

    store.mark_poll()
    _publish_all(store.devices())
    _emit_events(all_events)
    log.info("Poll complete: %d device(s), %d change event(s).",
             len(store.all_device_ids()), len(all_events))


def _poller_loop() -> None:
    # Small initial delay so the server is up before the first poll.
    _poller_stop.wait(2)
    while not _poller_stop.is_set():
        try:
            poll_once()
        except Exception as exc:  # never let the loop die
            log.exception("Unexpected poller error: %s", exc)
        _poller_stop.wait(Config.POLL_INTERVAL_SECONDS)


# ------------------------------------------------------------------- lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge, scheduler, engine

    # The automation engine reacts to inbound MQTT; build it first so the bridge
    # can hand it trigger messages.
    engine = AutomationEngine(
        apply_fn=apply_action,
        resolve_fn=store.all_device_ids,
        snapshot_read_fn=snapshot_read,
        notify_fn=notify,
        on_topics_changed=sync_automation_topics,
    )

    if Config.MQTT_ENABLED:
        try:
            from mqtt_bridge import MqttBridge
            bridge = MqttBridge(
                host=Config.MQTT_HOST, port=Config.MQTT_PORT,
                base_topic=Config.MQTT_BASE_TOPIC,
                username=Config.MQTT_USERNAME, password=Config.MQTT_PASSWORD,
                command_handler=handle_mqtt_command,
                trigger_handler=engine.handle_message,
            )
            bridge.start()
        except Exception as exc:
            log.error("MQTT bridge failed to start (continuing without it): %s", exc)
            bridge = None

    engine.start()
    sync_automation_topics()  # subscribe to whatever loaded rules watch

    scheduler = FacilityScheduler(
        apply_fn=apply_action,
        timezone=Config.SCHEDULE_TZ or None,
    )
    scheduler.start()

    poller = threading.Thread(target=_poller_loop, name="poller", daemon=True)
    poller.start()

    log.info("Startup complete. Authorized=%s", client.is_authorized)
    try:
        yield
    finally:
        _poller_stop.set()
        if engine:
            engine.stop()
        if scheduler:
            scheduler.stop()
        if bridge:
            bridge.stop()


app = FastAPI(title="Facility Thermostat Dashboard", lifespan=lifespan)


# ------------------------------------------------------------- optional gate

@app.middleware("http")
async def token_gate(request: Request, call_next):
    if Config.DASHBOARD_TOKEN:
        # Allow the OAuth callback through (Honeywell can't send our header).
        if not request.url.path.startswith("/auth/callback"):
            supplied = request.headers.get("X-Token") or request.query_params.get("token") or ""
            # Constant-time comparison so the token can't be guessed by timing.
            if not secrets.compare_digest(supplied, Config.DASHBOARD_TOKEN):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ------------------------------------------------------------------- OAuth

@app.get("/auth/login")
def auth_login():
    return RedirectResponse(client.authorize_url(state="dashboard"))


@app.get("/auth/callback")
def auth_callback(code: Optional[str] = None, error: Optional[str] = None):
    if error:
        return HTMLResponse(f"<h3>Authorization failed:</h3><pre>{error}</pre>", status_code=400)
    if not code:
        return HTMLResponse("<h3>Missing authorization code.</h3>", status_code=400)
    try:
        client.exchange_code(code)
    except HoneywellError as exc:
        return HTMLResponse(f"<h3>Token exchange failed:</h3><pre>{exc}</pre>", status_code=400)
    # Kick off an immediate poll in the background so data shows up fast.
    threading.Thread(target=poll_once, daemon=True).start()
    # Carry the dashboard token through the redirect; otherwise the gate would
    # 401 the bare "/" the user lands on right after connecting their account.
    dest = "/?token=" + quote(Config.DASHBOARD_TOKEN) if Config.DASHBOARD_TOKEN else "/"
    return RedirectResponse(dest)


# --------------------------------------------------------------------- API

@app.get("/api/status")
def api_status():
    return {
        "authorized": client.is_authorized,
        "device_count": len(store.all_device_ids()),
        "last_poll_ts": store.last_poll_ts,
        "last_poll_error": store.last_poll_error,
        "poll_interval_seconds": Config.POLL_INTERVAL_SECONDS,
        "mqtt_connected": bool(bridge and bridge.connected),
    }


@app.get("/api/devices")
def api_devices():
    return {"devices": store.devices()}


@app.post("/api/devices/{device_id}/set")
def api_set_device(device_id: str, payload: dict = Body(...)):
    """
    Body may include any of:
      mode, heatSetpoint, coolSetpoint, thermostatSetpointStatus,
      nextPeriodTime, autoChangeoverActive, fan
    """
    if not client.is_authorized:
        raise HTTPException(401, "Account not authorized. Connect it first.")
    loc = store.location_of(device_id)
    if loc is None:
        raise HTTPException(404, f"Unknown device {device_id} (has it been polled yet?)")

    fan_mode = payload.pop("fan", None)
    cached = store.get(device_id)
    current_cv = cached.get("changeableValues") if cached else None
    try:
        if payload:
            client.set_thermostat(device_id, loc, payload, current_changeable=current_cv)
        if fan_mode:
            client.set_fan(device_id, loc, fan_mode)
    except HoneywellError as exc:
        raise HTTPException(502, f"Honeywell API error: {exc}")

    _refresh_one(device_id, loc)
    return {"ok": True, "device": store.get(device_id) and
            {k: v for k, v in store.get(device_id).items() if k != "changeableValues"}}


@app.post("/api/refresh")
def api_refresh():
    threading.Thread(target=poll_once, daemon=True).start()
    return {"ok": True, "message": "Refresh started"}


@app.get("/api/alerts")
def api_alerts(limit: int = Query(50, ge=1, le=200)):
    return {"alerts": store.alerts(limit=limit)}


@app.get("/api/schedules")
def api_schedules():
    return {"schedules": scheduler.list_rules() if scheduler else []}


@app.post("/api/schedules")
def api_add_schedule(rule: dict = Body(...)):
    if not scheduler:
        raise HTTPException(503, "Scheduler not ready")
    try:
        created = scheduler.add_rule(rule)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "rule": created}


@app.delete("/api/schedules/{rule_id}")
def api_delete_schedule(rule_id: str):
    if not scheduler:
        raise HTTPException(503, "Scheduler not ready")
    ok = scheduler.remove_rule(rule_id)
    if not ok:
        raise HTTPException(404, "No such rule")
    return {"ok": True}


@app.post("/api/schedules/{rule_id}/enabled")
def api_toggle_schedule(rule_id: str, enabled: bool = Body(..., embed=True)):
    if not scheduler:
        raise HTTPException(503, "Scheduler not ready")
    ok = scheduler.set_enabled(rule_id, enabled)
    if not ok:
        raise HTTPException(404, "No such rule")
    return {"ok": True}


# ---------------------------------------------------- automations (MQTT rules)

@app.get("/api/automations")
def api_automations():
    return {
        "automations": engine.list_rules() if engine else [],
        "status": engine.status() if engine else {},
        "mqtt_enabled": Config.MQTT_ENABLED,
        "mqtt_connected": bool(bridge and bridge.connected),
    }


@app.post("/api/automations")
def api_add_automation(rule: dict = Body(...)):
    if not engine:
        raise HTTPException(503, "Automation engine not ready")
    try:
        created = engine.add_rule(rule)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "rule": created}


@app.delete("/api/automations/{rule_id}")
def api_delete_automation(rule_id: str):
    if not engine or not engine.remove_rule(rule_id):
        raise HTTPException(404, "No such automation")
    return {"ok": True}


@app.post("/api/automations/{rule_id}/enabled")
def api_toggle_automation(rule_id: str, enabled: bool = Body(..., embed=True)):
    if not engine or not engine.set_enabled(rule_id, enabled):
        raise HTTPException(404, "No such automation")
    return {"ok": True}


@app.post("/api/automations/{rule_id}/run")
def api_run_automation(rule_id: str):
    """Run an automation's actions now, ignoring the trigger. For testing."""
    if not client.is_authorized:
        raise HTTPException(401, "Account not authorized. Connect it first.")
    if not engine or not engine.run_rule_now(rule_id):
        raise HTTPException(404, "No such automation")
    return {"ok": True}


# --------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html)


# Serve any other static assets (none required, but handy for extension).
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=Config.HOST, port=Config.PORT, reload=False)
