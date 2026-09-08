import type { FrontendMessage, WorkflowCommand, WorkflowStateType } from '../contracts.js';
import { FrontendClient } from '../api/client.js';
import { createInitialState, reduceState } from '../state/store.js';
import { StateMachineModal } from './state-machine-modal.js';
import type { ModalData, ModalKind } from './state-machine-modal.js';
import {
  buildStateStyles,
  getObjectStateIndex,
  getStateColor,
  getTotalSteps,
} from '../config/object-styles.js';
import { getButtonRules } from '../config/button-rules.js';
import { getStateMessage } from '../config/state-messages.js';

interface AppConfig {
  wsUrl?: string;
  onStateChange?: (state: WorkflowStateType) => void;
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
    // Connection status
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

  // UI rendering
  private render(): void {
    this.updateStateBadge();
    this.updateProgress();
    this.updateObjectsGrid();
    this.updateSubsystemGrid();
    this.updateActionButtons();
    this.updateConnectionIndicator();
    this.updateEventLog();
    this.updateWorkflowMessage();
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
    this.detailModal.open(this.buildDetailData(kind, id), () => {
      this.detailRef = null;
    });
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

  /** Cached state styles, built from config or defaults. */
  private _stateStyles: Record<string, { colorClass: string; stepIndex: number }> = buildStateStyles();
  /** Whether backend has sent us a custom state order / colors. */
  private _backendConfigApplied = false;

  private updateStateBadge(): void {
    const badge = document.getElementById('stateBadge');
    if (!badge) return;
    badge.textContent = this.state.workflowState;
    badge.className = `state-badge ${this.state.workflowState}`;
  }

  private updateWorkflowMessage(): void {
    const el = document.getElementById('workflowMessage');
    if (!el) return;
    // Error state: always show the concrete error detail
    if (this.state.workflowState === 'error') {
      el.textContent = getStateMessage('error', this.state.error || undefined);
      return;
    }
    // Prefer messages sent by the backend (from global.yaml),
    // fall back to the frontend defaults from config/state-messages.ts
    const backendMsg = this.state.workflowStateMessages[this.state.workflowState];
    el.textContent =
      backendMsg ||
      getStateMessage(this.state.workflowState, this.state.error || undefined);
  }

  private updateProgress(): void {
    const label = document.getElementById('progressLabel');
    const fill = document.getElementById('progressFill');
    if (label) label.textContent = `Progress: ${this.state.progress} / ${this.state.totalObjects}`;
    if (fill) {
      const pct = this.state.totalObjects > 0
        ? Math.min((this.state.progress / this.state.totalObjects) * 100, 100)
        : 0;
      fill.style.width = `${pct}%`;
    }
  }

  private updateSubsystemGrid(): void {
    const grid = document.getElementById('subsystemGrid');
    if (!grid) return;
    grid.innerHTML = '';

    // Use configured subsystems if available, otherwise fallback to status keys
    const configNames = this.state.subsystemsConfig.map(cfg => cfg.name);
    const statusNames = Object.keys(this.state.subsystems);
    // Prefer config order, but include any extra subsystems from status
    const allNames = [...configNames, ...statusNames.filter(n => !configNames.includes(n))];

    if (allNames.length === 0) {
      grid.innerHTML = '<div style="color: var(--text-muted); padding: 10px;">No subsystems configured</div>';
      return;
    }

    for (const name of allNames) {
      const config = this.state.subsystemsConfig.find(cfg => cfg.name === name);
      const info = this.state.subsystems[name];
      const card = document.createElement('div');
      card.className = 'subsystem-card';

      // Determine display name and state
      const displayName = config
        ? config.display_name || name
        : name;
      const stateClass = info ? info.state : 'offline';
      const stateText = info ? info.state : 'offline';
      const stateDescription = info ? info.state_description || '' : '';

      // Show required indicator
      const requiredBadge = config && config.required
        ? '<span class="required-badge" title="Required">*</span>'
        : '';

      // Show state description if available
      const descriptionHtml = stateDescription
        ? `<div class="subsystem-state-desc">${stateDescription}</div>`
        : '';

      card.innerHTML = `
        <div class="subsystem-name">${displayName}${requiredBadge}</div>
        <div class="subsystem-state ${stateClass}">${stateText}</div>
        ${descriptionHtml}
      `;
      card.style.cursor = 'pointer';
      card.title = 'Click to view state machine and MQTT traffic';
      card.addEventListener('click', () => this.openDetail('subsystem', name));
      grid.appendChild(card);
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
    if (dot) dot.className = `connection-dot ${this.state.connectionStatus}`;
    if (text) text.textContent = this.state.connectionStatus === 'connected' ? 'Connected' : 'Disconnected';
  }

  private updateEventLog(): void {
    const log = document.getElementById('eventLog');
    if (!log) return;
    log.innerHTML = '';

    for (const entry of this.state.logs) {
      const div = document.createElement('div');
      div.className = 'log-entry';
      div.innerHTML = `<span class="log-time">${entry.time}</span><span class="log-source">[${entry.source}]</span><span class="log-msg ${entry.level}">${entry.message}</span>`;
      log.appendChild(div);
    }

    log.scrollTop = log.scrollHeight;
  }

  private updateObjectsGrid(): void {
    const grid = document.getElementById('objectsGrid');
    if (!grid) return;
    grid.innerHTML = '';

    const objects = this.state.objects;
    if (objects.length === 0) {
      grid.innerHTML = '<div style="color: var(--text-muted); padding: 10px;">No objects tracked yet</div>';
      return;
    }

    // Build state styles — use backend config if available, else defaults
    const stateOrder = this.state.objectStateOrder?.length > 0
      ? this.state.objectStateOrder
      : undefined;
    const stateColors = Object.keys(this.state.objectStateColors || {}).length > 0
      ? this.state.objectStateColors!
      : undefined;
    this._stateStyles = buildStateStyles(stateOrder, stateColors);
    this._backendConfigApplied = true;
    const totalSteps = getTotalSteps(stateOrder);

    for (const obj of objects) {
      const card = document.createElement('div');
      card.className = 'object-card';

      const style = this._stateStyles[obj.state] || { colorClass: 'gray', stepIndex: 0 };
      const stateClass = `state-${style.colorClass}`;
      const transitionCount = obj.transitions.length;
      const stepIndex = style.stepIndex;

      card.innerHTML = `
        <div class="object-name">${obj.object_id}</div>
        <div class="object-state ${stateClass}">${obj.state}</div>
        <div class="object-state-desc">${obj.state_description || ''}</div>
        <div class="object-progress">
          <span class="object-step">Step ${stepIndex + 1} / ${totalSteps}</span>
          <span class="object-transitions">${transitionCount} transitions</span>
        </div>
      `;
      card.style.cursor = 'pointer';
      card.title = 'Click to view state machine and internal messages';
      card.addEventListener('click', () => this.openDetail('object', obj.object_id));
      grid.appendChild(card);
    }
  }

  /**
   * Extract state order from backend config if available.
   * Falls back to defaults from config/object-styles.ts.
   */
  private _extractStateOrder(): string[] | undefined {
    if (this.state.objectStateOrder && this.state.objectStateOrder.length > 0) {
      return this.state.objectStateOrder;
    }
    return undefined;
  }

  /**
   * Extract state colors from backend config if available.
   * Falls back to defaults from config/object-styles.ts.
   */
  private _extractStateColors(): Record<string, string> {
    if (this.state.objectStateColors && Object.keys(this.state.objectStateColors).length > 0) {
      return this.state.objectStateColors;
    }
    return {};
  }

  // Public API for binding buttons
  onBindCommands(buttonSelectors: Record<string, WorkflowCommand>): void {
    for (const [selector, cmd] of Object.entries(buttonSelectors)) {
      const el = document.getElementById(selector);
      if (el) el.addEventListener('click', () => this.sendCommand(cmd));
    }
  }
}
