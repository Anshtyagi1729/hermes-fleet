// Polls /api/nodes on an interval and re-renders cards. This is the browser
// half of "no manual polling" -- you never refresh this page, and adding a
// node never means editing anything here or on the server.
const POLL_MS = 3000;

function fmtMb(mb) {
  if (mb == null) return "-";
  if (mb >= 1024) return (mb / 1024).toFixed(1) + " GB";
  return mb + " MB";
}

function fmtUptime(seconds) {
  if (seconds < 60) return Math.floor(seconds) + "s";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m";
  return (seconds / 3600).toFixed(1) + "h";
}

// Every field below comes from a node's self-reported register payload --
// name, gpu, and model strings are NOT trusted input. Without this, a node
// registering with name = "<img src=x onerror=...>" would run arbitrary JS
// in this dashboard the moment its card renders (stored XSS).
function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function nodeCard(n) {
  const memPct = n.vram_total_mb
    ? Math.round((100 * (n.vram_used_mb || 0)) / n.vram_total_mb)
    : null;
  const name = escapeHtml(n.name);
  const gpu = escapeHtml(n.gpu || "CPU only");
  const models = n.models.length ? escapeHtml(n.models.join(", ")) : "-";
  return `
    <div class="card ${n.online ? "online" : "offline"} ${n.enabled ? "" : "disabled"}">
      <div class="card-head">
        <span class="dot"></span>
        <span class="name">${name}</span>
        <button class="toggle" data-id="${escapeHtml(n.id)}" data-enabled="${n.enabled}">
          ${n.enabled ? "disable" : "enable"}
        </button>
      </div>
      <div class="row">${gpu}</div>
      <div class="row">VRAM ${fmtMb(n.vram_used_mb)} / ${fmtMb(n.vram_total_mb)}${
        memPct != null ? ` (${memPct}%)` : ""
      }</div>
      <div class="row">models: ${models}</div>
      <div class="row">uptime ${fmtUptime(n.uptime_s)}</div>
    </div>
  `;
}

async function refresh() {
  const res = await fetch("/api/nodes");
  const nodes = await res.json();

  document.getElementById("summary").textContent =
    `${nodes.filter((n) => n.online).length} / ${nodes.length} online`;

  const container = document.getElementById("nodes");
  container.innerHTML = nodes.length
    ? nodes.map(nodeCard).join("")
    : `<p class="empty">No nodes yet.</p>`;

  for (const btn of container.querySelectorAll(".toggle")) {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.id;
      const enabled = btn.dataset.enabled === "true";
      await fetch(`/api/nodes/${encodeURIComponent(id)}/enabled`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !enabled }),
      });
      refresh();
    });
  }
}

refresh();
setInterval(refresh, POLL_MS);
