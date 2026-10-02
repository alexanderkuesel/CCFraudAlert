// Spend historian: tag browser (category = device, merchant = tag), single-pen trend, month-to-date vs budget.
// Plain SVG. All names come from bank emails, so text is inserted with textContent only.
(function () {
  "use strict";
  const SVG = "http://www.w3.org/2000/svg";
  const $ = id => document.getElementById(id);
  const state = { pen: { type: "all" }, bucket: "day", days: 90, overview: null, series: null, open: new Set() };
  let currency = "";

  // ---------- helpers ----------
  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  };
  const h = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
  const money = (v, dp = 2) => (v || 0).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const compact = v => Math.abs(v) >= 1000 ? (v / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "K" : money(v, 0);
  const monthName = iso => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short" });
  const dayLabel = iso => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const STATUS = { hi: "HI", hihi: "HIHI" };
  const MIN_PACE_DAYS = 7; // a straight-line projection from the first few days of a month is noise
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
  }
  async function api(url, opts = {}) {
    const r = await fetch(url, { headers: { "content-type": "application/json" }, ...opts });
    const body = r.status === 204 ? {} : await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `request failed (${r.status})`);
    return body;
  }
  function flash(text, error = false) {
    const m = $("hist-msg"); m.textContent = text; m.hidden = false; m.classList.toggle("error", error);
    clearTimeout(flash.t); flash.t = setTimeout(() => (m.hidden = true), 4000);
  }

  // Sparkline: 6 monthly values, de-emphasised line, current month as the end dot.
  function sparkline(values, w = 72, hgt = 20) {
    const s = el("svg", { width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, class: "spark", "aria-hidden": "true" });
    const max = Math.max(...values, 1), step = (w - 6) / Math.max(values.length - 1, 1);
    const pts = values.map((v, i) => [3 + i * step, hgt - 3 - (v / max) * (hgt - 6)]);
    el("polyline", { points: pts.map(p => p.join(",")).join(" "), class: "spark-line" }, s);
    const [x, y] = pts[pts.length - 1];
    el("circle", { cx: x, cy: y, r: 2.5, class: "spark-end" }, s);
    return s;
  }

  // Analog indicator (ISA-101 style moving bar): fill = month-to-date vs budget, ticks at HI and HIHI.
  function gauge(d) {
    const wrap = h("span", null, "gauge" + (d.status === "hi" || d.status === "hihi" ? " " + d.status : ""));
    if (!d.budget) { wrap.classList.add("none"); wrap.title = "No budget set"; return wrap; }
    const fill = h("span", null, "gauge-fill");
    fill.style.width = Math.min(d.pct / 1.2, 1) * 100 + "%"; // track spans 0-120% of budget
    wrap.append(fill, Object.assign(h("span", null, "gauge-tick"), { style: `left:${(0.8 / 1.2) * 100}%` }),
      Object.assign(h("span", null, "gauge-tick sp"), { style: `left:${(1 / 1.2) * 100}%` }));
    wrap.title = `${Math.round(d.pct * 100)}% of the ${money(d.budget, 0)} ${currency} monthly budget`;
    return wrap;
  }
  function statusBadge(status) {
    if (!STATUS[status]) return null;
    const b = h("span", STATUS[status], "lim-badge " + status);
    b.title = status === "hihi" ? "Over budget (≥ 100%)" : "Approaching budget (≥ 80%)";
    return b;
  }

  // ---------- overview: KPIs, tag browser, management tables ----------
  async function loadOverview() {
    state.overview = await api("/api/spending/overview");
    currency = state.overview.currency;
    document.querySelectorAll('[data-bind="currency"]').forEach(e => (e.textContent = currency));
    const m = new Date(state.overview.month + "T12:00:00");
    document.querySelector('[data-bind="month-label"]').textContent =
      `${m.toLocaleDateString(undefined, { month: "long", year: "numeric" })} · day ${state.overview.day_of_month} of ${state.overview.days_in_month}`;
    renderKpis(); renderTree(); renderManage();
  }

  function renderKpis() {
    const o = state.overview, t = o.total, box = $("hist-kpis");
    box.replaceChildren();
    const kpi = (label, value, sub) => {
      const k = h("div", null, "kpi"); k.append(h("span", label), h("b", value));
      if (sub) k.append(h("small", sub)); box.append(k);
    };
    kpi("Spent this month", `${money(t.mtd, 0)} ${currency}`, t.budget ? `of ${money(t.budget, 0)} budgeted` : "no budgets set");
    if (o.day_of_month >= MIN_PACE_DAYS) kpi("On pace for", `${money(t.mtd / o.day_of_month * o.days_in_month, 0)} ${currency}`, "at this month's rate");
    else kpi("On pace for", "—", `shown from day ${MIN_PACE_DAYS}`);
    kpi("Last month", `${money(t.last_month, 0)} ${currency}`);
    const over = o.devices.filter(d => d.status === "hihi").length, near = o.devices.filter(d => d.status === "hi").length;
    kpi("Budgets at limit", `${over} HIHI · ${near} HI`, over + near ? "see the tag browser" : "all within budget");
  }

  function renderTree() {
    const tree = $("tb-tree"); tree.replaceChildren();
    const row = (pen, label, d, level) => {
      const r = h("div", null, `tb-row lvl${level}`);
      r.setAttribute("role", "treeitem"); r.tabIndex = 0;
      r.dataset.pen = JSON.stringify(pen);
      const isSel = JSON.stringify(state.pen) === r.dataset.pen;
      r.setAttribute("aria-selected", String(isSel)); if (isSel) r.classList.add("sel");
      const name = h("span", null, "tb-name");
      if (level === 0 && pen.type === "device") {
        const tw = h("button", state.open.has(pen.key) ? "▾" : "▸", "tb-twist");
        tw.setAttribute("aria-label", (state.open.has(pen.key) ? "Collapse " : "Expand ") + label);
        tw.addEventListener("click", ev => { ev.stopPropagation(); state.open.has(pen.key) ? state.open.delete(pen.key) : state.open.add(pen.key); renderTree(); });
        r.setAttribute("aria-expanded", String(state.open.has(pen.key)));
        name.append(tw);
      }
      name.append(h("span", label));
      if (d.assigned_by === "user") name.append(h("span", "you", "tb-you"));
      const val = h("span", `${money(d.mtd, 0)}`, "tb-val");
      const right = h("span", null, "tb-right");
      if (level === 0 && pen.type === "device") { right.append(gauge(d)); const b = statusBadge(d.status); if (b) right.append(b); }
      else right.append(h("span", d.count ? `${d.count}×` : "", "tb-count"));
      right.append(sparkline(d.spark));
      r.append(name, val, right);
      const pick = () => { state.pen = pen; renderTree(); loadSeries(); };
      r.addEventListener("click", pick);
      r.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
      tree.append(r);
    };
    const o = state.overview;
    row({ type: "all" }, "All spending", { mtd: o.total.mtd, spark: o.total.spark, status: "none" }, 0);
    for (const d of o.devices) {
      const key = d.id == null ? "uncategorized" : String(d.id);
      row({ type: "device", key }, d.name, d, 0);
      if (state.open.has(key)) for (const t of d.tags) row({ type: "tag", key: t.key }, t.name, t, 1);
    }
  }

  // ---------- trend ----------
  async function loadSeries() {
    const p = state.pen, q = new URLSearchParams({ bucket: state.bucket, days: state.days });
    if (p.type === "device") q.set("category", p.key);
    if (p.type === "tag") q.set("merchant", p.key);
    $("chart-trend").style.opacity = .5; $("chart-mtd").style.opacity = .5; // refetch keeps the frame
    state.series = await api("/api/spending/series?" + q);
    $("chart-trend").style.opacity = 1; $("chart-mtd").style.opacity = 1;
    const s = state.series;
    $("pen-title").textContent = s.label;
    $("pen-path").textContent = p.type === "tag" ? `Tag · device: ${s.device}` : p.type === "device" ? "Device (category)" : "All devices";
    const move = $("pen-move"); move.hidden = p.type !== "tag";
    if (p.type === "tag") fillCategorySelect($("move-select"), currentCategoryOf(p.key));
    drawTrend(); drawMtd(); drawTable();
  }

  function frame(container, height) {
    container.replaceChildren();
    const W = Math.max(container.clientWidth, 300), H = height, m = { l: 52, r: 12, t: 14, b: 26 };
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, container);
    return { svg, W, H, m, iw: W - m.l - m.r, ih: H - m.t - m.b };
  }
  function yAxis(f, max) {
    for (let i = 0; i <= 4; i++) {
      const v = max * i / 4, y = f.m.t + f.ih - (v / max) * f.ih;
      el("line", { x1: f.m.l, x2: f.W - f.m.r, y1: y, y2: y, class: i ? "grid" : "base" }, f.svg);
      el("text", { x: f.m.l - 6, y: y + 4, class: "tick", "text-anchor": "end" }, f.svg).textContent = compact(v);
    }
  }
  // HIHI is labelled above its line and HI below its (lower) line, so the two labels never collide.
  function limitLine(f, value, max, cls, label) {
    if (value == null || value > max) return;
    const y = f.m.t + f.ih - (value / max) * f.ih;
    el("line", { x1: f.m.l, x2: f.W - f.m.r, y1: y, y2: y, class: "limit " + cls }, f.svg);
    const t = el("text", { x: f.W - f.m.r - 2, y: cls === "hihi" ? y - 4 : y + 13, class: "limit-label", "text-anchor": "end" }, f.svg);
    t.textContent = label;
  }
  const tip = $("tip");
  function showTip(ev, lines) {
    tip.replaceChildren(); lines.forEach((l, i) => tip.append(h(i ? "div" : "b", l)));
    tip.hidden = false;
    const box = tip.parentElement.getBoundingClientRect();
    const x = ev.clientX - box.left, y = ev.clientY - box.top;
    tip.style.left = Math.min(x + 14, box.width - tip.offsetWidth - 4) + "px";
    tip.style.top = Math.max(y - tip.offsetHeight - 10, 4) + "px";
  }
  const hideTip = () => (tip.hidden = true);

  function drawTrend() {
    const s = state.series, f = frame($("chart-trend"), 240), pts = s.points;
    const monthly = s.bucket === "month" && s.budget;
    const max = niceMax(Math.max(...pts.map(p => p.value), monthly ? s.budget * 1.05 : 0, 1));
    yAxis(f, max);
    const slot = f.iw / pts.length, bw = Math.max(Math.min(24, slot - 2), 1); // <= 24px, 2px gap
    const every = Math.ceil(pts.length / Math.max(Math.floor(f.iw / 64), 1));
    pts.forEach((p, i) => {
      const x = f.m.l + i * slot + (slot - bw) / 2, hgt = (p.value / max) * f.ih, y = f.m.t + f.ih - hgt;
      const over = monthly && p.value >= s.budget ? " hihi" : monthly && p.value >= s.budget * 0.8 ? " hi" : "";
      if (hgt > 0) {
        const r = Math.min(4, hgt, bw / 2); // 4px rounded data-end, square at the baseline
        el("path", { class: "bar" + over, d: `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + bw - r}Q${x + bw},${y} ${x + bw},${y + r}V${y + hgt}Z` }, f.svg);
      }
      const hit = el("rect", { x: f.m.l + i * slot, y: f.m.t, width: slot, height: f.ih, class: "hit" }, f.svg);
      const when = s.bucket === "month" ? new Date(p.start + "T12:00:00").toLocaleDateString(undefined, { month: "long", year: "numeric" })
        : s.bucket === "week" ? "Week of " + dayLabel(p.start) : dayLabel(p.start);
      const lines = [`${money(p.value)} ${currency}`, when, `${p.count} transaction${p.count === 1 ? "" : "s"}`];
      if (monthly) lines.push(`${Math.round(p.value / s.budget * 100)}% of budget` + (over ? ` · ${STATUS[over.trim()]}` : ""));
      hit.addEventListener("pointermove", ev => showTip(ev, lines)); hit.addEventListener("pointerleave", hideTip);
      if (i % every === 0) {
        el("text", { x: f.m.l + i * slot + slot / 2, y: f.H - 8, class: "tick", "text-anchor": "middle" }, f.svg)
          .textContent = s.bucket === "month" ? monthName(p.start) : dayLabel(p.start);
      }
    });
    if (monthly) { limitLine(f, s.budget * 0.8, max, "hi", "HI 80%"); limitLine(f, s.budget, max, "hihi", "HIHI budget"); }
  }

  function drawMtd() {
    const s = state.series, m = s.mtd, f = frame($("chart-mtd"), 200);
    const n = m.days_in_month, cum = m.cumulative, today = cum.length, last = cum[today - 1] || 0;
    const paced = today >= MIN_PACE_DAYS, projected = paced ? last / today * n : last;
    const max = niceMax(Math.max(last, projected, m.budget ? m.budget * 1.05 : 0, 1));
    yAxis(f, max);
    const X = d => f.m.l + (d - 1) / Math.max(n - 1, 1) * f.iw, Y = v => f.m.t + f.ih - (v / max) * f.ih;
    for (const d of [1, 8, 15, 22, n]) el("text", { x: X(d), y: f.H - 8, class: "tick", "text-anchor": "middle" }, f.svg).textContent = String(d);
    if (m.budget) { limitLine(f, m.budget * 0.8, max, "hi", "HI 80%"); limitLine(f, m.budget, max, "hihi", "HIHI budget"); }
    if (paced && today < n) el("line", { x1: X(today), y1: Y(last), x2: X(n), y2: Y(projected), class: "projection" }, f.svg);
    if (today) {
      el("path", { class: "area", d: `M${X(1)},${Y(0)}` + cum.map((v, i) => `L${X(i + 1)},${Y(v)}`).join("") + `L${X(today)},${Y(0)}Z` }, f.svg);
      el("polyline", { class: "pen", points: cum.map((v, i) => `${X(i + 1)},${Y(v)}`).join(" ") }, f.svg);
      el("circle", { class: "pen-dot" + (m.status === "hi" || m.status === "hihi" ? " " + m.status : ""), cx: X(today), cy: Y(last), r: 4.5 }, f.svg);
      el("text", { x: Math.min(X(today) + 8, f.W - f.m.r - 60), y: Y(last) - 8, class: "end-label" }, f.svg).textContent = money(last, 0);
    }
    const pace = paced ? `, on pace for ${money(projected, 0)}` : "";
    $("mtd-sub").textContent = m.budget
      ? `· ${money(last, 0)} of ${money(m.budget, 0)} ${currency} (${Math.round(last / m.budget * 100)}%)` + pace
      : `· ${money(last, 0)} ${currency}${pace} (no budget set)`;
    // crosshair: snap to the nearest day
    const cross = el("line", { class: "cross", y1: f.m.t, y2: f.m.t + f.ih, visibility: "hidden" }, f.svg);
    const hit = el("rect", { x: f.m.l, y: f.m.t, width: f.iw, height: f.ih, class: "hit" }, f.svg);
    hit.addEventListener("pointermove", ev => {
      const box = f.svg.getBoundingClientRect(), d = Math.round((ev.clientX - box.left - f.m.l) / f.iw * (n - 1)) + 1;
      const day = Math.min(Math.max(d, 1), n); cross.setAttribute("x1", X(day)); cross.setAttribute("x2", X(day)); cross.setAttribute("visibility", "visible");
      const date = dayLabel(m.month.slice(0, 8) + String(day).padStart(2, "0"));
      showTip(ev, day <= today ? [`${money(cum[day - 1])} ${currency}`, `Running total to ${date}`, m.budget ? `${Math.round(cum[day - 1] / m.budget * 100)}% of budget` : ""].filter(Boolean)
        : paced ? [`${money(projected / n * day, 0)} ${currency}`, `Projected by ${date}`] : [date, `Projection starts on day ${MIN_PACE_DAYS}`]);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  }

  function drawTable() {
    const s = state.series, t = h("table");
    const head = t.createTHead().insertRow();
    ["Period", `Spend (${currency})`, "Transactions"].forEach(x => head.append(h("th", x)));
    const body = t.createTBody();
    [...s.points].reverse().forEach(p => {
      const r = body.insertRow();
      r.insertCell().textContent = s.bucket === "month" ? new Date(p.start + "T12:00:00").toLocaleDateString(undefined, { month: "long", year: "numeric" }) : dayLabel(p.start);
      const v = r.insertCell(); v.className = "num"; v.textContent = money(p.value);
      const c = r.insertCell(); c.className = "num"; c.textContent = String(p.count);
    });
    $("trend-table").replaceChildren(t);
  }

  // ---------- management ----------
  function categories() { return state.overview.devices.filter(d => d.id != null); }
  function currentCategoryOf(key) {
    for (const d of state.overview.devices) if (d.tags.some(t => t.key === key)) return d.id;
    return null;
  }
  function fillCategorySelect(select, selected) {
    select.replaceChildren();
    for (const d of categories()) { const o = h("option", d.name); o.value = String(d.id); select.append(o); }
    const u = h("option", "Uncategorized"); u.value = ""; select.append(u);
    select.value = selected == null ? "" : String(selected);
  }
  async function assign(keys, categoryId) {
    await api("/api/spending/assign", { method: "POST", body: JSON.stringify({ keys, category_id: categoryId === "" ? null : Number(categoryId) }) });
    await loadOverview(); await loadSeries();
  }

  function renderManage() {
    const tbody = $("cat-table").tBodies[0]; tbody.replaceChildren();
    for (const d of categories()) {
      const r = tbody.insertRow();
      const name = h("input"); name.value = d.name; name.maxLength = 64; name.setAttribute("aria-label", "Name");
      const budget = h("input"); budget.value = d.budget == null ? "" : d.budget; budget.inputMode = "decimal"; budget.placeholder = "none";
      budget.setAttribute("aria-label", `Monthly budget for ${d.name}`);
      r.insertCell().append(name); r.insertCell().append(budget);
      const mtd = r.insertCell(); mtd.className = "num"; mtd.textContent = money(d.mtd, 0);
      const n = r.insertCell(); n.className = "num"; n.textContent = String(d.tags.length);
      const act = r.insertCell(); act.className = "nowrap";
      const save = h("button", "Save", "btn btn-sm"), del = h("button", "Delete", "link danger");
      save.addEventListener("click", async () => {
        try { await api(`/api/spending/categories/${d.id}`, { method: "PATCH", body: JSON.stringify({ name: name.value, budget: budget.value }) });
          flash(`Saved “${name.value}”.`); await loadOverview(); await loadSeries(); } catch (e) { flash(e.message, true); }
      });
      del.addEventListener("click", async () => {
        if (!confirm(`Delete “${d.name}”? Its ${d.tags.length} merchant(s) move to Uncategorized.`)) return;
        try { const r2 = await api(`/api/spending/categories/${d.id}`, { method: "DELETE" });
          flash(`Deleted “${d.name}”; ${r2.uncategorized} merchant(s) now uncategorized.`);
          if (state.pen.type === "device" && state.pen.key === String(d.id)) state.pen = { type: "all" };
          await loadOverview(); await loadSeries(); } catch (e) { flash(e.message, true); }
      });
      act.append(save, del);
    }
    fillCategorySelect($("bulk-target"), categories()[0] ? categories()[0].id : null);
    const filter = $("tag-filter"), keep = filter.value; filter.replaceChildren(h("option", "All categories"));
    filter.firstChild.value = "";
    for (const d of state.overview.devices) { const o = h("option", d.name); o.value = d.id == null ? "uncategorized" : String(d.id); filter.append(o); }
    filter.value = keep; renderTags();
  }

  function renderTags() {
    const tbody = $("tag-table").tBodies[0]; tbody.replaceChildren();
    const q = $("tag-search").value.trim().toLowerCase(), f = $("tag-filter").value;
    let shown = 0;
    for (const d of state.overview.devices) {
      const dkey = d.id == null ? "uncategorized" : String(d.id);
      if (f && f !== dkey) continue;
      for (const t of d.tags) {
        if (q && !t.name.toLowerCase().includes(q)) continue;
        shown++;
        const r = tbody.insertRow();
        const sel = r.insertCell(); sel.className = "sel";
        const box = h("input"); box.type = "checkbox"; box.value = t.key; box.className = "tag-sel"; box.setAttribute("aria-label", "Select " + t.name);
        sel.append(box);
        r.insertCell().textContent = t.name;
        const s = h("select"); s.setAttribute("aria-label", "Category for " + t.name); fillCategorySelect(s, d.id);
        s.addEventListener("change", async () => { try { await assign([t.key], s.value); flash(`Moved ${t.name}.`); } catch (e) { flash(e.message, true); } });
        r.insertCell().append(s);
        r.insertCell().append(h("span", t.assigned_by === "user" ? "you" : "auto", t.assigned_by === "user" ? "tb-you" : "muted"));
        const m = r.insertCell(); m.className = "num"; m.textContent = money(t.mtd, 0);
        const six = r.insertCell(); six.className = "num"; six.textContent = money(t.spark.reduce((a, b) => a + b, 0), 0);
      }
    }
    $("tag-count").textContent = `${shown} merchant${shown === 1 ? "" : "s"}`;
    $("tag-all").checked = false;
  }

  // ---------- wiring ----------
  document.querySelectorAll("[data-bucket]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("[data-bucket]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    state.bucket = b.dataset.bucket; loadSeries();
  }));
  document.querySelectorAll("[data-days]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("[data-days]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    state.days = Number(b.dataset.days); loadSeries();
  }));
  document.querySelectorAll("[data-tab]").forEach(a => a.addEventListener("click", ev => {
    ev.preventDefault();
    document.querySelectorAll("[data-tab]").forEach(x => x.setAttribute("aria-selected", String(x === a)));
    $("tab-trends").hidden = a.dataset.tab !== "trends"; $("tab-manage").hidden = a.dataset.tab !== "manage";
    history.replaceState(null, "", "#" + a.dataset.tab);
    if (a.dataset.tab === "trends" && state.series) { drawTrend(); drawMtd(); }
  }));
  $("move-select").addEventListener("change", async ev => {
    try { await assign([state.pen.key], ev.target.value); flash(`Moved ${$("pen-title").textContent}.`); } catch (e) { flash(e.message, true); }
  });
  $("cat-add").addEventListener("submit", async ev => {
    ev.preventDefault();
    const form = ev.target;
    try { await api("/api/spending/categories", { method: "POST", body: JSON.stringify({ name: form.name.value, budget: form.budget.value }) });
      flash(`Added “${form.name.value}”.`); form.reset(); await loadOverview(); } catch (e) { flash(e.message, true); }
  });
  $("tag-search").addEventListener("input", renderTags);
  $("tag-filter").addEventListener("change", renderTags);
  $("tag-all").addEventListener("change", ev => document.querySelectorAll(".tag-sel").forEach(b => (b.checked = ev.target.checked)));
  $("bulk-move").addEventListener("click", async () => {
    const keys = [...document.querySelectorAll(".tag-sel:checked")].map(b => b.value);
    if (!keys.length) { flash("Select merchants first.", true); return; }
    try { await assign(keys, $("bulk-target").value); flash(`Moved ${keys.length} merchant(s).`); } catch (e) { flash(e.message, true); }
  });
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (state.series) { drawTrend(); drawMtd(); } }, 200); });

  (async () => {
    const fromHash = () => { const t = document.querySelector(`[data-tab="${location.hash.slice(1)}"]`); if (t) t.click(); };
    window.addEventListener("hashchange", fromHash); fromHash();
    try { await loadOverview(); await loadSeries(); } catch (e) { flash(e.message, true); }
  })();
})();
