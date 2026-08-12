"""The streaming router: forwards OpenAI-compatible requests to a chosen
node and streams the response back while measuring it.

This is where Hermes's one stable URL (http://127.0.0.1:8080/v1/...) turns
into "some friend's laptop, chosen right now, based on who's free." Hermes
is configured once, ever, against this host -- it never learns a node's IP.

The retry rule that shapes this whole file: a node may be swapped out for
another ONLY before the first byte of its response has been forwarded to
the caller. After that we are committed -- you cannot un-send bytes, so a
mid-stream failure becomes a visible error to Hermes, not a silent retry.
"""

import time
from typing import AsyncIterator

import httpx
from fastapi.responses import StreamingResponse

from .db import Database
from .load import LoadTracker
from .registry import NodeView, Registry
from .selection import pick_backend

# Ollama can be slow to first byte if the model needs to load into VRAM.
# Connect timeout stays tight on purpose -- a dead/unreachable node should
# fail fast so we can try the next candidate instead of hanging.
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 300.0

# Distinct nodes to try before giving up. Bounded so a fleet with many
# broken nodes doesn't make every request take proportionally longer to
# fail -- three real attempts is enough signal that something's wrong.
MAX_ATTEMPTS = 3


class ProxyError(Exception):
    """No backend could serve this request at all, before any streaming began."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


class _PreStreamFailure(Exception):
    """A node failed before any bytes were forwarded to the caller -- safe to retry elsewhere."""


async def proxy_chat_completion(
    *,
    path: str,
    body: dict,
    client: httpx.AsyncClient,
    registry: Registry,
    tracker: LoadTracker,
    db: Database,
) -> StreamingResponse:
    model = body.get("model")
    stream = bool(body.get("stream"))

    if not model:
        raise ProxyError(400, 'request body must include "model"')

    candidates = registry.candidates_for_model(model)
    if not candidates:
        _log_request(
            db, node_id=None, model=model, stream=stream, ttft_ms=None, total_ms=None,
            completion_tokens=None, status="no_backend",
            error=f"no online node has model {model!r}",
        )
        raise ProxyError(503, f"no online node has model {model!r}")

    attempted: set[str] = set()
    last_error: Exception | None = None

    for _ in range(min(MAX_ATTEMPTS, len(candidates))):
        remaining = [n for n in candidates if n.id not in attempted]
        if not remaining:
            break
        node = pick_backend(remaining, tracker)
        attempted.add(node.id)

        try:
            return await _stream_from_node(
                node=node, path=path, body=body, stream=stream, client=client, tracker=tracker, db=db
            )
        except _PreStreamFailure as e:
            last_error = e
            continue  # nothing sent to the caller yet -- safe to try another node

    _log_request(
        db, node_id=None, model=model, stream=stream, ttft_ms=None, total_ms=None,
        completion_tokens=None, status="error",
        error=str(last_error) if last_error else "all candidate nodes failed",
    )
    raise ProxyError(502, f"all candidate nodes failed: {last_error}")


async def _stream_from_node(
    *,
    node: NodeView,
    path: str,
    body: dict,
    stream: bool,
    client: httpx.AsyncClient,
    tracker: LoadTracker,
    db: Database,
) -> StreamingResponse:
    url = f"{node.base_url}{path}"
    tracker.start(node.id)
    t_start = time.monotonic()

    try:
        request = client.build_request(
            "POST",
            url,
            json=body,
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=READ_TIMEOUT_S, pool=READ_TIMEOUT_S
            ),
        )
        upstream = await client.send(request, stream=True)
    except httpx.HTTPError as e:
        tracker.finish(node.id)
        raise _PreStreamFailure(f"{node.name}: connect failed ({e})") from e

    if upstream.status_code >= 400:
        # No completion bytes have gone anywhere yet -- safe to abandon this
        # node and retry on another.
        error_body = await upstream.aread()
        await upstream.aclose()
        tracker.finish(node.id)
        raise _PreStreamFailure(f"{node.name}: HTTP {upstream.status_code}: {error_body[:200]!r}")

    # Past this point we're committed to `node`. The moment body_iter yields
    # its first chunk, StreamingResponse starts writing bytes to the caller,
    # and there is no way to take that back and try somewhere else.
    async def body_iter() -> AsyncIterator[bytes]:
        ttft_ms: float | None = None
        completion_tokens = 0
        status, error = "ok", None
        try:
            async for chunk in upstream.aiter_bytes():
                if ttft_ms is None:
                    ttft_ms = (time.monotonic() - t_start) * 1000
                if stream:
                    completion_tokens += _count_sse_chunks(chunk)
                yield chunk
        except (httpx.HTTPError, GeneratorExit) as e:
            status, error = "error", str(e)
            raise
        finally:
            total_ms = (time.monotonic() - t_start) * 1000
            tracker.finish(node.id)
            await upstream.aclose()
            _log_request(
                db,
                node_id=node.id,
                model=body.get("model"),
                stream=stream,
                ttft_ms=ttft_ms,
                total_ms=total_ms,
                # Non-streaming responses arrive as one JSON blob, not SSE --
                # there are no per-chunk markers to approximate a token count
                # from, so we don't pretend to have one.
                completion_tokens=completion_tokens if stream else None,
                status=status,
                error=error,
            )

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )


def _count_sse_chunks(chunk: bytes) -> int:
    """Approximate token count for streaming responses.

    Ollama/OpenAI-compatible streaming sends roughly one SSE "data: {...}"
    line per generated token. We don't tokenize the response ourselves --
    that would mean loading a tokenizer per model just to produce a number
    that only feeds a tok/sec estimate on the dashboard, not billing -- so
    counting "data:" lines is a good enough proxy for that purpose.
    """
    return chunk.count(b"data:")


def _log_request(
    db: Database,
    *,
    node_id: str | None,
    model: str | None,
    stream: bool,
    ttft_ms: float | None,
    total_ms: float | None,
    completion_tokens: int | None,
    status: str,
    error: str | None,
) -> None:
    db.execute(
        """
        INSERT INTO requests (ts, node_id, model, stream, ttft_ms, total_ms,
                               completion_tokens, status, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (time.time(), node_id, model, int(stream), ttft_ms, total_ms, completion_tokens, status, error),
    )
