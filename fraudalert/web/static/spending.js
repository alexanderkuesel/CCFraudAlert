// Spend historian: tag browser (category = device, merchant = tag), single-pen trend, month-to-date vs budget.
// Plain SVG. All names come from bank emails, so text is inserted with textContent only.
(function () {
  "use strict";
  const { h, money, longMonth, signed, api, MIN_PACE_DAYS, sparkline, statusBadge } = window.FTA;
  const $ = id => document.getElementById(id);
  const state = { pen: { type: "all" }, bucket: "day", days: 90, month: null, overview: null, series: null, open: new Set() };
  let currency = "";
  const gauge = d => window.FTA.gauge(d, currency);

  function flash(text, error = false) {
    const m = $("hist-msg"); m.textContent = text; m.hidden = false; m.classList.toggle("error", error);
    clearTimeout(flash.t); flash.t = setTimeout(() => (m.hidden = true), 4000);
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
    await loadExpenses();
  }

  // ---------- fixed expenses ----------
  const FX_FIELDS = ["name", "amount", "currency", "category_id", "day_of_month", "start_month", "end_month", "note"];
  function fxInputs(e) {
    const mk = (name, attrs = {}) => { const i = h("input"); i.name = name; Object.assign(i, attrs); return i; };
    const cat = h("select"); cat.name = "category_id"; fillCategorySelect(cat, e.category_id);
    return {
      name: mk("name", { value: e.name, maxLength: 128 }), amount: mk("amount", { value: e.amount, inputMode: "decimal" }),
      currency: mk("currency", { value: e.currency, maxLength: 3, className: "fx-cur" }), category_id: cat,
      day_of_month: mk("day_of_month", { type: "number", min: 1, max: 31, value: e.day_of_month, className: "fx-day" }),
      start_month: mk("start_month", { type: "month", value: e.start_month }),
      end_month: mk("end_month", { type: "month", value: e.end_month || "" }), note: mk("note", { value: e.note || "" }),
    };
  }
  const fxValues = inputs => Object.fromEntries(FX_FIELDS.map(k => [k, inputs[k].value]));
  async function afterExpenseChange(msg) {
    flash(msg); await loadOverview(); if (state.series) await loadSeries();
  }
  async function loadExpenses() {
    const list = await api("/api/spending/expenses"), tbody = $("fx-table").tBodies[0];
    tbody.replaceChildren();
    for (const e of list) {
      const r = tbody.insertRow(), inputs = fxInputs(e);
      for (const k of FX_FIELDS) {
        inputs[k].setAttribute("aria-label", `${k.replace(/_/g, " ")} for ${e.name}`);
        r.insertCell().append(inputs[k]);
      }
      const home = r.insertCell(); home.className = "num"; home.textContent = money(e.home_amount);
      const act = r.insertCell(); act.className = "nowrap";
      const save = h("button", "Save", "btn btn-sm"), del = h("button", "Delete", "link danger");
      save.addEventListener("click", async () => {
        try { await api(`/api/spending/expenses/${e.id}`, { method: "PATCH", body: JSON.stringify(fxValues(inputs)) });
          await afterExpenseChange(`Saved “${inputs.name.value}”.`); } catch (err) { flash(err.message, true); }
      });
      del.addEventListener("click", async () => {
        if (!confirm(`Delete “${e.name}”? It disappears from every month, past ones included. To stop it from now on, set “Until” instead.`)) return;
        try { await api(`/api/spending/expenses/${e.id}`, { method: "DELETE" });
          if (state.pen.type === "tag" && state.pen.key === e.key) state.pen = { type: "all" };
          await afterExpenseChange(`Deleted “${e.name}”.`); } catch (err) { flash(err.message, true); }
      });
      act.append(save, del);
    }
    if (!list.length) { const c = tbody.insertRow().insertCell(); c.colSpan = 10; c.className = "empty"; c.textContent = "No fixed expenses yet. Add one below."; }
    const nowMonth = state.overview.month.slice(0, 7);
    const active = list.filter(e => e.start_month <= nowMonth && (!e.end_month || e.end_month >= nowMonth));
    $("fx-total").textContent = list.length
      ? `${active.length} active this month, ${money(active.reduce((a, e) => a + e.home_amount, 0))} ${currency} in total.` : "";
    const row = document.querySelector(".fx-new");
    const sel = row.querySelector('[name="category_id"]'); fillCategorySelect(sel, sel.value === "" || !sel.value ? null : sel.value);
    const cur = row.querySelector('[name="currency"]'); if (!cur.value) cur.value = currency;
    const start = row.querySelector('[name="start_month"]'); if (!start.value) start.value = nowMonth;
  }
  $("fx-add").addEventListener("click", async () => {
    const row = document.querySelector(".fx-new"), values = {};
    row.querySelectorAll("[name]").forEach(i => (values[i.name] = i.value));
    try { await api("/api/spending/expenses", { method: "POST", body: JSON.stringify(values) });
      row.querySelectorAll('[name="name"],[name="amount"],[name="note"],[name="end_month"]').forEach(i => (i.value = ""));
      await afterExpenseChange(`Added “${values.name}”.`); } catch (err) { flash(err.message, true); }
  });

  function renderKpis() {
    const o = state.overview, t = o.total, box = $("hist-kpis");
    box.replaceChildren();
    const kpi = (label, value, sub) => {
      const k = h("div", null, "kpi"); k.append(h("span", label), h("b", value));
      if (sub) k.append(h("small", sub)); box.append(k);
    };
    kpi("Spent this month", `${money(t.mtd, 0)} ${currency}`,
      t.budget ? `budgeted categories: ${money(t.budgeted_mtd, 0)} of ${money(t.budget, 0)}` : "no budgets set");
    if (t.projected != null) kpi("Month-end forecast", `${money(t.projected, 0)} ${currency}`,
      t.expected != null ? `${signed(t.mtd - t.expected)} vs expected by today` : "at this month's pace");
    else kpi("Month-end forecast", "—", `from day ${MIN_PACE_DAYS}, or once a full month is on record`);
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
      if (d.assigned_by === "manual") name.append(h("span", "fixed", "tb-you"));
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
    if (state.month) q.set("month", state.month);
    $("chart-trend").style.opacity = .5; $("chart-mtd").style.opacity = .5; // refetch keeps the frame
    state.series = await api("/api/spending/series?" + q);
    $("chart-trend").style.opacity = 1; $("chart-mtd").style.opacity = 1;
    const s = state.series;
    $("pen-title").textContent = s.label;
    $("pen-path").textContent = p.type === "tag" ? `${p.key.startsWith("manual:") ? "Fixed expense" : "Tag"} · device: ${s.device}` : p.type === "device" ? "Device (category)" : "All devices";
    const move = $("pen-move"); move.hidden = p.type !== "tag";
    if (p.type === "tag") fillCategorySelect($("move-select"), currentCategoryOf(p.key));
    drawTrend(); drawMtd(); drawHist(); drawTable();
  }

  function drawTrend() {
    const s = state.series;
    const { anyFixed } = window.FTA.bars($("chart-trend"), { points: s.points, bucket: s.bucket, budget: s.budget, currency });
    $("trend-legend").hidden = !anyFixed;
  }

  function drawMtd() {
    const m = state.series.mtd;
    const r = window.FTA.runningTotal($("chart-mtd"), m, { currency });
    $("mtd-month").textContent = longMonth(m.month);
    $("mtd-next").disabled = m.current;
    $("key-forecast").hidden = !r.hasForecast;
    $("mtd-basis").textContent = r.basis;
    $("mtd-sub").textContent = r.summary;
    $("mtd-table").replaceChildren(window.FTA.runningTotalTable(m, currency));
  }

  // Expected vs actual by month; clicking a month opens its running total.
  function drawHist() {
    const s = state.series;
    window.FTA.history($("chart-hist"), s.history, {
      currency, selectedMonth: s.mtd.month,
      onPick: x => { state.month = x.current ? null : x.month; loadSeries(); },
    });
    $("hist-table").replaceChildren(window.FTA.historyTable(s.history, currency));
  }

  function drawTable() {
    const s = state.series, t = h("table");
    const head = t.createTHead().insertRow();
    ["Period", `Spend (${currency})`, "Transactions"].forEach(x => head.append(h("th", x)));
    const body = t.createTBody();
    [...s.points].reverse().forEach(p => {
      const r = body.insertRow();
      r.insertCell().textContent = s.bucket === "month" ? new Date(p.start + "T12:00:00").toLocaleDateString(undefined, { month: "long", year: "numeric" })
        : new Date(p.start + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
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
        r.insertCell().append(h("span", t.assigned_by === "user" ? "you" : t.assigned_by === "manual" ? "fixed" : "auto",
          t.assigned_by === "auto" ? "muted" : "tb-you"));
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
    state.bucket = b.dataset.bucket; syncRanges(); loadSeries();
  }));
  // Months need at least a year of range; 30 or 90 days would show one to three bars.
  function syncRanges() {
    const month = state.bucket === "month";
    if (month && state.days > 0 && state.days < 365) state.days = 365;
    document.querySelectorAll("[data-days]").forEach(x => {
      const d = Number(x.dataset.days);
      x.disabled = month && d > 0 && d < 365;
      x.setAttribute("aria-pressed", String(d === state.days));
    });
  }
  document.querySelectorAll("[data-days]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("[data-days]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    state.days = Number(b.dataset.days); loadSeries();
  }));
  document.querySelectorAll("[data-tab]").forEach(a => a.addEventListener("click", ev => {
    ev.preventDefault();
    document.querySelectorAll("[data-tab]").forEach(x => x.setAttribute("aria-selected", String(x === a)));
    for (const t of ["trends", "manage", "fixed"]) $("tab-" + t).hidden = a.dataset.tab !== t;
    history.replaceState(null, "", "#" + a.dataset.tab);
    if (a.dataset.tab === "trends" && state.series) { drawTrend(); drawMtd(); drawHist(); }
  }));
  function shiftMonth(by) {
    const cur = (state.series.mtd.month).slice(0, 7), [y, mo] = cur.split("-").map(Number);
    const d = new Date(y, mo - 1 + by, 1), next = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    state.month = next >= state.overview.month.slice(0, 7) ? null : next; loadSeries();
  }
  $("mtd-prev").addEventListener("click", () => shiftMonth(-1));
  $("mtd-next").addEventListener("click", () => shiftMonth(1));
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
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (state.series) { drawTrend(); drawMtd(); drawHist(); } }, 200); });

  (async () => {
    const fromHash = () => { const t = document.querySelector(`[data-tab="${location.hash.slice(1)}"]`); if (t) t.click(); };
    window.addEventListener("hashchange", fromHash); fromHash();
    try { await loadOverview(); await loadSeries(); } catch (e) { flash(e.message, true); }
  })();
})();
