/**
 * State machine graph: layered layout + SVG rendering.
 *
 * The layout is computed from the StateMachineDef sent by the backend
 * (same YAML that drives the engines), so the drawn graph always matches
 * the real machine:
 *
 *   1. depth = longest path from the initial state (relaxation, cycle-safe)
 *   2. states grouped into visual columns (depth order, big groups split)
 *   3. columns wrapped into rows (left→right, top→bottom)
 *   4. edges: forward = smooth bezier, row wrap = down/across/up bus,
 *      backward = routed under the nodes, self-loops over the node
 */

import type { StateMachineDef } from '../contracts.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

// Geometry constants (px)
const NODE_W = 148;
const NODE_H = 44;
const GAP_X = 64;      // gap between columns
const GAP_Y = 40;      // gap between stacked nodes of the same column
const ROW_GAP = 64;    // extra gap between rows (room for wrap edges)
const MARGIN = 28;
const MAX_PER_COL = 3;   // max nodes per visual column before splitting
const MAX_COLS_PER_ROW = 5; // max columns per row before wrapping

export type SmEdgeKind = 'plain' | 'scoped' | 'internal';

export interface SmNodePos {
  name: string;
  x: number;
  y: number;
  gcol: number; // global column index (flow order)
  row: number;
}

export interface SmEdgeGeom {
  source: string;
  target: string;
  event: string;
  kind: SmEdgeKind;
  path: string;
  labelX: number;
  labelY: number;
}

export interface SmLayoutResult {
  nodes: Map<string, SmNodePos>;
  edges: SmEdgeGeom[];
  width: number;
  height: number;
  initial: string;
}

/** Classify an event name for coloring: scoped events are colored. */
export function classifyEvent(event: string): SmEdgeKind {
  if (!event.includes('/')) return 'plain';
  const scope = event.split('/')[0];
  return scope === 'workflow' ? 'internal' : 'scoped';
}

/** Human label for an edge event (scoped events split into 2 parts). */
export function edgeLabelParts(event: string): [string, string | null] {
  const i = event.indexOf('/');
  if (i < 0) return [event, null];
  const scope = event.slice(0, i);
  const local = event.slice(i + 1);
  return [local, scope === 'workflow' ? '→ pipeline' : `→ ${scope}`];
}

/** Split a snake_case state name into display lines that fit the node. */
export function wrapStateName(name: string, maxChars = 15, maxLines = 3): string[] {
  const words = name.split('_');
  const lines: string[] = [];
  let cur = '';
  for (const w of words) {
    const cand = cur ? `${cur}_${w}` : w;
    if (cand.length <= maxChars) {
      cur = cand;
    } else {
      if (cur) lines.push(cur);
      cur = w;
    }
  }
  if (cur) lines.push(cur);
  if (lines.length > maxLines) {
    const tail = lines.slice(maxLines - 1).join('_');
    lines.length = maxLines - 1;
    lines.push(tail.length > maxChars ? `${tail.slice(0, maxChars - 1)}…` : tail);
  }
  return lines;
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

export function layoutStateMachine(def: StateMachineDef): SmLayoutResult {
  const names = Object.keys(def.states);
  const nodes = new Map<string, SmNodePos>();
  const edges: SmEdgeGeom[] = [];
  const initial = def.initial_state;

  if (names.length === 0) {
    return { nodes, edges, width: 0, height: 0, initial };
  }

  // 1) depth = longest path from the initial state (bounded relaxation,
  //    so cycles cannot loop forever)
  const depth = new Map<string, number>(names.map((n) => [n, 0]));
  for (let pass = 0; pass < names.length; pass++) {
    let changed = false;
    for (const s of names) {
      for (const t of def.states[s]?.transitions || []) {
        if (def.states[t.target] && (depth.get(t.target) || 0) < (depth.get(s) || 0) + 1) {
          depth.set(t.target, (depth.get(s) || 0) + 1);
          changed = true;
        }
      }
    }
    if (!changed) break;
  }

  // 2) visual columns: group states by depth (YAML order preserved within a
  //    depth), split oversized groups into extra columns
  const byDepth = new Map<number, string[]>();
  for (const n of names) {
    const d = depth.get(n) || 0;
    if (!byDepth.has(d)) byDepth.set(d, []);
    byDepth.get(d)!.push(n);
  }
  const columns: string[][] = [];
  for (const d of [...byDepth.keys()].sort((a, b) => a - b)) {
    const group = byDepth.get(d)!;
    for (let i = 0; i < group.length; i += MAX_PER_COL) {
      columns.push(group.slice(i, i + MAX_PER_COL));
    }
  }

  // 3) rows: at most MAX_COLS_PER_ROW columns each (flow: L→R, then next row)
  const rows: string[][][] = [];
  const rowStartGcol: number[] = [];
  for (let i = 0; i < columns.length; i += MAX_COLS_PER_ROW) {
    rows.push(columns.slice(i, i + MAX_COLS_PER_ROW));
    rowStartGcol.push(i);
  }

  // 4) coordinates
  rows.forEach((row, r) => {
    row.forEach((colStates, c) => {
      colStates.forEach((n, idx) => {
        nodes.set(n, {
          name: n,
          x: MARGIN + c * (NODE_W + GAP_X),
          y: MARGIN + r * (NODE_H + GAP_Y + ROW_GAP) + idx * (NODE_H + GAP_Y),
          gcol: rowStartGcol[r] + c,
          row: r,
        });
      });
    });
  });

  // 5) edges
  let maxBottom = 0;
  for (const p of nodes.values()) maxBottom = Math.max(maxBottom, p.y + NODE_H);

  const backCounter: Record<number, number> = {}; // row -> stagger index
  for (const s of names) {
    for (const t of def.states[s]?.transitions || []) {
      if (!def.states[t.target]) continue; // target not in this SM: skip
      const a = nodes.get(s)!;
      const b = nodes.get(t.target)!;
      const kind = classifyEvent(t.event);
      let path = '';
      let labelX = 0;
      let labelY = 0;

      if (s === t.target) {
        // self-loop: arc over the node
        const x1 = a.x + NODE_W * 0.35;
        const x2 = a.x + NODE_W * 0.65;
        path = `M ${x1} ${a.y} C ${x1 - 20} ${a.y - 26}, ${x2 + 20} ${a.y - 26}, ${x2} ${a.y}`;
        labelX = a.x + NODE_W / 2;
        labelY = Math.max(10, a.y - 32);
      } else if (b.row === a.row && b.gcol > a.gcol) {
        // forward within the row: smooth bezier right→left edges
        const x1 = a.x + NODE_W;
        const y1 = a.y + NODE_H / 2;
        const x2 = b.x;
        const y2 = b.y + NODE_H / 2;
        const dx = Math.max(x2 - x1, 24);
        path = `M ${x1} ${y1} C ${x1 + dx * 0.5} ${y1}, ${x2 - dx * 0.5} ${y2}, ${x2} ${y2}`;
        labelX = (x1 + x2) / 2;
        labelY = (y1 + y2) / 2 - 6;
      } else if (b.row > a.row) {
        // forward edge wrapping to a lower row: down, across, up into target
        const x1 = a.x + NODE_W / 2;
        const x2 = b.x + NODE_W / 2;
        const yGap = a.y + NODE_H + (GAP_Y + ROW_GAP) / 2;
        path = `M ${x1} ${a.y + NODE_H} L ${x1} ${yGap} L ${x2} ${yGap} L ${x2} ${b.y}`;
        labelX = (x1 + x2) / 2;
        labelY = yGap - 6;
      } else {
        // backward edge (or same-column across rows): routed under the rows
        const i = backCounter[a.row] || 0;
        backCounter[a.row] = i + 1;
        const yBack = maxBottom + 18 + i * 12;
        const x1 = a.x + NODE_W / 2;
        const x2 = b.x + NODE_W / 2;
        path = `M ${x1} ${a.y + NODE_H} L ${x1} ${yBack} L ${x2} ${yBack} L ${x2} ${b.y + NODE_H}`;
        labelX = (x1 + x2) / 2;
        labelY = yBack - 5;
      }

      edges.push({ source: s, target: t.target, event: t.event, kind, path, labelX, labelY });
    }
  }

  // 6) overall size
  const maxColsInRow = Math.max(...rows.map((r) => r.length));
  const width = MARGIN * 2 + (maxColsInRow - 1) * (NODE_W + GAP_X) + NODE_W;
  const maxBack = Math.max(0, ...Object.values(backCounter));
  const height = maxBottom + 20 + (maxBack > 0 ? 18 + (maxBack - 1) * 12 + 14 : 0);

  return { nodes, edges, width, height, initial };
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function el<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string> = {},
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag) as SVGElementTagNameMap[K];
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

/**
 * Render a state machine graph into `container`.
 * `currentState` (if it is a state of the machine) is highlighted.
 */
export function renderStateMachineGraph(
  container: HTMLElement,
  def: StateMachineDef,
  currentState: string | null,
): void {
  container.innerHTML = '';
  const layout = layoutStateMachine(def);
  if (layout.width === 0) {
    container.innerHTML = '<div class="sm-empty">No states defined</div>';
    return;
  }

  const svg = el('svg', {
    viewBox: `0 0 ${layout.width} ${layout.height}`,
    width: String(layout.width),
    height: String(layout.height),
  });
  svg.classList.add('sm-graph');

  // Arrowhead markers (one per edge kind so the color matches the stroke)
  const defs = el('defs');
  for (const kind of ['plain', 'scoped', 'internal'] as SmEdgeKind[]) {
    const marker = el('marker', {
      id: `sm-arrow-${kind}`,
      viewBox: '0 0 10 10',
      refX: '9',
      refY: '5',
      markerWidth: '7',
      markerHeight: '7',
      orient: 'auto-start-reverse',
    });
    marker.appendChild(el('path', { d: 'M 0 0 L 10 5 L 0 10 z' }));
    defs.appendChild(marker);
  }
  svg.appendChild(defs);

  // Edges first (nodes draw on top)
  for (const e of layout.edges) {
    const g = el('g');
    g.classList.add('sm-edge', `sm-edge-${e.kind}`);
    const path = el('path', {
      d: e.path,
      'marker-end': `url(#sm-arrow-${e.kind})`,
    });
    path.dataset.source = e.source;
    path.dataset.target = e.target;
    const label = el('text', {
      x: String(e.labelX),
      y: String(e.labelY),
      'text-anchor': 'middle',
    });
    label.classList.add('sm-edge-label');
    const [main, sub] = edgeLabelParts(e.event);
    const t1 = el('tspan', { x: String(e.labelX) });
    t1.textContent = main;
    label.appendChild(t1);
    if (sub) {
      const t2 = el('tspan', { x: String(e.labelX), dy: '11' });
      t2.textContent = sub;
      label.appendChild(t2);
    }
    const title = el('title');
    title.textContent = `${e.source} --[${e.event}]--> ${e.target}`;
    g.appendChild(path);
    g.appendChild(label);
    g.appendChild(title);
    svg.appendChild(g);
  }

  // Nodes
  const stateNames = Object.keys(def.states);
  for (const name of stateNames) {
    const pos = layout.nodes.get(name)!;
    const desc = def.states[name]?.description || '';
    const g = el('g');
    g.classList.add('sm-node');
    if (name === currentState) g.classList.add('sm-node-current');

    const halo = el('rect', {
      x: String(pos.x - 5),
      y: String(pos.y - 5),
      width: String(NODE_W + 10),
      height: String(NODE_H + 10),
      rx: '13',
    });
    halo.classList.add('sm-halo');

    const rect = el('rect', {
      x: String(pos.x),
      y: String(pos.y),
      width: String(NODE_W),
      height: String(NODE_H),
      rx: '9',
    });
    rect.classList.add('sm-node-rect');

    const lines = wrapStateName(name);
    const lineH = 11.5;
    const startY = pos.y + NODE_H / 2 - ((lines.length - 1) * lineH) / 2 + 4;
    const text = el('text', { x: String(pos.x + NODE_W / 2), 'text-anchor': 'middle' });
    text.classList.add('sm-node-text');
    lines.forEach((line, i) => {
      const tspan = el('tspan', {
        x: String(pos.x + NODE_W / 2),
        y: String(startY + i * lineH),
      });
      tspan.textContent = line;
      text.appendChild(tspan);
    });

    const title = el('title');
    title.textContent = desc ? `${name} — ${desc}` : name;

    g.appendChild(halo);
    g.appendChild(rect);
    g.appendChild(title);
    g.appendChild(text);

    // Hover: highlight the node's transitions, dim the rest
    g.addEventListener('mouseenter', () =>
      highlightNode(name, svg),
    );
    g.addEventListener('mouseleave', () => clearHighlight(svg));

    svg.appendChild(g);
  }

  // Initial-state marker (filled dot + stub line, UML style)
  const init = layout.nodes.get(layout.initial);
  if (init) {
    const cy = init.y + NODE_H / 2;
    const dot = el('circle', { cx: String(init.x - 13), cy: String(cy), r: '4' });
    dot.classList.add('sm-initial-dot');
    svg.appendChild(dot);
    const line = el('line', {
      x1: String(init.x - 9),
      y1: String(cy),
      x2: String(init.x - 1),
      y2: String(cy),
    });
    line.classList.add('sm-initial-line');
    svg.appendChild(line);
  }

  container.appendChild(svg);
}

function highlightNode(name: string, svg: SVGElement): void {
  for (const g of svg.querySelectorAll<SVGGElement>('.sm-edge')) {
    const p = g.querySelector('path');
    const src = p?.dataset.source;
    const tgt = p?.dataset.target;
    const related = src === name || tgt === name;
    g.classList.toggle('sm-edge-hl', related);
    g.classList.toggle('sm-edge-dim', !related);
  }
}

function clearHighlight(svg: SVGElement): void {
  for (const g of svg.querySelectorAll<SVGGElement>('.sm-edge')) {
    g.classList.remove('sm-edge-hl', 'sm-edge-dim');
  }
}

/** True if the machine has any scoped (subsystem-directed) or internal events. */
export function hasScopedEvents(def: StateMachineDef): boolean {
  for (const s of Object.values(def.states)) {
    for (const t of s.transitions) {
      if (t.event.includes('/')) return true;
    }
  }
  return false;
}
