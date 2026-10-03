// Overview: this month against your usual month, categories, the last 12 months and top merchants.
// Built on the shared chart kit (charts.js). Merchant names come from bank emails: textContent only.
(function () {
  "use strict";
  const { h, money, signed, pctText, longMonth, api, sparkline, statusBadge, MIN_PACE_DAYS } = window.FTA;
  const $ = id => document.getElementById(id);
  let overview = null, series = null, currency = "";

  function kpi(label, value, sub, title) {
    const k = h("div", null, "kpi");
    if (title) k.title = title;
    k.append(h("span", label), h("b", value));
    if (sub) k.append(h("small", sub));
    $("ov-kpis").insertBefore(k, $("ov-kpis").querySelector(".kpi-alarms"));
  }

  function renderKpis() {
    const t = overview.total;
    kpi("Spent this month", `${money(t.mtd, 0)} ${currency}`,
      t.expected != null ? `${signed(t.mtd - t.expected)} vs your usual by today` : `day ${overview.day_of_month} of ${overview.days_in_month}`);
    if (t.projected != null) {
      kpi("Month-end forecast", `${money(t.projected, 0)} ${currency}`,
        t.last_month ? `${pctText(t.projected - t.last_month, t.last_month)} vs last month` : "based on your usual month");
    } else {
      kpi("Month-end forecast", "—", `from day ${MIN_PACE_DAYS}, or once a full month is on record`);
    }
    kpi("Last month", `${money(t.last_month, 0)} ${currency}`);
    const budgeted = overview.devices.filter(d => d.budget);
    const over = budgeted.filter(d => d.status === "hihi").length, near = budgeted.filter(d => d.status === "hi").length;
    kpi("Budgets", budgeted.length ? `${budgeted.length - over - near} of ${budgeted.length} on track` : "None set",
      budgeted.length ? (over || near ? `${over} over · ${near} near the limit` : `${money(t.budgeted_mtd, 0)} of ${money(t.budget, 0)} used`)
        : "set them under Spending → Categories");
  }

  function renderMonth() {
    const m = series.mtd, r = window.FTA.runningTotal($("ov-month"), m, { currency, height: 250 });
    $("ov-month-sub").textContent = r.summary;
    $("ov-basis").textContent = r.basis;
    $("ov-key-forecast").hidden = !r.hasForecast;
    $("ov-month-table").replaceChildren(window.FTA.runningTotalTable(m, currency));
  }

  // Categories this month: budget indicator when there is a budget, otherwise the 6-month sparkline.
  function renderCategories() {
    const list = $("ov-cats"); list.replaceChildren();
    const rows = overview.devices.filter(d => d.mtd > 0 || d.budget).sort((a, b) => b.mtd - a.mtd);
    if (!rows.length) { list.append(h("li", "No spending this month yet.", "muted")); return; }
    for (const d of rows) {
      const li = h("li", null, "cat-row");
      const name = h("a", d.name, "cat-name"); name.href = "/spending";
      const value = h("span", `${money(d.mtd, 0)}`, "cat-val");
      const ind = h("span", null, "cat-ind");
      if (d.budget) {
        ind.append(window.FTA.gauge(d, currency));
        const b = statusBadge(d.status); if (b) ind.append(b);
        ind.title = `${Math.round(d.pct * 100)}% of ${money(d.budget, 0)} ${currency}`;
      } else {
        ind.append(sparkline(d.spark, 64, 18));
        ind.title = "Last 6 months (no budget set)";
      }
      li.append(name, ind, value);
      list.append(li);
    }
  }

  function renderYear() {
    const { anyFixed } = window.FTA.bars($("ov-year"), { points: series.points, bucket: "month", currency, height: 220 });
    $("ov-year-legend").hidden = !anyFixed;
    const t = h("table"), head = t.createTHead().insertRow();
    ["Month", `Spend (${currency})`, "of which fixed", "Card transactions"].forEach(x => head.append(h("th", x)));
    const body = t.createTBody();
    [...series.points].reverse().forEach(p => {
      const r = body.insertRow();
      r.insertCell().textContent = longMonth(p.start);
      for (const v of [money(p.value), money(p.fixed || 0), String(p.count)]) { const c = r.insertCell(); c.className = "num"; c.textContent = v; }
    });
    $("ov-year-table").replaceChildren(t);
  }

  function renderTop() {
    const list = $("ov-top"); list.replaceChildren();
    const tags = overview.devices.flatMap(d => d.tags.map(t => ({ ...t, category: d.name }))).filter(t => t.mtd > 0)
      .sort((a, b) => b.mtd - a.mtd).slice(0, 8);
    if (!tags.length) { list.append(h("li", "No spending this month yet.", "muted")); return; }
    const top = tags[0].mtd;
    for (const t of tags) {
      const li = h("li", null, "top-row");
      const label = h("span", null, "top-name");
      label.append(h("b", t.name), h("span", t.assigned_by === "manual" ? `${t.category} · fixed` : t.category, "muted small"));
      const bar = h("span", null, "top-bar"); const fill = h("span"); fill.style.width = (t.mtd / top) * 100 + "%"; bar.append(fill);
      li.append(label, bar, h("span", money(t.mtd, 0), "top-val"));
      list.append(li);
    }
  }

  function draw() { renderMonth(); renderYear(); }

  (async () => {
    try {
      [overview, series] = await Promise.all([api("/api/spending/overview"), api("/api/spending/series?bucket=month&days=365")]);
    } catch (e) {
      const m = $("ov-msg"); m.textContent = e.message; m.hidden = false; m.classList.add("error");
      return;
    }
    currency = overview.currency;
    const month = new Date(overview.month + "T12:00:00");
    document.querySelector('[data-bind="month-label"]').textContent =
      `${month.toLocaleDateString(undefined, { month: "long", year: "numeric" })} · day ${overview.day_of_month} of ${overview.days_in_month}`;
    renderKpis(); renderCategories(); renderTop(); draw();
    let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(draw, 200); });
  })();
})();
