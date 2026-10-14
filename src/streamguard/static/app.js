"use strict";
const $ = (id) => document.getElementById(id);
let key = "", ws = null, retry = 0;

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  Object.assign(n, props);                       // textContent only: never innerHTML (data is untrusted)
  kids.forEach((k) => n.append(k));
  return n;
}
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-API-Key": key, "Content-Type": "application/json" } });
  if (!r.ok) throw new Error(r.status === 401 ? "Invalid API key" : "HTTP " + r.status);
  return r.status === 204 ? null : r.json();
}
function card(label, value) { return el("div", { className: "card" }, el("b", { textContent: value }), el("small", { textContent: label })); }

async function loadStats() {
  const s = await api("/v1/stats?hours=24");
  const d = s.decisions, flagged = (d.REVIEW || 0) + (d.DECLINE || 0);
  $("cards").replaceChildren(
    card("Scored (24h)", s.total.toLocaleString()), card("Approved", (d.APPROVE || 0).toLocaleString()),
    card("Review", (d.REVIEW || 0).toLocaleString()), card("Declined", (d.DECLINE || 0).toLocaleString()),
    card("Flag rate", s.total ? (100 * flagged / s.total).toFixed(2) + "%" : "–"),
    card("Open alerts", s.open_alerts_total.toLocaleString()),
    card("p95 latency", s.latency_ms.p95 == null ? "–" : s.latency_ms.p95 + " ms"));
}

function row(a, fresh) {
  const why = el("td", { className: "why" });
  a.reasons.slice(0, 4).forEach((r) => why.append(el("div", { textContent: r.description, title: r.code })));
  const box = el("div", { className: "actions" });
  const actions = el("td", {}, box);
  if (a.status === "open") {
    box.append(
      el("button", { className: "sm bad", textContent: "Fraud", onclick: () => disposition(a.id, "confirmed_fraud") }),
      el("button", { className: "sm ok", textContent: "Legit", onclick: () => disposition(a.id, "false_positive") }));
  } else { box.append(el("span", { className: "muted", textContent: a.analyst || "" })); }
  const tr = el("tr", { className: fresh ? "new" : "" },
    el("td", { className: "num", textContent: new Date(a.created_at).toLocaleTimeString() }),
    el("td", {}, el("span", { className: "tag " + a.decision, textContent: a.decision })),
    el("td", { className: "num", textContent: a.risk_score.toFixed(1) }),
    el("td", { className: "num", textContent: a.amount.toLocaleString(undefined, { style: "currency", currency: a.currency }) }),
    el("td", { textContent: a.account_id, title: a.transaction_id }),
    el("td", { textContent: a.merchant_id + " · " + a.country }), why,
    el("td", { textContent: a.status.replace("_", " ") }), actions);
  tr.dataset.id = a.id;
  return tr;
}

async function loadAlerts() {
  const st = $("filter").value;
  const list = await api("/v1/alerts?limit=100" + (st ? "&status=" + st : ""));
  $("rows").replaceChildren(...list.map((a) => row(a, false)));
  $("empty").hidden = list.length > 0;
}
async function disposition(id, status) {
  const analyst = $("analyst").value.trim();
  if (!analyst) { alert("Enter your name first (recorded in the audit trail)."); return; }
  await api("/v1/alerts/" + id, { method: "PATCH", body: JSON.stringify({ status, analyst }) });
  await Promise.all([loadAlerts(), loadStats()]);
}
function connect() {
  if (ws) ws.close();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/alerts?api_key=${encodeURIComponent(key)}`);
  ws.onopen = () => { retry = 0; $("conn").textContent = "live"; $("conn").className = "pill on"; };
  ws.onmessage = (m) => {
    const f = $("filter").value;
    if (f && f !== "open") return;
    $("rows").prepend(row(JSON.parse(m.data), true)); $("empty").hidden = true;
  };
  ws.onclose = () => {
    $("conn").textContent = "disconnected"; $("conn").className = "pill off";
    if (key) setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++));
  };
}
$("keyform").onsubmit = async (e) => {
  e.preventDefault(); key = $("key").value.trim();
  try { await Promise.all([loadStats(), loadAlerts()]); connect(); }
  catch (err) { key = ""; alert(err.message); }
};
$("filter").onchange = () => loadAlerts().catch((e) => alert(e.message));
setInterval(() => key && loadStats().catch(() => {}), 10000);
