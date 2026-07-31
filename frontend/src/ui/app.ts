import type { FrontendMessage, WorkflowCommand, WorkflowStateType } from '../contracts.js';
import { FrontendClient } from '../api/client.js';
import { createInitialState, reduceState } from '../state/store.js';

interface AppConfig {
  wsUrl?: string;
  onStateChange?: (state: WorkflowStateType) => void;
}

export class App {
  private client: FrontendClient;
  private state = createInitialState();
  private onStateChange?: (state: WorkflowStateType) => void;

  constructor(config: AppConfig = {}) {
    this.client = new FrontendClient(config.wsUrl || 'ws://localhost:8080');
    this.onStateChange = config.onStateChange;
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
  }

  private updateStateBadge(): void {
    const badge = document.getElementById('stateBadge');
    if (!badge) return;
    badge.textContent = this.state.workflowState;
    badge.className = `state-badge ${this.state.workflowState}`;
  }

  private updateWorkflowMessage(): void {
    const el = document.getElementById('workflowMessage');
    if (!el) return;
    el.textContent = this.state.message;
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
      grid.appendChild(card);
    }
  }

  private updateActionButtons(): void {
    const state = this.state.workflowState;
    const startBtn = document.getElementById('btnStart') as HTMLButtonElement | null;
    const pauseBtn = document.getElementById('btnPause') as HTMLButtonElement | null;
    const resumeBtn = document.getElementById('btnResume') as HTMLButtonElement | null;
    const stopBtn = document.getElementById('btnStop') as HTMLButtonElement | null;
    const resetBtn = document.getElementById('btnReset') as HTMLButtonElement | null;

    const canStart = state === 'idle';
    const canPause = state === 'running';
    const canResume = state === 'paused';
    const canStop = state === 'running' || state === 'paused' || state === 'starting';
    const canReset = state === 'error';

    if (startBtn) startBtn.disabled = !canStart;
    if (pauseBtn) pauseBtn.disabled = !canPause;
    if (resumeBtn) resumeBtn.disabled = !canResume;
    if (stopBtn) stopBtn.disabled = !canStop;
    if (resetBtn) resetBtn.disabled = !canReset;
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

    for (const obj of objects) {
      const card = document.createElement('div');
      card.className = 'object-card';

      // Determine state class - map object states to color classes
      let stateClass = 'state-identified';
      if (obj.state === 'object_analyzed') {
        stateClass = 'state-analyzed';
      } else if (obj.state === 'moving_to_disk' || obj.state === 'moving_to_box') {
        stateClass = 'state-moving';
      } else if (obj.state === 'HSI_3DI_analysis') {
        stateClass = 'state-imaging';
      } else if (obj.state === 'XRF_RAMAN_analysis') {
        stateClass = 'state-measuring';
      } else if (obj.state === 'object_going_second_position' || obj.state === 'object_going_first_position') {
        stateClass = 'state-rotating';
      }

      // Determine progress: count transitions completed
      const transitionCount = obj.transitions.length;
      const stateIndex = this._getObjectStateIndex(obj.state);

      card.innerHTML = `
        <div class="object-name">${obj.object_id}</div>
        <div class="object-state ${stateClass}">${obj.state}</div>
        <div class="object-state-desc">${obj.state_description || ''}</div>
        <div class="object-progress">
          <span class="object-step">Step ${stateIndex + 1} / 8</span>
          <span class="object-transitions">${transitionCount} transitions</span>
        </div>
      `;
      grid.appendChild(card);
    }
  }

  private _getObjectStateIndex(state: string): number {
    const stateOrder = [
      'identified_in_box',
      'moving_to_disk',
      'HSI_3DI_analysis',
      'object_going_second_position',
      'XRF_RAMAN_analysis',
      'object_going_first_position',
      'moving_to_box',
      'object_analyzed',
    ];
    return stateOrder.indexOf(state);
  }

  // Public API for binding buttons
  onBindCommands(buttonSelectors: Record<string, WorkflowCommand>): void {
    for (const [selector, cmd] of Object.entries(buttonSelectors)) {
      const el = document.getElementById(selector);
      if (el) el.addEventListener('click', () => this.sendCommand(cmd));
    }
  }
}
