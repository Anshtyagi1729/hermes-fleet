"""Node registry: who is in the fleet, and are they alive right now.

The central idea of this file is that there is no membership list you edit.
A node exists because it told us it exists, and it is online because it told
us so recently. Silence is the offline signal -- we never poll a node.

Two states that are easy to confuse, keep them straight:

  online  = derived, not stored. now - last_seen < node_timeout_s.
            The node decides this by heartbeating (or not).
  enabled = stored in the DB, controlled by YOU from the dashboard.
            Lets you pull a flaky node out of rotation without touching
            your friend's machine.

The router only sends traffic to nodes that are online AND enabled.
The dashboard shows every node regardless, so you can see the flaky one.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from .db import Database


@dataclass
class NodeView:
    """A node plus its derived live state, as the dashboard/router see it."""

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
    models: list[str] = field(default_factory=list)
    first_seen: float = 0.0
    last_seen: float = 0.0
    enabled: bool = True
    online: bool = False

    @property
    def base_url(self) -> str:
        """Where the router sends traffic. Ollama speaks OpenAI-compat at /v1."""
        return f"http://{self.ip}:{self.port}"

    @property
    def uptime_s(self) -> float:
        return max(0.0, self.last_seen - self.first_seen)


class Registry:
    def __init__(self, db: Database, node_timeout_s: int):
        self.db = db
        self.node_timeout_s = node_timeout_s


    def register(self, payload: dict[str, Any]) -> str:
        node_id=payload["node_id"]
        now=time.time()
        self.db.execute(
            """
            INSERT INTO nodes (
                id, name, ip, port, backend, os, arch, gpu,
                vram_total_mb, ram_total_mb, agent_version,
                first_seen, last_seen, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                ip = excluded.ip,
                port = excluded.port,
                backend = excluded.backend,
                os = excluded.os,
                arch = excluded.arch,
                gpu = excluded.gpu,
                vram_total_mb = excluded.vram_total_mb,
                ram_total_mb = excluded.ram_total_mb,
                agent_version = excluded.agent_version,
                last_seen = excluded.last_seen
            """,
            (
                node_id,
                payload["name"],
                payload["ip"],
                payload["port"],
                payload["backend"],
                payload.get("os"),
                payload.get("arch"),
                payload.get("gpu"),
                payload.get("vram_total_mb"),
                payload.get("ram_total_mb"),
                payload.get("agent_version"),
                now,   # first_seen
                now,   # last_seen
            ),
        )
        self.db.execute("DELETE FROM node_models where node_id =?",(node_id,))
        models=payload.get("models",[])
        if models:
            self.db.executemany(
                "INSERT INTO node_models (node_id,model) VALUES (?,?)",[(node_id,m) for m in models],)
        return node_id

    def heartbeat(self, node_id: str, payload: dict[str, Any]) -> bool:

        now=time.time()
        cur=self.db.execute("UPDATE nodes SET last_seen=? WHERE id = ?",(now,node_id),)
        if cur.rowcount==0:
            return False
        self.db.execute(
            """
            INSERT INTO heartbeats (node_id,ts,vram_used_mb,ram_used_mb,cpu_pct)
            VALUES (?,?,?,?,?)
            """,
            (node_id,
                now,
                payload.get("vram_used_mb"),
                payload.get("ram_used_mb"),
                payload.get("cpu_pct"),
            ),
        )
        return True

    def set_enabled(self, node_id: str, enabled: bool) -> bool:
        """Dashboard toggle. Returns False if the node is unknown.

        TODO(you): one UPDATE. Note `enabled` is stored as INTEGER in SQLite
        (there is no bool type), so pass 1/0.
        """
        cur=self.db.execute(
            "UPDATE nodes SET enabled = ? WHERE id =?",
            (1 if enabled else 0 ,node_id),
        )
        return cur.rowcount >0

    def _rows_to_views(self, rows: list[Any]) -> list[NodeView]:
        now = time.time()
        views: list[NodeView] = []
        for r in rows:
            views.append(
                NodeView(
                    id=r["id"],
                    name=r["name"],
                    ip=r["ip"],
                    port=r["port"],
                    backend=r["backend"],
                    gpu=r["gpu"],
                    vram_total_mb=r["vram_total_mb"],
                    vram_used_mb=r["vram_used_mb"],
                    ram_total_mb=r["ram_total_mb"],
                    ram_used_mb=r["ram_used_mb"],
                    models=r["models"].split(",") if r["models"] else [],
                    first_seen=r["first_seen"],
                    last_seen=r["last_seen"],
                    enabled=bool(r["enabled"]),
                    online=(now - r["last_seen"]) < self.node_timeout_s,
                )
            )
        return views

    # Pulls the node, its model list, and its most recent heartbeat in one go.
    # The correlated subqueries on heartbeats are indexed by (node_id, ts) so
    # this stays cheap; group_concat flattens node_models into one column.
    _NODE_SELECT = """
        SELECT n.*,
               (SELECT group_concat(m.model)
                  FROM node_models m WHERE m.node_id = n.id) AS models,
               (SELECT h.vram_used_mb FROM heartbeats h
                 WHERE h.node_id = n.id ORDER BY h.ts DESC LIMIT 1) AS vram_used_mb,
               (SELECT h.ram_used_mb FROM heartbeats h
                 WHERE h.node_id = n.id ORDER BY h.ts DESC LIMIT 1) AS ram_used_mb
          FROM nodes n
    """

    def list_nodes(self) -> list[NodeView]:
        """Every node ever seen, online or not. Dashboard uses this."""
        rows = self.db.query(self._NODE_SELECT + " ORDER BY n.name")
        return self._rows_to_views(rows)

    def get_node(self, node_id: str) -> NodeView | None:
        rows = self.db.query(self._NODE_SELECT + " WHERE n.id = ?", (node_id,))
        views = self._rows_to_views(rows)
        return views[0] if views else None

    def candidates_for_model(self, model: str) -> list[NodeView]:
        """Nodes the router may send `model` to: online, enabled, has the model.

        This is the lookup the whole routing layer is built on. Note it returns
        a list -- picking WHICH one is the router's job (M2), not the
        registry's. Keeping selection policy out of here means we can change
        the policy without touching storage.
        """
        cutoff = time.time() - self.node_timeout_s
        rows = self.db.query(
            self._NODE_SELECT
            + """
             WHERE n.enabled = 1
               AND n.last_seen >= ?
               AND EXISTS (SELECT 1 FROM node_models m
                            WHERE m.node_id = n.id AND m.model = ?)
            """,
            (cutoff, model),
        )
        return self._rows_to_views(rows)

    def known_models(self) -> list[str]:
        """Union of models across online+enabled nodes. Backs GET /v1/models."""
        cutoff = time.time() - self.node_timeout_s
        rows = self.db.query(
            """
            SELECT DISTINCT m.model
              FROM node_models m
              JOIN nodes n ON n.id = m.node_id
             WHERE n.enabled = 1 AND n.last_seen >= ?
             ORDER BY m.model
            """,
            (cutoff,),
        )
        return [r["model"] for r in rows]
