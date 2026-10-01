// Network page: cards ↔ merchants force layout in plain SVG (no library).
// All text from the API is inserted with textContent (merchant names are untrusted email content).
(function () {
  "use strict";
  const SVG_NS = "http://www.w3.org/2000/svg";
  let W = 1000, H = 620; // layout space = the SVG's pixel size, so text renders at true size
  // ISA-18.2 alarm states. Colour is never the only cue: each state also has a glyph and a label.
  const STATES = {
    fraud: { glyph: "✕", label: "Fraud (acknowledged)" },
    p1: { glyph: "1", label: "Unacknowledged · High" },
    p2: { glyph: "2", label: "Unacknowledged · Medium" },
    p3: { glyph: "3", label: "Unacknowledged · Low" },
    legit: { glyph: "✓", label: "Acknowledged legit" },
    normal: { glyph: "", label: "Normal" },
  };
  const STATE_RANK = { fraud: 0, p1: 1, p2: 2, p3: 3, legit: 4, normal: 5 };
  const UNACK = new Set(["p1", "p2", "p3"]);

  const svg = document.getElementById("net-svg");
  const canvas = document.getElementById("net-canvas");
  const tooltip = document.getElementById("net-tooltip");
  const detail = document.getElementById("net-detail");
  const empty = document.getElementById("net-empty");
  const summary = document.getElementById("net-summary");
  const onlyAnomalies = document.getElementById("only-anomalies");
  const limitInput = document.getElementById("limit"), limitOut = document.getElementById("limit-out");
  const limitSummary = document.getElementById("limit-summary");
  let limit = null; // anomaly limit for the slider; first set from the alarm rule's threshold
  const tbody = document.querySelector("#net-table tbody");
  let data = null, currency = "", selected = null;

  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  };
  const html = (tag, text, cls) => {
    const e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  };
  const money = v => v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " " + currency;
  const date = iso => new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  const beyond = n => n.kind === "merchant" && (n.scores || []).filter(x => x >= limit).length;
  const isAnomalous = n => n.kind === "merchant" &&
    (n.state !== "normal" && n.state !== "legit" || n.new || n.foreign || beyond(n) > 0);
  // Halo = how unusual this merchant is overall: the share of its transactions the model rates in the
  // top ~15% of your history (score >= 0.85). Using the merchant's single highest score instead would light
  // up every merchant you use often. A single odd transaction is what the "beyond the limit" ring is for.
  const HALO_FROM = 0.85, HALO_MIN_SHARE = 0.25;
  const haloT = n => {
    const sc = n.scores || [];
    if (!sc.length) return 0;
    const share = sc.filter(x => x >= HALO_FROM).length / sc.length;
    return share >= HALO_MIN_SHARE ? share : 0;
  };

  // Deterministic PRNG so the same data lays out the same way on every load.
  function rng(seed) {
    return () => { seed |= 0; seed = (seed + 0x6D2B79F5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }

  function radius(n, maxTotal) {
    if (n.kind === "card") return 13;
    const r = 6 + 22 * Math.sqrt(Math.max(n.total, 0) / (maxTotal || 1));
    return n.state === "normal" ? r : Math.max(r, 11); // room for the state glyph
  }

  // Fruchterman–Reingold force layout with collision on each node's full footprint (body + rings).
  // Deterministic, and run to rest up front so the map doesn't wobble.
  function layout(nodes, edges) {
    const rand = rng(42);
    const N = nodes.length, iters = N > 400 ? 120 : N > 150 ? 220 : 400; // O(N²) per pass: fewer passes for big graphs
    const labelRoom = Math.min(150, W * 0.28); // labels sit to the right of nodes
    const k = Math.sqrt(((W - labelRoom) * H) / Math.max(N, 1)) * 0.6; // ideal edge length
    // Pull less along the frame's long side so a tall phone frame fills top to bottom.
    const aspect = H / Math.max(W - labelRoom, 1);
    const gx = 0.003 * k * Math.min(Math.max(aspect, 0.6), 1.6), gy = 0.003 * k / Math.min(Math.max(aspect, 0.6), 1.6);
    const cards = nodes.filter(n => n.kind === "card");
    nodes.forEach(n => { n.x = W / 2 + (rand() - 0.5) * W * 0.8; n.y = H / 2 + (rand() - 0.5) * H * 0.8; });
    cards.forEach((c, i) => {
      const a = (2 * Math.PI * i) / Math.max(cards.length, 1);
      const R = cards.length > 1 ? Math.min(W - labelRoom, H) * 0.25 : 0;
      c.x = (W - labelRoom) / 2 + R * Math.cos(a); c.y = H / 2 + R * Math.sin(a);
    });
    const byId = new Map(nodes.map(n => [n.id, n]));
    const links = edges.map(e => ({ s: byId.get(e.source), t: byId.get(e.target), e })).filter(l => l.s && l.t);
    for (let it = 0; it < iters; it++) {
      const temp = (W * 0.06) * (1 - it / iters) + 0.5;
      nodes.forEach(n => { n.dx = 0; n.dy = 0; });
      for (let i = 0; i < N; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < N; j++) {
          const b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y, d = Math.hypot(dx, dy);
          if (d < 0.01) { dx = rand() - 0.5; dy = rand() - 0.5; d = 0.5; }
          const minD = a.foot + b.foot + 14;
          const f = (k * k) / d + (d < minD ? (minD - d) * 8 : 0); // repulsion + hard collision
          a.dx += (dx / d) * f; a.dy += (dy / d) * f; b.dx -= (dx / d) * f; b.dy -= (dy / d) * f;
          // Labels run to the right: on the same row, keep the right node clear of the left one's label.
          const [l, r] = a.x <= b.x ? [a, b] : [b, a];
          if (Math.abs(a.y - b.y) < Math.max(a.foot, b.foot) + 12) {
            const need = l.foot + l.lw + r.foot + 10, gap = r.x - l.x;
            if (gap < need) {
              const push = (need - gap) * 4, lift = (a.y <= b.y ? -1 : 1) * push * 0.6;
              r.dx += push; l.dx -= push; a.dy += lift; b.dy -= lift; // slide apart, and off the shared row
            }
          }
        }
      }
      for (const l of links) {
        const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.hypot(dx, dy) || 0.01;
        const f = (d * d) / k;
        l.s.dx += (dx / d) * f; l.s.dy += (dy / d) * f; l.t.dx -= (dx / d) * f; l.t.dy -= (dy / d) * f;
      }
      for (const n of nodes) {
        n.dx += ((W - labelRoom) / 2 - n.x) * gx; n.dy += (H / 2 - n.y) * gy; // gravity, shaped to the frame
        const len = Math.hypot(n.dx, n.dy) || 1, step = Math.min(len, temp);
        n.x += (n.dx / len) * step; n.y += (n.dy / len) * step;
        // Stay inside the frame (1:1 scale, so text keeps its real size).
        n.x = Math.min(Math.max(n.x, n.foot + 8), W - labelRoom - n.foot);
        n.y = Math.min(Math.max(n.y, n.foot + 8), H - n.foot - 8);
      }
    }
    return links;
  }

  function fitViewBox(nodes) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of nodes) {
      x0 = Math.min(x0, n.x - n.foot - 12); y0 = Math.min(y0, n.y - n.foot - 12);
      x1 = Math.max(x1, n.x + n.foot + Math.min(150, W * 0.28)); y1 = Math.max(y1, n.y + n.foot + 12); // labels sit to the right
    }
    // Never zoom in past 1:1 (small graphs would blow up); pad out to the frame's size instead.
    let w = x1 - x0, h = y1 - y0;
    if (w < W) { x0 -= (W - w) / 2; w = W; }
    if (h < H) { y0 -= (H - h) / 2; h = H; }
    svg.setAttribute("viewBox", `${x0} ${y0} ${w} ${h}`);
  }

  function render() {
    W = Math.max(svg.clientWidth, 320); H = Math.max(svg.clientHeight, 300);
    for (const c of [...svg.childNodes]) if (c.nodeName !== "title") svg.removeChild(c);
    tooltip.hidden = true;
    const nodes = data.nodes, edges = data.edges;
    empty.hidden = nodes.length > 0;
    const merchants = nodes.filter(n => n.kind === "merchant");
    const maxTotal = Math.max(1, ...merchants.map(n => n.total));
    const topSpend = new Set([...merchants].sort((a, b) => b.total - a.total).slice(0, 10).map(n => n.id));
    const maxChars = W < 600 ? 14 : 26;
    nodes.forEach(n => {
      n.r = radius(n, maxTotal);
      // collision footprint includes the rings; reserve the "beyond limit" ring for anything that could cross it
      n.ringAt = n.r + (n.foreign ? 4 : 0) + (n.new ? 4 : 0) + 5;
      n.foot = n.kind === "merchant" && (n.max_score || 0) >= 0.8 ? n.ringAt + 2 : n.ringAt - 5;
      // Labels: every card and flagged/fraud/legit/new merchant, plus the biggest spenders. Others on hover.
      n.showLabel = n.kind === "card" || n.state !== "normal" || n.new || topSpend.has(n.id);
      n.text = n.label.length > maxChars ? n.label.slice(0, maxChars - 1) + "…" : n.label;
      n.lw = n.showLabel ? n.text.length * (n.kind === "card" ? 7.5 : 6.6) + 6 : 0; // approx. text width
    });
    const links = layout(nodes, edges);
    fitViewBox(nodes.length ? nodes : [{ x: W / 2, y: H / 2, r: 0 }]);

    const neighbours = new Map(nodes.map(n => [n.id, new Set([n.id])]));
    edges.forEach(e => { neighbours.get(e.source)?.add(e.target); neighbours.get(e.target)?.add(e.source); });

    const gEdges = el("g", {}, svg), gNodes = el("g", {}, svg), gLabels = el("g", {}, svg);
    const edgeEls = links.map(l => {
      const e = l.e;
      const cls = e.flagged && (l.t.state === "fraud" || UNACK.has(l.t.state)) ? `edge alarm s-${l.t.state}` : "edge";
      const line = el("line", { x1: l.s.x, y1: l.s.y, x2: l.t.x, y2: l.t.y, class: cls,
        "stroke-width": (1 + Math.log2(e.count)).toFixed(2) }, gEdges);
      return { line, s: l.s.id, t: l.t.id };
    });

    const nodeEls = new Map(), labelEls = new Map();
    for (const n of nodes) {
      const cls = n.kind === "card" ? "node card" : `node ${n.state}`;
      const g = el("g", { class: cls, transform: `translate(${n.x},${n.y})`, tabindex: "0", role: "button",
        "aria-label": ariaLabel(n) }, gNodes);
      el("circle", { r: Math.max(n.r + 10, 14), class: "hit" }, g); // hit target bigger than the mark
      if (n.kind === "card") {
        el("rect", { x: -n.r, y: -n.r, width: 2 * n.r, height: 2 * n.r, rx: 4, class: "body" }, g);
      } else {
        const t = haloT(n);
        if (t > 0) el("circle", { r: n.r + 5 + 11 * t, class: "halo", "fill-opacity": (0.15 + 0.35 * t).toFixed(2) }, g);
        if ((n.max_score || 0) >= 0.8) el("circle", { r: n.ringAt, class: "ring-limit" }, g);
        if (n.foreign) el("circle", { r: n.r + 4, class: "ring-foreign" }, g);
        if (n.new) el("circle", { r: n.r + (n.foreign ? 8 : 4), class: "ring-new" }, g);
        el("circle", { r: n.r, class: "body" }, g);
        if (STATES[n.state].glyph) {
          const glyph = el("text", { class: "glyph", "font-size": Math.max(9, Math.min(n.r * 1.1, 16)) }, g);
          glyph.textContent = STATES[n.state].glyph;
        }
      }
      const label = el("text", { x: n.x + n.foot + 5, y: n.y + 4,
        class: n.kind === "card" ? "label card-label" : "label" }, gLabels);
      label.textContent = n.text;
      label.style.display = n.showLabel ? "" : "none";
      label.dataset.always = n.showLabel ? "1" : "";
      nodeEls.set(n.id, g); labelEls.set(n.id, label);

      const focusOn = ev => { highlight(n.id); showTooltip(n, ev); };
      g.addEventListener("pointerenter", focusOn);
      g.addEventListener("pointermove", ev => showTooltip(n, ev));
      g.addEventListener("pointerleave", () => { highlight(selected); tooltip.hidden = true; });
      g.addEventListener("focus", ev => focusOn(ev));
      g.addEventListener("blur", () => { highlight(selected); tooltip.hidden = true; });
      g.addEventListener("click", () => select(n));
      g.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(n); } });
    }

    function highlight(id) {
      const near = id ? neighbours.get(id) : null;
      for (const [nid, g] of nodeEls) {
        const n = nodes.find(x => x.id === nid);
        const quiet = onlyAnomalies.checked && n.kind === "merchant" && !isAnomalous(n);
        g.classList.toggle("dim", !!near && !near.has(nid));
        g.classList.toggle("quiet", !near && quiet);
        const label = labelEls.get(nid);
        label.style.display = (near && near.has(nid)) || label.dataset.always ? "" : "none";
        label.classList.toggle("dim", !!near && !near.has(nid));
        label.classList.toggle("quiet", !near && quiet);
      }
      for (const e of edgeEls) {
        const on = near && (e.s === id || e.t === id);
        e.line.classList.toggle("dim", !!near && !on);
        const quiet = onlyAnomalies.checked && !e.line.classList.contains("alarm");
        e.line.classList.toggle("quiet", !near && quiet);
      }
      for (const [nid, g] of nodeEls) {
        g.classList.toggle("selected", nid === selected);
        const n = nodes.find(x => x.id === nid);
        g.classList.toggle("beyond", beyond(n) > 0);
        g.setAttribute("aria-label", ariaLabel(n));
      }
    }
    render.highlight = highlight;
    render.applyLimit = () => {
      highlight(selected);
      renderTable(merchants);
      const over = merchants.filter(n => beyond(n) > 0);
      const txns = over.reduce((a, n) => a + beyond(n), 0);
      limitSummary.textContent = `${over.length} merchant${over.length === 1 ? "" : "s"} · ${txns} transaction${txns === 1 ? "" : "s"} beyond ${limit.toFixed(2)}`;
      if (selected) showDetail(nodes.find(x => x.id === selected)); // keep a pinned node's numbers current
    };
    render.applyLimit();
    summary.textContent = `${nodes.filter(n => n.kind === "card").length} cards · ${merchants.length} merchants · `
      + `${merchants.filter(n => UNACK.has(n.state)).length} with unacknowledged alarms, ${merchants.filter(n => n.state === "fraud").length} fraud`;
  }

  function ariaLabel(n) {
    if (n.kind === "card") return `${n.label}: ${n.count} transactions, ${money(n.total)}`;
    const extra = [n.new && "new merchant", n.foreign && "foreign",
      beyond(n) > 0 && `${beyond(n)} transaction(s) beyond the anomaly limit`].filter(Boolean).join(", ");
    return `${n.label}: ${STATES[n.state].label}, ${n.count} transactions, ${money(n.total)}${extra ? ", " + extra : ""}`;
  }

  function showTooltip(n, ev) {
    tooltip.replaceChildren();
    tooltip.appendChild(html("b", money(n.total)));
    tooltip.appendChild(html("span", n.label, "tt-name"));
    const lines = n.kind === "card"
      ? [`${n.count} transactions`]
      : [STATES[n.state].label, `${n.count} transaction${n.count === 1 ? "" : "s"} · ${n.cards} card${n.cards === 1 ? "" : "s"}`,
         `First purchase ${date(n.first_seen)}${n.new ? " (new)" : ""}`, n.foreign ? "Foreign" : null,
         haloT(n) ? `${Math.round(haloT(n) * 100)}% of its transactions look unusual (≥ ${HALO_FROM})` : null,
         n.max_score != null ? `Highest anomaly score ${n.max_score.toFixed(2)}` +
           (beyond(n) ? ` · ${beyond(n)} beyond the ${limit.toFixed(2)} limit` : "") : null];
    lines.filter(Boolean).forEach(t => tooltip.appendChild(html("div", t)));
    if (n.reasons && n.reasons.length) {
      tooltip.appendChild(html("div", "Why unusual:", "tt-why-h"));
      n.reasons.forEach(r => tooltip.appendChild(html("div", "• " + r, "tt-why")));
    }
    tooltip.hidden = false;
    const box = canvas.getBoundingClientRect();
    let x, y;
    if (ev && ev.clientX != null && ev.type !== "focus") { x = ev.clientX - box.left; y = ev.clientY - box.top; }
    else { const r = ev.target.getBoundingClientRect(); x = r.right - box.left; y = r.top - box.top; }
    const tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
    tooltip.style.left = Math.min(Math.max(x + 14, 4), box.width - tw - 4) + "px";
    tooltip.style.top = Math.min(Math.max(y + 14, 4), box.height - th - 4) + "px";
  }

  function select(n) {
    selected = selected === n.id ? null : n.id;
    render.highlight(selected);
    showDetail(n);
  }

  function showDetail(n) {
    detail.replaceChildren();
    if (!selected) {
      detail.appendChild(html("p", "Hover a node to see its connections. Click (or Tab + Enter) to pin its details here.", "muted"));
      return;
    }
    detail.appendChild(html("h4", n.label));
    const dl = document.createElement("dl");
    const add = (k, v) => { dl.appendChild(html("dt", k)); dl.appendChild(html("dd", v)); };
    if (n.kind === "merchant") add("Status", STATES[n.state].label);
    add("Total", money(n.total));
    add("Transactions", String(n.count));
    if (n.kind === "merchant") {
      add("Cards used", String(n.cards));
      add("First purchase", date(n.first_seen) + (n.new ? " (new)" : ""));
      add("Last purchase", date(n.last_seen));
      add("Foreign", n.foreign ? "Yes" : "No");
      add("Max anomaly", n.max_score != null ? n.max_score.toFixed(2) : "—");
      add(`Beyond ${limit.toFixed(2)}`, `${beyond(n)} of ${n.count}`);
    }
    detail.appendChild(dl);
    if (n.reasons && n.reasons.length) {
      detail.appendChild(html("p", "Why unusual", "why-h"));
      const ul = document.createElement("ul"); ul.className = "why";
      n.reasons.forEach(r => ul.appendChild(html("li", r)));
      detail.appendChild(ul);
    }
    if (n.kind === "merchant") {
      const a = html("a", "View these transactions →");
      a.href = "/?q=" + encodeURIComponent(n.query);
      detail.appendChild(a);
    }
  }

  function renderTable(merchants) {
    tbody.replaceChildren();
    const rows = [...merchants].sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state] || b.total - a.total);
    if (!rows.length) {
      const tr = tbody.insertRow(); const td = tr.insertCell(); td.colSpan = 10; td.className = "muted";
      td.textContent = "No merchants in this range.";
    }
    for (const n of rows) {
      const tr = tbody.insertRow();
      const st = tr.insertCell(); st.className = "status-cell";
      const key = html("span", STATES[n.state].glyph, `key-node st-${n.state}`); key.setAttribute("aria-hidden", "true");
      st.append(key, document.createTextNode(STATES[n.state].label));
      const name = tr.insertCell(); const a = html("a", n.label); a.href = "/?q=" + encodeURIComponent(n.query); name.appendChild(a);
      const num = (v) => { const c = tr.insertCell(); c.className = "num"; c.textContent = v; };
      num(String(n.count)); num(money(n.total)); num(String(n.cards));
      tr.insertCell().textContent = date(n.first_seen);
      tr.insertCell().textContent = [n.new && "New", n.foreign && "Foreign"].filter(Boolean).join(", ") || "—";
      num(n.max_score != null ? n.max_score.toFixed(2) : "—");
      num(beyond(n) ? String(beyond(n)) : "—");
      const why = tr.insertCell(); why.className = "why-cell"; why.textContent = (n.reasons || []).join(" · ") || "—";
    }
  }

  async function load(days) {
    svg.style.opacity = data ? "0.5" : "1"; // refetch keeps the previous frame, dimmed
    const r = await fetch("/api/network?days=" + encodeURIComponent(days));
    data = await r.json();
    currency = data.home_currency;
    if (limit === null) { // first load: start at the "Unusual pattern" alarm threshold
      limit = data.anomaly.limit;
      limitInput.value = String(limit); limitOut.textContent = limit.toFixed(2);
    }
    document.getElementById("model-name").textContent = data.anomaly.isolation_forest
      ? `Isolation Forest (${data.anomaly.model.split("@")[1] || "trained"})` : "statistical baseline (Isolation Forest not trained yet)";
    selected = null;
    svg.style.opacity = "1";
    lastWidth = svg.clientWidth;
    render();
    const url = new URL(location.href); url.searchParams.set("days", days); history.replaceState(null, "", url);
  }

  document.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(".seg button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    load(b.dataset.days);
  }));
  let resizeTimer, lastWidth = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (data && Math.abs(svg.clientWidth - lastWidth) > 40) { lastWidth = svg.clientWidth; const keep = selected; render(); selected = keep; render.highlight(selected); }
    }, 200);
  });
  onlyAnomalies.addEventListener("change", () => render.highlight && render.highlight(selected));
  limitInput.addEventListener("input", () => {
    limit = parseFloat(limitInput.value); limitOut.textContent = limit.toFixed(2);
    if (render.applyLimit) render.applyLimit();
  });
  load(document.querySelector('.seg button[aria-pressed="true"]').dataset.days);
})();
