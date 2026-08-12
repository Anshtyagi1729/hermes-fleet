"""Fake Ollama-compatible inference server, for testing the M2 router end to
end without a real GPU or a real friend online.

Mimics just enough of Ollama's OpenAI-compatible /v1/chat/completions to be
indistinguishable from the real thing as far as proxy.py is concerned: real
SSE framing, an artificial delay before the first token (so ttft_ms is
actually measuring something), and a delay between tokens (so total_ms
grows visibly instead of returning instantly).

Usage:
    uv run uvicorn fake_ollama:app --port 11500
"""

import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

FAKE_TOKENS = ["Hello", ",", " world", "!", " This", " is", " a", " fake", " response", "."]
FIRST_TOKEN_DELAY_S = 0.3
PER_TOKEN_DELAY_S = 0.05


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict):
    model = payload.get("model", "fake-model")
    stream = payload.get("stream", False)

    if not stream:
        await asyncio.sleep(FIRST_TOKEN_DELAY_S)
        return {
            "id": "fake-1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(FAKE_TOKENS)},
                    "finish_reason": "stop",
                }
            ],
        }

    async def gen():
        await asyncio.sleep(FIRST_TOKEN_DELAY_S)
        for tok in FAKE_TOKENS:
            chunk = {
                "id": "fake-1",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": tok}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            await asyncio.sleep(PER_TOKEN_DELAY_S)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
