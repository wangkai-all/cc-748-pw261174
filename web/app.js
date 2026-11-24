/* Schedule risk analysis frontend — vanilla JS, no build step.
 *
 * Staleness model: every plan change (dataset / seed / n / deadline /
 * budget / crash allocation) produces a new simulation job. Running jobs
 * are tracked by type; starting a new one cancels the previous poller (and
 * asks the server to cancel the worker). A finished payload is applied only
 * if its signature matches the latest request, so an old job can never
 * overwrite the new page state.
 */
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);

const state = {
  catalog: [],
  datasetFile: null,
  network: null,            // GET /api/networks payload
  simResult: null,          // latest accepted simulation result
  frontierResult: null,
  crashDays: {},            // activity id -> days, edited in the detail panel
  selectedActivity: null,
  latestSig: { simulation: null, frontier: null },
  pollers: { simulation: null, frontier: null },
  pollGeneration: { simulation: 0, frontier: 0 },
  jobs: { simulation: null, frontier: null },
  invalidNetwork: false,
};

// Sequential blue ramp for criticality (light -> dark), mirrors CSS tokens.
const SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#1c5cab", "#104281"];
const COLOR_BLUE = "#2a78d6";
const COLOR_ORANGE = "#eb6834";
const COLOR_CRITICAL = "#d03b3b";

// --------------------------------------------------------------------- api
async function apiGet(path) {
  const res = await fetch(path);
  const body = await res.json();
  if (!res.ok) throw new ApiError(body.issues || [{ code: "http_error", message: path }]);
  return body;
}

async function apiPost(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json();
  if (!res.ok) throw new ApiError(body.issues || [{ code: "http_error", message: path }]);
  return body;
}

class ApiError extends Error {
  constructor(issues) { super(issues.map((i) => i.code).join(", ")); this.issues = issues; }
}

function currentSpec() {
  const deadlineRaw = $("deadlineInput").value.trim();
  return {
    dataset: state.datasetFile,
    seed: Number($("seedInput").value),
    n_scenarios: Number($("nInput").value),
    deadline: deadlineRaw === "" ? null : Number(deadlineRaw),
    budget: Number($("budgetInput").value || 0),
    crash_days: state.crashDays,
    step_days: 1,
  };
}

// ------------------------------------------------------------------ banner
function showIssues(issues, kind = "error") {
  const el = $("issueBanner");
  el.hidden = false;
  el.className = "issue-banner" + (kind === "warning" ? " warning" : "");
  el.innerHTML = "";
  const title = document.createElement("strong");
  title.textContent = kind === "warning" ? "提示" : "网络 / 参数校验未通过";
  el.appendChild(title);
  for (const iss of issues) {
    const row = document.createElement("div");
    const where = iss.activity_id ? `（工序 ${iss.activity_id}）` : "";
    row.innerHTML = `<code>${iss.code}</code> ${escapeHtml(iss.message)}${escapeHtml(where)}`;
    el.appendChild(row);
  }
}
function clearIssues() { $("issueBanner").hidden = true; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- datasets
async function loadCatalog() {
  const data = await apiGet("/api/datasets");
  state.catalog = data.datasets;
  const sel = $("datasetSelect");
  sel.innerHTML = "";
  for (const ds of state.catalog) {
    const opt = document.createElement("option");
    opt.value = ds.file;
    opt.textContent = ds.expect_invalid ? `⛔ ${ds.name}` : ds.name;
    sel.appendChild(opt);
  }
  // Default to the analytic tiny network for reproducible first view.
  const tiny = state.catalog.find((d) => d.file === "tiny_analytic.json");
  sel.value = tiny ? tiny.file : state.catalog[0].file;
}

async function selectDataset(file) {
  state.datasetFile = file;
  state.simResult = null;
  state.frontierResult = null;
  state.crashDays = {};
  state.selectedActivity = null;
  resetOutputs();
  try {
    const net = await apiGet(`/api/networks/${encodeURIComponent(file.replace(".json", ""))}`);
    state.network = net;
    state.invalidNetwork = false;
    clearIssues();
    $("networkTitle").textContent = net.name;
    $("networkHint").textContent = net.description;
    renderNetwork(net);
    if (!$("deadlineInput").value) {
      // Suggest a rounded deadline above the nominal duration.
      $("deadlineInput").value = Math.ceil(net.deterministic.nominal_duration * 1.15);
    }
    const nominal = net.deterministic.nominal_duration;
    $("exportHint").textContent =
      `名义工期 ${nominal} 天 · 全压缩极限 ${net.deterministic.minimum_achievable_duration} 天`;
    validateCurrent();
  } catch (err) {
    if (err instanceof ApiError) {
      state.invalidNetwork = true;
      state.network = null;
      showIssues(err.issues);
      $("networkSvg").innerHTML = "";
    } else {
      showIssues([{ code: "network_load_failed", message: err.message }]);
    }
  }
}

async function validateCurrent() {
  const spec = currentSpec();
  const q = new URLSearchParams({ dataset: spec.dataset });
  if (spec.deadline !== null) q.set("deadline", spec.deadline);
  try {
    const v = await apiGet(`/api/validate?${q.toString()}`);
    if (!v.valid) showIssues(v.issues); else clearIssues();
  } catch (err) {
    if (err instanceof ApiError) showIssues(err.issues);
  }
}

// ------------------------------------------------------------- network svg
function renderNetwork(net) {
  const svg = $("networkSvg");
  svg.innerHTML = "";
  const { width, height, nodes, edges } = net.layout;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const critSet = new Set(net.deterministic.critical_path);

  // Edges first so nodes paint above arrowheads.
  const defs = document.createElementNS(SVG_NS, "defs");
  defs.innerHTML = `<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="var(--axis)"></path>
    </marker>
    <marker id="arrow-crit" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="${COLOR_CRITICAL}"></path>
    </marker>`;
  svg.appendChild(defs);

  const edgeLayer = document.createElementNS(SVG_NS, "g");
  for (const e of edges) {
    const a = nodes[e.source];
    const b = nodes[e.target];
    const path = document.createElementNS(SVG_NS, "path");
    const x1 = a.x + 78, y1 = a.y, x2 = b.x - 78, y2 = b.y;
    const mx = (x1 + x2) / 2;
    path.setAttribute("d", `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`);
    const isCrit = critSet.has(e.source) && critSet.has(e.target);
    path.setAttribute("class", "edge" + (isCrit ? " critical" : ""));
    path.setAttribute("marker-end", isCrit ? "url(#arrow-crit)" : "url(#arrow)");
    edgeLayer.appendChild(path);
  }
  svg.appendChild(edgeLayer);

  const nodeLayer = document.createElementNS(SVG_NS, "g");
  for (const ac of net.activities) {
    const p = nodes[ac.id];
    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", "node");
    g.setAttribute("transform", `translate(${p.x - 78},${p.y - 30})`);
    g.dataset.activityId = ac.id;

    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("width", 156);
    rect.setAttribute("height", 60);
    rect.setAttribute("rx", 10);
    rect.setAttribute("fill", seqColor(0));
    g.appendChild(rect);

    const t1 = document.createElementNS(SVG_NS, "text");
    t1.setAttribute("x", 12); t1.setAttribute("y", 24);
    t1.setAttribute("class", "node-id");
    t1.textContent = `${ac.id} · ${ac.name}`;
    g.appendChild(t1);

    const t2 = document.createElementNS(SVG_NS, "text");
    t2.setAttribute("x", 12); t2.setAttribute("y", 44);
    t2.setAttribute("class", "node-sub");
    t2.textContent = `名义 ${ac.base_duration} 天 · 最短 ${ac.min_duration} 天`;
    g.appendChild(t2);

    g.addEventListener("click", () => selectActivity(ac.id));
    nodeLayer.appendChild(g);
  }
  svg.appendChild(nodeLayer);
}

function seqColor(t) {
  const idx = Math.max(0, Math.min(SEQ.length - 1, Math.round(t * (SEQ.length - 1))));
  return SEQ[idx];
}

function recolorNodesByCriticality() {
  if (!state.network || !state.simResult) return;
  const crit = state.simResult.activities;
  for (const g of $("networkSvg").querySelectorAll(".node")) {
    const id = g.dataset.activityId;
    const t = crit[id] ? crit[id].criticality_index : 0;
    g.querySelector("rect").setAttribute("fill", seqColor(t));
  }
}

// ------------------------------------------------------------- stat tiles
function fmt(v, digits = 2) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: digits });
}
function pct(v) { return v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%"; }

function renderStats(result) {
  $("statsGrid").hidden = false;
  const s = result.summary;
  $("stMean").textContent = fmt(s.mean_project_duration);
  $("stSd").textContent = fmt(s.sd_project_duration);
  $("stPercentiles").textContent = `${fmt(s.p5)} / ${fmt(s.p50)} / ${fmt(s.p95)}`;
  $("stOnTime").textContent = s.deadline === null || s.deadline === undefined
    ? "未设截止期" : pct(s.on_time_probability);
  $("stCost").textContent = fmt(s.total_crash_cost, 0);
  $("stSignature").textContent = result.signature || "—";
}

// ------------------------------------------------------------- histogram
function renderHistogram(result) {
  const svg = $("histSvg");
  svg.innerHTML = "";
  const W = svg.clientWidth || 560, H = 320;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const m = { top: 20, right: 18, bottom: 44, left: 52 };
  const iw = W - m.left - m.right, ih = H - m.top - m.bottom;
  const { bin_edges: edges, counts } = result.histogram;
  const maxC = Math.max(...counts);
  const xMin = edges[0], xMax = edges[edges.length - 1];
  const x = (v) => m.left + ((v - xMin) / (xMax - xMin || 1)) * iw;
  const y = (c) => m.top + ih - (c / (maxC || 1)) * ih;

  // Grid + y ticks.
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const c = (maxC / ticks) * i;
    const yy = y(c);
    svg.appendChild(svgEl("line", { x1: m.left, x2: m.left + iw, y1: yy, y2: yy,
      class: "grid-line" }));
    const lbl = svgEl("text", { x: m.left - 8, y: yy + 4, "text-anchor": "end",
      class: "tick-label" });
    lbl.textContent = Math.round(c);
    svg.appendChild(lbl);
  }
  // X ticks (~6).
  const xt = 6;
  for (let i = 0; i <= xt; i++) {
    const v = xMin + ((xMax - xMin) / xt) * i;
    const xx = x(v);
    svg.appendChild(svgEl("line", { x1: xx, x2: xx, y1: m.top + ih,
      y2: m.top + ih + 5, class: "axis-line" }));
    const lbl = svgEl("text", { x: xx, y: m.top + ih + 20, "text-anchor": "middle",
      class: "tick-label" });
    lbl.textContent = fmt(v, 1);
    svg.appendChild(lbl);
  }
  // Bars (2px surface gap via inset).
  const tip = $("histTooltip");
  for (let i = 0; i < counts.length; i++) {
    const bx = x(edges[i]) + 1;
    const bw = Math.max(0, x(edges[i + 1]) - x(edges[i]) - 2);
    const by = y(counts[i]);
    const bar = svgEl("rect", {
      x: bx, y: by, width: bw, height: m.top + ih - by, rx: 4, class: "bar",
    });
    bar.addEventListener("mousemove", (ev) => {
      tip.hidden = false;
      tip.innerHTML = `<strong>工期 ${fmt(edges[i], 1)}–${fmt(edges[i + 1], 1)} 天</strong>
        场景数：${counts[i]}（${pct(counts[i] / result.summary.n_scenarios)}）`;
      positionTip(tip, ev);
    });
    bar.addEventListener("mouseleave", () => { tip.hidden = true; });
    svg.appendChild(bar);
  }
  // Deadline marker.
  const dl = result.summary.deadline;
  if (dl !== null && dl !== undefined && dl >= xMin && dl <= xMax) {
    const dx = x(dl);
    svg.appendChild(svgEl("line", { x1: dx, x2: dx, y1: m.top, y2: m.top + ih,
      class: "deadline-line" }));
    const lbl = svgEl("text", { x: dx + 6, y: m.top + 14, class: "deadline-label" });
    lbl.textContent = `截止期 ${fmt(dl, 1)}`;
    svg.appendChild(lbl);
  }
  // Axes + titles.
  svg.appendChild(svgEl("line", { x1: m.left, x2: m.left + iw, y1: m.top + ih,
    y2: m.top + ih, class: "axis-line" }));
  const xtitle = svgEl("text", { x: m.left + iw / 2, y: H - 6, "text-anchor": "middle",
    class: "axis-title" });
  xtitle.textContent = "项目完工时间（天）";
  svg.appendChild(xtitle);
  const ytitle = svgEl("text", {
    x: 14, y: m.top + ih / 2, "text-anchor": "middle",
    transform: `rotate(-90 14 ${m.top + ih / 2})`, class: "axis-title",
  });
  ytitle.textContent = "场景数";
  svg.appendChild(ytitle);
}

// ------------------------------------------------------------- frontier
function renderFrontier(payload) {
  const svg = $("frontierSvg");
  svg.innerHTML = "";
  const points = payload.frontier.points;
  if (!points.length) {
    const t = svgEl("text", { x: 20, y: 40, class: "tick-label" });
    t.textContent = "无可行前沿点";
    svg.appendChild(t);
    return;
  }
  const W = svg.clientWidth || 560, H = 320;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const m = { top: 20, right: 22, bottom: 44, left: 56 };
  const iw = W - m.left - m.right, ih = H - m.top - m.bottom;
  const costs = points.map((p) => p.cost);
  const durs = points.map((p) => p.expected_duration);
  const cMin = Math.min(...costs), cMax = Math.max(...costs);
  const dMin = Math.min(...durs), dMax = Math.max(...durs);
  const x = (c) => m.left + ((c - cMin) / (cMax - cMin || 1)) * iw;
  const y = (d) => m.top + ih - ((d - dMin) / (dMax - dMin || 1)) * ih;

  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const d = dMin + ((dMax - dMin) / ticks) * i;
    const yy = y(d);
    svg.appendChild(svgEl("line", { x1: m.left, x2: m.left + iw, y1: yy, y2: yy,
      class: "grid-line" }));
    const lbl = svgEl("text", { x: m.left - 8, y: yy + 4, "text-anchor": "end",
      class: "tick-label" });
    lbl.textContent = fmt(d, 1);
    svg.appendChild(lbl);
  }
  for (let i = 0; i <= ticks; i++) {
    const c = cMin + ((cMax - cMin) / ticks) * i;
    const xx = x(c);
    const lbl = svgEl("text", { x: xx, y: m.top + ih + 20, "text-anchor": "middle",
      class: "tick-label" });
    lbl.textContent = fmt(c, 0);
    svg.appendChild(lbl);
  }

  const pathD = points.map((p, i) => `${i === 0 ? "M" : "L"} ${x(p.cost)} ${y(p.expected_duration)}`).join(" ");
  svg.appendChild(svgEl("path", { d: pathD, class: "frontier-line" }));

  const currentCrash = state.simResult
    ? state.simResult.summary.total_crash_cost : null;
  const tip = $("frontierTooltip");
  points.forEach((p) => {
    const dot = svgEl("circle", {
      cx: x(p.cost), cy: y(p.expected_duration), r: 5, class: "frontier-dot",
    });
    if (currentCrash !== null && Math.abs(p.cost - currentCrash) < 1e-6) {
      dot.classList.add("current");
    }
    dot.addEventListener("mousemove", (ev) => {
      tip.hidden = false;
      tip.innerHTML = `<strong>成本 ${fmt(p.cost, 0)}</strong>
        平均工期 ${fmt(p.expected_duration)} 天<br>
        P5/P95：${fmt(p.p5)} / ${fmt(p.p95)}<br>
        准时概率：${pct(p.on_time_probability)}<br>
        赶工：${Object.entries(p.crash_days).map(([a, d]) => `${a} ${fmt(d, 1)}天`).join("，") || "无"}`;
      positionTip(tip, ev);
    });
    dot.addEventListener("mouseleave", () => { tip.hidden = true; });
    svg.appendChild(dot);
  });

  // Label the best on-time probability endpoint sparingly.
  const last = points[points.length - 1];
  const endLbl = svgEl("text", { x: x(last.cost), y: y(last.expected_duration) - 10,
    "text-anchor": "middle", class: "frontier-label" });
  endLbl.textContent = last.on_time_probability !== null
    ? `准时 ${pct(last.on_time_probability)}` : `省 ${fmt(last.days_saved, 1)} 天`;
  svg.appendChild(endLbl);

  svg.appendChild(svgEl("line", { x1: m.left, x2: m.left + iw, y1: m.top + ih,
    y2: m.top + ih, class: "axis-line" }));
  const xt = svgEl("text", { x: m.left + iw / 2, y: H - 6, "text-anchor": "middle",
    class: "axis-title" });
  xt.textContent = "赶工成本（成本单位）";
  svg.appendChild(xt);
  const yt = svgEl("text", { x: 14, y: m.top + ih / 2, "text-anchor": "middle",
    transform: `rotate(-90 14 ${m.top + ih / 2})`, class: "axis-title" });
  yt.textContent = "平均完工时间（天）";
  svg.appendChild(yt);

  // Legend for the current-plan marker.
  const dot = svgEl("circle", { cx: m.left + 8, cy: m.top + 8, r: 5,
    fill: COLOR_ORANGE, stroke: "var(--surface)", "stroke-width": 2 });
  svg.appendChild(dot);
  const lgTxt = svgEl("text", { x: m.left + 20, y: m.top + 12, class: "tick-label" });
  lgTxt.textContent = "当前模拟方案";
  svg.appendChild(lgTxt);
}

function svgEl(name, attrs) {
  const el = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
function positionTip(tip, ev) {
  const host = ev.target.closest(".chart-card").getBoundingClientRect();
  tip.style.left = `${ev.clientX - host.left + 12}px`;
  tip.style.top = `${ev.clientY - host.top + 12}px`;
}

// ------------------------------------------------------------- activity table
function renderActivityTable() {
  const tbody = $("activityTable").querySelector("tbody");
  tbody.innerHTML = "";
  if (!state.simResult) return;
  const acts = state.network.activities;
  const stats = state.simResult.activities;
  for (const ac of acts) {
    const st = stats[ac.id];
    const tr = document.createElement("tr");
    if (ac.id === state.selectedActivity) tr.classList.add("selected");
    tr.dataset.activityId = ac.id;
    tr.innerHTML = `
      <td><strong>${ac.id}</strong> · ${escapeHtml(ac.name)}</td>
      <td class="num">${fmt(ac.base_duration, 1)}</td>
      <td class="num">${fmt(st.mean_duration)}</td>
      <td class="num">${fmt(st.duration_sd)}</td>
      <td class="num"><span class="crit-cell">
        <span class="crit-meter"><span style="width:${(st.criticality_index * 100).toFixed(1)}%"></span></span>
        ${pct(st.criticality_index)}</span></td>
      <td class="num">${fmt(st.cruciality_index, 3)}</td>
      <td class="num">${fmt(st.crash_days_applied, 1)}</td>
      <td class="num">${fmt(ac.crash_cost_per_day, 0)}</td>`;
    tr.addEventListener("click", () => selectActivity(ac.id));
    tbody.appendChild(tr);
  }
}

// ------------------------------------------------------------- detail panel
function selectActivity(aid) {
  state.selectedActivity = aid;
  document.querySelectorAll(".node").forEach((g) =>
    g.classList.toggle("selected", g.dataset.activityId === aid));
  renderActivityTable();
  renderDetail();
}

function renderDetail() {
  const aid = state.selectedActivity;
  const body = $("detailBody");
  if (!aid || !state.network) {
    body.className = "detail-empty";
    body.textContent = "尚未选择工序。";
    return;
  }
  body.className = "";
  const ac = state.network.activities.find((a) => a.id === aid);
  const st = state.simResult ? state.simResult.activities[aid] : null;
  const attr = state.simResult ? state.simResult.delay_attribution : null;
  const maxCrash = ac.base_duration - ac.min_duration;
  const applied = state.crashDays[aid] || 0;

  $("detailTitle").textContent = `${ac.id} · ${ac.name}`;
  let html = `
    <div class="detail-section">
      <h3>工序指标${st ? "（当前模拟）" : "（运行模拟后显示）"}</h3>
      <div class="kv-grid">
        <span class="k">名义 / 最短工期</span><span class="v">${fmt(ac.base_duration, 1)} / ${fmt(ac.min_duration, 1)} 天</span>
        <span class="k">关键度指数</span><span class="v">${st ? pct(st.criticality_index) : "—"}</span>
        <span class="k">关键性（相关系数）</span><span class="v">${st ? fmt(st.cruciality_index, 3) : "—"}</span>
        <span class="k">平均 / 波动工期</span><span class="v">${st ? fmt(st.mean_duration) : "—"} / ${st ? fmt(st.duration_sd) : "—"}</span>
        <span class="k">赶工单价</span><span class="v">${fmt(ac.crash_cost_per_day, 0)} /天</span>
      </div>
    </div>`;

  if (attr) {
    const aa = attr.activities[aid];
    const rows = [];
    for (const [fid, days] of Object.entries(aa.factor_contributions)) {
      const f = state.network.risk_factors.find((x) => x.id === fid);
      rows.push({ name: f ? f.name : fid, days,
        corr: attr.factors[fid].correlation_with_project_duration });
    }
    rows.push({ name: "工序专属残差", days: aa.idiosyncratic_mean, corr: null });
    const maxAbs = Math.max(1e-9, ...rows.map((r) => Math.abs(r.days)));
    html += `<div class="detail-section"><h3>迟延来源（平均天数贡献）</h3>`;
    for (const r of rows) {
      const w = Math.max(2, Math.abs(r.days) / maxAbs * 100);
      html += `<div class="attrib-row">
        <span class="attrib-name">${escapeHtml(r.name)}</span>
        <span class="attrib-bar"><span style="width:${w}%"></span></span>
        <span class="attrib-val">${r.days >= 0 ? "+" : ""}${fmt(r.days, 2)} 天</span>
      </div>`;
    }
    html += `</div>`;
  }

  html += `
    <div class="detail-section">
      <h3>赶工分配（仅限数据支持区间 0–${fmt(maxCrash, 1)} 天）</h3>
      <div class="crash-box">
        <input type="range" id="crashSlider" min="0" max="${maxCrash}"
          step="${maxCrash > 4 ? 0.5 : 0.25}" value="${applied}"
          ${maxCrash <= 0 ? "disabled" : ""}>
        <div class="crash-meta">
          <span>压缩 <strong id="crashDaysLabel">${fmt(applied, 2)}</strong> 天
            → 最短可到 ${fmt(ac.min_duration, 1)} 天</span>
          <span class="crash-cost" id="crashCostLabel">${fmt(applied * ac.crash_cost_per_day, 0)}</span>
        </div>
        <div class="detail-actions">
          <button class="btn btn-primary btn-sm" id="applyCrashBtn" type="button">应用并重新模拟</button>
          <button class="btn btn-sm" id="resetCrashBtn" type="button">清零</button>
        </div>
      </div>
    </div>`;
  body.innerHTML = html;

  $("crashSlider").addEventListener("input", (ev) => {
    const days = Number(ev.target.value);
    $("crashDaysLabel").textContent = fmt(days, 2);
    $("crashCostLabel").textContent = fmt(days * ac.crash_cost_per_day, 0);
  });
  $("applyCrashBtn").addEventListener("click", () => {
    const days = Number($("crashSlider").value);
    state.crashDays[aid] = days;
    if (days === 0) delete state.crashDays[aid];
    runSimulation();
  });
  $("resetCrashBtn").addEventListener("click", () => {
    delete state.crashDays[aid];
    $("crashSlider").value = 0;
    $("crashDaysLabel").textContent = "0.00";
    $("crashCostLabel").textContent = "0";
    runSimulation();
  });
}

// ------------------------------------------------------------- jobs/polling
function setBadge(text, running) {
  const badge = $("jobBadge");
  badge.hidden = !running;
  if (running) $("jobBadgeText").textContent = text;
}

function stopPoller(kind) {
  if (state.pollers[kind]) {
    clearInterval(state.pollers[kind]);
    state.pollers[kind] = null;
  }
}

async function startJob(kind, spec) {
  // A plan change invalidates any in-flight job of the same type. The
  // generation token also covers the await window below: if another
  // startJob happens while this one is still posting, its poller is
  // discarded rather than leaking a second interval.
  stopPoller(kind);
  const generation = ++state.pollGeneration[kind];
  const old = state.jobs[kind];
  if (old) apiPost(`/api/jobs/${old.job_id}/cancel`, {}).catch(() => {});

  const endpoint = kind === "simulation" ? "/api/simulate" : "/api/frontier";
  const job = await apiPost(endpoint, spec);
  if (generation !== state.pollGeneration[kind]) {
    // A newer plan superseded this request before its poller started.
    apiPost(`/api/jobs/${job.job_id}/cancel`, {}).catch(() => {});
    return job;
  }
  state.jobs[kind] = job;
  state.latestSig[kind] = job.signature;
  setBadge(`${kind === "simulation" ? "模拟" : "成本前沿"}计算中 0%`, true);
  pollJob(kind, job.job_id, generation);
  return job;
}

function pollJob(kind, jobId, generation) {
  stopPoller(kind);
  state.pollers[kind] = setInterval(async () => {
    // A newer startJob already cleared this interval; this guard only
    // protects the await window and must not touch the newer interval.
    if (generation !== state.pollGeneration[kind]) return;
    try {
      const job = await apiGet(`/api/jobs/${jobId}`);
      setBadge(`${kind === "simulation" ? "模拟" : "成本前沿"}计算中 ${(job.progress * 100).toFixed(0)}%`, true);
      if (job.status === "done") {
        stopPoller(kind);
        // Signature guard: never let an older job overwrite newer state.
        if (job.signature !== state.latestSig[kind]) {
          setBadge("旧任务结果已丢弃（方案已变更）", false);
          return;
        }
        if (kind === "simulation") acceptSimulation(job);
        else acceptFrontier(job);
        if (!state.pollers.simulation && !state.pollers.frontier) setBadge("", false);
      } else if (job.status === "error") {
        stopPoller(kind);
        showIssues(job.error.issues || [{ code: job.error.code, message: job.error.message }]);
        setBadge("", false);
      } else if (job.status === "canceled") {
        stopPoller(kind);
        setBadge("", false);
      }
    } catch (err) {
      stopPoller(kind);
      setBadge("", false);
      if (err instanceof ApiError) showIssues(err.issues);
    }
  }, 400);
}

function acceptSimulation(job) {
  state.simResult = job.result;
  renderStats(job.result);
  renderHistogram(job.result);
  renderActivityTable();
  recolorNodesByCriticality();
  renderDetail();
  $("exportCsvBtn").disabled = false;
  $("exportJsonBtn").disabled = false;
  $("exportHint").textContent =
    `任务 ${job.job_id} · ${job.result.summary.n_scenarios} 个场景 · 签名 ${job.signature}`;
  // Frontier marker highlighting depends on the current plan cost.
  if (state.frontierResult) renderFrontier(state.frontierResult);
}

function acceptFrontier(job) {
  state.frontierResult = job.result;
  renderFrontier(job.result);
  $("exportFrontierCsvBtn").disabled = false;
}

async function runSimulation() {
  if (state.invalidNetwork || !state.network) {
    showIssues([{ code: "network_invalid", message: "当前数据集无法模拟，请先解决校验问题。" }]);
    return;
  }
  clearIssues();
  try {
    await startJob("simulation", currentSpec());
  } catch (err) {
    if (err instanceof ApiError) showIssues(err.issues);
  }
}

async function runFrontier() {
  if (state.invalidNetwork || !state.network) {
    showIssues([{ code: "network_invalid", message: "当前数据集无法计算前沿。" }]);
    return;
  }
  clearIssues();
  try {
    await startJob("frontier", currentSpec());
  } catch (err) {
    if (err instanceof ApiError) showIssues(err.issues);
  }
}

// ------------------------------------------------------------- exports
function exportUrl(format) {
  const job = state.jobs.simulation;
  if (!job) return "#";
  const q = new URLSearchParams({ format, dataset: state.datasetFile });
  return `/api/export/${job.job_id}?${q.toString()}`;
}

function resetOutputs() {
  $("statsGrid").hidden = true;
  $("histSvg").innerHTML = "";
  $("frontierSvg").innerHTML = "";
  $("activityTable").querySelector("tbody").innerHTML = "";
  $("exportCsvBtn").disabled = true;
  $("exportJsonBtn").disabled = true;
  $("exportFrontierCsvBtn").disabled = true;
  renderDetail();
}

// ------------------------------------------------------------- wiring
function wire() {
  $("datasetSelect").addEventListener("change", (ev) => selectDataset(ev.target.value));
  $("runBtn").addEventListener("click", runSimulation);
  $("frontierBtn").addEventListener("click", runFrontier);
  $("cancelJobBtn").addEventListener("click", () => {
    for (const kind of ["simulation", "frontier"]) {
      const job = state.jobs[kind];
      if (job && state.pollers[kind]) {
        apiPost(`/api/jobs/${job.job_id}/cancel`, {}).catch(() => {});
        stopPoller(kind);
      }
    }
    setBadge("", false);
  });
  // Any plan parameter change marks prior results as a different signature.
  for (const id of ["seedInput", "nInput", "deadlineInput", "budgetInput"]) {
    $(id).addEventListener("change", () => {
      if (id === "deadlineInput") validateCurrent();
    });
  }
  $("exportCsvBtn").addEventListener("click", () => { window.location = exportUrl("csv"); });
  $("exportJsonBtn").addEventListener("click", () => { window.location = exportUrl("json"); });
  $("exportFrontierCsvBtn").addEventListener("click", () => {
    const job = state.jobs.frontier;
    if (job) window.location = `/api/export/${job.job_id}?format=csv`;
  });
  window.addEventListener("resize", () => {
    if (state.simResult) renderHistogram(state.simResult);
    if (state.frontierResult) renderFrontier(state.frontierResult);
  });
}

(async function init() {
  wire();
  try {
    await loadCatalog();
    await selectDataset($("datasetSelect").value);
  } catch (err) {
    showIssues([{ code: "init_failed", message: err.message }]);
  }
})();
