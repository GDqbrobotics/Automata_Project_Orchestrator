import type { FrontendMessage, WorkflowCommand, WorkflowStateType } from '../contracts.js';
import { FrontendClient } from '../api/client.js';
import { createInitialState, reduceState } from '../state/store.js';
import { StateMachineModal } from './state-machine-modal.js';
import type { ModalData, ModalKind } from './state-machine-modal.js';
import { buildStateStyles, getTotalSteps } from '../config/object-styles.js';
import { getButtonRules } from '../config/button-rules.js';
import { getStateMessage } from '../config/state-messages.js';

interface AppConfig {
  wsUrl?: string;
  onStateChange?: (state: WorkflowStateType) => void;
}

/** Create an element with an optional class and text content (DOM-safe). */
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/**
 * Map a raw subsystem state to a semantic tone class.
 *
 * The backend can report any state name defined in the subsystem YAMLs, so
 * instead of enumerating every state in CSS we collapse them into a small
 * set of tones: ready / busy / warning / error / idle / offline.
 */
function subsystemTone(state: string | null | undefined): string {
  switch (state) {
    case 'ready':
      return 'ready';
    case 'error':
      return 'error';
    case 'offline':
      return 'offline';
    case 'paused':
    case 'tool_changing':
      return 'warning';
    case 'stopped':
    case 'initializing':
    case 'reset':
      return 'idle';
    default:
      return 'busy';
  }
}

export class App {
  private client: FrontendClient;
  private state = createInitialState();
  private onStateChange?: (state: WorkflowStateType) => void;
  private detailModal: StateMachineModal;
  private detailRef: { kind: ModalKind; id: string } | null = null;

  constructor(config: AppConfig = {}) {
    this.client = new FrontendClient(config.wsUrl || 'ws://localhost:8080');
    this.onStateChange = config.onStateChange;
    this.detailModal = new StateMachineModal();
  }

  start(): void {
    this.client.connect((msg) => this.onMessage(msg));
    this.render();
  }

  stop(): void {
    this.client.close();
  }

  private onMessage(msg: FrontendMessage): void {
    // Connection status (pseudo-events pushed by the client on open/close)
    if (this.client.connected) {
      if (this.state.connectionStatus !== 'connected') {
        this.state = { ...this.state, connectionStatus: 'connected' };
      }
    } else {
      this.state = { ...this.state, connectionStatus: 'disconnected' };
    }

    this.state = reduceState(this.state, msg);
    this.render();

    if (this.onStateChange) {
      this.onStateChange(this.state.workflowState);
    }
  }

  sendCommand(cmd: WorkflowCommand): void {
    this.client.send(cmd);
  }

  /** Assign (or replace) the operator alias ID of an object. */
  assignAlias(objectId: string, alias: string): void {
    this.client.send('assign_alias', { object_id: objectId, alias });
  }

  /**
   * Debug mode (--debug): inject an MQTT message on one of the topics a
   * real subsystem is expected to publish, as if the subsystem sent it.
   * The backend rejects it when it is not running with --debug.
   */
  sendDebugSubsystemMessage(
    subsystemId: string,
    topic: string,
    payload: Record<string, unknown>,
  ): void {
    this.client.send('debug_send_subsystem_message', {
      subsystem: subsystemId,
      topic,
      payload,
    });
  }

  // UI rendering
  private render(): void {
    this.updateStateBadge();
    this.updateWorkflowMessage();
    this.updateProgress();
    this.updateObjectsGrid();
    this.updateAliasSelect();
    this.updateSubsystemGrid();
    this.updateActionButtons();
    this.updateConnectionIndicator();
    this.updateEventLog();
    // Keep the state machine inspector live while it is open
    if (this.detailRef) {
      this.detailModal.update(this.buildDetailData(this.detailRef.kind, this.detailRef.id));
    }
  }

  // ------------------------------------------------------------------
  // State machine inspector (subsystem / object detail modal)
  // ------------------------------------------------------------------
  openDetail(kind: ModalKind, id: string): void {
    this.detailRef = { kind, id };
    this.detailModal.open(
      this.buildDetailData(kind, id),
      () => {
        this.detailRef = null;
      },
      (topic, payload) => {
        // Only reachable from subsystem modals (the debug panel is hidden
        // for object modals and when debug mode is off).
        if (this.detailRef && this.detailRef.kind === 'subsystem') {
          this.sendDebugSubsystemMessage(this.detailRef.id, topic, payload);
        }
      },
    );
  }

  private buildDetailData(kind: ModalKind, id: string): ModalData {
    if (kind === 'subsystem') {
      const info = this.state.subsystems[id];
      const cfg = this.state.subsystemsConfig.find((c) => c.name === id);
      const def = this.state.subsystemStateMachines[id] || null;
      const rawState = info?.state || null;
      const currentState = rawState && def && def.states[rawState] ? rawState : null;
      return {
        kind,
        id,
        title: cfg?.display_name || id,
        subtitle: `subsystem · ${id}${cfg?.type ? ` · ${cfg.type}` : ''}`,
        def,
        currentState,
        stateDescription:
          (currentState && def?.states[currentState]?.description) ||
          (info?.state_description || ''),
        subsystemMessages: this.state.subsystemMessages[id] || [],
        // Debug mode (--debug): offer the manual MQTT injection panel with
        // the topics the real subsystem is expected to publish.
        debugTopics: this.state.debug?.enabled
          ? this.state.debug.topics[id] || undefined
          : undefined,
      };
    }
    const obj = this.state.objects.find((o) => o.object_id === id);
    const def = this.state.objectStateMachine;
    const rawState = obj?.state || null;
    const currentState = rawState && def && def.states[rawState] ? rawState : null;
    return {
      kind,
      id,
      title: id,
      subtitle: `object · ${rawState || 'unknown state'}`,
      def,
      currentState,
      stateDescription:
        obj?.state_description || (currentState && def?.states[currentState]?.description) || '',
      objectMessages: this.state.objectMessages[id] || [],
    };
  }

  // ------------------------------------------------------------------
  // Panel updates
  // ------------------------------------------------------------------

  private updateStateBadge(): void {
    const badge = document.getElementById('stateBadge');
    if (!badge) return;
    badge.textContent = this.state.workflowState;
    badge.className = `state-badge ${this.state.workflowState}`;
  }

  private updateWorkflowMessage(): void {
    const elMsg = document.getElementById('workflowMessage');
    if (!elMsg) return;
    // Error state: always show the concrete error detail
    if (this.state.workflowState === 'error') {
      elMsg.textContent = getStateMessage('error', this.state.error || undefined);
      return;
    }
    // Prefer messages sent by the backend (from global.yaml),
    // fall back to the frontend defaults from config/state-messages.ts
    const backendMsg = this.state.workflowStateMessages[this.state.workflowState];
    elMsg.textContent =
      backendMsg || getStateMessage(this.state.workflowState, this.state.error || undefined);
  }

  private updateProgress(): void {
    const label = document.getElementById('progressLabel');
    const fill = document.getElementById('progressFill');
    const pct = document.getElementById('progressPct');
    const { progress, totalObjects } = this.state;
    const percent =
      totalObjects > 0 ? Math.min(Math.round((progress / totalObjects) * 100), 100) : 0;
    if (label) label.textContent = `Progress: ${progress} / ${totalObjects}`;
    if (fill) fill.style.width = `${percent}%`;
    if (pct) pct.textContent = `${percent}%`;
  }

  private updateObjectsGrid(): void {
    const grid = document.getElementById('objectsGrid');
    if (!grid) return;
    grid.innerHTML = '';

    const countEl = document.getElementById('objectsCount');
    if (countEl) {
      const n = this.state.objects.length;
      countEl.textContent = `${n} object${n === 1 ? '' : 's'}`;
    }

    const objects = this.state.objects;
    if (objects.length === 0) {
      grid.appendChild(el('div', 'empty-hint', 'No objects tracked yet'));
      return;
    }

    // Build state styles from backend config when available, else defaults
    const stateOrder =
      this.state.objectStateOrder.length > 0 ? this.state.objectStateOrder : undefined;
    const stateColors =
      Object.keys(this.state.objectStateColors).length > 0
        ? this.state.objectStateColors
        : undefined;
    const stateStyles = buildStateStyles(stateOrder, stateColors);
    const totalSteps = getTotalSteps(stateOrder);

    for (const obj of objects) {
      const style = stateStyles[obj.state] || { colorClass: 'gray', stepIndex: 0 };
      const stepIndex = style.stepIndex;
      const percent =
        totalSteps > 0 ? Math.round(((stepIndex + 1) / totalSteps) * 100) : 0;

      const card = el('div', 'object-card');
      if (style.colorClass === 'analyzed') card.classList.add('is-done');
      card.title = 'Click to view state machine and internal messages';

      const top = el('div', 'object-top');
      top.appendChild(el('span', 'object-name', obj.object_id));
      const chip = el('span', `object-state state-${style.colorClass}`, obj.state);
      chip.title = obj.state;
      top.appendChild(chip);

      const foot = el('div', 'object-foot');
      const bar = el('div', 'object-bar');
      const barFill = el('div', 'object-bar-fill');
      barFill.style.width = `${percent}%`;
      bar.appendChild(barFill);
      foot.appendChild(bar);
      foot.appendChild(
        el('span', 'object-step', `${stepIndex + 1}/${totalSteps}`),
      );

      card.appendChild(top);
      if (obj.alias) {
        const aliasEl = el('div', 'object-alias', obj.alias);
        aliasEl.title = 'Alias ID (cloud database)';
        card.appendChild(aliasEl);
      }
      card.appendChild(el('div', 'object-state-desc', obj.state_description || ''));
      card.appendChild(foot);

      card.addEventListener('click', () => this.openDetail('object', obj.object_id));
      grid.appendChild(card);
    }
  }

  private updateSubsystemGrid(): void {
    const grid = document.getElementById('subsystemGrid');
    if (!grid) return;
    grid.innerHTML = '';

    // Prefer config order, but include any extra subsystems from status
    const configNames = this.state.subsystemsConfig.map((cfg) => cfg.name);
    const statusNames = Object.keys(this.state.subsystems);
    const allNames = [
      ...configNames,
      ...statusNames.filter((n) => !configNames.includes(n)),
    ];

    const countEl = document.getElementById('subsystemsCount');
    if (countEl) countEl.textContent = `${allNames.length}`;

    if (allNames.length === 0) {
      grid.appendChild(el('div', 'empty-hint', 'No subsystems configured'));
      return;
    }

    for (const name of allNames) {
      const config = this.state.subsystemsConfig.find((cfg) => cfg.name === name);
      const info = this.state.subsystems[name];
      const state = info?.state || 'offline';
      const tone = subsystemTone(info ? state : 'offline');
      const displayName = config?.display_name || name;
      const description = info?.state_description || '';

      const card = el('div', 'subsystem-card');
      card.title = 'Click to view state machine and MQTT traffic';

      const top = el('div', 'subsystem-top');
      top.appendChild(el('span', `subsystem-dot tone-${tone}`));
      const nameEl = el('span', 'subsystem-name', displayName);
      if (config?.required) {
        const badge = el('span', 'required-badge', '*');
        badge.title = 'Required subsystem';
        nameEl.appendChild(badge);
      }
      top.appendChild(nameEl);

      card.appendChild(top);
      card.appendChild(el('span', `subsystem-state tone-${tone}`, state));
      if (description) {
        card.appendChild(el('div', 'subsystem-state-desc', description));
      }

      card.addEventListener('click', () => this.openDetail('subsystem', name));
      grid.appendChild(card);
    }
  }

  /** Keep the alias-assignment object selector in sync with tracked objects. */
  private updateAliasSelect(): void {
    const select = document.getElementById('aliasObjectSelect') as HTMLSelectElement | null;
    if (!select) return;
    const objects = this.state.objects;
    const current = select.value;

    select.innerHTML = '';
    if (objects.length === 0) {
      const opt = el('option', undefined, '— no objects —');
      opt.value = '';
      opt.disabled = true;
      select.appendChild(opt);
      select.disabled = true;
      return;
    }
    select.disabled = false;
    for (const obj of objects) {
      const opt = el('option', undefined, obj.object_id);
      opt.value = obj.object_id;
      select.appendChild(opt);
    }
    if (objects.some((o) => o.object_id === current)) {
      select.value = current;
    }
  }

  private updateActionButtons(): void {
    const rules = getButtonRules(this.state.workflowState);
    const startBtn = document.getElementById('btnStart') as HTMLButtonElement | null;
    const pauseBtn = document.getElementById('btnPause') as HTMLButtonElement | null;
    const resumeBtn = document.getElementById('btnResume') as HTMLButtonElement | null;
    const stopBtn = document.getElementById('btnStop') as HTMLButtonElement | null;
    const resetBtn = document.getElementById('btnReset') as HTMLButtonElement | null;

    if (startBtn) startBtn.disabled = !rules.start;
    if (pauseBtn) pauseBtn.disabled = !rules.pause;
    if (resumeBtn) resumeBtn.disabled = !rules.resume;
    if (stopBtn) stopBtn.disabled = !rules.stop;
    if (resetBtn) resetBtn.disabled = !rules.reset;
  }

  private updateConnectionIndicator(): void {
    const dot = document.getElementById('connectionDot');
    const text = document.getElementById('connectionText');
    if (dot) dot.className = `connection-dot ${this.state.connectionStatus === 'connected' ? 'connected' : ''}`;
    if (text) text.textContent = this.state.connectionStatus === 'connected' ? 'Connected' : 'Disconnected';
  }

  private updateEventLog(): void {
    const log = document.getElementById('eventLog');
    if (!log) return;
    log.innerHTML = '';

    for (const entry of this.state.logs) {
      const row = el('div', 'log-entry');
      row.appendChild(el('span', 'log-time', entry.time));
      row.appendChild(el('span', 'log-source', `[${entry.source}]`));
      row.appendChild(el('span', `log-msg ${entry.level}`, entry.message));
      log.appendChild(row);
    }

    log.scrollTop = log.scrollHeight;
  }

  // Public API for binding buttons
  onBindCommands(buttonSelectors: Record<string, WorkflowCommand>): void {
    for (const [selector, cmd] of Object.entries(buttonSelectors)) {
      const elBtn = document.getElementById(selector);
      if (elBtn) elBtn.addEventListener('click', () => this.sendCommand(cmd));
    }
  }
}
