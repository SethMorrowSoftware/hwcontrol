#!/usr/bin/env bash
#
# install.sh - one-command setup for the Facility Thermostat Dashboard (hwcontrol).
#
#   git clone <repo> && cd hwcontrol && ./install.sh
#
# What it does (idempotent - safe to re-run):
#   1. Checks for Python 3.10+.
#   2. Creates a .venv and installs the Python requirements.
#   3. Creates .env from .env.example (placeholder credentials) if it's missing.
#   4. With --systemd (needs root): installs, enables, and starts a systemd
#      service so the dashboard runs on boot.
#
# It never overwrites an existing .env, and never prints your secrets.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --------------------------------------------------------------- options
DO_SYSTEMD=0
SERVICE_USER="hwcontrol"
SERVICE_NAME="hwcontrol"

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

  --systemd        Also install, enable, and start a systemd service
                   (requires root, e.g. sudo ./install.sh --systemd).
  --user NAME      Service account to run the service as (default: hwcontrol).
  --name NAME      systemd unit name (default: hwcontrol -> hwcontrol.service).
  -h, --help       Show this help.

Plain "./install.sh" sets up the virtualenv and .env only (no root needed).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --systemd) DO_SYSTEMD=1 ;;
    --user) SERVICE_USER="${2:?--user needs a value}"; shift ;;
    --name) SERVICE_NAME="${2:?--name needs a value}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- 1. Python
command -v python3 >/dev/null 2>&1 \
  || die "python3 not found. Install it: sudo apt install python3 python3-venv python3-pip"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
  || die "Python 3.10+ is required, found $PYV."
log "Python $PYV OK."

# --------------------------------------------------------- 2. venv + deps
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

# --------------------------------------------------------------- 3. .env
if [ -f .env ]; then
  log ".env already exists - leaving it untouched."
else
  cp .env.example .env
  log "Created .env from .env.example (placeholder credentials)."
  warn "Replace HONEYWELL_API_KEY / HONEYWELL_API_SECRET in .env with your real values before going live."
fi

# Port the service should listen on (from .env, default 8010).
PORT=$(grep -E '^PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]' || true)
PORT="${PORT:-8010}"

# ------------------------------------------------------- 4. systemd (opt)
if [ "$DO_SYSTEMD" -eq 1 ]; then
  [ "$(id -u)" -eq 0 ] || die "--systemd needs root. Re-run: sudo ./install.sh --systemd"
  UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "Creating system user '$SERVICE_USER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  # The working directory holds tokens.json and the other runtime files, so it
  # must be writable by the service account.
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$SCRIPT_DIR"

  log "Writing $UNIT ..."
  cat > "$UNIT" <<EOF
[Unit]
Description=Facility Thermostat Dashboard (hwcontrol)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=$SCRIPT_DIR/.env
ExecStart=$SCRIPT_DIR/.venv/bin/uvicorn app:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5
User=$SERVICE_USER
Group=$SERVICE_USER

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service" >/dev/null 2>&1 || systemctl enable "${SERVICE_NAME}.service"
  systemctl restart "${SERVICE_NAME}.service"
  log "Service '${SERVICE_NAME}' enabled and started on port $PORT."
  echo "    status:  systemctl status ${SERVICE_NAME}"
  echo "    logs:    journalctl -u ${SERVICE_NAME} -f"
fi

# --------------------------------------------------------------- summary
printf '\n\033[1;32mSetup complete.\033[0m\n\n'
cat <<EOF
Next steps:
  1. Put your real Honeywell key/secret in .env (currently placeholders):
       HONEYWELL_API_KEY, HONEYWELL_API_SECRET
  2. Register this redirect URI on https://developer.honeywellhome.com,
     byte-for-byte, and set HONEYWELL_REDIRECT_URI in .env to match:
       http://<this-host>:$PORT/auth/callback
  3. Authorize your Honeywell account once:
       ./.venv/bin/python authorize.py
EOF

if [ "$DO_SYSTEMD" -eq 1 ]; then
  echo "  4. Already running as a service: http://<this-host>:$PORT"
else
  cat <<EOF
  4. Start the dashboard:
       ./.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port $PORT
     then open http://<this-host>:$PORT

  To run it as a boot service instead: sudo ./install.sh --systemd
EOF
fi
