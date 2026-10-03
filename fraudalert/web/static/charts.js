// Shared chart kit for the Overview and Spending pages: plain SVG, no dependencies.
// Every label that can come from a bank email is inserted with textContent only.
(function () {
  "use strict";
  const SVG = "http://www.w3.org/2000/svg";

  // ---------- small helpers ----------
  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  };
  const h = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
  const money = (v, dp = 2) => (v || 0).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const compact = v => Math.abs(v) >= 1e6 ? (v / 1e6).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "M"
    : Math.abs(v) >= 1000 ? (v / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "K" : money(v, 0);
  const monthName = iso => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short" });
  const dayLabel = iso => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const longMonth = iso => new Date(iso.slice(0, 7) + "-01T12:00:00").toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const signed = v => (v >= 0 ? "+" : "−") + money(Math.abs(v), 0);
  const pctText = (v, base) => { const p = Math.round(Math.abs(v) / base * 100); return p ? `${v >= 0 ? "+" : "−"}${p}%` : "0%"; };
  const pctOf = (v, base) => base ? ` (${pctText(v, base)})` : "";
  const STATUS = { hi: "HI", hihi: "HIHI" };
  const MIN_PACE_DAYS = 7; // without history, a straight-line projection from the first few days of a month is noise
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 4 ? 4 : n <= 6 ? 6 : n <= 8 ? 8 : 10) * p; // a quarter of each is a clean tick
  }
  async function api(url, opts = {}) {
    const r = await fetch(url, { headers: { "content-type": "application/json" }, ...opts });
    const body = r.status === 204 ? {} : await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `request failed (${r.status})`);
    return body;
  }
  // Tick labels carry the year on the first tick and whenever it changes ("Jan '26").
  function tickLabel(iso, bucket, prevIso) {
    const d = new Date(iso + "T12:00:00"), yr = ` '${String(d.getFullYear()).slice(2)}`;
    const newYear = !prevIso || prevIso.slice(0, 4) !== iso.slice(0, 4);
    return (bucket === "month" ? monthName(iso) : dayLabel(iso)) + (newYear ? yr : "");
  }

  // ---------- tooltip: one floating element for the whole page ----------
  let tipEl = null;
  function showTip(ev, lines) {
    if (!tipEl) { tipEl = h("div", null, "chart-tip"); tipEl.setAttribute("role", "status"); document.body.append(tipEl); }
    tipEl.replaceChildren(); lines.filter(Boolean).forEach((l, i) => tipEl.append(h(i ? "div" : "b", l)));
    tipEl.hidden = false;
    const w = tipEl.offsetWidth, ht = tipEl.offsetHeight;
    tipEl.style.left = Math.max(4, Math.min(ev.clientX + 14, innerWidth - w - 8)) + "px";
    tipEl.style.top = (ev.clientY - ht - 12 < 4 ? ev.clientY + 16 : ev.clientY - ht - 12) + "px";
  }
  const hideTip = () => { if (tipEl) tipEl.hidden = true; };

  // ---------- frame & axes ----------
  function frame(container, height) {
    container.replaceChildren();
    const W = Math.max(container.clientWidth, 280), H = height, m = { l: 48, r: 12, t: 14, b: 26 };
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
    const below = cls !== "hihi" && y + 13 < f.m.t + f.ih; // HI sits under its line unless that hits the axis
    el("text", { x: below || cls === "hihi" ? f.W - f.m.r - 2 : f.m.l + 4, y: below ? y + 13 : y - 4, class: "limit-label",
      "text-anchor": below || cls === "hihi" ? "end" : "start" }, f.svg).textContent = label;
  }
  // A bar with a 4px rounded data end, square at the baseline.
  function roundTop(svg, x, yTop, w, hgt, cls) {
    const r = Math.min(4, hgt, w / 2);
    return el("path", { class: cls, d: `M${x},${yTop + hgt}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + w - r}Q${x + w},${yTop} ${x + w},${yTop + r}V${yTop + hgt}Z` }, svg);
  }

  // ---------- small indicators ----------
  // Sparkline: monthly values, de-emphasised line, the latest as the end dot.
  function sparkline(values, w = 72, hgt = 20) {
    const s = el("svg", { width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, class: "spark", "aria-hidden": "true" });
    const max = Math.max(...values, 1), step = (w - 6) / Math.max(values.length - 1, 1);
    const pts = values.map((v, i) => [3 + i * step, hgt - 3 - (v / max) * (hgt - 6)]);
    el("polyline", { points: pts.map(p => p.join(",")).join(" "), class: "spark-line" }, s);
    const [x, y] = pts[pts.length - 1];
    el("circle", { cx: x, cy: y, r: 2.5, class: "spark-end" }, s);
    return s;
  }
  // Moving-bar indicator: fill = month-to-date vs budget on a 0-120% track, ticks at HI (80%) and the budget.
  function gauge(d, currency) {
    const wrap = h("span", null, "gauge" + (d.status === "hi" || d.status === "hihi" ? " " + d.status : ""));
    if (!d.budget) { wrap.classList.add("none"); wrap.title = "No budget set"; return wrap; }
    const fill = h("span", null, "gauge-fill");
    fill.style.width = Math.min(d.pct / 1.2, 1) * 100 + "%";
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

  // ---------- charts ----------
  // Spend per period: card spending from the baseline, fixed expenses stacked on top in a lighter tone.
  // With a monthly budget, months over HI/HIHI are coloured and the limits drawn. Returns whether any
  // period had fixed expenses (so the caller can show the legend).
  function bars(container, { points, bucket, budget = null, currency, height = 240 }) {
    const f = frame(container, height), monthly = bucket === "month" && budget;
    let prevTick = null;
    const max = niceMax(Math.max(...points.map(p => p.value), monthly ? budget * 1.05 : 0, 1));
    yAxis(f, max);
    const slot = f.iw / Math.max(points.length, 1), bw = Math.max(Math.min(24, slot - 2), 1); // <= 24px, 2px gap
    const every = Math.ceil(points.length / Math.max(Math.floor(f.iw / 64), 1));
    points.forEach((p, i) => {
      const x = f.m.l + i * slot + (slot - bw) / 2, hgt = (p.value / max) * f.ih, y = f.m.t + f.ih - hgt;
      const over = monthly && p.value >= budget ? " hihi" : monthly && p.value >= budget * 0.8 ? " hi" : "";
      const fixed = p.fixed || 0, cardH = ((p.value - fixed) / max) * f.ih, fixedH = hgt - cardH;
      const gap = cardH > 0 && fixedH > 0 ? Math.min(2, fixedH) : 0;
      if (cardH > 0) {
        if (fixedH > 0) el("rect", { class: "bar" + over, x, y: f.m.t + f.ih - cardH, width: bw, height: cardH }, f.svg);
        else roundTop(f.svg, x, y, bw, hgt, "bar" + over);
      }
      if (fixedH - gap > 0) roundTop(f.svg, x, y, bw, fixedH - gap, "bar fixed");
      const hit = el("rect", { x: f.m.l + i * slot, y: f.m.t, width: slot, height: f.ih, class: "hit" }, f.svg);
      const when = bucket === "month" ? longMonth(p.start) : bucket === "week" ? "Week of " + dayLabel(p.start) : dayLabel(p.start);
      const lines = [`${money(p.value)} ${currency}`, when, `${p.count} card transaction${p.count === 1 ? "" : "s"}`];
      if (fixed > 0) lines.push(`incl. ${money(fixed, 0)} fixed expenses`);
      if (monthly) lines.push(`${Math.round(p.value / budget * 100)}% of budget` + (over ? ` · ${STATUS[over.trim()]}` : ""));
      hit.addEventListener("pointermove", ev => showTip(ev, lines)); hit.addEventListener("pointerleave", hideTip);
      if (i % every === 0) {
        el("text", { x: f.m.l + i * slot + slot / 2, y: f.H - 8, class: "tick", "text-anchor": "middle" }, f.svg)
          .textContent = tickLabel(p.start, bucket, prevTick);
        prevTick = p.start;
      }
    });
    if (monthly) { limitLine(f, budget * 0.8, max, "hi", "HI 80%"); limitLine(f, budget, max, "hihi", "HIHI budget"); }
    return { anyFixed: points.some(p => p.fixed > 0) };
  }

  // Running total for a month: actual against the expected path (your usual month plus fixed expenses),
  // the budget limits, and the forecast to month end. Returns a one-line summary and what was drawn.
  function runningTotal(container, m, { currency, height = 230 }) {
    const f = frame(container, height);
    const n = m.days_in_month, cum = m.cumulative, today = cum.length, last = cum[today - 1] || 0, exp = m.expected;
    const forecast = m.current && m.projected != null && today < n
      ? Array.from({ length: n - today + 1 }, (_, i) => exp
        ? last + exp[today - 1 + i] - exp[today - 1]
        : last + (m.projected - last) * i / (n - today))
      : null;
    const max = niceMax(Math.max(last, m.projected || 0, exp ? exp[n - 1] : 0, m.budget ? m.budget * 1.05 : 0, 1));
    yAxis(f, max);
    const X = d => f.m.l + (d - 1) / Math.max(n - 1, 1) * f.iw, Y = v => f.m.t + f.ih - (v / max) * f.ih;
    for (const d of [1, 8, 15, 22, n]) el("text", { x: X(d), y: f.H - 8, class: "tick", "text-anchor": "middle" }, f.svg).textContent = String(d);
    if (m.budget) { limitLine(f, m.budget * 0.8, max, "hi", "HI 80%"); limitLine(f, m.budget, max, "hihi", "HIHI budget"); }
    if (exp) {
      el("polyline", { class: "expected", points: exp.map((v, i) => `${X(i + 1)},${Y(v)}`).join(" ") }, f.svg);
      if (m.current && today < n) { // a finished month's numbers are in the summary; this would sit on the actual label
        el("text", { x: f.W - f.m.r - 2, y: Y(exp[n - 1]) - 6, class: "end-label muted-label", "text-anchor": "end" }, f.svg)
          .textContent = `expected ${money(exp[n - 1], 0)}`;
      }
    }
    if (forecast) el("polyline", { class: "projection", points: forecast.map((v, i) => `${X(today + i)},${Y(v)}`).join(" ") }, f.svg);
    if (today) {
      if (exp) el("path", { class: "gap", d: `M${X(1)},${Y(cum[0])}` + cum.map((v, i) => `L${X(i + 1)},${Y(v)}`).join("") +
        exp.slice(0, today).map((v, i) => `L${X(today - i)},${Y(exp[today - 1 - i])}`).join("") + "Z" }, f.svg);
      el("polyline", { class: "pen", points: cum.map((v, i) => `${X(i + 1)},${Y(v)}`).join(" ") }, f.svg);
      el("circle", { class: "pen-dot" + (m.status === "hi" || m.status === "hihi" ? " " + m.status : ""), cx: X(today), cy: Y(last), r: 4.5 }, f.svg);
      el("text", { x: Math.min(X(today) + 8, f.W - f.m.r - 60), y: Y(last) - 8, class: "end-label" }, f.svg).textContent = money(last, 0);
    }
    const cross = el("line", { class: "cross", y1: f.m.t, y2: f.m.t + f.ih, visibility: "hidden" }, f.svg);
    const hit = el("rect", { x: f.m.l, y: f.m.t, width: f.iw, height: f.ih, class: "hit area-hit" }, f.svg); // crosshair, no wash
    hit.addEventListener("pointermove", ev => {
      const box = f.svg.getBoundingClientRect(), d = Math.round((ev.clientX - box.left - f.m.l) / f.iw * (n - 1)) + 1;
      const day = Math.min(Math.max(d, 1), n); cross.setAttribute("x1", X(day)); cross.setAttribute("x2", X(day)); cross.setAttribute("visibility", "visible");
      const date = dayLabel(m.month.slice(0, 8) + String(day).padStart(2, "0")), e = exp ? exp[day - 1] : null, lines = [];
      if (day <= today) {
        lines.push(`${money(cum[day - 1])} ${currency}`, `Actual to ${date}`);
        if (e != null) lines.push(`Expected ${money(e, 0)} · ${signed(cum[day - 1] - e)}${pctOf(cum[day - 1] - e, e)}`);
        if (m.budget) lines.push(`${Math.round(cum[day - 1] / m.budget * 100)}% of budget`);
      } else {
        lines.push(forecast ? `${money(forecast[day - today], 0)} ${currency}` : date, forecast ? `Forecast by ${date}` : "No forecast yet");
        if (e != null) lines.push(`Expected ${money(e, 0)}`);
      }
      showTip(ev, lines);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });

    const bits = [];
    if (m.current) {
      bits.push(`${money(last, 0)} ${currency} so far`);
      if (exp) bits.push(`${signed(last - exp[today - 1])}${pctOf(last - exp[today - 1], exp[today - 1])} vs expected by today`);
      bits.push(m.projected != null ? `forecast ${money(m.projected, 0)} by month end` : `forecast from day ${MIN_PACE_DAYS}`);
    } else {
      bits.push(`${money(last, 0)} ${currency} spent`);
      if (exp) bits.push(`expected ${money(exp[n - 1], 0)}, ${signed(last - exp[n - 1])}${pctOf(last - exp[n - 1], exp[n - 1])}`);
    }
    if (m.budget) bits.push(`budget ${money(m.budget, 0)} (${Math.round(last / m.budget * 100)}% used)`);
    const basis = exp ? `(your usual month: average of ${m.basis.length === 1 ? longMonth(m.basis[0]) : m.basis.length + " months before"}` +
      (m.fixed ? `, plus ${money(m.fixed, 0)} fixed)` : ")") : "(needs a complete earlier month of history)";
    return { summary: bits.join(" · "), basis, hasForecast: !!forecast };
  }
  function runningTotalTable(m, currency) {
    const t = h("table"), head = t.createTHead().insertRow();
    ["Day", `Actual (${currency})`, `Expected (${currency})`, "Difference"].forEach(x => head.append(h("th", x)));
    const body = t.createTBody();
    m.cumulative.forEach((v, i) => {
      const r = body.insertRow(), e = m.expected ? m.expected[i] : null;
      r.insertCell().textContent = dayLabel(m.month.slice(0, 8) + String(i + 1).padStart(2, "0"));
      for (const x of [money(v), e == null ? "—" : money(e), e == null ? "—" : signed(v - e)]) { const c = r.insertCell(); c.className = "num"; c.textContent = x; }
    });
    return t;
  }

  // Expected vs actual by month: a bar per month (actual) with a tick for what was expected.
  function history(container, hist, { currency, selectedMonth = null, onPick = null, height = 200 }) {
    const f = frame(container, height);
    const max = niceMax(Math.max(...hist.map(x => Math.max(x.actual, x.expected || 0, x.projected || 0)), 1));
    yAxis(f, max);
    const slot = f.iw / hist.length, bw = Math.min(24, slot - 2), Y = v => f.m.t + f.ih - (v / max) * f.ih;
    hist.forEach((x, i) => {
      const cx = f.m.l + i * slot + slot / 2, xb = cx - bw / 2, y = Y(x.actual), hgt = f.m.t + f.ih - y;
      if (hgt > 0) roundTop(f.svg, xb, y, bw, hgt, "bar" + (x.current ? " partial" : ""));
      if (x.expected != null) el("line", { class: "exp-tick", x1: xb - 6, x2: xb + bw + 6, y1: Y(x.expected), y2: Y(x.expected) }, f.svg);
      if (x.current && x.projected != null) el("line", { class: "exp-tick forecast", x1: xb - 6, x2: xb + bw + 6, y1: Y(x.projected), y2: Y(x.projected) }, f.svg);
      el("text", { x: cx, y: f.H - 8, class: "tick" + (selectedMonth && selectedMonth.startsWith(x.month) ? " sel" : ""), "text-anchor": "middle" }, f.svg)
        .textContent = tickLabel(x.month + "-01", "month", i ? hist[i - 1].month + "-01" : null);
      const ref = x.current ? x.projected : x.actual;
      if (x.expected && ref != null) {
        const top = Math.min(y, Y(x.expected), x.current && x.projected != null ? Y(x.projected) : y);
        el("text", { x: cx, y: Math.max(top - 8, f.m.t + 8), class: "delta-label", "text-anchor": "middle" }, f.svg)
          .textContent = pctText(ref - x.expected, x.expected);
      }
      const lines = [longMonth(x.month), `${x.current ? "So far" : "Actual"} ${money(x.actual, 0)} ${currency}`];
      if (x.current && x.projected != null) lines.push(`Forecast ${money(x.projected, 0)}`);
      lines.push(x.expected != null ? `Expected ${money(x.expected, 0)}` + (ref != null ? ` · ${signed(ref - x.expected)}${pctOf(ref - x.expected, x.expected)}` : "") : "No expectation (not enough history)");
      const hit = el("rect", { x: f.m.l + i * slot, y: f.m.t, width: slot, height: f.ih, class: "hit" + (onPick ? " clickable" : "") }, f.svg);
      hit.addEventListener("pointermove", ev => showTip(ev, lines)); hit.addEventListener("pointerleave", hideTip);
      if (onPick) hit.addEventListener("click", () => onPick(x));
    });
  }
  function historyTable(hist, currency) {
    const t = h("table"), head = t.createTHead().insertRow();
    ["Month", `Actual (${currency})`, `Expected (${currency})`, "Difference", "Forecast"].forEach(x => head.append(h("th", x)));
    const body = t.createTBody();
    [...hist].reverse().forEach(x => {
      const r = body.insertRow(), ref = x.current ? x.projected : x.actual;
      r.insertCell().textContent = longMonth(x.month) + (x.current ? " (so far)" : "");
      for (const v of [money(x.actual), x.expected == null ? "—" : money(x.expected),
        x.expected == null || ref == null ? "—" : signed(ref - x.expected) + pctOf(ref - x.expected, x.expected),
        x.current && x.projected != null ? money(x.projected) : ""]) { const c = r.insertCell(); c.className = "num"; c.textContent = v; }
    });
    return t;
  }

  window.FTA = {
    el, h, money, compact, monthName, dayLabel, longMonth, signed, pctText, pctOf, niceMax, api, tickLabel,
    STATUS, MIN_PACE_DAYS, showTip, hideTip, sparkline, gauge, statusBadge,
    bars, runningTotal, runningTotalTable, history, historyTable,
  };
})();
