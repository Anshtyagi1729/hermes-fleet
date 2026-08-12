"""FastAPI app: the fleet's control plane.

Two trust boundaries, deliberately different:

  - Writes (register, heartbeat) require the invite token. This is the only
    thing stopping a random device on your tailnet from registering itself
    as a compute node and receiving Hermes's prompts.
  - Reads (dashboard, /api/nodes) require nothing. The dashboard is only
    reachable at all if you're on the tailnet already -- the tailnet itself
    is the real access control for "can see this box exists," same as the
    original design doc said for the node side.

Routing/proxying (/v1/...) is not here yet -- that's M2. This app currently
only answers "who is in the fleet," not "answer this prompt."
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .db import Database
from .registry import Registry
from .schemas import (
    EnabledRequest,
    HeartbeatRequest,
    NodeOut,
    RegisterRequest,
    RegisterResponse,
)

APP_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.db_path)
    app.state.db = db
    app.state.registry = Registry(db, settings.node_timeout_s)
    yield
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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {})
