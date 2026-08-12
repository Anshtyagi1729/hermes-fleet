#!/usr/bin/env bash
# Hermes Fleet node agent.
#
# Installed via the three-stage command on the /connect page:
#   1. install Tailscale
#   2. tailscale up (join the tailnet)
#   3. curl this script | bash   <- you are here
#
# No dependencies beyond bash + curl -- specifically no jq, since a friend's
# machine can't be assumed to have it. JSON bodies below are hand-built with
# printf/here-strings; every value embedded is either a fixed constant or
# comes from uname/nvidia-smi/hostname, not untrusted network input.
set -euo pipefail

HOST_URL="__HOST_URL__"
TOKEN="__TOKEN__"
MODEL="__MODEL__"

STATE_DIR="$HOME/.hermes-fleet"
NODE_ID_FILE="$STATE_DIR/node_id"
LOG_FILE="$STATE_DIR/agent.log"
mkdir -p "$STATE_DIR"

log() { echo "[hermes-fleet] $*"; }

# ---------------------------------------------------------------- 1. tailnet
# This script only reached HOST_URL at all because it's already on the
# tailnet (stage 2 ran `tailscale up` before this). Ollama has no auth of
# its own, so we bind it to THIS ip specifically, never 0.0.0.0 -- binding
# every interface would expose an unauthenticated inference server to
# anyone on the same Wi-Fi/LAN, not just the tailnet.
if ! command -v tailscale >/dev/null 2>&1; then
  log "ERROR: tailscale not found. Install it and run 'tailscale up' first."
  exit 1
fi
TS_IP="$(tailscale ip -4)"
if [ -z "$TS_IP" ]; then
  log "ERROR: could not determine this machine's Tailscale IP."
  exit 1
fi
log "tailscale IP: $TS_IP"

# ------------------------------------------------------------ 2. node identity
# Stable across reboots/reinstalls of this script, so re-running it updates
# the same fleet row instead of creating a duplicate node every time.
if [ -f "$NODE_ID_FILE" ]; then
  NODE_ID="$(cat "$NODE_ID_FILE")"
else
  NODE_ID="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "$NODE_ID" > "$NODE_ID_FILE"
fi
log "node id: $NODE_ID"

# ---------------------------------------------------------------- 3. ollama
if ! command -v ollama >/dev/null 2>&1; then
  log "installing Ollama..."
  curl -fsSL https://ollama.com/install.sh | sh
fi

if ! curl -fsS "http://$TS_IP:11434/" >/dev/null 2>&1; then
  log "starting Ollama on $TS_IP:11434..."
  # Subshell + trap so the server survives this script's process exiting
  # once it hands control back to the terminal (nohup alone doesn't always
  # stop a SIGHUP from propagating to a backgrounded shell job).
  ( trap '' HUP; OLLAMA_HOST="$TS_IP:11434" exec ollama serve ) >>"$LOG_FILE" 2>&1 &
  disown
  for _ in $(seq 1 15); do
    curl -fsS "http://$TS_IP:11434/" >/dev/null 2>&1 && break
    sleep 1
  done
fi

log "pulling $MODEL (first time can take a while)..."
OLLAMA_HOST="$TS_IP:11434" ollama pull "$MODEL"

# ------------------------------------------------------------ 4. GPU / VRAM
GPU="CPU only"
VRAM_TOTAL_MB=""
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_LINE="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null | head -n1)"
  if [ -n "$GPU_LINE" ]; then
    GPU="$(echo "$GPU_LINE" | cut -d',' -f1 | sed 's/^ *//;s/ *$//')"
    VRAM_TOTAL_MB="$(echo "$GPU_LINE" | cut -d',' -f2 | sed 's/^ *//;s/ *$//')"
  fi
elif command -v rocm-smi >/dev/null 2>&1; then
  GPU="AMD GPU (rocm-smi)"
  VRAM_TOTAL_MB="$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F',' 'NR==2{print int($2/1024/1024)}' || echo "")"
fi

# --------------------------------------------------------- 5. OS / arch / RAM
OS_NAME="$(uname -s)"
ARCH="$(uname -m)"
if [ "$OS_NAME" = "Linux" ]; then
  RAM_TOTAL_MB="$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
elif [ "$OS_NAME" = "Darwin" ]; then
  RAM_TOTAL_MB="$(( $(sysctl -n hw.memsize) / 1024 / 1024 ))"
else
  RAM_TOTAL_MB=""
fi

# ------------------------------------------------------- 6. models this node has
# Read from `ollama list`, not just "the one model we were told to pull" --
# if the model set changes by hand later, the next registration must catch
# up to reality, same as registry.register() expects (models are a SET, not
# an append log).
MODELS_JSON="$(OLLAMA_HOST="$TS_IP:11434" ollama list 2>/dev/null \
  | awk 'NR>1 && NF{print $1}' \
  | awk '{printf "%s\"%s\"", sep, $0; sep=","} END{print ""}')"
[ -z "$MODELS_JSON" ] && MODELS_JSON="\"$MODEL\""

json_str() {
  # Minimal escaping for values we don't fully control (hostname, GPU name
  # string) -- backslash and double-quote only. Source is uname/nvidia-smi
  # output, not untrusted network input, so this is defense in depth, not a
  # security boundary.
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

HOSTNAME_ESC="$(json_str "$(hostname)")"
GPU_ESC="$(json_str "$GPU")"

register() {
  curl -fsS -X POST "$HOST_URL/api/register" \
    -H "Content-Type: application/json" \
    -d "{
      \"token\": \"$TOKEN\",
      \"node_id\": \"$NODE_ID\",
      \"name\": \"$HOSTNAME_ESC\",
      \"ip\": \"$TS_IP\",
      \"port\": 11434,
      \"backend\": \"ollama\",
      \"os\": \"$OS_NAME\",
      \"arch\": \"$ARCH\",
      \"gpu\": \"$GPU_ESC\",
      \"vram_total_mb\": ${VRAM_TOTAL_MB:-null},
      \"ram_total_mb\": ${RAM_TOTAL_MB:-null},
      \"agent_version\": \"0.1.0\",
      \"models\": [$MODELS_JSON]
    }"
}

log "registering with $HOST_URL..."
REGISTER_RESP="$(register)"
log "registered."

HEARTBEAT_INTERVAL="$(printf '%s' "$REGISTER_RESP" | sed -n 's/.*"heartbeat_interval_s":[ ]*\([0-9]*\).*/\1/p')"
[ -z "$HEARTBEAT_INTERVAL" ] && HEARTBEAT_INTERVAL=5

# ------------------------------------------------------- 7. heartbeat forever
heartbeat_loop() {
  while true; do
    if command -v nvidia-smi >/dev/null 2>&1; then
      VRAM_USED_MB="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -n1)"
    else
      VRAM_USED_MB=""
    fi
    if [ "$OS_NAME" = "Linux" ]; then
      RAM_USED_MB="$(awk '/MemAvailable/{avail=$2} /MemTotal/{total=$2} END{print int((total-avail)/1024)}' /proc/meminfo)"
    else
      RAM_USED_MB=""
    fi

    STATUS="$(curl -fsS -o /dev/null -w '%{http_code}' -X POST "$HOST_URL/api/heartbeat" \
      -H "Content-Type: application/json" \
      -d "{\"token\":\"$TOKEN\",\"node_id\":\"$NODE_ID\",\"vram_used_mb\":${VRAM_USED_MB:-null},\"ram_used_mb\":${RAM_USED_MB:-null}}" \
      2>>"$LOG_FILE")" || STATUS="000"

    if [ "$STATUS" = "404" ]; then
      # Host lost its DB (or was reinstalled) -- re-register and carry on.
      # This is the fleet's self-heal path, described in registry.py.
      register >>"$LOG_FILE" 2>&1 || true
    fi

    sleep "$HEARTBEAT_INTERVAL"
  done
}

( trap '' HUP; heartbeat_loop ) >>"$LOG_FILE" 2>&1 &
disown
log "heartbeat loop running in background (pid $!), logging to $LOG_FILE"
log "done -- this node is now part of the fleet."
