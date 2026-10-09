// Wishlist dashboard: reads data/prices.json + wishlist.yaml, edits the wishlist by committing via the GitHub API.
const $ = (sel, el = document) => el.querySelector(sel);
const money = (n) => (n == null ? "—" : n.toLocaleString("en-NZ", { style: "currency", currency: "NZD" }));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ago = (iso) => {
  if (!iso) return "never";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 60) return `${m} min ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} days ago`;
};
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "item";
const COLORS = ["#2f5bd3", "#d9622b", "#1f8a4c", "#9b4dca", "#c2410c", "#0e7490", "#be185d"];
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let prices = { items: {} };
let wishlist = { items: [] };
let wishlistSha = null; // set when loaded from the GitHub API
const charts = [];

// ------------------------------------------------------------------ GitHub
function ghSettings() {
  let repo = store.get("gh_repo");
  if (!repo) {
    // Default from the GitHub Pages URL: https://<owner>.github.io/<repo>/
    const m = location.hostname.match(/^([^.]+)\.github\.io$/);
    const name = location.pathname.split("/").filter(Boolean)[0];
    if (m && name) repo = `${m[1]}/${name}`;
  }
  return { repo, token: store.get("gh_token") };
}

async function gh(path, opts = {}) {
  const { repo, token } = ghSettings();
  const r = await fetch(`https://api.github.com/repos/${repo}/${path}`, {
    ...opts,
    headers: { Accept: "application/vnd.github+json", Authorization: `Bearer ${token}`, ...(opts.headers || {}) },
  });
  if (!r.ok) throw new Error(`GitHub ${r.status}: ${(await r.json().catch(() => ({}))).message || r.statusText}`);
  return r.json();
}

const b64decode = (s) => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\n/g, "")), (c) => c.charCodeAt(0)));
const b64encode = (s) => btoa(Array.from(new TextEncoder().encode(s), (b) => String.fromCharCode(b)).join(""));

async function loadWishlistFromGitHub() {
  const f = await gh("contents/wishlist.yaml");
  wishlistSha = f.sha;
  return { text: b64decode(f.content), sha: f.sha };
}

async function saveWishlist(mutate, message) {
  const { repo, token } = ghSettings();
  if (!repo || !token) {
    openSettings();
    throw new Error("Connect GitHub first");
  }
  const { text, sha } = await loadWishlistFromGitHub(); // always edit the latest version
  const header = text.split("\n").filter((l) => l.startsWith("#")).join("\n");
  const doc = jsyaml.load(text) || {};
  doc.items = doc.items || [];
  mutate(doc.items);
  const body = (header ? header + "\n" : "") + jsyaml.dump({ ...doc, items: doc.items }, { lineWidth: -1, noRefs: true });
  const res = await gh("contents/wishlist.yaml", {
    method: "PUT",
    body: JSON.stringify({ message, content: b64encode(body), sha }),
  });
  wishlistSha = res.content.sha;
  wishlist = doc;
  render();
  toast("Saved — a price check starts now; results appear in a few minutes.");
}

// ------------------------------------------------------------------ data
async function load() {
  const [p, w] = await Promise.all([
    fetch("data/prices.json", { cache: "no-store" }).then((r) => (r.ok ? r.json() : { items: {} })).catch(() => ({ items: {} })),
    fetch("wishlist.yaml", { cache: "no-store" }).then((r) => (r.ok ? r.text() : "")).catch(() => ""),
  ]);
  prices = p;
  wishlist = jsyaml.load(w) || { items: [] };
  const { repo, token } = ghSettings();
  if (repo && token) {
    // Prefer the live wishlist so edits show immediately, before the next deploy.
    try { wishlist = jsyaml.load((await loadWishlistFromGitHub()).text) || wishlist; } catch (e) { console.warn(e); }
  }
  $("#updated").textContent = prices.updated ? `Prices checked ${ago(prices.updated)}` : "No price checks yet";
  render();
}

function bestOffer(offers) {
  const live = Object.entries(offers).filter(([, o]) => o.price != null && !o.error && o.in_stock !== false);
  return live.sort((a, b) => a[1].price - b[1].price)[0];
}

// ------------------------------------------------------------------ render
function render() {
  charts.splice(0).forEach((c) => c.destroy());
  const main = $("#items");
  const items = wishlist.items || [];
  if (!items.length) {
    main.innerHTML = `<div class="card empty">Your wishlist is empty. Click <b>+ Add item</b> to start tracking.</div>`;
    return;
  }
  main.innerHTML = "";
  for (const item of items) {
    const id = item.id || slug(item.name);
    const entry = prices.items?.[id] || { offers: {} };
    const urls = item.urls || [];
    const offers = Object.fromEntries(urls.map((u) => [u, entry.offers?.[u] || { retailer: new URL(u).hostname.replace(/^www\./, "") }]));
    const best = bestOffer(offers);
    // Out-of-stock prices can't be bought, so they don't count towards "lowest seen" or the chart.
    const allHist = Object.values(offers).flatMap((o) => o.history || []).filter((h) => h.price != null && h.in_stock !== false);
    const rows = Object.entries(offers);
    const soldOut = rows.filter(([, o]) => o.in_stock === false);
    const available = rows.filter(([, o]) => o.in_stock !== false);
    const low = allHist.length ? Math.min(...allHist.map((h) => h.price)) : null;
    const target = item.target_price;
    const deal = entry.deal;
    const DEAL_BADGE = { great: ["good", "🔥 Great deal"], good: ["good", "Good deal"], high: ["warn", "Above typical"] };
    let badge = "";
    if (best && target != null && best[1].price <= target) badge = `<span class="badge good">At or below target</span>`;
    else if (deal && DEAL_BADGE[deal.label]) badge = `<span class="badge ${DEAL_BADGE[deal.label][0]}">${DEAL_BADGE[deal.label][1]}</span>`;
    const dealWhy = deal?.reasons?.length ? `<div class="muted small deal-why">${deal.reasons.map(esc).join(" · ")}</div>` : "";

    const card = document.createElement("section");
    card.className = "card";
    card.innerHTML = `
      <div class="card-head">
        <h2>${esc(item.name)}</h2>
        <div>
          <button class="link" data-act="target">Edit target</button>
          <button class="link" data-act="addurl">Add store</button>
          <button class="link" data-act="remove">Remove</button>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="label">Best now</div>
          <div class="price">${best ? money(best[1].price) : "—"}</div>
          <div class="muted small">${best ? esc(best[1].retailer) + (best[1].shop ? " → " + esc(best[1].shop) : "") : soldOut.length === rows.length && rows.length ? "Out of stock everywhere" : rows.some(([, o]) => o.note) ? "No trusted shop lists it" : "Waiting for first check"} ${badge}</div></div>
        <div class="stat"><div class="label">Target</div><div class="price">${money(target)}</div></div>
        <div class="stat"><div class="label">Lowest seen</div><div class="price">${money(low)}</div></div>
      </div>
      ${dealWhy}
      ${allHist.length > 1 ? `<div class="chart"><canvas></canvas></div>` : ""}
      <div class="table-wrap"><table>
        <thead><tr><th>Store</th><th>Price</th><th>Stock</th><th>Checked</th><th></th></tr></thead>
        <tbody>${[...available, ...soldOut].map(([u, o]) => `
          <tr class="${o.in_stock === false ? "oos" : ""}">
            <td>${esc(o.retailer)}${o.shop ? ` → ${esc(o.shop)}` : ""}${o.title ? `<div class="muted small">${esc(o.title)}</div>` : ""}${o.method === "json-ld-range" ? `<div class="err">Multi-size page: price may not be your size</div>` : ""}
                ${o.error ? `<div class="err">${esc(o.error)}</div>` : ""}
                ${o.pending ? `<div class="note">Unconfirmed reading ${money(o.pending.price)}${o.pending.in_stock === false ? " (out of stock)" : ""}, rechecking next run</div>` : ""}
                ${o.identity_note ? `<div class="err">⚠ ${esc(o.identity_note)}</div>` : ""}
                ${sizeLine(o)}
                ${o.note ? `<div class="note">${esc(o.note)}</div>` : ""}
                ${sellersLine(o)}</td>
            <td class="num">${money(o.price)}${o.was ? `<span class="was">${money(o.was)}</span>` : ""}</td>
            <td>${o.in_stock === false ? "Out" : o.in_stock ? "In stock" : "—"}</td>
            <td class="muted small">${ago(o.checked)}</td>
            <td><a href="${esc(u)}" target="_blank" rel="noopener">Open ↗</a></td>
          </tr>`).join("")}
        </tbody></table></div>
      ${soldOut.length ? `<button class="link" data-act="oos">Show ${soldOut.length} out of stock</button>` : ""}`;
    main.append(card);

    card.querySelector('[data-act="remove"]').onclick = () => {
      if (confirm(`Stop tracking "${item.name}"? (Price history is kept.)`))
        saveWishlist((list) => list.splice(list.findIndex((i) => (i.id || slug(i.name)) === id), 1), `Remove ${id}`).catch(fail);
    };
    card.querySelector('[data-act="target"]').onclick = () => {
      const v = prompt("Target price in NZD (blank to clear)", target ?? "");
      if (v === null) return;
      saveWishlist((list) => {
        const it = list.find((i) => (i.id || slug(i.name)) === id);
        if (v.trim() === "") delete it.target_price; else it.target_price = Number(v);
      }, `Set target for ${id}`).catch(fail);
    };
    card.querySelector('[data-act="addurl"]').onclick = () => {
      const v = prompt("Product URL from another store");
      if (!v) return;
      saveWishlist((list) => {
        const it = list.find((i) => (i.id || slug(i.name)) === id);
        if (!it.urls.includes(v.trim())) it.urls.push(v.trim());
      }, `Add store to ${id}`).catch(fail);
    };

    const oosBtn = card.querySelector('[data-act="oos"]');
    if (oosBtn) oosBtn.onclick = () => {
      const shown = card.classList.toggle("show-oos");
      oosBtn.textContent = `${shown ? "Hide" : "Show"} ${soldOut.length} out of stock`;
    };

    const canvas = card.querySelector("canvas");
    if (canvas) drawChart(canvas, offers, target);
  }
}

function sellersLine(o) {
  const others = (o.shop_offers || []).filter((r) => !(r.trusted && r.shop === o.shop && r.price === o.price));
  if (!others.length) return "";
  return `<div class="muted small">Also listed: ${others.map((r) =>
    `${esc(r.shop)} ${money(r.price)}${r.trusted ? "" : " (untrusted)"}${r.in_stock === false ? " (out of stock)" : ""}`).join(" · ")}</div>`;
}

function sizeLine(o) {
  if (o.size_status === "ok" && o.my_sizes?.length)
    return `<div class="sizes">${o.my_sizes.map((s) =>
      `<span class="size ${s.in_stock ? "in" : "out"}" title="${s.in_stock ? "In stock" : "Sold out"}">${esc(s.label)} ${s.in_stock ? "✓" : "✗"}</span>`).join("")}</div>`;
  if (o.size_status === "none") return `<div class="note">Your sizes aren't listed at this store</div>`;
  if (o.size_status === "unknown") return `<div class="note">Couldn't read sizes here; stock shown is for any size</div>`;
  return "";
}

function drawChart(canvas, offers, target) {
  const css = getComputedStyle(document.documentElement);
  const grid = css.getPropertyValue("--border").trim();
  const text = css.getPropertyValue("--muted").trim();
  const datasets = Object.values(offers)
    .filter((o) => (o.history || []).some((h) => h.price != null && h.in_stock !== false))
    .map((o, i) => ({
      label: (() => {
        const full = o.title && o.title !== o.retailer ? `${o.retailer} – ${o.title}` : o.retailer;
        return full.length > 48 ? full.slice(0, 47) + "…" : full;  // long store titles overflow the legend
      })(),
      // Out-of-stock periods become gaps in the line.
      data: o.history.filter((h) => h.price != null).map((h) => ({ x: h.t, y: h.in_stock === false ? null : h.price })),
      borderColor: COLORS[i % COLORS.length],
      backgroundColor: COLORS[i % COLORS.length],
      stepped: "before", pointRadius: 2, borderWidth: 2,
    }));
  if (target != null) {
    const xs = datasets.flatMap((d) => d.data.map((p) => p.x)).sort();
    if (!xs.length) return;
    datasets.push({ label: "Target", data: [{ x: xs[0], y: target }, { x: xs.at(-1), y: target }],
      borderColor: text, borderDash: [5, 5], pointRadius: 0, borderWidth: 1 });
  }
  charts.push(new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      maintainAspectRatio: false, interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { type: "time", time: { minUnit: "hour", tooltipFormat: "d MMM yyyy, h:mm a" }, grid: { color: grid }, ticks: { color: text, maxRotation: 0, autoSkipPadding: 16 } },
        y: { grid: { color: grid }, ticks: { color: text, callback: (v) => "$" + v } },
      },
      plugins: {
        legend: { labels: { color: text, boxWidth: 12 } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}` } },
      },
    },
  }));
}

// ------------------------------------------------------------------ UI
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 4500);
}
const fail = (e) => toast(e.message || String(e));

function openSettings() {
  const f = $("#settingsForm");
  const s = ghSettings();
  f.repo.value = s.repo || "";
  f.token.value = s.token || "";
  $("#settingsDialog").showModal();
}

$("#settingsBtn").onclick = openSettings;
$("#settingsDialog").addEventListener("close", async () => {
  if ($("#settingsDialog").returnValue !== "save") return;
  const f = $("#settingsForm");
  store.set("gh_repo", f.repo.value.trim());
  store.set("gh_token", f.token.value.trim());
  try { await loadWishlistFromGitHub(); toast("Connected to GitHub"); load(); } catch (e) { fail(e); }
});

$("#addBtn").onclick = () => { $("#addForm").reset(); $("#addDialog").showModal(); };
$("#addDialog").addEventListener("close", () => {
  if ($("#addDialog").returnValue !== "save") return;
  const f = $("#addForm");
  const name = f.name.value.trim();
  const urls = f.urls.value.split(/\s+/).map((u) => u.trim()).filter((u) => /^https?:\/\//.test(u));
  if (!name || !urls.length) return toast("Name and at least one URL are required");
  saveWishlist((list) => {
    const ids = new Set(list.map((i) => i.id || slug(i.name)));
    let id = slug(name), n = 2;
    while (ids.has(id)) id = `${slug(name)}-${n++}`;
    const item = { id, name, urls };
    if (f.target.value) item.target_price = Number(f.target.value);
    list.push(item);
  }, `Add ${name}`).catch(fail);
});

load();
