"""FastAPI app: the fleet's control plane.

Two trust boundaries, deliberately different:

  - Writes (register, heartbeat) require the invite token. This is the only
    thing stopping a random device on your tailnet from registering itself
    as a compute node and receiving Hermes's prompts.
  - Reads (dashboard, /api/nodes) require nothing. The dashboard is only
    reachable at all if you're on the tailnet already -- the tailnet itself
    is the real access control for "can see this box exists," same as the
    original design doc said for the node side.

  - /v1/... (M2) requires nothing either. This is the endpoint Hermes talks
    to, and Hermes doesn't know about the invite token -- same trust model
    as the dashboard: reachable only if you're already on the tailnet (or,
    right now, on localhost, since bind_host defaults to 127.0.0.1).
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .db import Database
from .load import LoadTracker
from .proxy import ProxyError, proxy_chat_completion
from .registry import Registry
from .schemas import (
    EnabledRequest,
    HeartbeatRequest,
    NodeOut,
    RegisterRequest,
    RegisterResponse,
)

APP_DIR = Path(__file__).resolve().parent
AGENT_TEMPLATE = APP_DIR / "agent_template.sh"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.db_path)
    app.state.db = db
    app.state.registry = Registry(db, settings.node_timeout_s)
    app.state.load_tracker = LoadTracker()
    # One shared client for the process, not one per request -- reuses
    # connections to nodes instead of paying a fresh TCP+TLS-ish handshake
    # per proxied call.
    app.state.http_client = httpx.AsyncClient()
    yield
    await app.state.http_client.aclose()
    db.close()


app = FastAPI(title="Hermes Fleet", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


def _check_token(token: str) -> None:
    # Plain string comparison is fine here: this token lives in a URL your
    # friends paste into a terminal, it's not a high-value secret worth
    # constant-time comparison for.
    if token != settings.invite_token:
        raise HTTPException(status_code=401, detail="invalid invite token")


@app.post("/api/register", response_model=RegisterResponse)
def register(req: RegisterRequest, registry: Registry = Depends(get_registry)):
    _check_token(req.token)
    payload = req.model_dump(exclude={"token"})
    node_id = registry.register(payload)
    return RegisterResponse(node_id=node_id, heartbeat_interval_s=settings.heartbeat_interval_s)


@app.post("/api/heartbeat")
def heartbeat(req: HeartbeatRequest, registry: Registry = Depends(get_registry)):
    _check_token(req.token)
    ok = registry.heartbeat(
        req.node_id,
        {
            "vram_used_mb": req.vram_used_mb,
            "ram_used_mb": req.ram_used_mb,
            "cpu_pct": req.cpu_pct,
        },
    )
    if not ok:
        # Tells the agent's loop to go re-register instead of heartbeating
        # into the void -- this is what makes the fleet self-heal if you
        # ever wipe the DB while an agent is still running (see M3).
        raise HTTPException(status_code=404, detail="unknown node_id, re-register")
    return {"ok": True}


@app.get("/api/nodes", response_model=list[NodeOut])
def list_nodes(registry: Registry = Depends(get_registry)):
    return [NodeOut.from_view(n) for n in registry.list_nodes()]


@app.post("/api/nodes/{node_id}/enabled")
def set_enabled(node_id: str, req: EnabledRequest, registry: Registry = Depends(get_registry)):
    # No invite-token check here: this is a control you click on the
    # dashboard yourself, not something a node calls. Reachable by anyone who
    # can load the dashboard, which is the same tailnet-is-the-boundary
    # trust model used for reads above.
    if not registry.set_enabled(node_id, req.enabled):
        raise HTTPException(status_code=404, detail="unknown node_id")
    return {"ok": True}


async def _proxy(request: Request):
    body = await request.json()
    try:
        return await proxy_chat_completion(
            path=request.url.path,
            body=body,
            client=request.app.state.http_client,
            registry=request.app.state.registry,
            tracker=request.app.state.load_tracker,
            db=request.app.state.db,
        )
    except ProxyError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await _proxy(request)


@app.post("/v1/completions")
async def completions(request: Request):
    return await _proxy(request)


@app.get("/v1/models")
def list_models(registry: Registry = Depends(get_registry)):
    # Shape matches OpenAI's /v1/models so Hermes doesn't need to know it's
    # talking to a router instead of a real inference server.
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "hermes-fleet"} for m in registry.known_models()],
    }


def _default_model() -> str:
    return os.environ.get("FLEET_DEFAULT_MODEL", "hermes3")


def _build_install_command(model: str) -> str:
    host_url = f"http://{settings.advertise_ip or '<your-tailscale-ip>'}:{settings.port}"
    tailscale_up = (
        f"sudo tailscale up --authkey={settings.tailscale_authkey}"
        if settings.tailscale_authkey
        else "sudo tailscale up"
    )
    agent_url = f"{host_url}/agent.sh?token={settings.invite_token}&model={model}"
    return (
        "curl -fsSL https://tailscale.com/install.sh | sh"
        f" && {tailscale_up}"
        f" && curl -fsSL '{agent_url}' | bash"
    )


@app.get("/connect", response_class=HTMLResponse)
def connect(request: Request, model: str = ""):
    chosen_model = model or _default_model()
    return templates.TemplateResponse(
        request,
        "connect.html",
        {
            "model": chosen_model,
            "install_cmd": _build_install_command(chosen_model),
            "advertise_ip": settings.advertise_ip,
            "tailscale_authkey": settings.tailscale_authkey,
        },
    )


@app.get("/agent.sh", response_class=PlainTextResponse)
def agent_script(request: Request, token: str = "", model: str = ""):
    # Not gated on the invite token: the SCRIPT isn't the secret, the
    # /api/register call it makes later is (and that's checked there). An
    # invalid token embedded here just means registration 401s downstream.
    token = token or settings.invite_token
    model = model or _default_model()
    host_url = f"http://{settings.advertise_ip or request.client.host}:{settings.port}"

    script = AGENT_TEMPLATE.read_text()
    script = (
        script.replace("__HOST_URL__", host_url)
        .replace("__TOKEN__", token)
        .replace("__MODEL__", model)
    )
    return PlainTextResponse(script, media_type="text/x-shellscript")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {})
