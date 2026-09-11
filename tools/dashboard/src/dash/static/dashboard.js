"use strict";
/* HFT platform dashboard — M3 UI logic (PLAN §4).
 *
 * Vanilla ES2019 only: fetch, EventSource, setInterval. No frameworks,
 * no CDNs, no build step (air-gapped requirement, PLAN §4.3).
 *
 * Security rules (PLAN §4.3):
 *  - every dynamic string from the server lands in the DOM through
 *    textContent / document.createElement — never innerHTML;
 *  - every REST call checks the CONVENTIONS §1.2 envelope: a response
 *    carrying `error` becomes a toast with code + message, never raw
 *    HTML error text.
 *
 * Note on :7710 (config-service): its /changes/watch long-poll holds up
 * to 4 s, so nothing in this UI polls it directly — config changes
 * arrive through the SSE stream (M2, read timeout 5 s), which is the
 * only :7710 traffic the dashboard performs.
 */

/* ------------------------------------------------------------- helpers */

function $(id) {
  return document.getElementById(id);
}

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function fmtNum(v, digits) {
  if (v === null || v === undefined || (typeof v === "number" && !isFinite(v))) return "–";
  var d = digits === undefined ? 4 : digits;
  var n = Number(v);
  if (isFinite(n)) return n.toFixed(d);
  return String(v);
}

function fmtPct(v, digits) {
  if (v === null || v === undefined) return "–";
  var n = Number(v);
  if (!isFinite(n)) return "–";
  return n.toFixed(digits === undefined ? 2 : digits) + "%";
}

function fmtNs(ns) {
  if (!ns || typeof ns !== "number") return "–";
  var d = new Date(ns / 1e6);
  function p(x) { return (x < 10 ? "0" : "") + x; }
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function todayStr() {
  var d = new Date();
  function p(x) { return (x < 10 ? "0" : "") + x; }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}

/* --------------------------------------------------------------- toasts */

function toast(code, message, level) {
  var box = $("toasts");
  if (!box) return;
  var t = el("div", "toast" + (level ? " " + level : ""));
  var c = el("span", "toast-code", code);
  var m = el("span", "toast-msg", message);
  t.appendChild(c);
  t.appendChild(m);
  box.appendChild(t);
  while (box.children.length > 5) box.removeChild(box.firstChild);
  setTimeout(function () {
    if (t.parentNode) t.parentNode.removeChild(t);
  }, 8000);
}

/* ------------------------------------------------------------- API layer */

function ApiError(status, doc) {
  var err = new Error("HTTP " + status);
  err.status = status;
  err.doc = doc || null;
  return err;
}

/* fetch + §1.2 envelope check. Returns the parsed JSON doc on 2xx,
 * otherwise throws ApiError after raising a code+message toast. */
async function api(path, opts) {
  var resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    toast("NET", "cannot reach dashboard: " + (e && e.message ? e.message : e), "warn");
    throw ApiError(0, null);
  }
  var doc = null;
  try { doc = await resp.json(); } catch (e) { /* non-JSON body; keep null */ }
  if (resp.ok) return doc;
  if (doc && doc.error && doc.error.code) {
    toast(doc.error.code, doc.error.message || "", "error");
  } else {
    toast("HTTP " + resp.status, "request to " + path + " failed", "warn");
  }
  throw ApiError(resp.status, doc);
}

function prettyJson(doc) {
  try { return JSON.stringify(doc, null, 2); } catch (e) { return String(doc); }
}

/* ------------------------------------------------------------------ tabs */

var PANEL_IDS = {
  portfolio: "panel-portfolio",
  settlement: "panel-settlement",
  live: "panel-live",
  alerts: "panel-alerts"
};

function wireTabs() {
  var tabs = document.querySelectorAll("#tabnav .tab");
  Array.prototype.forEach.call(tabs, function (btn) {
    btn.addEventListener("click", function () {
      var target = btn.getAttribute("data-tab");
      Array.prototype.forEach.call(document.querySelectorAll("#tabnav .tab"), function (b) {
        var on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
      Object.keys(PANEL_IDS).forEach(function (key) {
        var panel = $(PANEL_IDS[key]);
        if (panel) panel.classList.toggle("active", key === target);
      });
    });
  });
}

/* ------------------------------------------------------------ global state */

var state = {
  readOnly: null,          // from /api/health summary.read_only
  health: null,            // last /api/health doc
  sourceStatus: {},        // src -> "ok" | "degraded" | "unknown"
  streamOpen: false,
  streamEvents: 0,
  es: null                 // EventSource for /live/stream
};

/* Health polling and the rest of the panels are wired in the follow-up
 * sections of this file (portfolio / settlement / live / alerts / footer).
 */

/* ------------------------------------------------------------ health grid */

function tileClass(svc) {
  if (!svc.live) return "red";
  if (svc.ready === "not_ready") return "amber";
  return "ok";
}

function renderHealth(doc) {
  state.health = doc;
  var grid = $("health-grid");
  if (!grid) return;
  while (grid.firstChild) grid.removeChild(grid.firstChild);
  (doc.services || []).forEach(function (svc) {
    var tile = el("div", "tile " + tileClass(svc));
    var dot = el("span", "tile-dot");
    var name = el("span", "tile-name", svc.name + " : " + svc.package);
    name.title = svc.name + " : " + svc.port +
      (svc.error ? " — " + svc.error : "");
    var meta;
    if (!svc.live) {
      meta = "down" + (svc.error ? " · " + svc.error : "");
    } else if (svc.ready === "not_ready") {
      meta = "not ready" +
        (svc.ready_reasons && svc.ready_reasons.length
         ? " · " + svc.ready_reasons[0] : "");
    } else {
      meta = "ok · " + (svc.latency_ms === null || svc.latency_ms === undefined ? "–" : svc.latency_ms) + " ms";
    }
    var metaEl = el("span", "tile-meta", meta);
    tile.appendChild(dot);
    tile.appendChild(name);
    tile.appendChild(metaEl);
    grid.appendChild(tile);
  });
  var sum = doc.summary || {};
  if (typeof sum.read_only === "boolean") setReadOnly(sum.read_only);
  var clock = $("clock");
  if (clock && typeof sum.elapsed_ms === "number") {
    clock.title = "health sweep took " + sum.elapsed_ms + " ms · " +
      sum.live + "/" + sum.checked + " live";
  }
}

function setReadOnly(flag) {
  state.readOnly = flag;
  var badge = $("readonly-badge");
  if (badge) badge.classList.toggle("badge-hidden", !flag);
  applyReadOnlyToSettlement();
}

/* Poll /api/health every 5 s (PLAN §4.2.1). The sweep itself probes all
 * 15 ports in parallel with an 800 ms budget, so 5 s is comfortable. */
async function pollHealth() {
  try {
    var doc = await api("/api/health");
    renderHealth(doc);
  } catch (e) { /* toast already raised by api() */ }
}

/* -------------------------------------------------------------- portfolio */

function pnlRowCells(r) {
  return [
    { v: r.symbol, cls: "" },
    { v: r.account, cls: "" },
    { v: String(r.net_qty), cls: "num" },
    { v: r.side, cls: "" },
    { v: fmtNum(r.avg_price, 2), cls: "num" },
    { v: fmtNum(r.mark, 2), cls: "num" },
    { v: fmtNum(r.market_value, 2), cls: "num" },
    { v: fmtNum(r.realized_pnl, 2), cls: "num " + (r.realized_pnl >= 0 ? "pos" : "neg") },
    { v: fmtNum(r.unrealized_pnl, 2), cls: "num " + (r.unrealized_pnl >= 0 ? "pos" : "neg") },
    { v: fmtNum(r.total_pnl, 2), cls: "num " + (r.total_pnl >= 0 ? "pos" : "neg") }
  ];
}

function fillPnlTable(rows, filter) {
  var tbody = $("pf-pnl-rows");
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  var f = (filter || "").trim().toUpperCase();
  rows.forEach(function (r) {
    if (f && (r.symbol || "").toUpperCase().indexOf(f) === -1) return;
    var tr = el("tr", "clickable");
    tr.title = "click for drilldown: " + r.symbol;
    pnlRowCells(r).forEach(function (c) {
      tr.appendChild(el("td", c.cls, String(c.v)));
    });
    tr.addEventListener("click", function () { drilldownSymbol(r.symbol); });
    tbody.appendChild(tr);
  });
}

async function drilldownSymbol(symbol) {
  var box = $("pf-drilldown");
  var title = $("pf-drilldown-title");
  var body = $("pf-drilldown-body");
  box.hidden = false;
  title.textContent = "drilldown: " + symbol;
  body.textContent = "loading…";
  try {
    var doc = await api("/api/portfolio/pnl/" + encodeURIComponent(symbol));
    body.textContent = prettyJson(doc);
  } catch (e) {
    body.textContent = "(unavailable — see toast)";
  }
}

function renderMetrics(doc) {
  if (!doc || !doc.metrics) return;
  var m = doc.metrics;
  function card(id, text, neg) {
    var node = $(id);
    if (!node) return;
    node.textContent = text;
    node.classList.toggle("neg", !!neg);
    node.classList.toggle("pos", !neg && text !== "–");
  }
  card("pf-sharpe", m.sharpe_ratio === null || m.sharpe_ratio === undefined ? "–" : m.sharpe_ratio.toFixed(3), false);
  card("pf-sortino", m.win_rate_pct === null || m.win_rate_pct === undefined
       ? "–" : "win " + m.win_rate_pct.toFixed(1) + "%", false);
  card("pf-maxdd", fmtPct(m.max_drawdown_pct, 2), m.max_drawdown_pct < 0);
  card("pf-curdd", fmtPct(m.current_drawdown_pct, 2), m.current_drawdown_pct < 0);
}

function renderTotals(doc) {
  if (!doc || !doc.totals) return;
  var t = doc.totals;
  function card(id, text, neg) {
    var node = $(id);
    if (!node) return;
    node.textContent = fmtNum(text, 2);
    node.classList.toggle("neg", !!neg);
    node.classList.toggle("pos", !neg);
  }
  card("pf-total-pnl", t.total_pnl, t.total_pnl < 0);
  card("pf-realized", t.realized_pnl, t.realized_pnl < 0);
  card("pf-unrealized", t.unrealized_pnl, t.unrealized_pnl < 0);
  card("pf-mv", t.market_value, false);
}

function renderVar(doc) {
  var wrap = $("pf-var");
  while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
  if (!doc || !doc.var) { wrap.appendChild(el("div", "card-k", "–")); return; }
  var v = doc.var;
  var head = el("div", "card-k",
    "VaR (" + v.method + ", " + fmtPct(v.confidence * 100, 0) + ", n=" + v.samples + ")");
  wrap.appendChild(head);
  if (v.insufficient_data) {
    wrap.appendChild(el("div", "var-note",
      doc.note || "insufficient return history"));
    return;
  }
  var line = el("div", "var-line");
  line.appendChild(el("span", "", "VaR " + fmtNum(v.var, 6)));
  var track = el("div", "var-bar-track");
  var bar = el("div", "var-bar");
  // Scale the bar against CVaR (the wider tail) when present.
  var scale = v.cvar !== null && v.cvar > 0 ? v.cvar : (v.var || 0);
  var w = scale > 0 && v.var !== null ? Math.max(2, Math.min(100, v.var / scale * 100)) : 0;
  bar.style.width = w + "%";
  track.appendChild(bar);
  line.appendChild(track);
  line.appendChild(el("span", "", v.cvar === null ? "" : "CVaR " + fmtNum(v.cvar, 6)));
  wrap.appendChild(line);
}

function renderAttribution(doc) {
  var list = $("pf-attribution");
  while (list.firstChild) list.removeChild(list.firstChild);
  if (!doc || !doc.symbols) { list.appendChild(el("div", "card-k", "–")); return; }
  var maxAbs = 0;
  doc.symbols.forEach(function (a) {
    maxAbs = Math.max(maxAbs, Math.abs(a.total));
  });
  doc.symbols.forEach(function (a) {
    var row = el("div", "attr-row");
    row.appendChild(el("span", "attr-sym", a.symbol));
    var track = el("div", "attr-bar-track");
    var bar = el("div", "attr-bar " + (a.total >= 0 ? "pos" : "neg"));
    var w = maxAbs > 0 ? Math.max(1, Math.min(100, Math.abs(a.total) / maxAbs * 100)) : 0;
    bar.style.width = w + "%";
    track.appendChild(bar);
    row.appendChild(track);
    row.appendChild(el("span", "attr-val " + (a.total >= 0 ? "pos" : "neg"),
      fmtNum(a.total, 2) + " (" + fmtPct(a.weight_pct, 1) + ")"));
    list.appendChild(row);
  });
}

function drawSparkline(samples) {
  var canvas = $("pf-spark");
  if (!canvas || !samples || !samples.length) return;
  var ctx = canvas.getContext("2d");
  var W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  // /portfolio/history is newest-first; plot oldest→newest left→right.
  var pts = samples.slice().reverse();
  var eq = pts.map(function (p) { return p.equity; });
  var lo = Math.min.apply(null, eq);
  var hi = Math.max.apply(null, eq);
  var span = hi - lo;
  if (span === 0) span = 1;
  ctx.strokeStyle = "#58a6ff";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  pts.forEach(function (p, i) {
    var x = W * (i / Math.max(1, pts.length - 1));
    var y = H - 8 - ((p.equity - lo) / span) * (H - 16);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.strokeStyle = "#21262d";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, H - 8); ctx.lineTo(W, H - 8); ctx.stroke();
  ctx.fillStyle = "#8b949e";
  ctx.font = "10px monospace";
  ctx.fillText("hi " + hi.toFixed(2), 4, 12);
  ctx.fillText("lo " + lo.toFixed(2), 4, H - 12);
}

async function refreshPortfolio() {
  var filter = $("pf-symbol-filter");
  try {
    var pnlDoc = await api("/api/portfolio/pnl");
    renderTotals(pnlDoc);
    fillPnlTable(pnlDoc.symbols || [], filter ? filter.value : "");
  } catch (e) { /* toasted */ }
  try {
    var mDoc = await api("/api/portfolio/metrics");
    renderMetrics(mDoc);
  } catch (e) { /* toasted */ }
  try {
    var vDoc = await api("/api/portfolio/var");
    renderVar(vDoc);
  } catch (e) { /* toasted */ }
  try {
    var aDoc = await api("/api/portfolio/attribution");
    renderAttribution(aDoc);
  } catch (e) { /* toasted */ }
  try {
    var hDoc = await api("/api/portfolio/history?limit=200");
    drawSparkline(hDoc.series || []);
  } catch (e) { /* toasted */ }
}

/* ------------------------------------------------------------- settlement */

function applyReadOnlyToSettlement() {
  var form = $("stl-settle-form");
  var fin = $("stl-finalize");
  if (!form || !fin) return;
  if (state.readOnly) {
    form.classList.add("disabled");
    form.querySelectorAll("input, select, button").forEach(function (n) {
      n.disabled = true;
    });
    fin.disabled = true;
  } else {
    form.classList.remove("disabled");
    form.querySelectorAll("input, select, button").forEach(function (n) {
      n.disabled = false;
    });
    fin.disabled = false;
  }
}

function renderRun(doc) {
  var box = $("stl-run");
  while (box.firstChild) box.removeChild(box.firstChild);
  if (!doc) { box.appendChild(el("div", "card-k", "–")); return; }
  box.appendChild(el("pre", "json-pre", prettyJson(doc)));
}

function renderReport(doc) {
  var box = $("stl-report");
  while (box.firstChild) box.removeChild(box.firstChild);
  if (!doc) { box.appendChild(el("div", "card-k", "–")); return; }
  box.appendChild(el("pre", "json-pre", prettyJson(doc)));
}

// Wire fields of one stls Discrepancy.to_dict() (CONVENTIONS wire format).
var DISC_COLS = ["date", "venue", "symbol", "kind",
                 "severity", "our_qty", "stmt_qty", "qty_delta"];

function renderDiscrepancies(doc) {
  var tbody = $("stl-disc-rows");
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  var rows = doc && doc.discrepancies ? doc.discrepancies : [];
  rows.forEach(function (d) {
    var tr = el("tr", "");
    DISC_COLS.forEach(function (k) {
      var v = d[k];
      var cls = "num";
      var txt = (v === null || v === undefined) ? "–" : String(v);
      if (k === "severity") {
        cls = v === "CRITICAL" ? "neg" : "";
      } else if (k === "qty_delta") {
        var n = Number(v);
        if (isFinite(n) && n !== 0) cls = "num " + (n > 0 ? "pos" : "neg");
      }
      tr.appendChild(el("td", cls, txt));
    });
    tbody.appendChild(tr);
  });
  if (!rows.length) {
    var tr = el("tr", "");
    tr.appendChild(el("td", "muted", "no discrepancies"));
    tbody.appendChild(tr);
  }
}

function settleDate() {
  var node = $("stl-date");
  return node && node.value ? node.value : todayStr();
}

async function refreshSettlement() {
  var date = settleDate();
  try {
    var runDoc = await api("/api/settlement/runs/" + encodeURIComponent(date));
    renderRun(runDoc);
  } catch (e) { /* toasted (e.g. STL-204 unknown date) */ }
  try {
    var repDoc = await api("/api/settlement/reports/" + encodeURIComponent(date));
    renderReport(repDoc);
  } catch (e) { /* toasted */ }
  try {
    var discDoc = await api("/api/settlement/discrepancies?limit=50");
    renderDiscrepancies(discDoc);
  } catch (e) { /* toasted */ }
}

function wireSettlement() {
  var dateInput = $("stl-date");
  if (dateInput) dateInput.value = todayStr();

  $("stl-refresh").addEventListener("click", refreshSettlement);

  $("stl-finalize").addEventListener("click", async function () {
    var date = settleDate();
    try {
      var doc = await api("/api/settlement/finalize/" + encodeURIComponent(date),
        { method: "POST" });
      $("stl-settle-result").hidden = false;
      $("stl-settle-result").textContent = "finalize " + date + ":\n" + prettyJson(doc);
      refreshSettlement();
    } catch (e) { /* toasted; may be UI-205 in read-only mode */ }
  });

  $("stl-settle-form").addEventListener("submit", async function (ev) {
    ev.preventDefault();
    var body = {
      date: $("stl-settle-date").value || todayStr(),
      fills: [{
        fill_id: "dash-" + Date.now(),
        ts_ns: Date.now() * 1e6,
        venue: $("stl-settle-venue").value,
        symbol: $("stl-settle-symbol").value,
        side: $("stl-settle-side").value,
        px: Number($("stl-settle-px").value),
        qty: Number($("stl-settle-qty").value)
      }],
      statement_lines: []
    };
    try {
      var doc = await api("/api/settlement/settle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      $("stl-settle-result").hidden = false;
      $("stl-settle-result").textContent = "settle:\n" + prettyJson(doc);
      refreshSettlement();
    } catch (e) { /* toasted; UI-205 in read-only, STL-2xx upstream */ }
  });
}

/* --------------------------------------------------------------- live SSE */

var ALL_SOURCES = ["mdg", "obb", "latmon", "altsvc", "cfgs", "audl"];
var MAX_FEED_ROWS = 400;

function ensureChips() {
  var row = $("live-sources");
  while (row.firstChild) row.removeChild(row.firstChild);
  ALL_SOURCES.forEach(function (src) {
    var cls = "chip";
    var st = state.sourceStatus[src];
    if (st === "ok") cls += " ok";
    else if (st === "degraded") cls += " degraded";
    row.appendChild(el("span", cls, src));
  });
}

function setSourceStatus(src, status) {
  state.sourceStatus[src] = status;
  ensureChips();
  var badge = $("live-state");
  if (badge) {
    var anyDegraded = ALL_SOURCES.some(function (s) {
      return state.sourceStatus[s] === "degraded";
    });
    badge.textContent = anyDegraded ? "degraded" : "streaming";
    badge.classList.toggle("bad", anyDegraded);
    badge.classList.toggle("ok", !anyDegraded);
  }
}

function pushFeed(evt) {
  var feed = $("live-feed");
  var li = el("li", "src-" + (evt.src || "unknown"));
  li.appendChild(el("span", "ts", fmtNs(evt.ts)));
  li.appendChild(el("span", "src", evt.src || "?"));
  li.appendChild(el("span", "kind", evt.kind || "?"));
  var bodyText = (evt.kind === "degraded" || evt.kind === "recovered")
    ? JSON.stringify(evt.data || {})
    : JSON.stringify(evt.data === undefined ? evt : evt.data);
  var body = el("span", "body", bodyText);
  body.title = bodyText;
  li.appendChild(body);
  feed.appendChild(li);
  while (feed.children.length > MAX_FEED_ROWS) {
    feed.removeChild(feed.firstChild);
  }
  // Auto-scroll: only if the user is already at the bottom.
  var wrap = feed.parentNode;
  if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 24) {
    wrap.scrollTop = wrap.scrollHeight;
  }
  var count = $("live-count");
  if (count) count.textContent = (state.streamEvents) + " events";
}

function pushAlert(evt) {
  pushEventList("alerts-list", "alert", evt);
}

function pushAudit(evt) {
  pushEventList("audit-list", "audit-event", evt);
}

function pushEventList(listId, kind, evt) {
  var list = $(listId);
  var empty = list.querySelector(".empty");
  if (empty) list.removeChild(empty);
  var li = el("li", "src-" + (evt.src || "unknown"));
  var sev = evt.data && evt.data.severity;
  if (sev === "CRITICAL") li.className += " sev-critical";
  else if (sev === "WARNING") li.className += " sev-warning";
  li.appendChild(el("span", "ev-ts", fmtNs(evt.ts)));
  li.appendChild(el("span", "ev-kind", kind));
  var text = JSON.stringify(evt.data === undefined ? evt : evt.data);
  var body = el("span", "ev-body", text);
  body.title = text;
  li.appendChild(body);
  list.insertBefore(li, list.firstChild); // newest first
  while (list.children.length > 200) list.removeChild(list.lastChild);
}

function handleStreamEvent(evt) {
  if (!evt || !evt.src) return;
  state.streamEvents += 1;
  var kind = evt.kind;
  if (kind === "hello") {
    (evt.data && evt.data.subscribed ? evt.data.subscribed : []).forEach(function (s) {
      setSourceStatus(s, "ok");
    });
    ALL_SOURCES.forEach(function (s) {
      if (!state.sourceStatus[s]) state.sourceStatus[s] = "unknown";
    });
    ensureChips();
    return;
  }
  if (kind === "degraded") { setSourceStatus(evt.src, "degraded"); }
  if (kind === "recovered") { setSourceStatus(evt.src, "ok"); }
  if (kind === "keepalive") return; // cadence only; not shown in the feed
  if (kind === "alert") { pushAlert(evt); return; }
  if (kind === "audit-event") { pushAudit(evt); return; }
  pushFeed(evt);
}

function openStream() {
  if (state.es) return; // already connected
  if (typeof EventSource === "undefined") {
    var badge = $("live-state");
    if (badge) badge.textContent = "EventSource unsupported";
    return;
  }
  var es = new EventSource("/live/stream");
  state.es = es;
  es.onopen = function () {
    state.streamOpen = true;
    var badge = $("live-state");
    if (badge) { badge.textContent = "streaming"; badge.classList.add("ok"); badge.classList.remove("bad"); }
  };
  es.onmessage = function (msg) {
    try {
      var evt = JSON.parse(msg.data);
      handleStreamEvent(evt);
    } catch (e) { /* malformed frame: ignore */ }
  };
  es.onerror = function () {
    // EventSource auto-reconnects; surface the gap in the badge.
    state.streamOpen = false;
    var badge = $("live-state");
    if (badge) { badge.textContent = "reconnecting…"; badge.classList.add("bad"); badge.classList.remove("ok"); }
  };
}

/* ------------------------------------------------------- alerts & audit */

function ensureEmptyPlaceholders() {
  [["alerts-list", "no alerts yet (via the M2 stream)"],
   ["audit-list", "no audit events yet (via the M2 stream)"]].forEach(function (pair) {
    var list = $(pair[0]);
    if (list && !list.children.length) {
      list.appendChild(el("li", "empty", pair[1]));
    }
  });
}

/* ------------------------------------------------------- gateway footer */

async function pollGatewayStats() {
  try {
    var doc = await api("/api/gateway-stats");
    var s = doc.stats || {};
    var d = doc.dashboard || {};
    var txt = "req " + (s.requests_total !== undefined ? s.requests_total : "–") +
      " · proxied " + (s.proxied !== undefined ? s.proxied : "–") +
      " · 404 " + (s.route_404 !== undefined ? s.route_404 : "–") +
      " · 405 " + (s.route_405 !== undefined ? s.route_405 : "–") +
      " · auth↓ " + (s.auth_upstream_errors !== undefined ? s.auth_upstream_errors : "–") +
      " · tokens " + (d.auth && d.auth.tokens_issued !== undefined ? d.auth.tokens_issued : "–") +
      (d.read_only ? " · READ-ONLY" : "");
    var node = $("gw-stats");
    if (node) {
      node.textContent = txt;
      node.classList.remove("muted");
    }
  } catch (e) { /* toasted */ }
}

/* --------------------------------------------------------------- boot */

function tickClock() {
  var node = $("clock");
  if (!node) return;
  var d = new Date();
  function p(x) { return (x < 10 ? "0" : "") + x; }
  node.textContent = p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function boot() {
  wireTabs();
  wireSettlement();
  ensureChips();
  ensureEmptyPlaceholders();
  tickClock();
  setInterval(tickClock, 1000);

  // Initial load, then the steady-state polls (PLAN §4.2 cadences:
  // health 5 s, gateway 10 s).
  pollHealth();
  refreshPortfolio();
  refreshSettlement();
  pollGatewayStats();
  setInterval(pollHealth, 5000);
  setInterval(pollGatewayStats, 10000);

  $("pf-refresh").addEventListener("click", refreshPortfolio);
  $("pf-symbol-filter").addEventListener("input", function () {
    if (state.health) { /* re-render from the last pnl doc is not kept;
        simplest correct behaviour: refetch */ refreshPortfolio(); }
  });
  $("pf-drilldown-close").addEventListener("click", function () {
    $("pf-drilldown").hidden = true;
  });

  openStream();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
