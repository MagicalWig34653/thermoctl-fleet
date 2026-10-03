/* thermoctl-fleet -- interactive architecture diagram.
 * Vanilla JS, inline SVG, no library, no build step (site/architektur.html).
 * Reads ARCH_NODES / ARCH_EDGES / ARCH_STORIES from architecture-data.js.
 *
 * Layout is computed, not hand-placed per pixel: computeLayout() stacks
 * each zone's nodes in a single column with a fixed row height and gap,
 * so within a zone nodes can never overlap by construction. Zones are
 * placed in non-overlapping x-ranges (wide layout) or stacked
 * top-to-bottom (narrow layout) for the same reason.
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

  let currentLayout = null;
  let activeStory = null; // { story, stepIndex }
  let lastFocusedNode = null;

  function nodesByZone() {
    const byZone = {};
    ARCH_NODES.forEach((n) => {
      (byZone[n.zone] = byZone[n.zone] || []).push(n);
    });
    Object.values(byZone).forEach((list) => list.sort((a, b) => a.order - b.order));
    return byZone;
  }

  // Wide layout: three side-by-side zones (apt, cloud, browser). The gap
  // between them is wide (220) on purpose -- it is where cross-zone edge
  // labels ("POST /v1/commands/{id}/result" and the like) live, and at
  // 220px even the longest label fits without reaching either zone's
  // boxes. Registry is *not* a fourth column between apt and cloud (an
  // earlier version put it there and every apt<->cloud label collided
  // with its box) -- it sits in its own strip above, centered over the
  // apt/cloud gap, reachable only from the agent via its own short edge.
  function computeWideLayout() {
    const byZone = nodesByZone();
    const colGap = 220;
    const nodeH = 56;
    const rowGap = 14;
    const topPad = 64;
    const bottomPad = 20;
    const sidePad = 18;
    const mainTop = 180;

    const cols = [
      { id: "apt", x: 36, w: 320 },
      { id: "cloud", x: 36 + 320 + colGap, w: 320 },
      { id: "browser", x: 36 + 320 + colGap + 320 + colGap, w: 210 },
    ];

    const zones = {};
    const nodes = {};
    let maxBottom = 0;

    cols.forEach((col) => {
      const list = byZone[col.id] || [];
      const h = topPad + list.length * nodeH + Math.max(0, list.length - 1) * rowGap + bottomPad;
      zones[col.id] = { x: col.x, y: mainTop, w: col.w, h };
      list.forEach((n, i) => {
        nodes[n.id] = {
          x: col.x + sidePad,
          y: mainTop + topPad + i * (nodeH + rowGap),
          w: col.w - sidePad * 2,
          h: nodeH,
          zone: col.id,
        };
      });
      maxBottom = Math.max(maxBottom, mainTop + h);
    });

    // Registry: a small standalone strip above, centered over the gap
    // between apt and cloud -- never inside either column's x-range, so
    // it can never collide with an apt or cloud node or label.
    const regList = byZone.reg || [];
    const regW = 190;
    const aptRight = cols[0].x + cols[0].w;
    const cloudLeft = cols[1].x;
    const regX = aptRight + (cloudLeft - aptRight - regW) / 2;
    const regH = topPad + regList.length * nodeH + bottomPad;
    zones.reg = { x: regX, y: 10, w: regW, h: regH };
    regList.forEach((n, i) => {
      nodes[n.id] = {
        x: regX + sidePad,
        y: 10 + topPad + i * (nodeH + rowGap),
        w: regW - sidePad * 2,
        h: nodeH,
        zone: "reg",
      };
    });

    const width = cols[cols.length - 1].x + cols[cols.length - 1].w + 30;
    const height = maxBottom + 20;
    return { nodes, zones, width, height, narrow: false, colsOrder: ["reg", "apt", "cloud", "browser"] };
  }

  const NARROW_BUS_MARGIN = 112; // reserved at the right of every zone for edge "bus" lanes

  function computeNarrowLayout() {
    const byZone = nodesByZone();
    const order = ["apt", "reg", "cloud", "browser"];
    const nodeH = 50;
    const rowGap = 10;
    const topPad = 56;
    const bottomPad = 18;
    const sidePad = 16;
    const zoneGap = 22;
    const width = 460;
    const colW = width - 40;

    const zones = {};
    const nodes = {};
    let y = 24;

    order.forEach((zoneId) => {
      const list = byZone[zoneId] || [];
      const h = topPad + list.length * nodeH + Math.max(0, list.length - 1) * rowGap + bottomPad;
      zones[zoneId] = { x: 20, y, w: colW, h };
      list.forEach((n, i) => {
        nodes[n.id] = {
          x: 20 + sidePad,
          y: y + topPad + i * (nodeH + rowGap),
          w: colW - sidePad * 2 - NARROW_BUS_MARGIN,
          h: nodeH,
          zone: zoneId,
        };
      });
      y += h + zoneGap;
    });

    const height = y;
    return { nodes, zones, width, height, narrow: true, colsOrder: order };
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
      layerZones.appendChild(textEl(z.x + 14, z.y + 26, meta.label, "zone-label"));
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
    ARCH_NODES.forEach((n) => {
      const r = layout.nodes[n.id];
      if (!r) return;
      const g = el("g", {
        class: "node", "data-node": n.id, tabindex: "0", role: "button",
        "aria-label": n.title + ". " + n.sub + ". Öffnet Details.",
      });
      g.appendChild(el("rect", { class: "node-rect", x: r.x, y: r.y, width: r.w, height: r.h }));
      g.appendChild(textEl(r.x + 12, r.y + 21, n.title, "node-title"));
      const subLines = wrapLines(n.sub, Math.max(18, Math.floor(r.w / 6.2)));
      subLines.forEach((line, i) => {
        g.appendChild(textEl(r.x + 12, r.y + 37 + i * 13, line, "node-sub"));
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

  function anchorSide(rect, side) {
    switch (side) {
      case "left": return { x: rect.x, y: rect.y + rect.h / 2 };
      case "right": return { x: rect.x + rect.w, y: rect.y + rect.h / 2 };
      case "top": return { x: rect.x + rect.w / 2, y: rect.y };
      case "bottom": return { x: rect.x + rect.w / 2, y: rect.y + rect.h };
      default: return { x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 };
    }
  }

  // Same-zone edges (agent<->watchdog, agent<->state file, ...) are short
  // hops between rows 14px apart -- there is no room next to them for a
  // label of any length without it reaching into the neighbouring zone,
  // so these draw as a plain line only. What they carry is explained in
  // the two nodes' own side panels instead.
  function routeWideSameZone(src, tgt, lane) {
    const goingDown = tgt.y > src.y;
    const a = anchorSide(src, goingDown ? "bottom" : "top");
    const b = anchorSide(tgt, goingDown ? "top" : "bottom");
    const laneX = (a.x + b.x) / 2 + (lane - 2) * 8;
    return { d: `M${a.x},${a.y} L${laneX},${a.y} L${laneX},${b.y} L${b.x},${b.y}` };
  }

  // Cross-zone edges (crossing a trust boundary) are the ones worth
  // labelling with their protocol. They travel through the wide (220px)
  // gap between columns, each on its own horizontal row (laneY) so
  // labels -- shown on hover or when a story is active, see
  // applyStoryHighlight -- never stack on top of each other.
  function routeWideCrossZone(src, tgt, laneY) {
    const goingRight = tgt.x > src.x;
    const a = anchorSide(src, goingRight ? "right" : "left");
    const b = anchorSide(tgt, goingRight ? "left" : "right");
    return {
      d: `M${a.x},${a.y} L${a.x + (goingRight ? 10 : -10)},${laneY} L${b.x - (goingRight ? 10 : -10)},${laneY} L${b.x},${b.y}`,
      mid: { x: (a.x + b.x) / 2, y: laneY - 5 },
      anchor: "middle",
    };
  }

  // The one edge that doesn't run left/right: agent (apt column) up to
  // the registry strip above. Routed explicitly rather than generically,
  // since it is the only vertical cross-zone edge in the diagram. Leaves
  // the apt column on its *right* edge first (rather than straight up
  // through its own column, which would cross every node stacked above
  // agent) and only turns vertical once clear of the column.
  function routeRegistryEdge(src, tgt) {
    const a = anchorSide(src, "right");
    const outX = a.x + 24;
    const b = anchorSide(tgt, "bottom");
    return {
      d: `M${a.x},${a.y} L${outX},${a.y} L${outX},${b.y} L${b.x},${b.y}`,
      mid: { x: outX + 6, y: a.y - (a.y - b.y) / 2 },
      anchor: "start",
    };
  }

  // Narrow (mobile) layout: a single stacked column, so every edge is
  // routed as a right-margin "bus" line, each in its own lane (x offset)
  // so the lines fan out instead of stacking directly on each other.
  // Labels stay hidden (see applyStoryHighlight) until their edge is
  // part of the active story -- there is no room on a phone for all of
  // them to be legible at once.
  function routeNarrow(src, tgt, laneIndex) {
    const a = anchorSide(src, "right");
    const b = anchorSide(tgt, "right");
    const laneX = a.x + 14 + (laneIndex % 8) * 12;
    return {
      d: `M${a.x},${a.y} L${laneX},${a.y} L${laneX},${b.y} L${b.x},${b.y}`,
      mid: { x: laneX + 3, y: a.y + (b.y - a.y) / 2 },
      anchor: "start",
    };
  }

  function renderEdges(layout) {
    layerEdges.innerHTML = "";
    Object.keys(edgeLabelEls).forEach((k) => delete edgeLabelEls[k]);

    if (layout.narrow) {
      ARCH_EDGES.forEach((e, i) => {
        const src = layout.nodes[e.from];
        const tgt = layout.nodes[e.to];
        if (!src || !tgt) return;
        const route = routeNarrow(src, tgt, i);
        appendEdge(e, route, true);
      });
      return;
    }

    const busStartY = layout.zones.apt.y + 40;
    let crossIndex = 0;
    ARCH_EDGES.forEach((e) => {
      const src = layout.nodes[e.from];
      const tgt = layout.nodes[e.to];
      if (!src || !tgt) return;
      if (src.zone === tgt.zone) {
        appendEdge(e, routeWideSameZone(src, tgt, e.lane), false);
        return;
      }
      if (e.id === "e-agent-registry") {
        appendEdge(e, routeRegistryEdge(src, tgt), true);
        return;
      }
      const laneY = busStartY + crossIndex * 26;
      crossIndex += 1;
      appendEdge(e, routeWideCrossZone(src, tgt, laneY), true);
    });
  }

  const edgeLabelEls = {};

  function appendEdge(e, route, hasLabel) {
    const g = el("g", { class: "edge", "data-edge": e.id });
    g.appendChild(el("path", { class: "edge-line", d: route.d, "marker-end": "url(#d-arrow)" }));
    if (hasLabel) {
      const baseY = route.mid.y - 4;
      const label = textEl(route.mid.x, baseY, "", "edge-label");
      label.setAttribute("text-anchor", route.anchor);
      label.dataset.baseY = String(baseY);
      g.appendChild(label);
      edgeLabelEls[e.id] = label;
      g.addEventListener("mouseenter", () => {
        if (!activeStory) label.textContent = e.label;
      });
      g.addEventListener("mouseleave", () => {
        if (!activeStory) label.textContent = "";
      });
    }
    layerEdges.appendChild(g);
  }

  function render() {
    currentLayout = computeLayout();
    svg.setAttribute("viewBox", `0 0 ${currentLayout.width} ${currentLayout.height}`);
    svg.classList.toggle("is-narrow", currentLayout.narrow);
    renderZones(currentLayout);
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
      li.textContent = (i + 1) + ". " + step.text;
      if (i === activeStory.stepIndex) li.className = "active";
      storyStepsList.appendChild(li);
    });
    storyPrev.disabled = activeStory.stepIndex === 0;
    storyNext.disabled = activeStory.stepIndex === activeStory.story.steps.length - 1;
    applyStoryHighlight();
  }

  function applyStoryHighlight() {
    const nodeEls = document.querySelectorAll("#diagram-svg .node");
    const edgeEls = document.querySelectorAll("#diagram-svg .edge");
    const byId = {};
    ARCH_EDGES.forEach((e) => { byId[e.id] = e; });
    if (!activeStory) {
      nodeEls.forEach((g) => g.classList.remove("is-dim", "is-active"));
      edgeEls.forEach((g) => g.classList.remove("is-dim", "is-active"));
      Object.keys(edgeLabelEls).forEach((id) => { edgeLabelEls[id].textContent = ""; });
      return;
    }
    const step = activeStory.story.steps[activeStory.stepIndex];
    const activeNodes = new Set(step.nodes);
    const activeEdges = new Set(step.edges);
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
    // In narrow mode several edges can be active in the same step (see
    // ARCH_STORIES); fan their labels out vertically from each one's own
    // base position so two labels revealed together never land on the
    // same row, even when their lanes sit close together.
    let shown = 0;
    Object.keys(edgeLabelEls).forEach((id) => {
      const label = edgeLabelEls[id];
      const isShown = activeEdges.has(id) && byId[id];
      label.textContent = isShown ? byId[id].label : "";
      if (isShown && currentLayout && currentLayout.narrow) {
        const baseY = parseFloat(label.dataset.baseY || "0");
        label.setAttribute("y", baseY + shown * 13);
        shown += 1;
      } else if (!currentLayout || !currentLayout.narrow) {
        label.setAttribute("y", label.dataset.baseY || label.getAttribute("y"));
      }
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
