"use strict";
// Everything from the API is rendered with textContent or SVG attributes,
// never innerHTML: report text and probe errors come from the network.

const FLOW = ["detect", "verify", "diagnose", "decide", "act", "report"];
const POLL_MS = 5000;
const SVG = "http://www.w3.org/2000/svg";

const ui = {
  selectedIncident: null,
  selectedState: null,
  selectedReport: null,
  lastOk: null,
  data: null,
};

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

function svg(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (_) { return null; }
}

function ago(ts, now) {
  if (!ts) return "never";
  const s = Math.max(0, (Date.parse(now) - Date.parse(ts)) / 1000);
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function clock(ts) { return ts ? ts.slice(11, 19) : ""; }

/* ---------- theme ---------- */
(function theme() {
  const saved = store("warden-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  document.getElementById("theme").addEventListener("click", () => {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const current = document.documentElement.dataset.theme || (dark ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store("warden-theme", next);
  });
})();

/* ---------- target state ---------- */
function targetState(t) {
  const last = t.probes[t.probes.length - 1];
  const ev = t.last_event;
  if (!last) return { cls: "", label: "no reading" };
  if (!last.ok) {
    if (ev && ev.kind === "failure" && ev.fault === "probe") return { cls: "probe", label: "probe fault" };
    if (ev && ev.kind === "failure" && ev.fault === "dependency") return { cls: "fault", label: `via ${ev.culprit}` };
    return { cls: "fault", label: "failing" };
  }
  if (ev && ev.kind === "drift" && Date.parse(last.ts) - Date.parse(ev.ts) < 10 * 60 * 1000) return { cls: "drift", label: "drifting" };
  return { cls: "ok", label: "healthy" };
}

function sparkline(t) {
  const w = 240, h = 36, pad = 3;
  const vals = t.probes.map((p) => (p.ok ? p.latency_ms : null));
  const max = Math.max(t.latency_budget_ms * 1.15, ...vals.filter((v) => v !== null), 1);
  const x = (i) => pad + (i * (w - 2 * pad)) / Math.max(1, vals.length - 1);
  const y = (v) => h - pad - (v / max) * (h - 2 * pad);
  const s = svg("svg", { viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none", class: "spark", role: "img",
    "aria-label": `${t.name} latency, last ${vals.length} probes, budget ${t.latency_budget_ms} ms` });
  s.append(svg("line", { class: "budget", x1: 0, x2: w, y1: y(t.latency_budget_ms), y2: y(t.latency_budget_ms) }));
  let d = "";
  vals.forEach((v, i) => {
    if (v === null) { s.append(svg("rect", { class: "miss", x: x(i) - 1.5, y: h - pad - 6, width: 3, height: 6 })); d += " "; return; }
    d += `${d.endsWith(" ") || !d ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
  });
  s.append(svg("path", { class: "line", d: d.trim().replace(/ +/g, " ") }));
  return s;
}

function renderTargets(data) {
  const list = document.getElementById("targets");
  if (!data.targets.length) {
    list.replaceChildren(el("li", { class: "empty", text: "No targets yet. Add a [[target]] to warden.toml and restart the sensor." }));
    return;
  }
  list.replaceChildren(...data.targets.map((t) => {
    const st = targetState(t);
    const last = t.probes[t.probes.length - 1];
    const okLat = t.probes.filter((p) => p.ok && p.latency_ms !== null).map((p) => p.latency_ms).sort((a, b) => a - b);
    const p50 = okLat.length ? okLat[Math.floor(okLat.length / 2)] : null;
    const kindLine = t.kind === "heartbeat" ? `heartbeat file ${t.endpoint}` : t.endpoint;
    const lastLine = last ? (last.ok ? `${Math.round(last.latency_ms)} ms` : (last.error || `status ${last.status}`)) : "waiting";
    return el("li", { class: "target" },
      el("span", { class: `dot ${st.cls}`, "aria-hidden": "true" }),
      el("div", {},
        el("div", { class: "tname", text: t.name }),
        el("div", { class: "tsub", text: kindLine }),
        el("div", { class: `state ${st.cls}`, text: st.label })),
      el("div", { class: "spark-cell" }, t.probes.length ? sparkline(t) : el("span", { class: "tsub", text: "no probes yet" })),
      el("div", { class: "tnum" },
        el("b", { text: lastLine.length > 22 ? lastLine.slice(0, 22) + "..." : lastLine, title: lastLine }),
        el("span", { text: `p50 ${p50 === null ? "-" : Math.round(p50)} / ${t.latency_budget_ms} ms` })));
  }));
}

/* ---------- dependency graph ---------- */
function renderGraph(data) {
  const box = document.getElementById("graph");
  const byName = Object.fromEntries(data.targets.map((t) => [t.name, t]));
  const depth = {};
  const level = (n, seen = new Set()) => {
    if (depth[n] !== undefined) return depth[n];
    if (seen.has(n)) return 0;
    seen.add(n);
    const deps = (byName[n] && byName[n].depends_on) || [];
    depth[n] = deps.length ? 1 + Math.max(...deps.map((d) => level(d, seen))) : 0;
    return depth[n];
  };
  data.targets.forEach((t) => level(t.name));
  const cols = {};
  for (const t of data.targets) (cols[depth[t.name]] ||= []).push(t.name);
  const ncol = Object.keys(cols).length || 1;
  const nodeW = 128, nodeH = 26, gapX = 44, gapY = 14;
  const rows = Math.max(1, ...Object.values(cols).map((c) => c.length));
  const W = ncol * nodeW + (ncol - 1) * gapX + 8, H = rows * (nodeH + gapY) + 8;
  const pos = {};
  Object.entries(cols).forEach(([c, names]) => names.forEach((n, i) => {
    pos[n] = { x: 4 + (ncol - 1 - Number(c)) * (nodeW + gapX), y: 4 + i * (nodeH + gapY) + ((rows - names.length) * (nodeH + gapY)) / 2 };
  }));
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": "Dependency graph of watched targets" });
  const defs = svg("defs");
  const marker = svg("marker", { id: "arrow", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: "auto" });
  marker.append(svg("path", { d: "M0,0 L8,4 L0,8 z" }));
  defs.append(marker);
  s.append(defs);
  for (const t of data.targets) for (const d of t.depends_on || []) {
    if (!pos[d]) continue;
    const a = pos[t.name], b = pos[d];
    const x1 = a.x + nodeW, y1 = a.y + nodeH / 2, x2 = b.x, y2 = b.y + nodeH / 2;
    const mx = (x1 + x2) / 2;
    s.append(svg("path", { class: "edge", d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 2},${y2}`, "marker-end": "url(#arrow)" }));
  }
  for (const t of data.targets) {
    const p = pos[t.name];
    const g = svg("g", { class: `node ${targetState(t).cls}`, transform: `translate(${p.x},${p.y})` });
    g.append(svg("rect", { width: nodeW, height: nodeH, rx: 2 }));
    const label = svg("text", { x: 8, y: 17 });
    label.textContent = t.name.length > 17 ? t.name.slice(0, 16) + "." : t.name;
    const title = svg("title");
    title.textContent = `${t.name}: ${targetState(t).label}`;
    g.append(label, title);
    s.append(g);
  }
  box.replaceChildren(data.targets.length ? s : el("p", { class: "empty", text: "No targets to draw." }));
}

/* ---------- incident ---------- */
async function renderIncident(data) {
  const list = data.incidents;
  const rail = document.getElementById("rail");
  const step = document.getElementById("step");
  const hint = document.getElementById("incident-hint");
  if (!list.length) { rail.replaceChildren(); return; }
  const open = list.find((i) => !i.closed_at);
  // Follow the newest incident until someone picks one by hand.
  if (!ui.pinnedIncident || !list.some((i) => i.id === ui.selectedIncident)) {
    const newest = (open || list[0]).id;
    if (newest !== ui.selectedIncident) ui.selectedState = null;
    ui.selectedIncident = newest;
  }
  let detail;
  try {
    detail = await getJSON(`/api/incidents/${ui.selectedIncident}`);
  } catch (e) {
    step.replaceChildren(el("p", { class: "empty", text: `Could not load incident ${ui.selectedIncident}: ${e.message}` }));
    return;
  }
  const inc = detail.incident;
  const key = [inc.id, inc.state, detail.transitions.length, ui.selectedState, list.map((i) => i.id).join(",")].join("|");
  if (key === ui.incidentKey) return;
  ui.incidentKey = key;
  const seen = {};
  for (const t of detail.transitions) seen[t.state] = t;
  hint.textContent = `#${inc.id} ${inc.target}, ${inc.state === "closed" ? "closed" : inc.state.replace("_", " ")}${inc.verdict ? `, verdict ${inc.verdict.replace("_", " ")}` : ""}`;
  const current = inc.state === "closed" ? null : (inc.state === "awaiting_approval" ? "act" : inc.state);
  if (!ui.selectedState || !seen[ui.selectedState]) ui.selectedState = detail.transitions.length ? detail.transitions[detail.transitions.length - 1].state : null;
  rail.replaceChildren(...FLOW.map((name, i) => el("li", {},
    el("button", {
      type: "button", disabled: !seen[name], "data-done": seen[name] ? "true" : "false",
      "data-current": current === name ? "true" : "false", "aria-pressed": ui.selectedState === name ? "true" : "false",
      onclick: () => { ui.selectedState = name; renderIncident(ui.data); },
    }, el("span", { class: "n", text: `0${i + 1}` }), name))));

  const picker = el("div", { class: "picker", role: "group", "aria-label": "Recent incidents" },
    ...list.slice(0, 8).map((i) => el("button", {
      type: "button", "aria-pressed": i.id === ui.selectedIncident ? "true" : "false",
      onclick: () => { ui.selectedIncident = i.id; ui.pinnedIncident = true; ui.selectedState = null; renderIncident(ui.data); },
      text: `#${i.id} ${i.target}`,
    })));

  const t = seen[ui.selectedState];
  if (!t) { step.replaceChildren(picker, el("p", { class: "empty", text: "This incident has not logged a step yet." })); return; }
  const pre = (obj) => el("pre", { text: JSON.stringify(obj, null, 2) });
  step.replaceChildren(picker,
    el("h3", { text: `${t.state} at ${clock(t.ts)} UTC, ${t.duration_ms.toFixed(0)} ms` }),
    el("p", { text: (t.output && t.output.summary) || "" }),
    el("div", { class: "io" },
      el("details", {}, el("summary", { text: "Input" }), pre(t.input)),
      el("details", { open: true }, el("summary", { text: "Output" }), pre(t.output))));
}

/* ---------- approvals ---------- */
function renderApprovals(data) {
  const list = document.getElementById("approvals");
  // Rebuilding on every poll would pull a button out from under a click or keyboard focus.
  const key = data.approvals.map((a) => a.id).join(",") || "none";
  if (key === ui.approvalsKey) return;
  ui.approvalsKey = key;
  if (!data.approvals.length) {
    list.replaceChildren(el("li", { class: "empty", text: "Nothing to approve. Restarts of services that may hold state wait here." }));
    return;
  }
  list.replaceChildren(...data.approvals.map((a) => {
    const status = el("p", { class: "hint", role: "status" });
    const send = async (decision, btn) => {
      btn.disabled = true;
      status.textContent = "";
      try {
        await decide(a.id, decision);
        status.textContent = decision === "approve" ? "Approved. The brain runs it on its next pass." : "Denied. Nothing will run.";
        refresh();
      } catch (e) {
        status.textContent = e.message;
        btn.disabled = false;
      }
    };
    const approve = el("button", { class: "act primary", type: "button", text: "Approve", onclick: (e) => send("approve", e.currentTarget) });
    const deny = el("button", { class: "act deny", type: "button", text: "Deny", onclick: (e) => send("deny", e.currentTarget) });
    return el("li", { class: "approval" },
      el("p", { text: a.action }),
      el("p", { class: "hint", text: `Incident #${a.incident_id}, asked ${ago(a.requested_at, data.now)}` }),
      el("div", { class: "row" }, approve, deny), status);
  }));
}

async function decide(id, decision) {
  const headers = { "X-Warden-Request": "1", "Content-Type": "text/plain" };
  const token = sessionToken();
  if (token) headers["X-Warden-Token"] = token;
  const res = await fetch(`/api/approvals/${id}`, { method: "POST", headers, body: decision });
  if (res.status === 403) {
    const t = prompt("This Warden needs the approval token (WARDEN_TOKEN). Paste it to continue.");
    if (t) { try { sessionStorage.setItem("warden-token", t); } catch (_) {} return decide(id, decision); }
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `The sensor answered ${res.status}.`);
  }
}

function sessionToken() { try { return sessionStorage.getItem("warden-token"); } catch (_) { return null; } }

/* ---------- reports ---------- */
function renderReports(data) {
  const withReports = data.incidents.filter((i) => i.has_report);
  const list = document.getElementById("report-list");
  const tokens = data.incidents.reduce((s, i) => s + (i.cost.tokens_in || 0) + (i.cost.tokens_out || 0), 0);
  const wall = data.incidents.reduce((s, i) => s + (i.cost.wall_ms || 0), 0);
  document.getElementById("cost-line").textContent =
    `${data.incidents.length} incidents cost ${tokens} model tokens and ${(wall / 1000).toFixed(1)} s of brain time`;
  if (!withReports.length) {
    list.replaceChildren(el("li", { class: "empty", text: "No reports yet. Healthy checks write none; incidents that reach the report step do." }));
    return;
  }
  list.replaceChildren(...withReports.map((i) => el("li", {},
    el("button", {
      type: "button", "aria-current": i.id === ui.selectedReport ? "true" : "false",
      onclick: () => openReport(i.id),
    },
    el("span", { class: "rt", text: `#${i.id} ${i.target}` }),
    el("span", { class: "rm", text: `${(i.verdict || "").replace("_", " ")}, ${i.outcome || ""}` })))));
}

async function openReport(id) {
  ui.selectedReport = id;
  renderReports(ui.data);
  const reader = document.getElementById("reader");
  try {
    const res = await fetch(`/api/reports/${id}`);
    if (!res.ok) throw new Error(`The sensor answered ${res.status}.`);
    reader.replaceChildren(...markdown(await res.text()));
    reader.focus({ preventScroll: true });
  } catch (e) {
    reader.replaceChildren(el("p", { class: "empty", text: `Could not load report ${id}: ${e.message}` }));
  }
}

// The subset of markdown the brain writes: headings, paragraphs, lists, tables, `code`.
function markdown(src) {
  const out = [];
  const lines = src.replace(/\r/g, "").split("\n");
  const inline = (text) => {
    const span = document.createDocumentFragment();
    text.split(/(`[^`]+`)/).forEach((part) => {
      if (part.startsWith("`") && part.endsWith("`") && part.length > 1) span.append(el("code", { text: part.slice(1, -1) }));
      else span.append(document.createTextNode(part.replace(/\\\|/g, "|")));
    });
    return span;
  };
  const cells = (row) => row.replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim());
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    let m;
    if ((m = line.match(/^(#{1,3}) (.*)$/))) { out.push(el(`h${m[1].length}`, {}, inline(m[2]))); continue; }
    if (line.startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      i--;
      const table = el("table");
      const body = rows.filter((r) => !/^\|[-| ]+\|$/.test(r));
      body.forEach((r, j) => {
        const tr = el("tr");
        cells(r).forEach((c) => tr.append(el(j === 0 ? "th" : "td", {}, inline(c))));
        table.append(tr);
      });
      out.push(el("div", { class: "table-wrap" }, table));
      continue;
    }
    if (/^(- |\d+\. )/.test(line)) {
      const ordered = /^\d+\. /.test(line);
      const listEl = el(ordered ? "ol" : "ul");
      while (i < lines.length && /^(- |\d+\. )/.test(lines[i])) listEl.append(el("li", {}, inline(lines[i++].replace(/^(- |\d+\. )/, ""))));
      i--;
      out.push(listEl);
      continue;
    }
    out.push(el("p", {}, inline(line)));
  }
  return out;
}

/* ---------- log ---------- */
function renderLog(data) {
  const items = [
    ...data.events.map((e) => ({ ts: e.ts, cls: `k-${e.kind}`,
      text: e.kind === "failure" ? `${e.target} failure, fault at ${e.fault}${e.culprit && e.culprit !== e.target ? ` (${e.culprit})` : ""}`
        : e.kind === "drift" ? `${e.target} latency drift, z ${e.score}` : `${e.target} recovered` })),
    ...data.restarts.map((r) => ({ ts: r.ts, cls: "k-restart", text: `supervisor restarted ${r.task} (${r.restarts}): ${r.reason}` })),
  ].sort((a, b) => (a.ts < b.ts ? 1 : -1)).slice(0, 30);
  const list = document.getElementById("log");
  if (!items.length) { list.replaceChildren(el("li", {}, el("span"), el("span", { class: "hint", text: "No events. Every probe so far matched its expected answer." }))); return; }
  list.replaceChildren(...items.map((it) => el("li", {}, el("time", { datetime: it.ts, text: clock(it.ts) }), el("span", { class: it.cls, text: it.text }))));
}

/* ---------- header ---------- */
function renderBar(data) {
  const hb = Object.fromEntries(data.heartbeats.map((h) => [h.component, h]));
  const brain = hb.brain;
  const mode = document.getElementById("mode");
  const live = brain && brain.note && brain.note.mode === "live";
  mode.replaceChildren(live ? el("span", { class: "chip-live", text: "LIVE" }) : document.createTextNode(brain ? "dry run" : "brain not started"));
  document.getElementById("sensor-age").textContent = hb.sensor ? ago(hb.sensor.ts, data.now) : "never";
  document.getElementById("brain-age").textContent = brain ? ago(brain.ts, data.now) : "never";
  document.getElementById("model").textContent = (brain && brain.note && brain.note.model) || "none";
}

/* ---------- loop ---------- */
async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${url} answered ${res.status}`);
  return res.json();
}

async function refresh() {
  const banner = document.getElementById("stale");
  try {
    const data = await getJSON("/api/overview");
    ui.data = data;
    ui.lastOk = Date.now();
    banner.hidden = true;
    renderBar(data);
    renderTargets(data);
    renderGraph(data);
    renderApprovals(data);
    renderReports(data);
    renderLog(data);
    await renderIncident(data);
  } catch (e) {
    banner.hidden = false;
    banner.textContent = ui.lastOk
      ? `The sensor stopped answering ${Math.round((Date.now() - ui.lastOk) / 1000)} s ago. Showing the last reading. (${e.message})`
      : `Cannot reach the sensor's API: ${e.message}. Start it with warden-sensor run.`;
  }
}

refresh();
setInterval(refresh, POLL_MS);
