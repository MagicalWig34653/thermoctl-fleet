/* thermoctl-fleet -- interactive architecture diagram.
 * Vanilla JS, inline SVG, no library, no build step (site/architektur.html).
 * Reads ARCH_NODES / ARCH_EDGES / ARCH_STORIES from architecture-data.js.
 *
 * The wide layout uses coordinates from architecture-data.js. The narrow
 * layout has a compact two-column arrangement with the same reading order.
 */

(function () {
  "use strict";

  const ZONE_META = {
    apt: { label: "Wohnung / Basisstation", fill: "var(--zone-apartment)", stroke: "var(--zone-apartment-border)" },
    reg: { label: "Registry (extern)", fill: "transparent", stroke: "var(--border)" },
    cloud: { label: "Cloud (Vermieter)", fill: "var(--zone-cloud)", stroke: "var(--zone-cloud-border)" },
    browser: { label: "Browser des Vermieters", fill: "var(--zone-browser)", stroke: "var(--zone-browser-border)" },
  };

  const svg = document.getElementById("diagram-svg");
  if (!svg) return; // noscript path handles the no-JS case entirely in markup.

  const layerZones = document.getElementById("layer-zones");
  const layerEdges = document.getElementById("layer-edges");
  const layerNodes = document.getElementById("layer-nodes");
  const layerLabels = document.getElementById("layer-labels");

  let currentLayout = null;
  let activeStory = null; // { story, stepIndex }
  let lastFocusedNode = null;

  function computeWideLayout() {
    const nodes = Object.fromEntries(ARCH_NODES.map((n) => [n.id, n]));
    const zones = {
      apt: { x: 24, y: 119, w: 574, h: 574 },
      reg: { x: 509, y: 17, w: 182, h: 100 },
      cloud: { x: 758, y: 220, w: 418, h: 359 },
      browser: { x: 758, y: 597, w: 418, h: 118 },
    };
    return { nodes, zones, width: 1200, height: 735, narrow: false, colsOrder: ["apt", "reg", "cloud", "browser"] };
  }

  function computeNarrowLayout() {
    const nodes = {};
    const put = (id, x, y, w, h = 62) => { nodes[id] = { id, x, y, w, h, zone: ARCH_NODES.find((n) => n.id === id).zone }; };
    put("thermoctl", 38, 70, 180); put("z2m", 242, 70, 180);
    put("agent", 38, 169, 384, 70);
    put("watchdog", 38, 274, 180); put("restore_mover", 242, 274, 180);
    put("state_files", 38, 374, 180); put("docker", 242, 374, 180);
    put("boot", 38, 474, 180); put("led", 242, 474, 180);
    put("fleet", 38, 852, 384, 70);
    put("db", 38, 958, 180); put("blob", 242, 958, 180);
    put("webui", 38, 1050, 180); put("notifier", 242, 1050, 180);
    put("browser", 38, 1224, 384);
    put("registry", 38, 1389, 384);
    const zones = {
      apt: { x: 20, y: 20, w: 420, h: 535 },
      cloud: { x: 20, y: 805, w: 420, h: 326 },
      browser: { x: 20, y: 1175, w: 420, h: 130 },
      reg: { x: 20, y: 1350, w: 420, h: 120 },
    };
    return { nodes, zones, width: 470, height: 1490, narrow: true, colsOrder: ["apt", "cloud", "browser", "reg"] };
  }

  function computeLayout() {
    const narrow = window.matchMedia("(max-width: 760px)").matches;
    return narrow ? computeNarrowLayout() : computeWideLayout();
  }

  function el(tag, attrs, children) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach((k) => node.setAttribute(k, attrs[k]));
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }

  function textEl(x, y, content, cls) {
    const t = el("text", { x, y, class: cls });
    t.textContent = content;
    return t;
  }

  function renderZones(layout) {
    layerZones.innerHTML = "";
    layout.colsOrder.forEach((zoneId) => {
      const z = layout.zones[zoneId];
      const meta = ZONE_META[zoneId];
      const rect = el("rect", {
        class: "zone", x: z.x, y: z.y, width: z.w, height: z.h, rx: 14,
        fill: meta.fill, stroke: meta.stroke, "stroke-width": 1.4, "stroke-dasharray": zoneId === "reg" ? "5 4" : "none",
      });
      layerZones.appendChild(rect);
      // Wide layout: the browser zone's title starts further in, so the
      // Web-UI→Browser edge can enter at the card's far left without
      // crossing the title text.
      const labelInset = zoneId === "browser" && !layout.narrow ? 46 : 14;
      layerZones.appendChild(textEl(z.x + labelInset, z.y + 26, meta.label, "zone-label"));
    });
  }

  function wrapLines(text, maxChars) {
    if (text.length <= maxChars) return [text];
    const words = text.split(" ");
    const lines = [];
    let cur = "";
    words.forEach((w) => {
      if ((cur + " " + w).trim().length > maxChars) {
        lines.push(cur.trim());
        cur = w;
      } else {
        cur = (cur + " " + w).trim();
      }
    });
    if (cur) lines.push(cur);
    return lines.slice(0, 2);
  }

  function renderNodes(layout) {
    layerNodes.innerHTML = "";
    const zoneOrder = Object.fromEntries(layout.colsOrder.map((id, index) => [id, index]));
    [...ARCH_NODES].sort((a, b) => zoneOrder[a.zone] - zoneOrder[b.zone] ||
      layout.nodes[a.id].y - layout.nodes[b.id].y ||
      layout.nodes[a.id].x - layout.nodes[b.id].x).forEach((n) => {
      const r = layout.nodes[n.id];
      if (!r) return;
      const g = el("g", {
        class: "node", "data-node": n.id, tabindex: "0", role: "button",
        "aria-label": n.title + ". " + n.sub + ". Öffnet Details.",
      });
      g.appendChild(el("rect", { class: "node-rect", x: r.x, y: r.y, width: r.w, height: r.h }));
      const shortRegistry = n.id === "registry" && !layout.narrow;
      if (shortRegistry) {
        g.appendChild(textEl(r.x + 12, r.y + 18, "Registry", "node-title"));
        g.appendChild(textEl(r.x + 12, r.y + 31, "(ghcr.io u. a.)", "node-title"));
      } else {
        g.appendChild(textEl(r.x + 12, r.y + 21, n.title, "node-title"));
      }
      const subLines = wrapLines(n.sub, Math.max(18, Math.floor(r.w / 6.2)));
      subLines.forEach((line, i) => {
        g.appendChild(textEl(r.x + 12, r.y + (shortRegistry ? 44 : 37) + i * 12, line, "node-sub"));
      });
      g.addEventListener("click", () => openPanel(n.id, g));
      g.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          openPanel(n.id, g);
        }
      });
      layerNodes.appendChild(g);
    });
  }

  function point(r, side, offset = 0) {
    if (side === "left") return { x: r.x, y: r.y + r.h / 2 + offset };
    if (side === "right") return { x: r.x + r.w, y: r.y + r.h / 2 + offset };
    return { x: r.x + r.w / 2 + offset, y: side === "top" ? r.y : r.y + r.h };
  }

  function path(points) {
    return points.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ");
  }

  function routeVertical(a, b, x) {
    return path([a, { x, y: a.y }, { x, y: b.y }, b]);
  }

  // Local hops use the central column gutter or a free row gap.
  function routeLocal(e, layout) {
    const s = layout.nodes[e.from], t = layout.nodes[e.to];
    const narrow = layout.narrow;
    const directX = {
      "e-thermoctl-agent": narrow ? 131 : 172,
      "e-z2m-agent": narrow ? 329 : 448,
      "e-agent-watchdog": narrow ? 131 : 172,
      "e-agent-restoremover": narrow ? 329 : 448,
      "e-watchdog-state": narrow ? 131 : 172,
      "e-fleet-db": narrow ? 131 : 866,
      "e-fleet-blob": narrow ? 329 : 1065,
    };
    if (Object.prototype.hasOwnProperty.call(directX, e.id)) {
      const x = directX[e.id];
      return path([{ x, y: s.y + s.h }, { x, y: t.y }]);
    }
    if (e.id === "e-webui-browser" && narrow) {
      return path([point(s, "bottom"), { x: 128, y: 1146 },
        { x: 350, y: 1146 }, point(t, "top", 120)]);
    }
    const aptGutter = narrow ? 230 : 310;
    const cloudGutter = narrow ? 230 : 964;
    if (e.id === "e-watchdog-docker") {
      return routeVertical(point(s, "right"), point(t, "left", -12), aptGutter - 6);
    }
    if (e.id === "e-agent-docker") {
      return routeVertical(point(s, "bottom", aptGutter + 6 - (s.x + s.w / 2)),
        point(t, "left", 12), aptGutter + 6);
    }
    if (e.id === "e-agent-state" || e.id === "e-boot-agent") {
      const x = narrow ? (e.id === "e-agent-state" ? 222 : 230) :
        (e.id === "e-agent-state" ? 302 : 310);
      return e.id === "e-agent-state" ?
        path([{ x, y: s.y + s.h }, { x, y: t.y + t.h / 2 }, point(t, "right")]) :
        path([point(s, "right"), { x, y: s.y + s.h / 2 }, { x, y: t.y + t.h }]);
    }
    if (e.id === "e-agent-led") {
      const x = narrow ? 238 : 318;
      return path([{ x, y: s.y + s.h }, { x, y: t.y + t.h / 2 }, point(t, "left")]);
    }
    if (e.id === "e-fleet-webui" || e.id === "e-fleet-notifier") {
      const webui = e.id === "e-fleet-webui";
      const x = cloudGutter + (webui ? -6 : 6);
      return routeVertical(point(s, "bottom", x - (s.x + s.w / 2)),
        point(t, webui ? "right" : "left", webui ? -12 : 12), x);
    }
    throw new Error(`No local route for ${e.id}`);
  }

  function routeWide(e, layout) {
    const s = layout.nodes[e.from], t = layout.nodes[e.to];
    if (e.id === "e-agent-registry") {
      const a = point(s, "right", -13), b = point(t, "bottom", -14);
      return path([a, { x: 586, y: a.y }, { x: 586, y: b.y }]);
    }
    if (s.zone === "apt" && t.zone === "cloud" || s.zone === "cloud" && t.zone === "apt") {
      const lane = e.lane;
      const forward = s.zone === "apt";
      const y = 280 + lane * 16;
      const ax = point(layout.nodes.agent, "right").x;
      const fx = point(layout.nodes.fleet, "left").x;
      return forward ? path([{ x: ax, y }, { x: fx, y }]) : path([{ x: fx, y }, { x: ax, y }]);
    }
    if (s.zone === "cloud" && t.zone === "browser" || s.zone === "browser" && t.zone === "cloud") {
      if (e.id === "e-webui-browser") {
        return path([{ x: 796, y: s.y + s.h }, { x: 796, y: t.y }]);
      }
      if (e.id === "e-fleet-browser-recipient") {
        return path([{ x: 968, y: s.y + s.h }, { x: 968, y: 588 },
          { x: 1085, y: 588 }, { x: 1085, y: t.y }]);
      }
      return path([{ x: 1120, y: s.y }, { x: 1120, y: 588 },
        { x: 960, y: 588 }, { x: 960, y: t.y + t.h }]);
    }
    return routeLocal(e, layout);
  }

  function routeNarrow(e, layout) {
    const s = layout.nodes[e.from], t = layout.nodes[e.to];
    if (s.zone === t.zone || e.id === "e-webui-browser") return routeLocal(e, layout);
    if (e.id === "e-agent-registry") {
      return routeVertical(point(s, "left", -18), point(t, "left"), 10);
    }
    if (s.zone === "apt" && t.zone === "cloud" || s.zone === "cloud" && t.zone === "apt") {
      const laneX = 220 + e.lane * 6;
      const agent = { x: laneX, y: layout.nodes.agent.y + layout.nodes.agent.h };
      const fleet = { x: laneX, y: layout.nodes.fleet.y };
      const points = [agent, fleet];
      return path(s.zone === "apt" ? points : points.reverse());
    }
    const bus = 438 + e.lane * 12;
    return routeVertical(point(s, "right", e.lane * 3), point(t, "right", e.lane * 3), bus);
  }

  function channelRect(layout) {
    return layout.narrow ? { x: 20, y: 585, w: 420, h: 180 } :
      { x: 604, y: 228, w: 148, h: 168 };
  }

  function checkRectBounds(layout) {
    const rects = [
      ...Object.entries(layout.nodes).map(([id, r]) => ["node", id, r]),
      ...Object.entries(layout.zones).map(([id, r]) => ["zone", id, r]),
      ["channel", "internet", channelRect(layout)],
    ];
    for (const [kind, id, r] of rects) {
      if (![r.x, r.y, r.w, r.h].every(Number.isFinite) ||
          r.x < 0 || r.y < 0 || r.w <= 0 || r.h <= 0 ||
          r.x + r.w > layout.width || r.y + r.h > layout.height) {
        console.error(`Out-of-viewBox ${layout.narrow ? "narrow" : "wide"} ${kind} ${id}:`,
          { rect: r, viewBox: [0, 0, layout.width, layout.height] });
      }
    }
  }

  function checkEdgeRoute(e, layout, d) {
    const points = [...d.matchAll(/[ML](-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/g)]
      .map((m) => ({ x: Number(m[1]), y: Number(m[2]) }));
    const zones = Object.entries(layout.zones);
    const bad = [];
    for (let i = 1; i < points.length; i += 1) {
      const a = points[i - 1], b = points[i];
      const vertical = a.x === b.x, horizontal = a.y === b.y;
      if (![a.x, a.y, b.x, b.y].every(Number.isFinite) ||
          Math.min(a.x, b.x) < 0 || Math.max(a.x, b.x) > layout.width ||
          Math.min(a.y, b.y) < 0 || Math.max(a.y, b.y) > layout.height ||
          !vertical && !horizontal) bad.push(`segment ${i} leaves the frame or is diagonal`);
      for (const [id, z] of zones) {
        const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x);
        const y0 = Math.min(a.y, b.y), y1 = Math.max(a.y, b.y);
        if (id !== layout.nodes[e.from].zone && id !== layout.nodes[e.to].zone &&
            (vertical && a.x > z.x && a.x < z.x + z.w &&
              Math.min(y1, z.y + z.h) > Math.max(y0, z.y) ||
            horizontal && a.y > z.y && a.y < z.y + z.h &&
              Math.min(x1, z.x + z.w) > Math.max(x0, z.x))) {
          bad.push(`segment ${i} crosses unrelated ${id} zone`);
        }
        if (vertical && y1 > z.y && y0 < z.y + z.h &&
            (Math.abs(a.x - z.x) < 8 || Math.abs(a.x - z.x - z.w) < 8) ||
            horizontal && x1 > z.x && x0 < z.x + z.w &&
            (Math.abs(a.y - z.y) < 8 || Math.abs(a.y - z.y - z.h) < 8)) {
          bad.push(`segment ${i} hugs ${id} zone border`);
        }
      }
    }
    if (bad.length) console.error(`Invalid ${layout.narrow ? "narrow" : "wide"} route for ${e.id}:`, bad, d);
  }

  function renderChannel(layout) {
    const { x, y, w, h } = channelRect(layout);
    const g = el("g", { class: "internet-channel" });
    g.appendChild(el("rect", { x, y, width: w, height: h, rx: 20, class: "channel-band" }));
    if (layout.narrow) {
      g.appendChild(textEl(100, 610, "Internet", "channel-title"));
      g.appendChild(textEl(230, 790, "nur ausgehend vom Agenten · HTTPS", "channel-sub"));
    } else {
      g.appendChild(textEl(678, 245, "Internet", "channel-title"));
      g.appendChild(textEl(678, 413, "nur ausgehend vom", "channel-sub"));
      g.appendChild(textEl(678, 430, "Agenten · HTTPS", "channel-sub"));
    }
    layerZones.appendChild(g);
  }

  function renderEdges(layout) {
    layerEdges.replaceChildren();
    layerLabels.replaceChildren();
    for (const e of ARCH_EDGES) {
      const g = el("g", { class: "edge", "data-edge": e.id, tabindex: "0", role: "img", "aria-label": `${e.label}: ${ARCH_NODES.find((n) => n.id === e.from).title} zu ${ARCH_NODES.find((n) => n.id === e.to).title}` });
      const d = layout.narrow ? routeNarrow(e, layout) : routeWide(e, layout);
      g.appendChild(el("path", { class: "edge-line", d, "marker-end": "url(#d-arrow)" }));
      g.appendChild(el("path", { class: "edge-hit", d }));
      g.addEventListener("pointerenter", () => showHoverLabel(e));
      g.addEventListener("pointerleave", clearHoverLabel);
      g.addEventListener("focus", () => showFocusLabel(e));
      g.addEventListener("blur", clearHoverLabel);
      layerEdges.appendChild(g);
    }
  }

  const NARROW_LABELS = {
    "e-thermoctl-agent": [130, 151], "e-z2m-agent": [330, 151],
    "e-agent-state": [125, 354], "e-watchdog-state": [130, 354],
    "e-agent-watchdog": [125, 257], "e-watchdog-docker": [340, 354],
    "e-agent-docker": [330, 354], "e-agent-led": [330, 455],
    "e-agent-restoremover": [330, 257], "e-boot-agent": [130, 455],
    "e-agent-registry": [235, 1330],
    "e-agent-fleet-heartbeat": [340, 612], "e-agent-fleet-backup": [340, 648],
    "e-agent-fleet-result": [340, 684], "e-fleet-agent-sse": [340, 720],
    "e-fleet-db": [130, 943], "e-fleet-blob": [330, 943],
    "e-fleet-notifier": [330, 1037], "e-fleet-webui": [130, 1037],
    "e-webui-browser": [130, 1157], "e-browser-fleet-key": [330, 1144],
    "e-fleet-browser-recipient": [330, 1170],
  };

  function labelAnchor(e, layout) {
    const pair = layout.narrow ? NARROW_LABELS[e.id] : [e.labelX, e.labelY];
    return pair ? { x: pair[0], y: pair[1] } : { x: NaN, y: NaN };
  }

  function checkLabelAnchors(layout) {
    for (const e of ARCH_EDGES) {
      const { x, y } = labelAnchor(e, layout);
      const outside = !Number.isFinite(x) || !Number.isFinite(y) ||
        x < 0 || x > layout.width || y < 0 || y > layout.height;
      const card = Object.values(layout.nodes).find((r) =>
        x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h);
      if (outside || card) {
        console.error(`Invalid ${layout.narrow ? "narrow" : "wide"} label anchor for ${e.id}:`,
          { x, y, outside, card: card && card.id });
      }
    }
  }

  function labelPill(e, x, y, cls) {
    const internet = e.from === "agent" && e.to === "fleet" || e.from === "fleet" && e.to === "agent";
    const width = internet ? 142 : Math.min(300, e.label.length * 6.1 + 22);
    const g = el("g", { class: cls + (internet ? " channel-pill" : ""), "data-label": e.id });
    g.appendChild(el("rect", { x: x - width / 2, y: y - (internet ? 17 : 16), width,
      height: internet ? 28 : 24, rx: 12 }));
    const lines = internet ? {
      "e-agent-fleet-heartbeat": ["POST /v1/heartbeat", "(120 s)"],
      "e-agent-fleet-backup": ["POST /v1/backups", "(opak)"],
      "e-agent-fleet-result": ["POST /v1/commands", "/{id}/result"],
      "e-fleet-agent-sse": ["GET /v1/commands", "(SSE)"],
    }[e.id] : [e.label];
    lines.forEach((line, i) => {
      const t = textEl(x, internet ? y - 4 + i * 11 : y, line, "pill-text");
      t.setAttribute("text-anchor", "middle");
      g.appendChild(t);
    });
    return g;
  }

  function clearHoverLabel() {
    layerLabels.querySelectorAll(".hover-pill").forEach((g) => g.remove());
  }
  function showFocusLabel(e) {
    clearHoverLabel();
    const { x, y } = labelAnchor(e, currentLayout);
    layerLabels.appendChild(labelPill(e, x, y, "hover-pill"));
  }
  function showHoverLabel(e) { showFocusLabel(e); }

  function render() {
    currentLayout = computeLayout();
    for (const layout of [computeWideLayout(), computeNarrowLayout()]) {
      checkRectBounds(layout);
      ARCH_EDGES.forEach((e) => checkEdgeRoute(e, layout,
        layout.narrow ? routeNarrow(e, layout) : routeWide(e, layout)));
    }
    checkLabelAnchors(currentLayout);
    svg.setAttribute("viewBox", `0 0 ${currentLayout.width} ${currentLayout.height}`);
    svg.classList.toggle("is-narrow", currentLayout.narrow);
    renderZones(currentLayout);
    renderChannel(currentLayout);
    renderEdges(currentLayout);
    renderNodes(currentLayout);
    applyStoryHighlight();
  }

  // ---- Side panel -------------------------------------------------

  const panel = document.getElementById("side-panel");
  const panelBackdrop = document.getElementById("panel-backdrop");
  const panelTitle = document.getElementById("panel-title");
  const panelSub = document.getElementById("panel-sub");
  const panelBody = document.getElementById("panel-body");
  const panelClose = document.getElementById("panel-close");

  function dt(label) {
    const d = document.createElement("dt");
    d.textContent = label;
    return d;
  }
  function dd(text) {
    const d = document.createElement("dd");
    d.textContent = text;
    return d;
  }

  function openPanel(nodeId, triggerEl) {
    const n = ARCH_NODES.find((x) => x.id === nodeId);
    if (!n) return;
    lastFocusedNode = triggerEl || null;
    panelTitle.textContent = n.title;
    panelSub.textContent = n.sub;
    panelBody.innerHTML = "";
    panelBody.appendChild(dt("Was es ist"));
    panelBody.appendChild(dd(n.panel.what));
    panelBody.appendChild(dt("Darf / darf nicht"));
    panelBody.appendChild(dd(n.panel.allowed));
    panelBody.appendChild(dt("Welche Daten es sieht"));
    panelBody.appendChild(dd(n.panel.data));
    panelBody.appendChild(dt("Sicherheitsgrundsatz"));
    panelBody.appendChild(dd(n.panel.principle));
    const linkDt = dt("Mehr dazu");
    panelBody.appendChild(linkDt);
    const linkDd = document.createElement("dd");
    const a = document.createElement("a");
    a.href = "https://github.com/MagicalWig34653/thermoctl-fleet/blob/main/docs/specification.md";
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = n.panel.linkText;
    linkDd.appendChild(a);
    panelBody.appendChild(linkDd);

    panel.hidden = false;
    panelBackdrop.classList.add("open");
    requestAnimationFrame(() => panel.classList.add("open"));
    panelClose.focus();

    document.querySelectorAll("#diagram-svg .node").forEach((g) => {
      g.classList.toggle("is-current", g.getAttribute("data-node") === nodeId);
    });
  }

  function closePanel() {
    panel.classList.remove("open");
    panelBackdrop.classList.remove("open");
    setTimeout(() => {
      panel.hidden = true;
    }, 200);
    document.querySelectorAll("#diagram-svg .node.is-current").forEach((g) => g.classList.remove("is-current"));
    if (lastFocusedNode) lastFocusedNode.focus();
  }

  panelClose.addEventListener("click", closePanel);
  panelBackdrop.addEventListener("click", closePanel);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !panel.hidden) closePanel();
  });

  // ---- Storys -------------------------------------------------------

  const storyBar = document.getElementById("story-bar");
  const storySteps = document.getElementById("story-steps");
  const storyStepsList = document.getElementById("story-steps-list");
  const storyPrev = document.getElementById("story-prev");
  const storyNext = document.getElementById("story-next");
  const storyClear = document.getElementById("story-clear");
  const storyCounter = document.getElementById("story-counter");

  function buildStoryBar() {
    storyBar.innerHTML = "";
    ARCH_STORIES.forEach((story) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "story-btn";
      btn.textContent = story.label;
      btn.setAttribute("aria-pressed", "false");
      btn.addEventListener("click", () => selectStory(story.id));
      storyBar.appendChild(btn);
    });
  }

  function selectStory(storyId) {
    const story = ARCH_STORIES.find((s) => s.id === storyId);
    if (!story) return;
    activeStory = { story, stepIndex: 0 };
    renderStoryUI();
  }

  function clearStory() {
    activeStory = null;
    renderStoryUI();
  }

  function renderStoryUI() {
    Array.from(storyBar.children).forEach((btn, i) => {
      btn.setAttribute("aria-pressed", activeStory && ARCH_STORIES[i].id === activeStory.story.id ? "true" : "false");
    });
    if (!activeStory) {
      storySteps.hidden = true;
      applyStoryHighlight();
      return;
    }
    storySteps.hidden = false;
    storyStepsList.innerHTML = "";
    activeStory.story.steps.forEach((step, i) => {
      const li = document.createElement("li");
      li.textContent = step.text;
      if (i === activeStory.stepIndex) li.className = "active";
      storyStepsList.appendChild(li);
    });
    storyPrev.disabled = activeStory.stepIndex === 0;
    storyNext.disabled = activeStory.stepIndex === activeStory.story.steps.length - 1;
    storyCounter.textContent = `Schritt ${activeStory.stepIndex + 1} von ${activeStory.story.steps.length}`;
    applyStoryHighlight();
  }

  function applyStoryHighlight() {
    const nodeEls = document.querySelectorAll("#diagram-svg .node");
    const edgeEls = document.querySelectorAll("#diagram-svg .edge");
    layerLabels.querySelectorAll(".story-pill").forEach((g) => g.remove());
    if (!activeStory) {
      nodeEls.forEach((g) => g.classList.remove("is-dim", "is-active"));
      edgeEls.forEach((g) => g.classList.remove("is-dim", "is-active"));
      svg.querySelector(".internet-channel").classList.remove("is-dim");
      return;
    }
    const step = activeStory.story.steps[activeStory.stepIndex];
    const activeNodes = new Set(step.nodes);
    const activeEdges = new Set(step.edges);
    svg.querySelector(".internet-channel").classList.toggle("is-dim",
      ![...activeEdges].some((id) => id.startsWith("e-agent-fleet") || id === "e-fleet-agent-sse"));
    ARCH_EDGES.filter((e) => activeEdges.has(e.id)).forEach((e) => {
      activeNodes.add(e.from);
      activeNodes.add(e.to);
    });
    nodeEls.forEach((g) => {
      const id = g.getAttribute("data-node");
      g.classList.toggle("is-active", activeNodes.has(id));
      g.classList.toggle("is-dim", !activeNodes.has(id));
    });
    edgeEls.forEach((g) => {
      const id = g.getAttribute("data-edge");
      g.classList.toggle("is-active", activeEdges.has(id));
      g.classList.toggle("is-dim", !activeEdges.has(id));
    });
    ARCH_EDGES.filter((e) => activeEdges.has(e.id)).forEach((e) => {
      const { x, y } = labelAnchor(e, currentLayout);
      layerLabels.appendChild(labelPill(e, x, y, "story-pill"));
    });
  }

  storyPrev.addEventListener("click", () => {
    if (!activeStory) return;
    activeStory.stepIndex = Math.max(0, activeStory.stepIndex - 1);
    renderStoryUI();
  });
  storyNext.addEventListener("click", () => {
    if (!activeStory) return;
    activeStory.stepIndex = Math.min(activeStory.story.steps.length - 1, activeStory.stepIndex + 1);
    renderStoryUI();
  });
  storyClear.addEventListener("click", clearStory);
  document.addEventListener("keydown", (ev) => {
    if (!activeStory || !panel.hidden || !["ArrowLeft", "ArrowRight"].includes(ev.key)) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
    ev.preventDefault();
    activeStory.stepIndex = Math.max(0, Math.min(activeStory.story.steps.length - 1,
      activeStory.stepIndex + (ev.key === "ArrowRight" ? 1 : -1)));
    renderStoryUI();
  });

  // ---- Boot -----------------------------------------------------------

  buildStoryBar();
  render();

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 150);
  });

  // Deep link: #story-<id> selects a story on load (used by index.html's
  // lifecycle timeline, e.g. architektur.html#story-enrollment).
  if (location.hash.indexOf("#story-") === 0) {
    const id = location.hash.replace("#story-", "");
    if (ARCH_STORIES.some((s) => s.id === id)) selectStory(id);
  }
})();
