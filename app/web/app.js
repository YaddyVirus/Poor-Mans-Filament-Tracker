/* Poor Man's Filament Tracker UI — all API paths relative for HA ingress. */
const $ = (sel) => document.querySelector(sel);

const state = {
  spools: [], usage: [], recent: [], status: null, catalog: null,
  showArchived: false, currency: "INR",
};

const CURRENCIES = [
  { code: "USD", symbol: "$", label: "$ USD" },
  { code: "EUR", symbol: "€", label: "€ EUR" },
  { code: "GBP", symbol: "£", label: "£ GBP" },
  { code: "INR", symbol: "₹", label: "₹ INR" },
  { code: "JPY", symbol: "¥", label: "¥ JPY" },
  { code: "CNY", symbol: "¥", label: "¥ CNY" },
  { code: "AUD", symbol: "A$", label: "A$ AUD" },
  { code: "CAD", symbol: "C$", label: "C$ CAD" },
  { code: "CHF", symbol: "Fr", label: "Fr CHF" },
  { code: "KRW", symbol: "₩", label: "₩ KRW" },
  { code: "BRL", symbol: "R$", label: "R$ BRL" },
  { code: "MXN", symbol: "$", label: "$ MXN" },
];
const currencySymbol = (code) => CURRENCIES.find((c) => c.code === code)?.symbol || code;

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});
const put = (path, body) => api(path, {
  method: "PUT", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const fmt = (g) => (g >= 1000 ? (g / 1000).toFixed(2) + " kg" : Math.round(g * 10) / 10 + " g");
const fmtCost = (c) => currencySymbol(state.currency) + c.toFixed(2);
const pct = (s) => (s.initial_weight_g ? Math.max(0, Math.min(100, (100 * s.remaining_g) / s.initial_weight_g)) : 0);
// Cost isn't stored per print — derive it from the spool's cost/weight ratio at read time.
const rowCost = (u) => (u.spool_cost && u.spool_initial_weight_g ? (u.grams / u.spool_initial_weight_g) * u.spool_cost : null);
const spoolSpent = (s) => (s.cost && s.initial_weight_g ? Math.max(0, ((s.initial_weight_g - s.remaining_g) / s.initial_weight_g) * s.cost) : null);
const barClass = (p) => (p < 10 ? "bad" : p < 25 ? "warn" : "");
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const spoolLabel = (s) => `${s.brand} ${s.name}`.trim();

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 3200);
}

async function refresh() {
  const archived = state.showArchived ? "?archived=1" : "";
  [state.spools, state.usage, state.recent, state.status] = await Promise.all([
    api("api/spools" + archived),
    api("api/usage"),
    api("api/usage?recent=1"),
    api("api/status"),
  ]);
  if (state.status?.currency) state.currency = state.status.currency;
  const sel = $("#currency-select");
  if (sel && document.activeElement !== sel) sel.value = state.currency;
  render();
}

function render() {
  renderPills();
  renderVerify();
  renderBanner();
  renderHero();
  renderDetection();
  renderSpools();
  renderRecent();
  renderHistory();
}

function pillHTML(title, sub) {
  return `<span class="dot"></span><span class="pl"><b>${esc(title)}</b><small>${esc(sub)}</small></span>`;
}

function renderPills() {
  const st = state.status || {};
  const printer = $("#pill-printer");
  printer.className = "pill " + (st.connected ? "on" : "off");
  printer.innerHTML = pillHTML(
    "Printer",
    `${st.mode === "direct" ? "direct" : "via HA"} · ${st.connected ? "online" : "offline"}`
  );
  const haOn = st.ha_available || st.ha_control?.available;
  const ha = $("#pill-ha");
  ha.className = "pill " + (haOn ? "on" : "off");
  ha.innerHTML = pillHTML(
    "Home Assistant",
    haOn ? (st.ha_control?.available ? "sync + control" : "sync only") : "offline"
  );
  if (st.ha_control?.entity_id) ha.title = st.ha_control.entity_id;
}

function renderVerify() {
  const el = $("#verify-card");
  const det = state.status?.detection;
  el.classList.toggle("hidden", !det?.ambiguous);
  if (!det?.ambiguous) return;
  const active = state.status?.active_spool;
  const cands = det.candidates.map((c) => `
    <button class="cand" onclick="confirmSpool(${c.id})">
      <span class="swatch xs" style="background:${esc(c.color_hex)}"></span>
      ${esc(c.label)} <span class="muted">· ${fmt(c.remaining_g)} left</span>
      ${active && active.id === c.id ? " ✓ current" : ""}
    </button>`).join("");
  el.innerHTML = `
    <div class="alert-title">⚠️ Which spool did you load?</div>
    <div class="muted small">Multiple spools match the filament set on the printer.
    Usage is going to <b>${esc(active ? spoolLabel(active) : "no spool")}</b> until you confirm.</div>
    <div class="cands">${cands}</div>`;
}

function renderBanner() {
  const el = $("#print-banner");
  const p = state.status?.printer;
  const printing = p && ["prepare", "running", "pause", "slicing", "init"].includes(p.status);
  el.classList.toggle("hidden", !printing);
  if (!printing) return;
  el.innerHTML = `
    <span>🖨️</span>
    <b>${esc(p.task || "Printing…")}</b>
    <div class="prog"><div class="bar"><div style="width:${Math.round(p.progress)}%"></div></div></div>
    <span class="num">${Math.round(p.progress)}%</span>
    <span class="muted small">${p.weight ? "est. " + fmt(p.weight) : "weight pending"}</span>`;
}

function renderHero() {
  const s = state.spools.find((x) => x.active && !x.archived);
  const el = $("#hero");
  if (!s) {
    el.innerHTML = `<div class="empty" style="width:100%">No spool loaded — add one and hit <b>Load</b>, or set the filament on the printer and let auto-detect find it.</div>`;
    return;
  }
  const p = pct(s);
  el.innerHTML = `
    <div class="swatch lg" style="background:${esc(s.color_hex)}"></div>
    <div class="info">
      <div class="name">${esc(spoolLabel(s))}</div>
      <div class="sub">${esc(s.material)} · loaded on external spool</div>
      <div class="bar ${barClass(p)}"><div style="width:${p}%"></div></div>
      <div class="nums"><b class="num">${fmt(s.remaining_g)}</b> of ${fmt(s.initial_weight_g)} · ${p.toFixed(0)}%</div>
    </div>
    <button class="btn small" onclick="openUse(${s.id})">Log usage</button>`;
}

function renderDetection() {
  const el = $("#detection");
  const det = state.status?.printer?.detected;
  const info = state.status?.detection || {};
  const show = det && !info.ambiguous;
  el.classList.toggle("hidden", !show);
  if (!show) return;
  const label = det.type || det.name || "?";
  el.className = "detect" + (info.note === "no matching spool" ? " warn" : "");
  el.innerHTML =
    `🎯 printer filament: ` +
    (det.color ? `<span class="swatch xs" style="background:${esc(det.color.slice(0, 7))}"></span>` : "") +
    `<b>${esc(label)}</b>` +
    (info.note ? `<span>— ${esc(info.note)}</span>` : "");
}

function renderSpools() {
  $("#spools").innerHTML = state.spools.map((s) => {
    const p = pct(s);
    const spent = spoolSpent(s);
    return `
    <div class="spool ${s.active ? "active" : ""} ${s.archived ? "archived" : ""}">
      <div class="head">
        <div class="swatch sm" style="background:${esc(s.color_hex)}"></div>
        <div class="name">${esc(spoolLabel(s))}</div>
        <div class="tag ${s.active ? "loaded" : ""}">${s.active ? "LOADED" : esc(s.material)}</div>
      </div>
      <div class="bar ${barClass(p)}"><div style="width:${p}%"></div></div>
      <div class="meta num">${fmt(s.remaining_g)} left · ${esc(s.material)}${s.cost ? " · " + currencySymbol(state.currency) + s.cost : ""}</div>
      ${spent != null ? `<div class="meta num muted small">${fmtCost(spent)} spent so far</div>` : ""}
      <div class="btns">
        ${s.active ? "" : `<button class="btn small primary" onclick="activate(${s.id})">Load</button>`}
        <button class="btn small" onclick="openEdit(${s.id})">Edit</button>
        <button class="btn small ghost" onclick="archive(${s.id}, ${s.archived ? "false" : "true"})">${s.archived ? "Restore" : "Archive"}</button>
        <button class="btn small ghost danger" onclick="removeSpool(${s.id})">Delete</button>
      </div>
    </div>`;
  }).join("") || `<div class="card empty" style="grid-column:1/-1">No spools yet — add your first one.</div>`;
}

function usageRow(u, showEdit) {
  const zero = u.grams === 0 && (u.kind === "print" || u.kind === "failed");
  const cost = rowCost(u);
  return `
  <div class="rowitem">
    <span class="kind ${zero ? "zero" : esc(u.kind)}">${zero ? "0 g?" : esc(u.kind)}</span>
    <div class="job">${esc(u.job_name)}</div>
    <div class="spoolchip"><span class="swatch xs" style="background:${esc(u.color_hex)}"></span>${esc(u.brand)} ${esc(u.name)}</div>
    <div class="when">${new Date(u.ts).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</div>
    <div class="grams num">${u.grams < 0 ? "+" + fmt(-u.grams) : fmt(u.grams)}</div>
    <div class="cost num muted">${cost != null ? (cost < 0 ? "+" + fmtCost(-cost) : fmtCost(cost)) : ""}</div>
    ${showEdit ? `<button class="btn small ghost" title="Edit / reassign" onclick="openUsage(${u.id})">✎</button>` : ""}
  </div>`;
}

function renderRecent() {
  const el = $("#recent");
  const prints = state.recent.filter((u) => u.kind !== "adjust");
  const others = state.spools.filter((s) => !s.archived);
  $("#moveall").classList.toggle("hidden", !prints.length || others.length < 2);
  if (prints.length && others.length >= 2) {
    $("#moveall-select").innerHTML = others
      .map((s) => `<option value="${s.id}">${esc(spoolLabel(s))}</option>`).join("");
  }
  el.innerHTML = prints.map((u) => usageRow(u, true)).join("") ||
    `<div class="empty">No prints since the current spool was loaded.</div>`;
}

function renderHistory() {
  $("#history").innerHTML = state.usage.map((u) => usageRow(u, u.kind !== "adjust")).join("") ||
    `<div class="empty">Nothing here yet — finish a print and it shows up.</div>`;
}

/* ---- actions ---- */
window.activate = async (id) => { await post(`api/spools/${id}/activate`); toast("Spool loaded"); refresh(); };
window.confirmSpool = async (id) => { await post("api/detection/confirm", { spool_id: id }); toast("Thanks — spool verified"); refresh(); };
window.archive = async (id, flag) => { await post(`api/spools/${id}/archive`, { archived: flag }); refresh(); };
window.removeSpool = async (id) => {
  const s = state.spools.find((x) => x.id === id);
  if (!confirm(`Delete "${spoolLabel(s)}" and its history? Archiving keeps the history.`)) return;
  await api(`api/spools/${id}`, { method: "DELETE" });
  refresh();
};

$("#moveall-btn").onclick = async () => {
  const target = parseInt($("#moveall-select").value);
  const prints = state.recent.filter((u) => u.kind !== "adjust" && u.spool_id !== target);
  for (const u of prints) await put(`api/usage/${u.id}`, { spool_id: target });
  toast(`Moved ${prints.length} print${prints.length === 1 ? "" : "s"}`);
  refresh();
};

/* ---- spool dialog + swatch picker ---- */
const dialog = $("#spool-dialog"), form = $("#spool-form");

const GENERIC_MATERIALS = ["PLA+", "PLA", "PLA Matte", "PLA Silk", "PETG",
  "ABS", "ASA", "TPU", "PC", "PA", "Other"];

const brandColors = () =>
  (state.catalog?.brands || []).find((b) => b.name === form.brand.value)?.colors || [];

function fillBrands() {
  const brands = state.catalog?.brands || [];
  $("#brand-select").innerHTML = brands
    .map((b) => `<option>${esc(b.name)}</option>`).join("");
}

function fillMaterials(current) {
  const lines = [...new Set(brandColors().map((c) => c.line))];
  const opts = [...lines, ...GENERIC_MATERIALS.filter((m) => !lines.includes(m))];
  if (current && !opts.includes(current)) opts.unshift(current);
  form.material.innerHTML = opts.map((o) => `<option>${esc(o)}</option>`).join("");
  form.material.value = current || opts[0];
}

function renderSwatches() {
  const el = $("#swatches");
  const colors = brandColors();
  el.classList.toggle("hidden", !colors.length);
  if (!colors.length) return;
  // show only the selected material's swatches when the brand offers it
  const mat = form.material.value;
  const shown = colors.some((c) => c.line === mat)
    ? colors.filter((c) => c.line === mat) : colors;
  const lines = [...new Set(shown.map((c) => c.line))];
  el.innerHTML = lines.map((line) => `
    <div class="line-label">${esc(line)}</div>
    <div class="swatch-grid">
      ${shown.filter((c) => c.line === line).map((c) => `
        <button type="button" class="sw" title="${esc(c.name)}" data-name="${esc(c.name)}"
          data-line="${esc(c.line)}" data-hex="${esc(c.hex)}" style="background:${esc(c.hex)}"></button>
      `).join("")}
    </div>`).join("");
  el.querySelectorAll(".sw").forEach((btn) => {
    btn.onclick = () => {
      el.querySelectorAll(".sw.sel").forEach((b) => b.classList.remove("sel"));
      btn.classList.add("sel");
      form.color_hex.value = btn.dataset.hex;
      form.name.value = btn.dataset.name;
      form.material.value = btn.dataset.line;
    };
  });
}

$("#brand-select").onchange = () => { fillMaterials(); renderSwatches(); };
$("#material-select").onchange = renderSwatches;

$("#btn-add").onclick = () => {
  form.reset();
  form.id.value = "";
  form.brand.value = state.catalog?.brands?.some((b) => b.name === "Numakers") ? "Numakers" : form.brand.value;
  fillMaterials("PLA+");
  $("#dialog-title").textContent = "Add spool";
  renderSwatches();
  dialog.showModal();
};

window.openEdit = (id) => {
  const s = state.spools.find((x) => x.id === id);
  form.reset();
  form.brand.value = [...form.brand.options].some((o) => o.value === s.brand) ? s.brand : "Other";
  fillMaterials(s.material);
  for (const f of ["id", "name", "color_hex", "initial_weight_g", "remaining_g", "cost", "notes"])
    if (form[f] && s[f] != null) form[f].value = s[f];
  $("#dialog-title").textContent = "Edit spool";
  renderSwatches();
  dialog.showModal();
};

$("#btn-cancel").onclick = () => dialog.close();

form.onsubmit = async (e) => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(form));
  const body = {
    brand: d.brand, name: d.name, material: d.material, color_hex: d.color_hex,
    initial_weight_g: parseFloat(d.initial_weight_g) || 1000,
    notes: d.notes || "",
  };
  if (d.remaining_g !== "") body.remaining_g = parseFloat(d.remaining_g);
  body.cost = d.cost === "" ? null : parseFloat(d.cost);  // null clears a saved cost
  if (d.id) await put(`api/spools/${d.id}`, body);
  else await post("api/spools", body);
  dialog.close();
  toast(d.id ? "Spool updated" : "Spool added");
  refresh();
};

/* ---- manual usage dialog ---- */
const useDialog = $("#use-dialog"), useForm = $("#use-form");
window.openUse = (id) => { useForm.reset(); useForm.id.value = id; useDialog.showModal(); };
$("#btn-use-cancel").onclick = () => useDialog.close();
useForm.onsubmit = async (e) => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(useForm));
  await post(`api/spools/${d.id}/use`, { grams: parseFloat(d.grams), job_name: d.job_name || "Manual entry" });
  useDialog.close();
  refresh();
};

/* ---- edit print dialog ---- */
const usageDialog = $("#usage-dialog"), usageForm = $("#usage-form");
window.openUsage = (id) => {
  const u = [...state.usage, ...state.recent].find((x) => x.id === id);
  if (!u) return;
  usageForm.reset();
  usageForm.id.value = id;
  usageForm.grams.value = u.grams;
  $("#usage-job").textContent = `${u.job_name} · ${new Date(u.ts).toLocaleString()}`;
  usageForm.spool_id.innerHTML = state.spools
    .filter((s) => !s.archived || s.id === u.spool_id)
    .map((s) => `<option value="${s.id}" ${s.id === u.spool_id ? "selected" : ""}>${esc(spoolLabel(s))}</option>`)
    .join("");
  usageDialog.showModal();
};
$("#btn-usage-cancel").onclick = () => usageDialog.close();
usageForm.onsubmit = async (e) => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(usageForm));
  await put(`api/usage/${d.id}`, { grams: parseFloat(d.grams), spool_id: parseInt(d.spool_id) });
  usageDialog.close();
  toast("Print updated");
  refresh();
};

$("#show-archived").onchange = (e) => { state.showArchived = e.target.checked; refresh(); };

/* ---- theme ---- */
const themeBtn = $("#theme-toggle");
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("pmft-theme", t);
  themeBtn.textContent = t === "light" ? "🌙" : "☀️";
}
setTheme(localStorage.getItem("pmft-theme") ||
  (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
themeBtn.onclick = () =>
  setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");

/* ---- currency ---- */
const currencySelect = $("#currency-select");
currencySelect.innerHTML = CURRENCIES
  .map((c) => `<option value="${c.code}">${esc(c.label)}</option>`).join("");
currencySelect.onchange = async () => {
  state.currency = currencySelect.value;
  render();
  await post("api/settings/currency", { currency: state.currency });
  toast(`Currency set to ${currencySelect.value}`);
};

/* ---- boot ---- */
(async () => {
  try { state.catalog = await api("api/catalog"); } catch { state.catalog = { brands: [] }; }
  fillBrands();
  fillMaterials("PLA+");
  await refresh();
})();
setInterval(() => refresh().catch(() => {}), 8000);
