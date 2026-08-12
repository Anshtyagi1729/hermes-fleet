"""Fleet simulator: pretends to be N nodes registering and heartbeating.

Exists because the machine developing this has an MX450 with 2GB VRAM and
can't run a real inference server -- this is the only way to see the
registry and dashboard working end to end without a friend online.

Kill it with Ctrl+C and watch the node(s) go offline on the dashboard after
node_timeout_s. No extra code needed for that: "offline" is just "stopped
heartbeating," and this script demonstrates that by, well, stopping.

Usage:
    uv run python fake_node.py                # 1 fake node against localhost
    uv run python fake_node.py --count 3       # 3 fake nodes at once
    uv run python fake_node.py --host http://100.x.y.z:8080
"""

import argparse
import asyncio
import random
import time

import httpx

from app.config import settings

FAKE_GPUS = [
    ("NVIDIA RTX 4070", 12288),
    ("NVIDIA RTX 3080", 10240),
    ("NVIDIA RTX 4090", 24576),
    ("AMD RX 7900 XT", 20480),
]
FAKE_MODELS = [["hermes3"], ["hermes3", "qwen2.5:7b"], ["llama3.1:8b"]]


async def run_fake_node(
    client: httpx.AsyncClient, host: str, token: str, index: int, interval: int
) -> None:
    # Deterministic id (not random) so re-running the script for the same
    # index exercises the real re-registration path in registry.register(),
    # not just first-time inserts.
    node_id = f"fake-{index}"
    gpu, vram_total = random.choice(FAKE_GPUS)
    models = random.choice(FAKE_MODELS)

    register_payload = {
        "token": token,
        "node_id": node_id,
        "name": f"fake-gpu-{index}",
        "ip": "127.0.0.1",
        "port": 11434,
        "backend": "ollama",
        "os": "linux",
        "arch": "x86_64",
        "gpu": gpu,
        "vram_total_mb": vram_total,
        "ram_total_mb": 32768,
        "agent_version": "sim-0.1.0",
        "models": models,
    }
    resp = await client.post(f"{host}/api/register", json=register_payload)
    resp.raise_for_status()
    print(f"[{node_id}] registered as {register_payload['name']} ({gpu})")

    # Ramp VRAM usage up and down over time instead of a flat number, so the
    # dashboard's usage bar actually moves during a demo.
    t0 = time.monotonic()
    while True:
        phase = (time.monotonic() - t0) / 20.0
        usage_frac = 0.3 + 0.4 * abs((phase % 2) - 1)
        heartbeat_payload = {
            "token": token,
            "node_id": node_id,
            "vram_used_mb": int(vram_total * usage_frac),
            "ram_used_mb": int(32768 * 0.2),
            "cpu_pct": round(random.uniform(2, 15), 1),
        }
        try:
            resp = await client.post(f"{host}/api/heartbeat", json=heartbeat_payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Host restarted and lost its DB -- re-register and continue,
                # same recovery a real agent is expected to do.
                await client.post(f"{host}/api/register", json=register_payload)
            else:
                raise
        await asyncio.sleep(interval)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=f"http://127.0.0.1:{settings.port}")
    parser.add_argument("--token", default=settings.invite_token)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--interval", type=int, default=settings.heartbeat_interval_s)
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=5.0) as client:
        tasks = [
            run_fake_node(client, args.host, args.token, i, args.interval)
            for i in range(1, args.count + 1)
        ]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
