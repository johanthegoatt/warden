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
  filter: "all",
  query: "",
  openGroups: new Set(),
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
    const prev = t.probes[t.probes.length - 2];
    const open = ev && ev.kind === "failure";
    // One missed probe with no open failure is the same noise the sensor ignores.
    if (!open && prev && prev.ok) return { cls: "ok", label: "healthy, one probe missed" };
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

function targetRow(t) {
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
      el("b", { text: lastLine.length > 40 ? lastLine.slice(0, 40) + "..." : lastLine, title: lastLine }),
      el("span", { text: `p50 ${p50 === null ? "-" : Math.round(p50)} / ${t.latency_budget_ms} ms` })));
}

function targetTile(t) {
  const st = targetState(t);
  const lastOk = [...t.probes].reverse().find((p) => p.ok);
  const value = !t.probes.length ? "waiting" : st.cls === "ok" && lastOk ? `${Math.round(lastOk.latency_ms)} ms` : st.label;
  return el("li", { class: "tile", title: `${t.name}: ${st.label}, ${t.endpoint}` },
    el("span", { class: `dot ${st.cls}`, "aria-hidden": "true" }),
    el("span", { class: "n", text: t.name }),
    el("span", { class: `v ${st.cls}`, text: value }));
}

function groupOf(t) {
  if (t.kind === "heartbeat") return "Local processes";
  if (t.depends_on && t.depends_on.length) return `Served by ${t.depends_on[0]}`;
  return "Standalone services";
}

function renderTargets(data, forced = false) {
  const attentionWrap = document.getElementById("attention-wrap");
  const attention = document.getElementById("attention");
  const groupsBox = document.getElementById("groups");
  const noMatch = document.getElementById("no-match");
  if (!data.targets.length) {
    attentionWrap.hidden = true;
    groupsBox.replaceChildren(el("p", { class: "empty", text: "No targets yet. Add a [[target]] to warden.toml and restart the sensor." }));
    return;
  }
  const q = ui.query.trim().toLowerCase();
  const matches = (t) => !q || t.name.toLowerCase().includes(q) || t.endpoint.toLowerCase().includes(q);
  const shown = data.targets.filter(matches);
  const bad = shown.filter((t) => targetState(t).cls !== "ok");
  const good = shown.filter((t) => targetState(t).cls === "ok");

  attentionWrap.hidden = ui.filter === "healthy" || !bad.length;
  attention.replaceChildren(...bad.map(targetRow));

  const groups = new Map();
  if (ui.filter !== "attention") for (const t of good) {
    const g = groupOf(t);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(t);
  }
  // A poll must not pull a focused group header out from under the keyboard.
  const keepFocus = !forced && groupsBox.contains(document.activeElement);
  if (!keepFocus) groupsBox.replaceChildren(...[...groups.entries()].sort((a, b) => b[1].length - a[1].length).map(([name, members]) => {
    const det = el("details", { class: "group", open: ui.openGroups.has(name) || Boolean(q) },
      el("summary", {}, el("span", { text: name }), el("span", { class: "count", text: `${members.length} healthy` })),
      el("ol", { class: "tile-grid" }, ...members.map(targetTile)));
    det.addEventListener("toggle", () => {
      if (q) return;
      if (det.open) ui.openGroups.add(name); else ui.openGroups.delete(name);
      store("warden-open-groups", JSON.stringify([...ui.openGroups]));
    });
    return det;
  }));

  const nothing = !shown.length || (ui.filter === "attention" && !bad.length) || (ui.filter === "healthy" && !good.length);
  noMatch.hidden = !nothing;
  if (nothing) {
    noMatch.textContent = q && !shown.length
      ? `No target matches "${ui.query}". Clear the search to see all ${data.targets.length}.`
      : ui.filter === "attention" ? "Nothing needs attention. Every target passed its last probe." : "No healthy targets match this view.";
  }
  document.getElementById("targets-hint").textContent =
    q ? `${shown.length} of ${data.targets.length} match` : `${data.targets.length} targets, problems first`;
}

/* ---------- summary ---------- */
function renderSummary(data) {
  const counts = { ok: 0, fault: 0, drift: 0, probe: 0, "": 0 };
  for (const t of data.targets) counts[targetState(t).cls] += 1;
  const open = data.incidents.filter((i) => !i.closed_at).length;
  const fixed = data.incidents.filter((i) => (i.outcome || "").startsWith("fixed")).length;
  const line = data.targets.length
    ? `${counts.ok} healthy, ${counts.fault} failing, ${counts.drift} drifting, ${counts.probe} with a probe fault. ` +
      `${open} open ${open === 1 ? "incident" : "incidents"}, ${data.approvals.length} waiting on you.`
    : "No targets yet.";
  const node = document.getElementById("summary-line");
  if (node.textContent !== line) node.textContent = line;
  const tile = (label, value, cls) =>
    el("div", { class: value === 0 && cls !== "is-ok" ? "is-zero" : cls }, el("dt", { text: label }), el("dd", { text: String(value) }));
  document.getElementById("tiles").replaceChildren(
    tile("Healthy", counts.ok, "is-ok"),
    tile("Failing", counts.fault, "is-fault"),
    tile("Drifting", counts.drift, "is-drift"),
    tile("Probe faults", counts.probe, "is-probe"),
    tile("Open incidents", open, "is-drift"),
    tile("Fixes confirmed", fixed, "is-ok"));
}

/* ---------- dependency graph ---------- */
// Only targets with an edge are drawn. Leaves that share one dependency
// collapse into a cluster node once there are more than four of them.
const CLUSTER_MIN = 5;
const RANK = { fault: 4, probe: 3, drift: 2, ok: 1, "": 0 };

function renderGraph(data) {
  const box = document.getElementById("graph");
  const note = document.getElementById("graph-note");
  const byName = Object.fromEntries(data.targets.map((t) => [t.name, t]));
  const dependents = {};
  for (const t of data.targets) for (const d of t.depends_on || []) (dependents[d] ||= []).push(t.name);
  const isLeaf = (n) => !(dependents[n] || []).length;

  const nodes = new Map(); // id -> { label, state, title, cluster }
  const edges = [];
  const add = (name) => {
    if (!nodes.has(name) && byName[name]) {
      const st = targetState(byName[name]);
      nodes.set(name, { label: name, state: st.cls, title: `${name}: ${st.label}` });
    }
  };
  for (const [dep, kids] of Object.entries(dependents)) {
    add(dep);
    const leaves = kids.filter((k) => isLeaf(k) && (byName[k].depends_on || []).length === 1);
    const others = kids.filter((k) => !leaves.includes(k));
    if (leaves.length >= CLUSTER_MIN) {
      const id = `cluster:${dep}`;
      const states = leaves.map((k) => targetState(byName[k]));
      const worst = states.reduce((a, b) => (RANK[b.cls] > RANK[a.cls] ? b : a));
      const bad = states.filter((s) => s.cls !== "ok").length;
      nodes.set(id, { label: `${leaves.length} sites`, state: worst.cls, cluster: true,
        title: `${leaves.length} sites served by ${dep}: ${bad ? `${bad} not healthy` : "all healthy"}` });
      edges.push([id, dep]);
    } else {
      for (const k of leaves) { add(k); edges.push([k, dep]); }
    }
    for (const k of others) { add(k); edges.push([k, dep]); }
  }

  const unlinked = data.targets.length - [...nodes.values()].reduce((n, v) => n + (v.cluster ? Number(v.label.split(" ")[0]) : 1), 0);
  note.textContent = nodes.size
    ? `${unlinked} ${unlinked === 1 ? "target has" : "targets have"} no dependencies and ${unlinked === 1 ? "is" : "are"} left out.`
    : "";
  if (!nodes.size) {
    box.replaceChildren(el("p", { class: "empty", text: "No dependencies in warden.toml yet. Add depends_on to a target to see it here." }));
    return;
  }

  const out = Object.fromEntries([...nodes.keys()].map((id) => [id, []]));
  for (const [from, to] of edges) out[from].push(to);
  const depth = {};
  const level = (id, seen = new Set()) => {
    if (depth[id] !== undefined) return depth[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    depth[id] = out[id].length ? 1 + Math.max(...out[id].map((d) => level(d, seen))) : 0;
    return depth[id];
  };
  for (const id of nodes.keys()) level(id);
  const cols = {};
  for (const id of nodes.keys()) (cols[depth[id]] ||= []).push(id);
  const ncol = Object.keys(cols).length;
  const nodeW = 150, nodeH = 28, gapX = 40, gapY = 12;
  const rows = Math.max(...Object.values(cols).map((c) => c.length));
  const W = ncol * nodeW + (ncol - 1) * gapX + 8, H = rows * (nodeH + gapY) + 8;
  const pos = {};
  Object.entries(cols).forEach(([c, ids]) => ids.forEach((id, i) => {
    pos[id] = { x: 4 + (ncol - 1 - Number(c)) * (nodeW + gapX), y: 4 + i * (nodeH + gapY) + ((rows - ids.length) * (nodeH + gapY)) / 2 };
  }));
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
    "aria-label": `Dependency graph: ${edges.length} links between ${nodes.size} nodes` });
  const defs = svg("defs");
  const marker = svg("marker", { id: "arrow", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: "auto" });
  marker.append(svg("path", { d: "M0,0 L8,4 L0,8 z" }));
  defs.append(marker);
  s.append(defs);
  for (const [from, to] of edges) {
    const a = pos[from], b = pos[to];
    const x1 = a.x + nodeW, y1 = a.y + nodeH / 2, x2 = b.x, y2 = b.y + nodeH / 2, mx = (x1 + x2) / 2;
    s.append(svg("path", { class: "edge", d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 2},${y2}`, "marker-end": "url(#arrow)" }));
  }
  for (const [id, n] of nodes) {
    const p = pos[id];
    const g = svg("g", { class: `node ${n.state}${n.cluster ? " cluster" : ""}`, transform: `translate(${p.x},${p.y})` });
    g.append(svg("rect", { width: nodeW, height: nodeH, rx: 2 }));
    const label = svg("text", { x: 8, y: 18 });
    label.textContent = n.label.length > 18 ? n.label.slice(0, 17) + "." : n.label;
    const title = svg("title");
    title.textContent = n.title;
    g.append(label, title);
    s.append(g);
  }
  box.replaceChildren(s);
}

/* ---------- incident ---------- */
async function renderIncident(data) {
  // Checks that closed as healthy are not incidents anyone needs to read.
  const real = data.incidents.filter((i) => i.verdict !== "healthy" || !i.closed_at);
  const list = real.length ? real : data.incidents;
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
      onclick: () => {
        ui.selectedIncident = i.id; ui.pinnedIncident = true; ui.selectedState = null;
        setHash(`incident-${i.id}`);
        renderIncident(ui.data);
      },
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
  setHash(`report-${id}`);
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
    renderSummary(data);
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

/* ---------- filters, deep links, polling ---------- */
function setHash(h) {
  try { history.replaceState(null, "", `#${h}`); } catch (_) {}
}

(function wire() {
  try { ui.openGroups = new Set(JSON.parse(store("warden-open-groups") || "[]")); } catch (_) {}
  const q = document.getElementById("q");
  q.addEventListener("input", () => { ui.query = q.value; if (ui.data) renderTargets(ui.data, true); });
  for (const b of document.querySelectorAll(".filters button")) {
    b.addEventListener("click", () => {
      ui.filter = b.dataset.filter;
      for (const o of document.querySelectorAll(".filters button")) o.setAttribute("aria-pressed", String(o === b));
      if (ui.data) renderTargets(ui.data, true);
    });
  }
  const m = location.hash.match(/^#(incident|report)-(\d+)$/);
  if (m && m[1] === "incident") { ui.selectedIncident = Number(m[2]); ui.pinnedIncident = true; }
  if (m && m[1] === "report") ui.pendingReport = Number(m[2]);
})();

// Hidden tabs stop polling; coming back refreshes at once.
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
refresh().then(() => {
  if (ui.pendingReport) { openReport(ui.pendingReport); ui.pendingReport = null; }
});
