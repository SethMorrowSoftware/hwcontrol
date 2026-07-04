#!/usr/bin/env bash
#
# install.sh - guided setup for the Facility Thermostat Dashboard (hwcontrol).
#
#   git clone https://github.com/SethMorrowSoftware/hwcontrol.git
#   cd hwcontrol
#   sudo ./install.sh          # <- recommended: also installs an always-on service
#
# Run with sudo to install a systemd service so the dashboard starts on boot and
# restarts automatically - the reliable, always-available setup. Run without root
# to only build the virtualenv and .env (you can add the service later).
#
# The script asks for your Honeywell client ID / secret and a few settings, each
# with a sensible default (press Enter to keep it). Defaults come from your
# existing .env if present, otherwise from .env.example. It's safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ------------------------------------------------------------------ options
INSTALL_SERVICE=auto            # auto | yes | no
SERVICE_USER="hwcontrol"
SERVICE_NAME="hwcontrol"
INTERACTIVE=1
[ -t 0 ] || INTERACTIVE=0
UNIT_DIR="${HWCONTROL_UNIT_DIR:-/etc/systemd/system}"

usage() {
  cat <<'EOF'
Usage: [sudo] ./install.sh [options]

  --service        Force installing the systemd service (needs root).
  --no-service     Skip the systemd service (venv + .env only).
  --user NAME      Service account to run as (default: hwcontrol).
  --name NAME      systemd unit name (default: hwcontrol -> hwcontrol.service).
  -y, --yes        Non-interactive: accept all defaults, don't prompt.
  -h, --help       Show this help.

Default: run with sudo to set up an always-on service; run without root to just
build the virtualenv and .env.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --service|--systemd) INSTALL_SERVICE=yes ;;
    --no-service) INSTALL_SERVICE=no ;;
    --user) SERVICE_USER="${2:?--user needs a value}"; shift ;;
    --name) SERVICE_NAME="${2:?--name needs a value}"; shift ;;
    -y|--yes|--non-interactive) INTERACTIVE=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Make sure the service account can actually reach and run the app. A system user
# usually can't traverse a home directory (mode 0750), which makes systemd fail
# with status=203/EXEC. Grant traverse on each ancestor it can't enter, and make
# the venv readable/executable.
#
# Preferred: a scoped POSIX ACL that grants ONLY the service user (setfacl). This
# does not touch the "other" permission bits, so unrelated local users gain no new
# access. Fallback (only if setfacl is missing): the old chmod o+x / o+rX, which
# widens access to EVERY local user - we warn loudly when we have to do that.
ensure_service_access() {
  local user="$1" d="$SCRIPT_DIR"
  if command -v setfacl >/dev/null 2>&1; then
    while [ "$d" != "/" ]; do
      if ! runuser -u "$user" -- test -x "$d" 2>/dev/null; then
        setfacl -m u:"$user":x "$d"
        log "Granted traverse (ACL u:$user:x) on $d so '$user' can reach the app."
      fi
      d=$(dirname "$d")
    done
    if ! runuser -u "$user" -- test -x "$SCRIPT_DIR/.venv/bin/uvicorn" 2>/dev/null; then
      setfacl -R -m u:"$user":rX "$SCRIPT_DIR/.venv"
      # Best-effort default ACL so files added later (e.g. a re-run's pip) inherit
      # the grant; never abort the install if the FS can't store default ACLs.
      setfacl -R -d -m u:"$user":rX "$SCRIPT_DIR/.venv" 2>/dev/null || true
      log "Adjusted .venv ACLs so '$user' can run it (scoped to that user)."
    fi
  else
    warn "setfacl not found - falling back to chmod, which grants access to ALL local users, not just '$user'."
    warn "For a scoped grant, install the 'acl' package (e.g. sudo apt install acl) or relocate the repo to /opt, then re-run."
    while [ "$d" != "/" ]; do
      if ! runuser -u "$user" -- test -x "$d" 2>/dev/null; then
        chmod o+x "$d"
        log "Granted traverse (o+x, world-wide) on $d so '$user' can reach the app."
      fi
      d=$(dirname "$d")
    done
    if ! runuser -u "$user" -- test -x "$SCRIPT_DIR/.venv/bin/uvicorn" 2>/dev/null; then
      chmod -R o+rX "$SCRIPT_DIR/.venv"
      log "Adjusted .venv permissions (world-readable) so '$user' can run it."
    fi
  fi
}

# ------------------------------------------------------------------ 1. Python
command -v python3 >/dev/null 2>&1 \
  || die "python3 not found. Install it: sudo apt install python3 python3-venv python3-pip"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
  || die "Python 3.10+ is required, found $PYV."
log "Python $PYV OK."

# ---------------------------------------------------------- 2. venv + deps
if [ ! -d .venv ]; then
  log "Creating virtualenv (.venv)..."
  python3 -m venv .venv 2>/dev/null \
    || die "Could not create the virtualenv. Install venv support: sudo apt install python3-venv"
else
  log "Reusing existing .venv."
fi
log "Installing dependencies (this can take a minute)..."
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt
log "Dependencies installed."

# ------------------------------------------------------------ 3. configure
# Current value for KEY from .env, else .env.example, else the given fallback.
cfg() {
  local key="$1" def="${2:-}" cur=""
  if [ -f .env ]; then
    cur=$(grep -E "^${key}=" .env | tail -1 | cut -d= -f2- || true)
  fi
  if [ -z "$cur" ] && [ -f .env.example ]; then
    cur=$(grep -E "^${key}=" .env.example | tail -1 | cut -d= -f2- || true)
  fi
  printf '%s' "${cur:-$def}"
}
# ask VAR "Prompt" "default" [hidden]
ask() {
  local var="$1" text="$2" def="$3" hidden="${4:-}" ans=""
  if [ "$INTERACTIVE" -eq 1 ]; then
    if [ -n "$hidden" ]; then
      read -rs -p "  $text [keep current]: " ans </dev/tty || ans=""
      echo
    else
      read -r -p "  $text [${def}]: " ans </dev/tty || ans=""
    fi
  fi
  printf -v "$var" '%s' "${ans:-$def}"
}

log "Configuration (press Enter to accept each default):"
ask API_KEY    "Honeywell API key (client ID)" "$(cfg HONEYWELL_API_KEY your-consumer-key-here)"
ask API_SECRET "Honeywell API secret"          "$(cfg HONEYWELL_API_SECRET your-consumer-secret-here)" hidden
ask PORT       "Web port"                       "$(cfg PORT 8010)"

REDIRECT_CUR=""
[ -f .env ] && REDIRECT_CUR=$(grep -E '^HONEYWELL_REDIRECT_URI=' .env | tail -1 | cut -d= -f2- || true)
ask REDIRECT   "OAuth redirect URI" "${REDIRECT_CUR:-http://localhost:${PORT}/auth/callback}"

ask MQTT_EN    "Enable MQTT bridge? (true/false)" "$(cfg MQTT_ENABLED false)"
MQTT_HOST=$(cfg MQTT_HOST localhost)
MQTT_PORT=$(cfg MQTT_PORT 1883)
MQTT_USER=$(cfg MQTT_USERNAME "")
MQTT_PASS=$(cfg MQTT_PASSWORD "")
MQTT_BASE=$(cfg MQTT_BASE_TOPIC honeywell)
case "${MQTT_EN,,}" in
  true|1|yes|on|y)
    MQTT_EN=true
    ask MQTT_HOST "  MQTT broker host"              "$MQTT_HOST"
    ask MQTT_PORT "  MQTT broker port"              "$MQTT_PORT"
    ask MQTT_USER "  MQTT username (blank = none)"  "$MQTT_USER"
    ask MQTT_PASS "  MQTT password (blank = none)"  "$MQTT_PASS" hidden
    ask MQTT_BASE "  MQTT base topic"               "$MQTT_BASE"
    ;;
  *) MQTT_EN=false ;;
esac
ask DASH_TOKEN "Dashboard access token (blank = none)" "$(cfg DASHBOARD_TOKEN "")"

POLL=$(cfg POLL_INTERVAL_SECONDS 300)
RL_MIN=$(cfg RL_MIN_INTERVAL 1.0)
RL_CAP=$(cfg RL_HOURLY_CAP 250)
SCHED_TZ=$(cfg SCHEDULE_TZ "")
BIND_HOST=$(cfg HOST 0.0.0.0)

# Write .env with restrictive permissions (it holds secrets).
old_umask=$(umask); umask 077
cat > .env <<EOF
# Generated by install.sh. Edit freely - a re-run reuses these as the defaults.

# --- Honeywell / Resideo credentials ---
HONEYWELL_API_KEY=${API_KEY}
HONEYWELL_API_SECRET=${API_SECRET}
HONEYWELL_REDIRECT_URI=${REDIRECT}

# --- Polling & rate-limit guardrails ---
POLL_INTERVAL_SECONDS=${POLL}
RL_MIN_INTERVAL=${RL_MIN}
RL_HOURLY_CAP=${RL_CAP}

# --- MQTT ---
MQTT_ENABLED=${MQTT_EN}
MQTT_HOST=${MQTT_HOST}
MQTT_PORT=${MQTT_PORT}
MQTT_USERNAME=${MQTT_USER}
MQTT_PASSWORD=${MQTT_PASS}
MQTT_BASE_TOPIC=${MQTT_BASE}

# --- Scheduler ---
SCHEDULE_TZ=${SCHED_TZ}

# --- Server ---
HOST=${BIND_HOST}
PORT=${PORT}

# --- Optional dashboard gate ---
DASHBOARD_TOKEN=${DASH_TOKEN}
EOF
umask "$old_umask"
chmod 600 .env
log ".env written (permissions 600)."
if [ "$API_KEY" = "your-consumer-key-here" ] || [ "$API_SECRET" = "your-consumer-secret-here" ]; then
  warn "Honeywell credentials are still placeholders - edit .env (or re-run this script) with your real client ID/secret before the dashboard can connect."
fi

# ------------------------------------------------------- 4. systemd service
want_service() {
  case "$INSTALL_SERVICE" in
    yes) return 0 ;;
    no)  return 1 ;;
    *)   [ "$(id -u)" -eq 0 ] ;;   # auto: only when root
  esac
}

if want_service; then
  [ "$(id -u)" -eq 0 ] || die "Installing the service needs root. Re-run: sudo ./install.sh"

  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "Creating system user '$SERVICE_USER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  SERVICE_GROUP=$(id -gn "$SERVICE_USER")

  # Ensure the service account can traverse to and execute the venv (handles the
  # common case of the repo living under a 0750 home directory).
  ensure_service_access "$SERVICE_USER"

  # Let the service account read .env (config.py loads it directly, in addition
  # to systemd's EnvironmentFile). Owner keeps write; group gets read; not world.
  if [ -f "$SCRIPT_DIR/.env" ]; then
    chown "$(stat -c '%U' "$SCRIPT_DIR"):$SERVICE_GROUP" "$SCRIPT_DIR/.env" 2>/dev/null \
      || chgrp "$SERVICE_GROUP" "$SCRIPT_DIR/.env" 2>/dev/null || true
    chmod 640 "$SCRIPT_DIR/.env"
    log ".env permissions set to 640 (owner rw, group '$SERVICE_GROUP' read) so the service account can read it."
  fi

  # We deliberately do NOT chown the repo. The code stays owned by whoever cloned
  # it, so `git pull` keeps working. The service instead runs from a private state
  # directory (/var/lib/<name>, created and owned by systemd) and imports the code
  # via PYTHONPATH. systemd reads the .env as root before dropping to the service
  # account, so that account never needs to own or read repo files.
  STATE_DIR="/var/lib/${SERVICE_NAME}"

  # Validate PORT/HOST before embedding them (unquoted) in ExecStart, so a stray
  # value can't inject extra arguments into the uvicorn command line.
  case "$PORT" in
    ''|*[!0-9]*) die "Invalid PORT '$PORT': must be an integer between 1 and 65535." ;;
  esac
  if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    die "Invalid PORT '$PORT': must be an integer between 1 and 65535."
  fi
  case "$BIND_HOST" in
    -*) die "Invalid HOST '$BIND_HOST': must not start with '-' (would look like a flag)." ;;
    ''|*[![:alnum:].:_-]*) die "Invalid HOST '$BIND_HOST': expected a bare hostname or IP address (no spaces or flags)." ;;
  esac

  UNIT="${UNIT_DIR}/${SERVICE_NAME}.service"
  mkdir -p "$UNIT_DIR"
  log "Writing $UNIT ..."
  cat > "$UNIT" <<EOF
[Unit]
Description=Facility Thermostat Dashboard (hwcontrol)
After=network-online.target
Wants=network-online.target
# Stop retrying if it can't stay up (e.g. .env missing credentials -> SystemExit),
# instead of crash-looping forever every RestartSec seconds.
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
User=$SERVICE_USER
Group=$SERVICE_GROUP
# Runtime state (tokens.json and the *.json files) lives here, owned by the
# service account. systemd creates $STATE_DIR automatically on start.
StateDirectory=${SERVICE_NAME}
WorkingDirectory=$STATE_DIR
Environment=PYTHONPATH=$SCRIPT_DIR
EnvironmentFile=$SCRIPT_DIR/.env
ExecStart=$SCRIPT_DIR/.venv/bin/uvicorn app:app --host $BIND_HOST --port $PORT
Restart=always
RestartSec=5
# --- Sandboxing ---
NoNewPrivileges=true
ProtectSystem=strict
# read-only (not 'true'): 'true' mounts an empty tmpfs over /home, which would
# HIDE the app's own code when the repo lives in a home dir (the layout this
# installer supports) - and a tmpfs can't be re-exposed by ReadOnlyPaths. 'read-only'
# keeps home readable but not writable, so the service can still import its code.
ProtectHome=read-only
PrivateTmp=true
# ProtectSystem=strict makes the filesystem read-only; keep the StateDirectory
# writable for the JSON state, and (belt and suspenders) mark the code read-only.
ReadOnlyPaths=$SCRIPT_DIR
ReadWritePaths=$STATE_DIR

[Install]
WantedBy=multi-user.target
EOF

  if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    systemctl restart "${SERVICE_NAME}.service" || true
    sleep 2   # let it start (or fail) before we check
    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
      log "Service '${SERVICE_NAME}' is running and enabled on port $PORT."
    else
      warn "Service '${SERVICE_NAME}' did not stay up. Check: journalctl -u ${SERVICE_NAME} -n 30"
    fi
    echo "    status:  systemctl status ${SERVICE_NAME}"
    echo "    logs:    journalctl -u ${SERVICE_NAME} -f"
  else
    warn "systemd not detected (no /run/systemd/system): wrote the unit but did not start it."
    warn "On the target server this step enables and starts the service automatically."
  fi
elif [ "$INSTALL_SERVICE" = "auto" ]; then
  warn "Not running as root, so the always-on systemd service was NOT installed."
  warn "For a service that starts on boot and restarts on failure, re-run: sudo ./install.sh"
fi

# --------------------------------------------------------------- summary
printf '\n\033[1;32mSetup complete.\033[0m\n\n'
PLACEHOLDER=0
[ "$API_KEY" = "your-consumer-key-here" ] && PLACEHOLDER=1

echo "Next steps:"
if [ "$PLACEHOLDER" -eq 1 ]; then
  echo "  1. Add your real Honeywell client ID/secret - re-run 'sudo ./install.sh'"
  echo "     and type them at the prompts, or edit .env directly (then restart)."
else
  echo "  1. Credentials are set in .env."
fi
echo "  2. Register this redirect URI on https://developer.honeywellhome.com, exactly:"
echo "       ${REDIRECT}"

if want_service && [ -d /run/systemd/system ]; then
  echo "  3. The service is running and starts on boot. Open the dashboard and click"
  echo "     \"Connect account\" to authorize Honeywell (one time):"
  echo "       http://<this-host>:${PORT}"
  echo "     After editing .env later, apply changes with: sudo systemctl restart ${SERVICE_NAME}"
else
  echo "  3. Start the dashboard:"
  echo "       ./.venv/bin/python -m uvicorn app:app --host ${BIND_HOST} --port ${PORT}"
  echo "     then open http://<this-host>:${PORT}"
  echo "  4. Authorize Honeywell once - click \"Connect account\" in the dashboard,"
  echo "     or run:  ./.venv/bin/python authorize.py"
fi
