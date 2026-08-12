"""Pydantic request/response models for the fleet API.

Kept separate from main.py so the wire format -- what an agent actually
sends, what the dashboard actually receives -- is readable in one place
without wading through route handlers.
"""

from pydantic import BaseModel, Field

from .registry import NodeView


class RegisterRequest(BaseModel):
    token: str
    node_id: str
    name: str
    ip: str
    port: int
    backend: str
    os: str | None = None
    arch: str | None = None
    gpu: str | None = None
    vram_total_mb: int | None = None
    ram_total_mb: int | None = None
    agent_version: str | None = None
    models: list[str] = Field(default_factory=list)


class RegisterResponse(BaseModel):
    node_id: str
    # Told to the agent rather than hardcoded in agent.sh, so changing the
    # host's cadence doesn't require re-running the install command on every
    # friend's machine.
    heartbeat_interval_s: int


class HeartbeatRequest(BaseModel):
    token: str
    node_id: str
    vram_used_mb: int | None = None
    ram_used_mb: int | None = None
    cpu_pct: float | None = None


class EnabledRequest(BaseModel):
    enabled: bool


class NodeOut(BaseModel):
    id: str
    name: str
    ip: str
    port: int
    backend: str
    gpu: str | None
    vram_total_mb: int | None
    vram_used_mb: int | None
    ram_total_mb: int | None
    ram_used_mb: int | None
    models: list[str]
    online: bool
    enabled: bool
    uptime_s: float
    last_seen: float

    @classmethod
    def from_view(cls, v: NodeView) -> "NodeOut":
        return cls(
            id=v.id,
            name=v.name,
            ip=v.ip,
            port=v.port,
            backend=v.backend,
            gpu=v.gpu,
            vram_total_mb=v.vram_total_mb,
            vram_used_mb=v.vram_used_mb,
            ram_total_mb=v.ram_total_mb,
            ram_used_mb=v.ram_used_mb,
            models=v.models,
            online=v.online,
            enabled=v.enabled,
            uptime_s=v.uptime_s,
            last_seen=v.last_seen,
        )
