#!/usr/bin/env bash
# =============================================================================
# agentns — Remote Deployment Script
# =============================================================================
#
# Deploys agentns to any Linux server (Ubuntu / Debian / CentOS / Rocky).
# Installs Docker if not present, sets up the service, opens the firewall.
#
# Usage:
#   ./deploy.sh <user@host>              # deploy with defaults
#   ./deploy.sh <user@host> --port 9000  # custom port
#   ./deploy.sh <user@host> --env .env   # load env from file
#
# Examples:
#   ./deploy.sh root@96.126.111.107
#   ./deploy.sh ubuntu@myserver.com --port 8200 --env .env.prod
#
# What it does:
#   1. Copies agentns source to the remote server
#   2. Installs Docker (if missing)
#   3. Builds the Docker image on the server
#   4. Writes a systemd service so agentns survives reboots
#   5. Opens the port in ufw / firewalld (if active)
#   6. Starts agentns and prints the health check URL
# =============================================================================

set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────────────────
TARGET=""
PORT=8200
ENV_FILE=".env"
REMOTE_DIR="/opt/agentns"
SERVICE_NAME="agentns"

# ── parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --port)  PORT="$2";     shift 2 ;;
    --env)   ENV_FILE="$2"; shift 2 ;;
    --dir)   REMOTE_DIR="$2"; shift 2 ;;
    -h|--help)
      sed -n '/^# Usage/,/^# ====/p' "$0" | head -20
      exit 0
      ;;
    *)
      TARGET="$1"; shift ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  echo "❌  No target specified."
  echo "    Usage: ./deploy.sh user@host [--port 8200] [--env .env]"
  exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║         agentns  —  Remote Deployment            ║"
echo "╚══════════════════════════════════════════════════╝"
echo "  Target  : $TARGET"
echo "  Port    : $PORT"
echo "  Env     : ${ENV_FILE}"
echo "  Dir     : ${REMOTE_DIR}"
echo ""

# ── load env vars from file (if it exists) ────────────────────────────────────
AGENTNS_TLD="${AGENTNS_TLD:-agentns.local}"
AGENTNS_NAMESPACE="${AGENTNS_NAMESPACE:-agents.local}"
AGENTNS_HEALTH_INTERVAL="${AGENTNS_HEALTH_INTERVAL:-30}"
MONGODB_URI="${MONGODB_URI:-}"
MONGODB_DB="${MONGODB_DB:-agentns}"
AGENTNS_GEOCODING="${AGENTNS_GEOCODING:-on}"

if [[ -f "$ENV_FILE" ]]; then
  echo "📄  Loading env from $ENV_FILE"
  set -o allexport
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +o allexport
fi

# ── copy source to remote ─────────────────────────────────────────────────────
echo "📦  Copying source to ${TARGET}:${REMOTE_DIR} ..."
ssh "$TARGET" "mkdir -p ${REMOTE_DIR}"
rsync -az --exclude='node_modules' --exclude='__pycache__' --exclude='.git' \
  --exclude='*.pyc' --exclude='.env*' \
  ./ "${TARGET}:${REMOTE_DIR}/"

# ── everything below runs on the remote server ────────────────────────────────
ssh "$TARGET" bash -s -- \
  "$REMOTE_DIR" "$PORT" "$SERVICE_NAME" \
  "$AGENTNS_TLD" "$AGENTNS_NAMESPACE" "$AGENTNS_HEALTH_INTERVAL" \
  "$MONGODB_URI" "$MONGODB_DB" "$AGENTNS_GEOCODING" \
<<'REMOTE_SCRIPT'

REMOTE_DIR="$1"
PORT="$2"
SERVICE_NAME="$3"
AGENTNS_TLD="$4"
AGENTNS_NAMESPACE="$5"
AGENTNS_HEALTH_INTERVAL="$6"
MONGODB_URI="$7"
MONGODB_DB="$8"
AGENTNS_GEOCODING="$9"

set -euo pipefail

# ── detect OS ─────────────────────────────────────────────────────────────────
if   command -v apt-get &>/dev/null; then PKG="apt";
elif command -v yum     &>/dev/null; then PKG="yum";
elif command -v dnf     &>/dev/null; then PKG="dnf";
else echo "⚠️  Unknown package manager — install Docker manually"; PKG="unknown"; fi

# ── install Docker ────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  echo "🐳  Docker not found — installing..."
  if [[ "$PKG" == "apt" ]]; then
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  elif [[ "$PKG" == "yum" || "$PKG" == "dnf" ]]; then
    $PKG install -y -q yum-utils
    yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    $PKG install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
  fi
  systemctl enable --now docker
  echo "✅  Docker installed"
else
  echo "✅  Docker already installed: $(docker --version)"
fi

# ── build image ───────────────────────────────────────────────────────────────
echo "🔨  Building agentns image..."
cd "$REMOTE_DIR"
docker build -t agentns:latest . --quiet
echo "✅  Image built"

# ── write environment file ────────────────────────────────────────────────────
ENV_PATH="/etc/agentns.env"
cat > "$ENV_PATH" <<ENV
AGENTNS_PORT=${PORT}
AGENTNS_TLD=${AGENTNS_TLD}
AGENTNS_NAMESPACE=${AGENTNS_NAMESPACE}
AGENTNS_HEALTH_INTERVAL=${AGENTNS_HEALTH_INTERVAL}
AGENTNS_GEOCODING=${AGENTNS_GEOCODING}
MONGODB_URI=${MONGODB_URI}
MONGODB_DB=${MONGODB_DB}
ENV
chmod 600 "$ENV_PATH"
echo "✅  Environment written to $ENV_PATH"

# ── write systemd service ─────────────────────────────────────────────────────
cat > /etc/systemd/system/${SERVICE_NAME}.service <<SERVICE
[Unit]
Description=agentns — Agent Name Service
After=docker.service
Requires=docker.service

[Service]
Restart=always
RestartSec=5
EnvironmentFile=${ENV_PATH}
ExecStartPre=-/usr/bin/docker stop ${SERVICE_NAME}
ExecStartPre=-/usr/bin/docker rm   ${SERVICE_NAME}
ExecStart=/usr/bin/docker run --rm --name ${SERVICE_NAME} \
  -p ${PORT}:8200 \
  --env-file ${ENV_PATH} \
  agentns:latest
ExecStop=/usr/bin/docker stop ${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
echo "✅  systemd service started (agentns.service)"

# ── open firewall port ────────────────────────────────────────────────────────
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "${PORT}/tcp" comment "agentns" > /dev/null
  echo "✅  ufw: port ${PORT} opened"
elif command -v firewall-cmd &>/dev/null && firewall-cmd --state &>/dev/null; then
  firewall-cmd --permanent --add-port="${PORT}/tcp"
  firewall-cmd --reload
  echo "✅  firewalld: port ${PORT} opened"
else
  echo "⚠️   No active firewall detected — ensure port ${PORT} is open in your cloud security group"
fi

# ── health check ──────────────────────────────────────────────────────────────
echo ""
echo "⏳  Waiting for agentns to be ready..."
for i in $(seq 1 15); do
  if curl -sf "http://localhost:${PORT}/health" > /dev/null 2>&1; then
    echo "✅  agentns is healthy!"
    break
  fi
  sleep 2
  if [[ $i == 15 ]]; then
    echo "⚠️   Health check timed out — check: journalctl -u agentns -n 50"
  fi
done

REMOTE_SCRIPT

# ── print summary ─────────────────────────────────────────────────────────────
SERVER_IP=$(echo "$TARGET" | cut -d@ -f2)

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                   agentns  deployed!                        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Service URL : http://${SERVER_IP}:${PORT}"
echo "  Health      : http://${SERVER_IP}:${PORT}/health"
echo "  Agents      : http://${SERVER_IP}:${PORT}/agents"
echo ""
echo "  TLD         : ${AGENTNS_TLD}"
echo "  Namespace   : ${AGENTNS_NAMESPACE}"
echo "  MongoDB     : ${MONGODB_URI:-in-memory (no persistence)}"
echo ""
echo "  Manage on server:"
echo "    systemctl status agentns"
echo "    systemctl restart agentns"
echo "    journalctl -u agentns -f"
echo ""
echo "  Point your agents at:"
echo "    AGENTNS_URL=http://${SERVER_IP}:${PORT}"
echo ""
