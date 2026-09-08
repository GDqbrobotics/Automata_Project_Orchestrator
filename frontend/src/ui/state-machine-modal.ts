/**
 * State machine inspector modal.
 *
 * Opens when the operator clicks a subsystem card or an object card:
 *   - graphical representation of the state machine (SVG, from the backend
 *     definition) with the current state enlightened,
 *   - live feed of the traffic it observes:
 *       subsystem → MQTT messages sent to / received from it
 *       object    → internal messages (transitions + fired scoped events)
 *
 * The component is stateless between `open()`/`update()` calls: the App
 * rebuilds the ModalData on every render so the open modal stays live.
 */

import type { ObjectMessage, StateMachineDef, SubsystemMessage } from '../contracts.js';
import { hasScopedEvents, renderStateMachineGraph } from './sm-graph.js';

export type ModalKind = 'subsystem' | 'object';

export interface ModalData {
  kind: ModalKind;
  id: string;
  /** Display title (subsystem display name or object id). */
  title: string;
  /** Small caption under the title. */
  subtitle: string;
  def: StateMachineDef | null;
  /** Current state of the machine (null → nothing enlightened). */
  currentState: string | null;
  /** Description of the current state. */
  stateDescription: string;
  subsystemMessages?: SubsystemMessage[];
  objectMessages?: ObjectMessage[];
}

function formatTime(ts: string): string {
  if (!ts) return '';
  const d = new Date(ts);
  if (!isNaN(d.getTime())) return d.toLocaleTimeString([], { hour12: false });
  const m = ts.match(/\d{2}:\d{2}:\d{2}/);
  return m ? m[0] : ts;
}

function truncate(s: string, n = 180): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function stringifyPayload(p: unknown): string {
  if (p === null || p === undefined) return '';
  if (typeof p === 'string') return p;
  try {
    return JSON.stringify(p);
  } catch {
    return String(p);
  }
}

export class StateMachineModal {
  private overlay: HTMLDivElement;
  private titleEl: HTMLElement;
  private subtitleEl: HTMLElement;
  private stateValueEl: HTMLElement;
  private stateDescEl: HTMLElement;
  private graphWrapEl: HTMLElement;
  private legendEl: HTMLDivElement;
  private feedTitleEl: HTMLElement;
  private feedCountEl: HTMLElement;
  private feedEl: HTMLDivElement;
  private data: ModalData | null = null;
  private onClose: (() => void) | null = null;

  constructor(parent: ParentNode = document.body) {
    this.overlay = document.createElement('div');
    this.overlay.className = 'sm-overlay';
    this.overlay.innerHTML = `
      <div class="sm-modal" role="dialog" aria-modal="true">
        <div class="sm-modal-header">
          <div class="sm-modal-title">
            <span class="sm-modal-name"></span>
            <span class="sm-modal-sub"></span>
          </div>
          <div class="sm-modal-state">
            <span class="sm-modal-state-label">current</span>
            <span class="sm-modal-state-value"></span>
            <span class="sm-modal-state-desc"></span>
          </div>
          <button class="sm-modal-close" aria-label="Close" title="Close (Esc)">×</button>
        </div>
        <div class="sm-modal-graph-wrap">
          <div class="sm-modal-graph"></div>
        </div>
        <div class="sm-legend"></div>
        <div class="sm-modal-feed-header">
          <span class="sm-modal-feed-title"></span>
          <span class="sm-modal-feed-count"></span>
        </div>
        <div class="sm-modal-feed"></div>
      </div>`;

    this.titleEl = this.overlay.querySelector('.sm-modal-name')!;
    this.subtitleEl = this.overlay.querySelector('.sm-modal-sub')!;
    this.stateValueEl = this.overlay.querySelector('.sm-modal-state-value')!;
    this.stateDescEl = this.overlay.querySelector('.sm-modal-state-desc')!;
    this.graphWrapEl = this.overlay.querySelector('.sm-modal-graph')!;
    this.legendEl = this.overlay.querySelector('.sm-legend')!;
    this.feedTitleEl = this.overlay.querySelector('.sm-modal-feed-title')!;
    this.feedCountEl = this.overlay.querySelector('.sm-modal-feed-count')!;
    this.feedEl = this.overlay.querySelector('.sm-modal-feed')!;

    this.overlay.querySelector('.sm-modal-close')!.addEventListener('click', () => this.close());
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.close();
    });
    document.addEventListener('keydown', this._onKey);

    parent.appendChild(this.overlay);
  }

  private _onKey = (e: KeyboardEvent): void => {
    if (e.key === 'Escape' && this.data) this.close();
  };

  get isOpen(): boolean {
    return this.data !== null;
  }

  open(data: ModalData, onClose: () => void): void {
    this.data = data;
    this.onClose = onClose;
    this.overlay.classList.add('open');
    this.render();
  }

  update(data: ModalData): void {
    if (!this.data) return;
    this.data = data;
    this.render();
  }

  close(): void {
    if (!this.data) return;
    this.data = null;
    this.overlay.classList.remove('open');
    this.graphWrapEl.innerHTML = '';
    this.feedEl.innerHTML = '';
    const cb = this.onClose;
    this.onClose = null;
    cb?.();
  }

  destroy(): void {
    document.removeEventListener('keydown', this._onKey);
    this.overlay.remove();
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------
  private render(): void {
    const d = this.data;
    if (!d) return;

    this.titleEl.textContent = d.title;
    this.subtitleEl.textContent = d.subtitle;
    this.stateValueEl.textContent = d.currentState || '—';
    this.stateValueEl.classList.toggle('unknown', !d.currentState);
    this.stateDescEl.textContent = d.stateDescription;

    // ── graph ──
    this.graphWrapEl.innerHTML = '';
    this.legendEl.innerHTML = '';
    if (d.def && Object.keys(d.def.states).length > 0) {
      renderStateMachineGraph(this.graphWrapEl, d.def, d.currentState);
      this.renderLegend(d.def);
    } else {
      this.graphWrapEl.innerHTML =
        '<div class="sm-empty">State machine definition not received yet.<br>' +
        'Press “⟳ Refresh Status” or wait for the next state broadcast.</div>';
    }

    // ── message feed ──
    const isSubsystem = d.kind === 'subsystem';
    this.feedTitleEl.textContent = isSubsystem
      ? `MQTT traffic — ${d.id}`
      : `Internal messages — ${d.id}`;

    this.feedEl.innerHTML = '';
    if (isSubsystem) {
      const msgs = d.subsystemMessages || [];
      this.feedCountEl.textContent = `${msgs.length} message${msgs.length === 1 ? '' : 's'}`;
      if (msgs.length === 0) {
        this.feedEl.appendChild(this.emptyFeedLine('No MQTT traffic recorded yet'));
      } else {
        for (const m of msgs) this.feedEl.appendChild(this.subsystemMsgLine(m));
      }
    } else {
      const msgs = d.objectMessages || [];
      this.feedCountEl.textContent = `${msgs.length} message${msgs.length === 1 ? '' : 's'}`;
      if (msgs.length === 0) {
        this.feedEl.appendChild(this.emptyFeedLine('No internal messages recorded yet'));
      } else {
        for (const m of msgs) this.feedEl.appendChild(this.objectMsgLine(m));
      }
    }
    this.feedEl.scrollTop = this.feedEl.scrollHeight;
  }

  private renderLegend(def: StateMachineDef): void {
    const items: Array<[string, string]> = [
      ['lg-current', 'current state'],
      ['lg-initial', 'initial state'],
    ];
    if (hasScopedEvents(def)) {
      items.push(['lg-scoped', 'event → subsystem (MQTT)']);
      items.push(['lg-internal', 'internal pipeline event']);
    }
    for (const [cls, label] of items) {
      const span = document.createElement('span');
      const i = document.createElement('i');
      i.className = `lg ${cls}`;
      span.appendChild(i);
      span.appendChild(document.createTextNode(label));
      this.legendEl.appendChild(span);
    }
  }

  private emptyFeedLine(text: string): HTMLElement {
    const div = document.createElement('div');
    div.className = 'sm-feed-empty';
    div.textContent = text;
    return div;
  }

  private subsystemMsgLine(m: SubsystemMessage): HTMLElement {
    const div = document.createElement('div');
    div.className = `sm-msg ${m.direction}`;

    const head = document.createElement('div');
    head.className = 'sm-msg-head';

    const dir = document.createElement('span');
    dir.className = 'sm-msg-dir';
    dir.textContent = m.direction === 'sent' ? '→ sent' : '← received';

    const topic = document.createElement('span');
    topic.className = 'sm-msg-topic';
    topic.textContent = m.topic;

    head.appendChild(dir);
    head.appendChild(topic);
    if ((m.count || 1) > 1) {
      const count = document.createElement('span');
      count.className = 'sm-msg-count';
      count.title = 'Consecutive identical messages (e.g. heartbeats)';
      count.textContent = `×${m.count}`;
      head.appendChild(count);
    }

    const time = document.createElement('span');
    time.className = 'sm-msg-time';
    time.textContent = formatTime(m.timestamp);
    head.appendChild(time);

    div.appendChild(head);

    const payload = document.createElement('div');
    payload.className = 'sm-msg-payload';
    payload.textContent = truncate(stringifyPayload(m.payload));
    div.appendChild(payload);

    return div;
  }

  private objectMsgLine(m: ObjectMessage): HTMLElement {
    const div = document.createElement('div');

    const head = document.createElement('div');
    head.className = 'sm-msg-head';

    const time = document.createElement('span');
    time.className = 'sm-msg-time sm-msg-time-first';
    time.textContent = formatTime(m.timestamp);
    head.appendChild(time);

    if (m.kind === 'transition') {
      div.classList.add('sm-msg', 'om-transition');

      const states = document.createElement('span');
      states.className = 'om-states';
      const arrow = document.createElement('span');
      arrow.className = 'om-arrow';
      arrow.textContent = ' → ';
      states.appendChild(document.createTextNode(m.from || '?'));
      states.appendChild(arrow);
      states.appendChild(document.createTextNode(m.to || '?'));
      head.appendChild(states);

      const evt = document.createElement('span');
      evt.className = 'om-event';
      evt.textContent = `event: ${m.event}`;
      head.appendChild(evt);
    } else {
      div.classList.add('sm-msg', 'om-event-msg');

      const evt = document.createElement('span');
      evt.className = `om-event evt-${m.event.includes('/') ? (m.event.split('/')[0] === 'workflow' ? 'internal' : 'scoped') : 'plain'}`;
      evt.textContent = m.event;
      head.appendChild(evt);

      const target = document.createElement('span');
      target.className = 'om-target';
      target.textContent = `→ ${formatObjectTarget(m.target)}`;
      head.appendChild(target);
    }

    div.appendChild(head);
    return div;
  }
}

function formatObjectTarget(target?: string): string {
  if (!target) return 'orchestrator';
  if (target === 'pipeline (internal)') return 'pipeline (internal)';
  if (target === 'all subsystems (broadcast)') return 'all subsystems (broadcast)';
  return `${target} subsystem`;
}
