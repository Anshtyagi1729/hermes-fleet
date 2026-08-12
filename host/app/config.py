"""Settings for the fleet host.

Everything is env-overridable so the systemd unit (M4) can configure this
without editing code. The invite token is generated once and persisted to
disk, so restarting the host does not invalidate the command you already
sent to your friends.
"""

import os
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FLEET_DATA_DIR", HOST_DIR / "data"))


def _persistent_token(name: str) -> str:
    """Read a secret from data/, creating it on first run."""
    path = DATA_DIR / name
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token)
    path.chmod(0o600)
    return token


def _persisted_secret(name: str, env_value: str) -> str:
    """Like _persistent_token, but for a secret WE don't generate -- you do
    (the Tailscale admin console gives you the auth key). If the env var is
    set, write it to disk so future runs don't need it re-exported every
    time; otherwise fall back to whatever was persisted last time.
    """
    path = DATA_DIR / name
    if env_value:
        path.write_text(env_value)
        path.chmod(0o600)
        return env_value
    if path.exists():
        return path.read_text().strip()
    return ""


def _detect_tailscale_ip() -> str:
    """Best-effort `tailscale ip -4`, empty string if tailscale isn't
    installed/running. Used so /connect can show a working command without
    you having to hand-set FLEET_ADVERTISE_IP once tailscale is set up here.
    """
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3
        )
        ip = result.stdout.strip()
        return ip if result.returncode == 0 and ip else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


@dataclass(frozen=True)
class Settings:
    bind_host: str
    port: int
    db_path: Path

    # Shared secret baked into the install one-liner. A node must present this
    # to register. The tailnet is the real access control; this stops an
    # accidental/curious tailnet member from silently joining the pool.
    invite_token: str

    # Tailscale pre-auth key, so the friend's `tailscale up` asks them nothing.
    # Created by you in the Tailscale admin console. Set FLEET_TS_AUTHKEY once
    # and it's persisted to data/ from then on -- no need to re-export it in
    # every new shell. Empty means /connect shows a manual-login warning.
    tailscale_authkey: str

    # How the host reaches itself in the generated one-liner. Detected from
    # `tailscale ip -4` at startup when not set.
    advertise_ip: str

    # A node with no heartbeat in this many seconds is considered offline.
    # Must be comfortably more than heartbeat_interval_s to tolerate one
    # dropped beat without flapping the node offline.
    node_timeout_s: int

    # What we instruct agents to use. Served to them, so changing it here
    # changes every agent's cadence on their next restart.
    heartbeat_interval_s: int

    # Heartbeat history is what makes the uptime/VRAM graphs possible, but it
    # grows forever if unbounded. Rows older than this get pruned.
    heartbeat_retention_days: int


def load_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings(
        # Default is loopback-only, NOT 0.0.0.0. The dashboard/registry reads
        # have no auth beyond "you're on the tailnet" -- binding every
        # interface would let anyone on the same Wi-Fi/LAN reach them too,
        # quietly breaking that model. M4 will set this to the machine's
        # Tailscale interface IP for real deployment; override with
        # FLEET_BIND for local dev across machines in the meantime.
        bind_host=os.environ.get("FLEET_BIND", "127.0.0.1"),
        port=int(os.environ.get("FLEET_PORT", "8080")),
        db_path=Path(os.environ.get("FLEET_DB", DATA_DIR / "fleet.db")),
        invite_token=os.environ.get("FLEET_INVITE_TOKEN") or _persistent_token("invite_token"),
        tailscale_authkey=_persisted_secret("tailscale_authkey", os.environ.get("FLEET_TS_AUTHKEY", "")),
        advertise_ip=os.environ.get("FLEET_ADVERTISE_IP") or _detect_tailscale_ip(),
        node_timeout_s=int(os.environ.get("FLEET_NODE_TIMEOUT", "15")),
        heartbeat_interval_s=int(os.environ.get("FLEET_HEARTBEAT_INTERVAL", "5")),
        heartbeat_retention_days=int(os.environ.get("FLEET_HB_RETENTION_DAYS", "7")),
    )


settings = load_settings()
