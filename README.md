# Hermes Fleet

A control plane that lets Hermes Agent route inference requests across friends' GPUs over a
private [Tailscale](https://tailscale.com) network. Hermes itself doesn't do inference — it calls
an OpenAI-compatible endpoint. Hermes Fleet is that endpoint: one stable URL that quietly picks
whichever friend's machine is free right now, streams the response back, and measures how it
performed.

The core idea that shapes the whole design: **the host sits in the data path, not just a
dashboard next to it.** Hermes is configured once, against `http://127.0.0.1:8080/v1/...`,
forever. Nodes joining or going offline never require touching Hermes's config — the router picks
a live backend per request, and a node existing is just "it told us it exists," not an entry in a
file you maintain.

## What it is right now

**A friend joins with one command.** `/connect` generates a copy-paste one-liner (bash for
Linux/macOS, PowerShell for Windows) that installs Tailscale, joins your tailnet, installs Ollama,
binds it to the tailnet interface specifically, pulls a model, and registers — nothing else
required from them. Every step is safe to re-run: it skips reinstalling anything already present,
and reconfigures rather than double-runs an already-installed Ollama (systemd-aware on Linux).

**Nodes are tracked by heartbeat, not a config file.** Every ~5s a node reports itself alive; a
node is "online" purely by `now - last_seen < 15s`. Nobody polls anybody, nobody edits a list to
add or remove a friend.

**A live dashboard** at `/` shows every node — online/offline, GPU, VRAM usage, model list — polling
`/api/nodes` every few seconds. An enable/disable toggle lets you pull a flaky node out of rotation
without touching their machine.

**A real OpenAI-compatible router** at `/v1/chat/completions`, `/v1/completions`, `/v1/models`.
Picks the least-busy online node (ties broken randomly so an idle fleet doesn't pile onto one
machine), streams the response back, and measures TTFT/duration/token count itself — the node
agent never reports performance, because the router is the one thing actually positioned to
measure it. Retries a dead node before any bytes reach the caller; once streaming starts, a
failure becomes a visible error, never a silent swap.

**Security-reviewed, not just written.** SQL is fully parameterized, the dashboard escapes
untrusted node-reported strings (a real stored-XSS bug was found and fixed here), and a real
remote-code-execution vulnerability in the install-script generation was found, proven exploitable
with an actual payload, and fixed with strict input validation — covered by regression tests.

**Cross-platform nodes.** `agent.sh` (Linux/macOS) and `agent_template.ps1` (Windows) implement the
identical register/heartbeat contract, each written idiomatically for its platform rather than
one script awkwardly covering both.

### Architecture

```
Hermes ──> http://127.0.0.1:8080/v1/...   (configured once, never changes)
                    │
              ┌─────┴──────┐
              │  the host  │   registry · router · dashboard · SQLite
              └─────┬──────┘
        ┌───────────┼───────────┐
        │           │           │
   friend A's   friend B's   friend C's
    Ollama       Ollama       Ollama      (each bound to ITS OWN tailscale IP,
  (Linux/mac)   (Windows)      (...)       never 0.0.0.0 -- Ollama has no auth
                                            of its own)
```

Everything is reachable only because Tailscale already put it on the same private, encrypted
mesh — there is no public-internet exposure anywhere in this chain. The invite token is a second,
smaller gate *inside* the tailnet: it stops an already-tailnet-connected device from silently
registering itself as a compute node.

### Repo layout

```
host/
  app/
    main.py              FastAPI routes: register, heartbeat, dashboard, router, /connect
    registry.py          who's in the fleet, derived online/enabled state
    proxy.py             the streaming router: pick a node, forward, measure, retry
    selection.py         least-in-flight backend selection policy
    load.py              in-memory in-flight request counter
    db.py                SQLite schema + connection handling
    config.py            env-overridable settings, persisted secrets
    schemas.py           Pydantic request/response models
    agent_template.sh    node agent -- Linux/macOS
    agent_template.ps1   node agent -- Windows
    templates/           dashboard.html, connect.html
    static/               dashboard.css, dashboard.js
  fake_node.py           simulates fleet nodes for local dev (no GPU needed)
  fake_ollama.py         fake OpenAI-compatible backend for testing the router
  tests/                 28 tests: registry, selection policy, injection regressions
```

## What it's going to have

- **Deployability (next up).** A `systemd --user` unit with `loginctl enable-linger` so the host
  survives reboot and logout without a terminal window staying open, plus a proper install script.
- **Shard mode.** Splitting one model across multiple friends' GPUs via llama.cpp's `rpc-server`,
  for when no single machine fits a model alone. Deliberately deferred until pool routing (today's
  mode) is solid — a shard cluster will register as a single backend, so the router itself needs
  no changes to support it.
- **Windows reboot persistence.** The Linux agent's systemd drop-in survives a reboot; the Windows
  equivalent doesn't yet (Ollama's Windows installer autostarts back on the default, non-tailnet
  address after a restart). Needs either a persistent environment variable or a scheduled task.
- **A test-prompt button** on the dashboard — pick any online node, send a quick prompt, see
  response time, without needing to hand-craft a curl command.
- **Tighter default trust model, if the fleet grows.** `/v1/chat/completions` and dashboard reads
  currently require only tailnet membership, not the invite token — fine for a handful of trusted
  friends, worth one added check if that group grows significantly.

## Running it

```bash
cd host
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Visit `/` for the dashboard, `/connect` for the install command to send a friend. Point Hermes at
`http://127.0.0.1:8080/v1`.

No GPU on your machine? `uv run python fake_node.py --count 3` simulates a small fleet against a
running host for local development.

```bash
uv run --group dev pytest -q
```
