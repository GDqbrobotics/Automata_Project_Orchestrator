/**
 * Configuration wizard (debug mode only).
 *
 * A guided, multi-step editor for the YAML files under backend/config/:
 *
 *   ① Workflow    – workflow lifecycle metadata, global settings, object
 *                   blueprint summary + raw YAML editor;
 *   ② Subsystems  – edit existing subsystem files, create new ones from a
 *                   guided form, stage removals (with a reference check);
 *   ③ MQTT formats – the payload field-mapping layer (mqtt_formats.yaml)
 *                   as structured forms per message kind + per-subsystem
 *                   overrides;
 *   ④ Review      – validate everything, then save it all (and create /
 *                   delete the corresponding files) in one pass.
 *
 * The raw YAML text is the source of truth: structured forms are applied
 * server-side as comment-preserving line patches (`patch_config`), so the
 * wizard never silently reformats a file. Everything is validated by the
 * backend before it is written; the wizard only ever sees JSON.
 *
 * A restart is always required after saving: state machines, adapters and
 * MQTT formats are loaded at orchestrator startup.
 */

import type {
  ConfigFileSnapshot,
  ConfigPatch,
  ConfigSnapshotDetails,
  FrontendMessage,
  MessageFormatsDocument,
  MqttFormatGroup,
  WorkflowCommand,
} from '../contracts.js';

type SendCommand = (command: WorkflowCommand, data?: Record<string, unknown>) => void;
type WizardStep = 'workflow' | 'subsystems' | 'messages' | 'review';

interface WizardFile {
  path: string;
  label: string;
  category: string;
  /** The content as loaded (baseline for the dirty check). */
  original: string;
  /** The current buffer the wizard edits. */
  content: string;
  parsed: Record<string, unknown> | null;
  name?: string;
  simExists?: boolean;
  created?: boolean;
  deleted?: boolean;
  /** New subsystem: create <name>_sim.py when saving. */
  simRequested?: boolean;
  errors?: string[];
}

interface PendingOp {
  kind: 'save' | 'delete' | 'sim' | 'render_sm';
  path?: string;
  name?: string;
  content?: string;
  deleteSimulator?: boolean;
}

/** One action of a transition, as edited in the state-machine editor. */
interface DraftAction {
  type: 'log_event' | 'event' | 'other';
  /** Event name for type='event'; JSON text for type='other'. */
  value: string;
  /** Original action object for type='other' (unknown action kinds). */
  raw?: unknown;
}

interface DraftTransition {
  event: string;
  target: string;
  actions: DraftAction[];
}

interface DraftState {
  name: string;
  description: string;
  transitions: DraftTransition[];
  /** object.yaml per-state metadata, preserved untouched. */
  meta?: Record<string, unknown>;
}

interface StateMachineDraft {
  initial: string;
  states: DraftState[];
}

const STEPS: Array<{ id: WizardStep; label: string; icon: string }> = [
  { id: 'workflow', label: 'Workflow', icon: '①' },
  { id: 'subsystems', label: 'Subsystems', icon: '②' },
  { id: 'messages', label: 'MQTT formats', icon: '③' },
  { id: 'review', label: 'Review & save', icon: '④' },
];

/** Built-in fallbacks shown as placeholders when no value is configured. */
const FORMAT_FALLBACKS: Record<string, Record<string, string>> = {
  command: {
    event_field: 'event',
    source_field: 'source',
    source_value: 'orchestrator',
    timestamp_field: 'timestamp',
    object_id_field: 'object_id',
  },
  hardware_command: {
    event_field: 'event',
    source_field: 'source',
    source_value: 'orchestrator',
  },
  status: {
    state_field: 'state',
    ready_field: 'ready',
    paused_field: 'paused',
    name_field: 'name',
  },
  response: {
    type_field: 'type',
    objects_field: 'objects',
    count_field: 'count',
    object_id_field: 'object_id',
    points_field: 'points',
  },
  points_command: {
    event_field: 'event',
    object_id_field: 'object_id',
    points_field: 'points',
    source_field: 'source',
    source_value: 'orchestrator',
    timestamp_field: 'timestamp',
  },
};

// ---------------------------------------------------------------------------
// DOM / model helpers
// ---------------------------------------------------------------------------
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

function isObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function getPath(obj: unknown, path: string[]): unknown {
  let node: unknown = obj;
  for (const part of path) {
    if (!isObject(node)) return undefined;
    node = node[part];
  }
  return node;
}

function setPath(obj: Record<string, unknown>, path: Array<string | number>, value: unknown): void {
  if (path.length === 0) return;
  let node: Record<string, unknown> = obj;
  for (const part of path.slice(0, -1)) {
    const next = node[part];
    if (!isObject(next)) {
      node[part] = {};
    }
    node = node[part] as Record<string, unknown>;
  }
  node[path[path.length - 1]] = value;
}

function str(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.map((v) => String(v)).join(', ');
  return String(value);
}

function isDirty(file: WizardFile): boolean {
  return file.content !== file.original;
}

/** Build the editable draft of a state machine from its parsed YAML. */
function buildStateMachineDraft(parsed: Record<string, unknown> | null): StateMachineDraft {
  const statesRaw = isObject(parsed?.['states']) ? (parsed!['states'] as Record<string, unknown>) : {};
  const states: DraftState[] = [];
  for (const [name, defRaw] of Object.entries(statesRaw)) {
    const def = isObject(defRaw) ? defRaw : {};
    const transitions: DraftTransition[] = [];
    const transRaw = Array.isArray(def['transitions']) ? (def['transitions'] as unknown[]) : [];
    for (const tRaw of transRaw) {
      const t = isObject(tRaw) ? tRaw : {};
      const actions: DraftAction[] = [];
      const actionsRaw = Array.isArray(t['actions']) ? (t['actions'] as unknown[]) : [];
      for (const a of actionsRaw) {
        if (typeof a === 'string') {
          if (a === 'log_event') actions.push({ type: 'log_event', value: '' });
          else actions.push({ type: 'other', value: JSON.stringify(a), raw: a });
        } else if (isObject(a) && typeof a['event'] === 'string') {
          actions.push({ type: 'event', value: a['event'] });
        } else {
          actions.push({ type: 'other', value: JSON.stringify(a), raw: a });
        }
      }
      transitions.push({ event: str(t['event']), target: str(t['target']), actions });
    }
    const state: DraftState = {
      name,
      description: str(def['description']),
      transitions,
    };
    if (isObject(def['meta'])) state.meta = def['meta'];
    states.push(state);
  }
  return { initial: str(parsed?.['initial_state']), states };
}

function patchedInput(
  path: string,
  value: string,
  kind: 'text' | 'number' | 'bool' | 'csv' = 'text',
  placeholder = '',
): HTMLInputElement {
  const input = document.createElement('input');
  input.className = 'cw-input';
  input.type = kind === 'number' ? 'number' : 'text';
  if (kind === 'number') input.step = 'any';
  if (kind === 'bool') input.type = 'checkbox';
  else input.value = value;
  if (kind === 'bool') input.checked = value === 'true';
  input.placeholder = placeholder;
  input.dataset.patch = path;
  input.dataset.kind = kind;
  return input;
}

function fieldRow(label: string, control: HTMLElement, hint = ''): HTMLElement {
  const row = el('label', 'cw-field');
  const text = el('span', 'cw-field-label');
  text.appendChild(document.createTextNode(label));
  row.appendChild(text);
  row.appendChild(control);
  if (hint) row.appendChild(el('span', 'cw-field-hint', hint));
  return row;
}

// ---------------------------------------------------------------------------
// Wizard
// ---------------------------------------------------------------------------
export class ConfigWizard {
  private overlay: HTMLDivElement;
  private bodyEl: HTMLDivElement;
  private stepsEl: HTMLDivElement;
  private statusEl: HTMLElement;
  private backBtn: HTMLButtonElement;
  private nextBtn: HTMLButtonElement;
  private saveBtn: HTMLButtonElement;
  private send: SendCommand;

  private openFlag = false;
  private loading = false;
  private step: WizardStep = 'workflow';
  private files: WizardFile[] = [];
  private messageFormats: MessageFormatsDocument = { default: {}, subsystems: {} };

  private activeTopPath: string | null = null;
  private activeSubsystemPath: string | null = null;
  private creatingSubsystem = false;
  private overrideName = '';
  private newSubsystemSim = true;
  private newSubsystemSpec: Record<string, string | boolean> = {};

  /** Per-file state-machine drafts (persist while navigating the wizard). */
  private smDrafts = new Map<string, StateMachineDraft>();
  /** Files whose draft has unapplied state-machine edits. */
  private smDirtyPaths = new Set<string>();
  /** State-machine renders awaiting a response (interactive or queued). */
  private smRenderPending = new Set<string>();
  /** Expanded/selected states in the editor. */
  private expandedStates = new Set<string>();
  private subDetailTab: 'basics' | 'states' | 'raw' = 'basics';
  private newStateName = '';

  private pendingOps: PendingOp[] = [];
  private currentOp: PendingOp | null = null;
  private totalOps = 0;

  constructor(parent: ParentNode, send: SendCommand) {
    this.send = send;

    this.overlay = el('div', 'cw-overlay');
    this.overlay.innerHTML = `
      <div class="cw-modal" role="dialog" aria-modal="true">
        <div class="cw-header">
          <div class="cw-title">
            <span class="cw-title-name">Configuration wizard</span>
            <span class="cw-badge">debug mode</span>
          </div>
          <button class="cw-close" aria-label="Close" title="Close (Esc)">×</button>
        </div>
        <div class="cw-steps"></div>
        <div class="cw-body"></div>
        <div class="cw-footer">
          <span class="cw-status"></span>
          <div class="cw-footer-actions">
            <button class="btn btn-ghost cw-back" type="button">← Back</button>
            <button class="btn btn-ghost cw-next" type="button">Next →</button>
            <button class="btn btn-primary cw-save" type="button" hidden>Save all changes</button>
          </div>
        </div>
      </div>`;

    this.stepsEl = this.overlay.querySelector('.cw-steps')!;
    this.bodyEl = this.overlay.querySelector('.cw-body')!;
    this.statusEl = this.overlay.querySelector('.cw-status')!;
    this.backBtn = this.overlay.querySelector('.cw-back')!;
    this.nextBtn = this.overlay.querySelector('.cw-next')!;
    this.saveBtn = this.overlay.querySelector('.cw-save')!;

    this.overlay.querySelector('.cw-close')!.addEventListener('click', () => this.close());
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.close();
    });
    this.backBtn.addEventListener('click', () => this.goStep(-1));
    this.nextBtn.addEventListener('click', () => this.goStep(1));
    this.saveBtn.addEventListener('click', () => this.startSaveAll());
    document.addEventListener('keydown', this.onKey);

    parent.appendChild(this.overlay);
  }

  get isOpen(): boolean {
    return this.openFlag;
  }

  private onKey = (e: KeyboardEvent): void => {
    if (e.key === 'Escape' && this.openFlag) this.close();
  };

  open(): void {
    if (this.openFlag) return;
    this.openFlag = true;
    this.loading = true;
    this.step = 'workflow';
    this.files = [];
    this.pendingOps = [];
    this.currentOp = null;
    this.smRenderPending.clear();
    this.overlay.classList.add('open');
    this.setStatus('Loading configuration…', 'info');
    this.render();
    this.send('get_config');
  }

  close(): void {
    if (!this.openFlag) return;
    this.openFlag = false;
    this.overlay.classList.remove('open');
    this.bodyEl.innerHTML = '';
    this.pendingOps = [];
    this.currentOp = null;
  }

  destroy(): void {
    document.removeEventListener('keydown', this.onKey);
    this.overlay.remove();
  }

  // ------------------------------------------------------------------
  // Backend messages
  // ------------------------------------------------------------------
  onBackendMessage(msg: FrontendMessage): void {
    if (!this.openFlag) return;

    if (msg.event === 'config_snapshot' && msg.details) {
      this.applySnapshot(msg.details as unknown as ConfigSnapshotDetails);
      return;
    }
    if (msg.event === 'config_patched' && msg.details) {
      this.handlePatched(msg.details);
      return;
    }
    if (msg.event === 'config_validated' && msg.details) {
      const d = msg.details;
      const file = this.files.find((f) => f.path === d['path']);
      if (file) {
        file.errors = (d['errors'] as string[]) || [];
        if (!file.errors.length) this.setStatus(`✓ ${file.path} is valid`, 'ok');
        else this.setStatus(`${file.errors.length} error(s) in ${file.path}`, 'error');
      }
      this.render();
      return;
    }
    if (msg.event === 'config_saved' && msg.details) {
      const d = msg.details;
      if (this.currentOp?.kind === 'save') {
        const file = this.files.find((f) => f.path === this.currentOp?.path);
        if (file) {
          file.original = file.content;
          file.errors = [];
          file.created = false;
          if (file.simRequested) file.simExists = true;
        }
      }
      this.currentOp = null;
      this.advanceQueue();
      this.render();
      return;
    }
    if (msg.event === 'subsystem_deleted' && msg.details) {
      const name = msg.details['name'] as string;
      this.files = this.files.filter((f) => f.name !== name);
      if (this.activeSubsystemPath && !this.files.some((f) => f.path === this.activeSubsystemPath)) {
        this.activeSubsystemPath = this.files.find((f) => f.category === 'subsystem')?.path ?? null;
      }
      this.currentOp = null;
      this.advanceQueue();
      this.render();
      return;
    }
    if (msg.event === 'error_reported') {
      const message = str(msg.details?.['message'] ?? 'Configuration error');
      if (this.currentOp !== null || this.pendingOps.length > 0) {
        this.pendingOps = [];
        this.currentOp = null;
      }
      this.loading = false;
      this.setStatus(message, 'error');
      this.render();
    }
  }

  private applySnapshot(details: ConfigSnapshotDetails): void {
    this.files = (details.files || []).map((f: ConfigFileSnapshot) => ({
      path: f.path,
      label: f.label,
      category: f.category,
      original: f.content,
      content: f.content,
      parsed: f.parsed ?? null,
      name: f.name,
      simExists: f.sim_exists,
      errors: f.errors ?? [],
    }));
    this.messageFormats = details.message_formats || { default: {}, subsystems: {} };
    this.smDrafts.clear();
    this.smDirtyPaths.clear();
    this.expandedStates.clear();
    this.loading = false;
    if (!this.activeTopPath || !this.files.some((f) => f.path === this.activeTopPath)) {
      this.activeTopPath = this.files.find((f) => f.category === 'workflow')?.path ?? null;
    }
    if (!this.activeSubsystemPath || !this.files.some((f) => f.path === this.activeSubsystemPath)) {
      this.activeSubsystemPath = this.files.find((f) => f.category === 'subsystem')?.path ?? null;
    }
    this.overrideName =
      this.overrideName && Object.prototype.hasOwnProperty.call(this.messageFormats.subsystems, this.overrideName)
        ? this.overrideName
        : '';
    this.setStatus('', 'info');
    this.render();
  }

  private handlePatched(details: Record<string, unknown>): void {
    const path = str(details['path']);
    const content = details['content'];
    if (details['state_machine'] === true) {
      this.handleStateMachinePatch(details);
      return;
    }
    if (details['created'] === true) {
      const name = str(details['name']);
      const parsed = isObject(details['parsed']) ? (details['parsed'] as Record<string, unknown>) : null;
      this.files.push({
        path,
        label: `${name}.yaml`,
        category: 'subsystem',
        original: '',
        content: typeof content === 'string' ? content : '',
        parsed,
        name,
        created: true,
        simExists: false,
        simRequested: this.newSubsystemSim,
      });
      this.activeSubsystemPath = path;
      this.creatingSubsystem = false;
      this.newSubsystemSpec = {};
      this.newSubsystemSim = true;
      this.setStatus(`New subsystem '${name}' staged — review and save`, 'ok');
      this.step = 'subsystems';
    } else if (path && typeof content === 'string') {
      const file = this.files.find((f) => f.path === path);
      if (file) {
        file.content = content;
        file.errors = [];
        if (isObject(details['parsed'])) {
          file.parsed = details['parsed'] as Record<string, unknown>;
        }
      }
      this.setStatus(`Applied changes to ${path}`, 'ok');
    }
    this.render();
  }

  /** A `render_state_machine` response: update the buffer, keep the draft. */
  private handleStateMachinePatch(details: Record<string, unknown>): void {
    const path = str(details['path']);
    const content = details['content'];
    const file = this.files.find((f) => f.path === path);
    if (file) {
      if (typeof content === 'string') file.content = content;
      file.errors = [];
      if (isObject(details['parsed'])) {
        file.parsed = details['parsed'] as Record<string, unknown>;
      }
    }
    this.smDirtyPaths.delete(path);
    this.smRenderPending.delete(path);

    const isQueued = this.currentOp?.kind === 'render_sm' && this.currentOp.path === path;
    if (isQueued) {
      // The save sequence rendered a pending draft; now queue that file's
      // save (and simulator creation) and continue with the rest.
      if (file && !file.deleted) {
        if (file.created && file.simRequested && file.name) {
          this.pendingOps.unshift({ kind: 'sim', name: file.name });
        }
        this.pendingOps.unshift({
          kind: 'save',
          path: file.path,
          name: file.name,
          content: file.content,
        });
      }
      this.currentOp = null;
      this.advanceQueue();
      this.render();
      return;
    }
    this.setStatus(`State machine of ${path} applied to the buffer`, 'ok');
    this.render();
  }

  // ------------------------------------------------------------------
  // Shell rendering
  // ------------------------------------------------------------------
  private setStatus(text: string, kind: 'info' | 'ok' | 'error'): void {
    this.statusEl.textContent = text;
    this.statusEl.className = `cw-status ${kind}`;
  }

  /** A file with pending changes: edited, drafted or staged for deletion. */
  private isPendingFile(file: WizardFile): boolean {
    return file.deleted === true || isDirty(file) || this.smDirtyPaths.has(file.path);
  }

  /** True while the save queue runs or a state-machine render is in flight
   *  (interactive edits are paused so requests cannot interleave). */
  private isSaving(): boolean {
    return (
      this.currentOp !== null ||
      this.pendingOps.length > 0 ||
      this.smRenderPending.size > 0
    );
  }

  private goStep(delta: number): void {
    const idx = STEPS.findIndex((s) => s.id === this.step);
    const next = STEPS[Math.max(0, Math.min(STEPS.length - 1, idx + delta))];
    this.step = next.id;
    this.render();
  }

  private render(): void {
    // Step nav
    this.stepsEl.innerHTML = '';
    for (const s of STEPS) {
      const btn = el(
        'button',
        `cw-step${this.step === s.id ? ' active' : ''}`,
      );
      btn.type = 'button';
      btn.appendChild(el('span', 'cw-step-icon', s.icon));
      btn.appendChild(el('span', 'cw-step-label', s.label));
      const pending = this.files.filter((f) => this.isPendingFile(f)).length;
      if (s.id === 'review' && pending > 0) {
        btn.appendChild(el('span', 'cw-step-count', String(pending)));
      }
      btn.addEventListener('click', () => {
        this.step = s.id;
        this.render();
      });
      this.stepsEl.appendChild(btn);
    }

    // Footer
    const idx = STEPS.findIndex((s) => s.id === this.step);
    this.backBtn.disabled = idx === 0;
    this.nextBtn.hidden = idx === STEPS.length - 1;
    this.saveBtn.hidden = idx !== STEPS.length - 1;
    this.saveBtn.disabled = this.isSaving();

    this.bodyEl.innerHTML = '';
    if (this.loading) {
      this.bodyEl.appendChild(el('div', 'cw-empty', 'Loading configuration…'));
      return;
    }
    // Existing validation problems are surfaced immediately (e.g. a
    // subsystem stage that can never complete).
    const invalid = this.files.filter(
      (f) => !f.deleted && (f.errors?.length ?? 0) > 0,
    );
    if (invalid.length > 0) {
      const banner = el('div', 'cw-errors cw-invalid-banner');
      banner.appendChild(
        el(
          'div',
          'cw-error',
          `${invalid.length} configuration file(s) have validation errors:`,
        ),
      );
      for (const f of invalid.slice(0, 6)) {
        banner.appendChild(
          el('div', 'cw-error', `${f.path}: ${(f.errors ?? [])[0]}`),
        );
      }
      this.bodyEl.appendChild(banner);
    }
    if (this.step === 'workflow') this.renderWorkflowStep();
    else if (this.step === 'subsystems') this.renderSubsystemsStep();
    else if (this.step === 'messages') this.renderMessagesStep();
    else this.renderReviewStep();
  }

  // ------------------------------------------------------------------
  // Step ① — Workflow / global / object
  // ------------------------------------------------------------------
  private renderWorkflowStep(): void {
    this.bodyEl.appendChild(
      el(
        'p',
        'cw-intro',
        'Edit the workflow lifecycle, the global settings and the object blueprint. ' +
          'The raw YAML editor is authoritative; the forms apply comment-preserving changes to it.',
      ),
    );
    const files = this.files.filter((f) => ['workflow', 'global', 'object'].includes(f.category));
    if (files.length === 0) {
      this.bodyEl.appendChild(el('div', 'cw-empty', 'No workflow/global files found.'));
      return;
    }
    const tabs = el('div', 'cw-tabs');
    for (const f of files) {
      const btn = el('button', `cw-tab${f.path === this.activeTopPath ? ' active' : ''}`);
      btn.type = 'button';
      btn.appendChild(document.createTextNode(f.label));
      if (f.errors?.length) btn.appendChild(el('span', 'cw-chip err', '!'));
      btn.addEventListener('click', () => {
        this.activeTopPath = f.path;
        this.render();
      });
      tabs.appendChild(btn);
    }
    this.bodyEl.appendChild(tabs);

    const file = files.find((f) => f.path === this.activeTopPath) ?? files[0];
    this.activeTopPath = file.path;

    const grid = el('div', 'cw-columns');
    const left = el('div', 'cw-col');
    const right = el('div', 'cw-col');
    if (file.category === 'workflow') left.appendChild(this.buildWorkflowForm(file));
    else if (file.category === 'global') left.appendChild(this.buildGlobalForm(file));
    else left.appendChild(this.buildObjectSummary(file));
    // The structured state-machine editor works for every machine; for the
    // object blueprint it also edits each state's physical meta.
    if (file.category !== 'global') this.renderStateMachineEditor(left, file);
    right.appendChild(this.buildRawEditor(file, 20));
    grid.appendChild(left);
    grid.appendChild(right);
    this.bodyEl.appendChild(grid);
  }

  private buildWorkflowForm(file: WizardFile): HTMLElement {
    const md = (getPath(file.parsed, ['metadata']) ?? {}) as Record<string, unknown>;
    const op = isObject(md['operator_events']) ? md['operator_events'] : {};
    const readiness = isObject(md['readiness']) ? md['readiness'] : {};
    const wake = isObject(md['wake']) ? md['wake'] : {};
    const toggle = Array.isArray(md['toggle_allowed_in']) ? (md['toggle_allowed_in'] as unknown[]) : [];

    const form = el('div', 'cw-form');
    form.appendChild(el('h4', 'cw-form-title', 'Workflow lifecycle (metadata)'));
    form.appendChild(
      el(
        'p',
        'cw-form-hint',
        'These values drive the operator button matrix, the readiness gate, the error handling and the simulation toggle.',
      ),
    );
    const fields = el('div', 'cw-fields');
    fields.appendChild(fieldRow('Start event', patchedInput('metadata.operator_events.start', str(op['start']))));
    fields.appendChild(fieldRow('Pause event', patchedInput('metadata.operator_events.pause', str(op['pause']))));
    fields.appendChild(fieldRow('Resume event', patchedInput('metadata.operator_events.resume', str(op['resume']))));
    fields.appendChild(fieldRow('Stop event', patchedInput('metadata.operator_events.stop', str(op['stop']))));
    fields.appendChild(fieldRow('Reset event', patchedInput('metadata.operator_events.reset', str(op['reset']))));
    fields.appendChild(
      fieldRow('Stop completion event', patchedInput('metadata.stop_completion_event', str(md['stop_completion_event']))),
    );
    fields.appendChild(fieldRow('Readiness state', patchedInput('metadata.readiness.in_state', str(readiness['in_state']))));
    fields.appendChild(fieldRow('Readiness event', patchedInput('metadata.readiness.fire', str(readiness['fire']))));
    fields.appendChild(fieldRow('Error event', patchedInput('metadata.error_event', str(md['error_event']))));
    fields.appendChild(fieldRow('Error state', patchedInput('metadata.error_state', str(md['error_state']))));
    fields.appendChild(
      fieldRow(
        'Toggle-allowed states',
        patchedInput('metadata.toggle_allowed_in', toggle.join(', '), 'csv'),
        'comma separated',
      ),
    );
    fields.appendChild(fieldRow('Wake from state', patchedInput('metadata.wake.from_state', str(wake['from_state']))));
    fields.appendChild(fieldRow('Wake to state', patchedInput('metadata.wake.to_state', str(wake['to_state']))));
    form.appendChild(fields);
    form.appendChild(this.applyButton(file, form));
    return form;
  }

  private buildGlobalForm(file: WizardFile): HTMLElement {
    const form = el('div', 'cw-form');
    form.appendChild(el('h4', 'cw-form-title', 'Global settings'));
    form.appendChild(
      el(
        'p',
        'cw-form-hint',
        'Broker, ports and orchestration defaults. Restart required after saving.',
      ),
    );
    const fields = el('div', 'cw-fields');
    fields.appendChild(fieldRow('MQTT broker', patchedInput('mqtt.broker', str(getPath(file.parsed, ['mqtt', 'broker'])))));
    fields.appendChild(fieldRow('MQTT port', patchedInput('mqtt.port', str(getPath(file.parsed, ['mqtt', 'port'])), 'number')));
    fields.appendChild(
      fieldRow('Topic prefix', patchedInput('mqtt.topic_prefix', str(getPath(file.parsed, ['mqtt', 'topic_prefix'])))),
    );
    fields.appendChild(fieldRow('Frontend host', patchedInput('frontend.host', str(getPath(file.parsed, ['frontend', 'host'])))));
    fields.appendChild(fieldRow('WebSocket port', patchedInput('frontend.port', str(getPath(file.parsed, ['frontend', 'port'])), 'number')));
    fields.appendChild(fieldRow('Dev server port', patchedInput('frontend.dev_port', str(getPath(file.parsed, ['frontend', 'dev_port'])), 'number')));
    fields.appendChild(
      fieldRow(
        'State-expect timeout (s)',
        patchedInput('orchestration.state_expect_timeout', str(getPath(file.parsed, ['orchestration', 'state_expect_timeout'])), 'number'),
      ),
    );
    fields.appendChild(
      fieldRow('Alias pattern', patchedInput('orchestration.alias_pattern', str(getPath(file.parsed, ['orchestration', 'alias_pattern'])), 'text', '^[A-Za-z0-9_\\-\\. ]{1,64}$')),
    );
    fields.appendChild(
      fieldRow('Default objects', patchedInput('simulation.default_num_objects', str(getPath(file.parsed, ['simulation', 'default_num_objects'])), 'number')),
    );
    fields.appendChild(
      fieldRow('Heartbeat interval (s)', patchedInput('simulation.heartbeat_interval', str(getPath(file.parsed, ['simulation', 'heartbeat_interval'])), 'number')),
    );
    fields.appendChild(
      fieldRow('Default init duration (s)', patchedInput('simulation.default_init_duration', str(getPath(file.parsed, ['simulation', 'default_init_duration'])), 'number')),
    );
    fields.appendChild(
      fieldRow('Default work duration (s)', patchedInput('simulation.default_work_duration', str(getPath(file.parsed, ['simulation', 'default_work_duration'])), 'number')),
    );
    fields.appendChild(
      fieldRow('Object ID format', patchedInput('simulation.object_id_format', str(getPath(file.parsed, ['simulation', 'object_id_format'])))),
    );
    form.appendChild(fields);
    form.appendChild(this.applyButton(file, form));
    return form;
  }

  private buildObjectSummary(file: WizardFile): HTMLElement {
    const form = el('div', 'cw-form');
    form.appendChild(el('h4', 'cw-form-title', 'Object blueprint'));
    form.appendChild(
      el(
        'p',
        'cw-form-hint',
        'The object journey is edited as YAML on the right (states, transitions, per-state meta: ' +
          'position, actor, parallel_actors, rotate_request/command, actor_op).',
      ),
    );
    const md = (getPath(file.parsed, ['metadata']) ?? {}) as Record<string, unknown>;
    const states = isObject(file.parsed?.['states']) ? Object.keys(file.parsed!['states'] as object) : [];
    const summary = el('div', 'cw-summary');
    summary.appendChild(el('div', 'cw-summary-row', `States: ${states.length}`));
    summary.appendChild(el('div', 'cw-summary-row', `Initial state: ${str(getPath(file.parsed, ['initial_state']))}`));
    summary.appendChild(el('div', 'cw-summary-row', `Disks: ${str(md['disks'])} (${str(md['disk_names']).replace(/,/g, ', ')})`));
    summary.appendChild(el('div', 'cw-summary-row', `Hand slots: ${str(md['hand_slots'])}`));
    const chains = isObject(md['task_chains']) ? Object.keys(md['task_chains']) : [];
    summary.appendChild(el('div', 'cw-summary-row', `Task chains: ${chains.join(', ') || '—'}`));
    form.appendChild(summary);
    return form;
  }

  private applyButton(file: WizardFile, container: HTMLElement): HTMLButtonElement {
    const btn = el('button', 'btn btn-primary cw-mini', 'Apply to YAML');
    btn.type = 'button';
    btn.addEventListener('click', () => this.applyPatches(file, container));
    return btn;
  }

  private applyPatches(file: WizardFile, container: HTMLElement): void {
    const patches = this.collectPatches(container);
    if (patches.length === 0) {
      this.setStatus('Nothing to apply', 'info');
      return;
    }
    // Optimistic local mirror (the raw text stays authoritative server-side).
    if (!file.parsed) file.parsed = {};
    for (const p of patches) setPath(file.parsed, p.path, p.value);
    this.send('patch_config', { path: file.path, content: file.content, patches });
    this.setStatus(`Applying ${patches.length} change(s) to ${file.path}…`, 'info');
  }

  private collectPatches(container: HTMLElement): ConfigPatch[] {
    const patches: ConfigPatch[] = [];
    container.querySelectorAll<HTMLInputElement>('input[data-patch]').forEach((input) => {
      const path = (input.dataset.patch ?? '').split('.').filter(Boolean);
      if (path.length === 0) return;
      const kind = input.dataset.kind;
      let value: unknown;
      if (kind === 'bool') value = input.checked;
      else if (kind === 'number') value = input.value === '' ? undefined : Number(input.value);
      else if (kind === 'csv') value = input.value.split(',').map((s) => s.trim()).filter(Boolean);
      else value = input.value;
      patches.push({ path, value });
    });
    return patches;
  }

  // ------------------------------------------------------------------
  // Step ② — Subsystems
  // ------------------------------------------------------------------
  private renderSubsystemsStep(): void {
    this.bodyEl.appendChild(
      el(
        'p',
        'cw-intro',
        'Edit a subsystem, create a new one from a guided template, or stage a removal. ' +
          'Removals report other config files that still reference the subsystem.',
      ),
    );
    const split = el('div', 'cw-split');
    const list = el('div', 'cw-list');
    const subs = this.files.filter((f) => f.category === 'subsystem');
    for (const f of subs) {
      const item = el(
        'button',
        `cw-list-item${f.path === this.activeSubsystemPath ? ' active' : ''}${f.deleted ? ' is-deleted' : ''}`,
      );
      item.type = 'button';
      item.appendChild(el('span', 'cw-list-name', f.name || f.label));
      if (f.simExists) item.appendChild(el('span', 'cw-chip sim', 'SIM'));
      if (f.created) item.appendChild(el('span', 'cw-chip new', 'new'));
      if (f.errors?.length) item.appendChild(el('span', 'cw-chip err', '!'));
      if (!f.deleted && (isDirty(f) || this.smDirtyPaths.has(f.path))) {
        item.appendChild(el('span', 'cw-chip dirty', '•'));
      }
      item.addEventListener('click', () => {
        this.activeSubsystemPath = f.path;
        this.creatingSubsystem = false;
        this.render();
      });
      list.appendChild(item);
    }
    const addBtn = el('button', 'btn btn-ghost cw-add', '＋ New subsystem');
    addBtn.type = 'button';
    addBtn.addEventListener('click', () => {
      this.creatingSubsystem = true;
      this.activeSubsystemPath = null;
      this.render();
    });
    list.appendChild(addBtn);
    split.appendChild(list);

    const detail = el('div', 'cw-detail');
    if (this.creatingSubsystem) {
      this.buildCreateForm(detail);
    } else {
      const file = subs.find((f) => f.path === this.activeSubsystemPath);
      if (file) this.buildSubsystemDetail(detail, file);
      else detail.appendChild(el('div', 'cw-empty', 'Select a subsystem or create a new one.'));
    }
    split.appendChild(detail);
    this.bodyEl.appendChild(split);
  }

  private buildSubsystemDetail(detail: HTMLElement, file: WizardFile): void {
    if (file.deleted) {
      detail.appendChild(el('div', 'cw-empty', `'${file.name}' is staged for deletion.`));
      const undo = el('button', 'btn btn-ghost cw-mini', 'Undo removal');
      undo.type = 'button';
      undo.addEventListener('click', () => {
        file.deleted = false;
        this.setStatus(`Removal of '${file.name}' undone`, 'info');
        this.render();
      });
      detail.appendChild(undo);
      return;
    }

    detail.appendChild(el('h4', 'cw-detail-title', file.name || file.label));
    if (file.created) {
      detail.appendChild(el('p', 'cw-form-hint', 'New subsystem (not saved yet).'));
    }

    // Sub-detail tabs keep the pane focused: basics / the structured state
    // machine editor / the raw YAML. The choice persists across files.
    const tabs = el('div', 'cw-tabs');
    const tabDefs: Array<[typeof this.subDetailTab, string]> = [
      ['basics', 'Basics'],
      ['states', 'State machine'],
      ['raw', 'Raw YAML'],
    ];
    for (const [tab, label] of tabDefs) {
      const btn = el('button', `cw-tab${this.subDetailTab === tab ? ' active' : ''}`);
      btn.type = 'button';
      btn.appendChild(document.createTextNode(label));
      if (tab === 'states' && this.smDirtyPaths.has(file.path)) {
        btn.appendChild(el('span', 'cw-chip dirty', '•'));
      }
      btn.addEventListener('click', () => {
        this.subDetailTab = tab;
        this.render();
      });
      tabs.appendChild(btn);
    }
    detail.appendChild(tabs);

    if (this.subDetailTab === 'basics') {
      const form = el('div', 'cw-form');
      form.appendChild(el('h5', 'cw-form-title', 'Basics'));
      const fields = el('div', 'cw-fields');
      fields.appendChild(fieldRow('Display name', patchedInput('display_name', str(getPath(file.parsed, ['display_name'])))));
      fields.appendChild(fieldRow('Type', patchedInput('type', str(getPath(file.parsed, ['type'])))));
      fields.appendChild(fieldRow('MQTT topic', patchedInput('mqtt_topic', str(getPath(file.parsed, ['mqtt_topic'])), 'text', 'automata/subsystem/{name}')));
      fields.appendChild(fieldRow('Required', patchedInput('required', str(getPath(file.parsed, ['required'])), 'bool')));
      fields.appendChild(
        fieldRow('Init duration (s)', patchedInput('metadata.init_duration', str(getPath(file.parsed, ['metadata', 'init_duration'])), 'number')),
      );
      form.appendChild(fields);
      form.appendChild(el('h5', 'cw-form-title', 'Interface (real hardware)'));
      const ifaceFields = el('div', 'cw-fields');
      ifaceFields.appendChild(
        fieldRow('Manage interface', patchedInput('metadata.interface.enabled', str(getPath(file.parsed, ['metadata', 'interface', 'enabled'])), 'bool')),
      );
      ifaceFields.appendChild(
        fieldRow('Startup command', patchedInput('metadata.interface.startup', str(getPath(file.parsed, ['metadata', 'interface', 'startup'])))),
      );
      ifaceFields.appendChild(
        fieldRow('Shutdown command', patchedInput('metadata.interface.shutdown', str(getPath(file.parsed, ['metadata', 'interface', 'shutdown'])))),
      );
      ifaceFields.appendChild(
        fieldRow('Working directory', patchedInput('metadata.interface.cwd', str(getPath(file.parsed, ['metadata', 'interface', 'cwd'])))),
      );
      form.appendChild(ifaceFields);
      form.appendChild(this.applyButton(file, form));
      detail.appendChild(form);
      detail.appendChild(
        el('p', 'cw-form-hint', 'The machine initial state is chosen in the State machine tab.'),
      );
      detail.appendChild(
        el(
          'p',
          'cw-form-hint',
          'Interface: the shell command that starts (and stops) the real subsystem interface. ' +
            'Run headless by the orchestrator when the workflow starts (not when the GUI is launched; ' +
            'SIM→REAL too) and when the workflow stops or the GUI closes; the subsystem state is then ' +
            'known through MQTT only. Output goes to backend/interface_logs/<name>.log. ' +
            'Simulated subsystems are never touched.',
        ),
      );

      if (file.created) {
        const simLabel = el('label', 'cw-check');
        const simBox = document.createElement('input');
        simBox.type = 'checkbox';
        simBox.checked = file.simRequested ?? true;
        simBox.addEventListener('change', () => {
          file.simRequested = simBox.checked;
        });
        simLabel.appendChild(simBox);
        simLabel.appendChild(document.createTextNode('Also create the simulator script (<name>_sim.py)'));
        detail.appendChild(simLabel);
      }
    } else if (this.subDetailTab === 'states') {
      this.renderStateMachineEditor(detail, file);
    } else {
      detail.appendChild(this.buildRawEditor(file, 16));
    }

    // References + staged removal
    const refs = this.findReferences(file.name || '');
    if (refs.length > 0) {
      detail.appendChild(el('h5', 'cw-form-title', 'Referenced by'));
      const refList = el('div', 'cw-refs');
      for (const r of refs) {
        refList.appendChild(el('div', 'cw-ref', `${r.path}:${r.line} — ${r.text}`));
      }
      detail.appendChild(refList);
    }
    const danger = el('div', 'cw-danger');
    const removeBtn = el('button', 'btn btn-danger cw-mini', 'Remove subsystem');
    removeBtn.type = 'button';
    removeBtn.addEventListener('click', () => {
      file.deleted = true;
      this.setStatus(
        `'${file.name}' staged for removal — save in the review step to delete the file` +
          (refs.length ? ` (${refs.length} reference(s) still exist)` : ''),
        'info',
      );
      this.render();
    });
    danger.appendChild(removeBtn);
    detail.appendChild(danger);
  }

  // ------------------------------------------------------------------
  // Structured state-machine editor (states, transitions, messages)
  // ------------------------------------------------------------------
  private getDraft(file: WizardFile): StateMachineDraft {
    let draft = this.smDrafts.get(file.path);
    if (!draft) {
      draft = buildStateMachineDraft(file.parsed);
      this.smDrafts.set(file.path, draft);
    }
    return draft;
  }

  private touchDraft(path: string): void {
    this.smDirtyPaths.add(path);
  }

  /** Serialize the draft into the `states:` document the backend renders. */
  private draftToStates(draft: StateMachineDraft): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const st of draft.states) {
      const stateOut: Record<string, unknown> = {};
      if (st.description.trim()) stateOut['description'] = st.description.trim();
      if (st.meta && Object.keys(st.meta).length > 0) stateOut['meta'] = st.meta;
      stateOut['transitions'] = st.transitions.map((t) => {
        const transition: Record<string, unknown> = {
          event: t.event.trim(),
          target: t.target,
        };
        const actions = t.actions.map((a) => {
          if (a.type === 'log_event') return 'log_event';
          if (a.type === 'event') return { event: a.value.trim() };
          try {
            return JSON.parse(a.value);
          } catch {
            return a.raw;
          }
        });
        if (actions.length > 0) transition['actions'] = actions;
        return transition;
      });
      out[st.name] = stateOut;
    }
    return out;
  }

  private renderStateMachineEditor(container: HTMLElement, file: WizardFile): void {
    const draft = this.getDraft(file);
    const form = el('div', 'cw-form');
    form.appendChild(el('h5', 'cw-form-title', 'State machine'));
    const scopeHint =
      file.category === 'subsystem'
        ? 'Every work state needs a transition back to the ready state: completion is detected when the subsystem reports ready (subsystem transitions cannot fire internal events).'
        : file.category === 'object'
          ? 'The object journey is linear: the pipeline fires each state\u2019s outgoing transition when its actor(s) complete. Actors must be subsystems that report back to the ready state.'
          : 'The lifecycle machine: transitions drive the operator buttons, the readiness gate, the error handling and the hardware commands.';
    form.appendChild(
      el(
        'p',
        'cw-form-hint',
        'States, transitions and the messages (events/actions) they carry. ' +
          '\u201cApply state machine\u201d regenerates the states: section of the YAML \u2014 the rest of the file ' +
          '(metadata and its comments) is preserved. Pending drafts are applied automatically when saving. ' +
          scopeHint,
      ),
    );

    if (draft.states.length === 0) {
      form.appendChild(el('div', 'cw-empty', 'No states in this file yet — add one below.'));
    }

    // Initial state selector
    const initialRow = el('div', 'cw-sm-initial');
    initialRow.appendChild(el('span', 'cw-field-label', 'Initial state'));
    const initialSel = document.createElement('select');
    initialSel.className = 'cw-select';
    for (const st of draft.states) {
      const opt = document.createElement('option');
      opt.value = st.name;
      opt.textContent = st.name;
      opt.selected = st.name === draft.initial;
      initialSel.appendChild(opt);
    }
    initialSel.disabled = draft.states.length === 0;
    initialSel.addEventListener('change', () => {
      draft.initial = initialSel.value;
      this.touchDraft(file.path);
      this.render();
    });
    initialRow.appendChild(initialSel);
    form.appendChild(initialRow);

    // State cards
    const list = el('div', 'cw-sm-list');
    for (const st of draft.states) {
      list.appendChild(this.buildSmStateCard(file, draft, st));
    }
    form.appendChild(list);

    // Add a state
    const addRow = el('div', 'cw-sm-add');
    const nameInput = document.createElement('input');
    nameInput.className = 'cw-input';
    nameInput.value = this.newStateName;
    nameInput.placeholder = 'new_state_name';
    nameInput.addEventListener('input', () => {
      this.newStateName = nameInput.value;
    });
    nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') this.addDraftState(file);
    });
    addRow.appendChild(nameInput);
    const addBtn = el('button', 'btn btn-ghost cw-mini', '＋ Add state');
    addBtn.type = 'button';
    addBtn.disabled = this.isSaving();
    addBtn.addEventListener('click', () => this.addDraftState(file));
    addRow.appendChild(addBtn);
    form.appendChild(addRow);

    const actions = el('div', 'cw-inline-actions');
    const applyBtn = el('button', 'btn btn-primary cw-mini', 'Apply state machine');
    applyBtn.type = 'button';
    applyBtn.disabled = this.isSaving();
    applyBtn.addEventListener('click', () => this.applyStateMachine(file));
    actions.appendChild(applyBtn);
    const resetBtn = el('button', 'btn btn-ghost cw-mini', 'Reset draft');
    resetBtn.type = 'button';
    resetBtn.disabled = this.isSaving() || !this.smDirtyPaths.has(file.path);
    resetBtn.title = 'Discard unapplied state-machine edits and reload from the YAML';
    resetBtn.addEventListener('click', () => {
      this.smDrafts.delete(file.path);
      this.smDirtyPaths.delete(file.path);
      this.setStatus('State-machine draft reset to the last applied YAML', 'info');
      this.render();
    });
    actions.appendChild(resetBtn);
    form.appendChild(actions);
    container.appendChild(form);
  }

  private buildSmStateCard(
    file: WizardFile,
    draft: StateMachineDraft,
    st: DraftState,
  ): HTMLElement {
    const card = el('div', 'cw-sm-state');
    const expanded = this.expandedStates.has(st.name);

    const head = el('div', 'cw-sm-head');
    const toggle = el('button', `cw-sm-toggle${expanded ? ' open' : ''}`, expanded ? '▾' : '▸');
    toggle.type = 'button';
    toggle.title = expanded ? 'Collapse' : 'Expand transitions';
    toggle.addEventListener('click', () => {
      if (expanded) this.expandedStates.delete(st.name);
      else this.expandedStates.add(st.name);
      this.render();
    });
    head.appendChild(toggle);
    head.appendChild(el('span', 'cw-sm-name', st.name));
    if (draft.initial === st.name) head.appendChild(el('span', 'cw-chip init', 'initial'));
    head.appendChild(el('span', 'cw-sm-spacer'));
    head.appendChild(
      el('span', 'cw-sm-count', `${st.transitions.length} transition${st.transitions.length === 1 ? '' : 's'}`),
    );
    card.appendChild(head);
    if (!expanded) return card;

    const body = el('div', 'cw-sm-body');
    const descInput = document.createElement('input');
    descInput.className = 'cw-input';
    descInput.value = st.description;
    descInput.placeholder = `${st.name} description`;
    descInput.addEventListener('input', () => {
      st.description = descInput.value;
      this.touchDraft(file.path);
    });
    body.appendChild(fieldRow('Description', descInput));

    if (file.category === 'object') {
      body.appendChild(this.buildObjectMetaFields(file, st));
    }

    const actions = el('div', 'cw-inline-actions');
    const makeInitial = el('button', 'btn btn-ghost cw-mini', 'Make initial');
    makeInitial.type = 'button';
    makeInitial.disabled = draft.initial === st.name || this.isSaving();
    makeInitial.addEventListener('click', () => {
      draft.initial = st.name;
      this.touchDraft(file.path);
      this.render();
    });
    actions.appendChild(makeInitial);
    const removeState = el('button', 'btn btn-danger cw-mini', 'Remove state');
    removeState.type = 'button';
    removeState.disabled = this.isSaving();
    removeState.addEventListener('click', () => this.removeDraftState(file, st.name));
    actions.appendChild(removeState);
    body.appendChild(actions);

    for (const [index, transition] of st.transitions.entries()) {
      body.appendChild(this.buildSmTransition(file, draft, st, index, transition));
    }
    const addTransition = el('button', 'btn btn-ghost cw-mini cw-add-transition', '＋ Add transition');
    addTransition.type = 'button';
    addTransition.disabled = this.isSaving();
    addTransition.addEventListener('click', () => {
      const target = draft.states.find((s) => s.name !== st.name)?.name ?? st.name;
      st.transitions.push({ event: '', target, actions: [] });
      this.touchDraft(file.path);
      this.render();
    });
    body.appendChild(addTransition);
    card.appendChild(body);
    return card;
  }

  /**
   * The `meta:` facts of one object state (settings panel §R2): where the
   * object physically is and which subsystem(s) work on it. The backend
   * cross-validates these against the configured subsystems on apply.
   */
  private buildObjectMetaFields(file: WizardFile, st: DraftState): HTMLElement {
    const wrap = el('div', 'cw-meta');
    wrap.appendChild(el('h6', 'cw-form-title', 'Physical facts (meta)'));
    wrap.appendChild(
      el(
        'p',
        'cw-form-hint',
        'Where the object is and who works on it. Actors must be subsystems whose state machine returns to ready.',
      ),
    );
    const meta: Record<string, unknown> = st.meta ?? (st.meta = {});
    const md = isObject(file.parsed?.['metadata'])
      ? (file.parsed!['metadata'] as Record<string, unknown>)
      : {};
    const diskNames = Array.isArray(md['disk_names'])
      ? (md['disk_names'] as unknown[]).map(String)
      : [];
    const subsystemNames = this.files
      .filter((f) => f.category === 'subsystem')
      .map((f) => f.name || '')
      .filter(Boolean);
    const setMeta = (key: string, value: unknown): void => {
      if (value === '' || value === undefined || (Array.isArray(value) && value.length === 0)) {
        delete meta[key];
      } else {
        meta[key] = value;
      }
      this.touchDraft(file.path);
    };

    const position = str(meta['position']);
    const positionChoices = [
      '',
      'box',
      'hand',
      ...diskNames,
      ...(position && !['box', 'hand', ...diskNames].includes(position) ? [position] : []),
    ];
    const posSel = document.createElement('select');
    posSel.className = 'cw-select';
    for (const p of positionChoices) {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = p || '—';
      opt.selected = p === position;
      posSel.appendChild(opt);
    }
    posSel.addEventListener('change', () => setMeta('position', posSel.value));
    wrap.appendChild(fieldRow('Position', posSel));

    const actor = str(meta['actor']);
    const actorSel = document.createElement('select');
    actorSel.className = 'cw-select';
    for (const a of [
      '',
      ...subsystemNames,
      ...(actor && !subsystemNames.includes(actor) ? [actor] : []),
    ]) {
      const opt = document.createElement('option');
      opt.value = a;
      opt.textContent = a || '—';
      opt.selected = a === actor;
      actorSel.appendChild(opt);
    }
    actorSel.addEventListener('change', () => setMeta('actor', actorSel.value));
    wrap.appendChild(fieldRow('Actor', actorSel));

    const parallel = Array.isArray(meta['parallel_actors'])
      ? (meta['parallel_actors'] as unknown[]).map(String).join(', ')
      : '';
    const parallelInput = document.createElement('input');
    parallelInput.className = 'cw-input';
    parallelInput.value = parallel;
    parallelInput.placeholder = 'e.g. robot_first, xrf';
    parallelInput.addEventListener('input', () => {
      setMeta(
        'parallel_actors',
        parallelInput.value.split(',').map((s) => s.trim()).filter(Boolean),
      );
    });
    wrap.appendChild(fieldRow('Parallel actors', parallelInput, 'comma separated'));

    const textFields: Array<[string, string, string]> = [
      ['rotate_request', 'Rotate request', 'workflow/rotation_requested_second'],
      ['rotate_command', 'Rotate command', 'rotate_to_second'],
      ['actor_op', 'Actor op', 'return'],
    ];
    for (const [key, label, placeholder] of textFields) {
      const input = document.createElement('input');
      input.className = 'cw-input';
      input.value = str(meta[key]);
      input.placeholder = placeholder;
      input.addEventListener('input', () => setMeta(key, input.value.trim()));
      wrap.appendChild(fieldRow(label, input));
    }
    return wrap;
  }

  private buildSmTransition(
    file: WizardFile,
    draft: StateMachineDraft,
    st: DraftState,
    index: number,
    transition: DraftTransition,
  ): HTMLElement {
    const wrap = el('div', 'cw-transition');
    const line = el('div', 'cw-transition-row');
    line.appendChild(el('span', 'cw-field-label', 'on'));

    const eventInput = document.createElement('input');
    eventInput.className = 'cw-input';
    eventInput.value = transition.event;
    eventInput.placeholder = 'event name (e.g. measure, complete)';
    eventInput.addEventListener('input', () => {
      transition.event = eventInput.value;
      this.touchDraft(file.path);
    });
    line.appendChild(eventInput);
    line.appendChild(el('span', 'cw-arrow', '→'));

    const targetSel = document.createElement('select');
    targetSel.className = 'cw-select';
    for (const s of draft.states) {
      const opt = document.createElement('option');
      opt.value = s.name;
      opt.textContent = s.name;
      opt.selected = s.name === transition.target;
      targetSel.appendChild(opt);
    }
    targetSel.addEventListener('change', () => {
      transition.target = targetSel.value;
      this.touchDraft(file.path);
    });
    line.appendChild(targetSel);

    const removeBtn = el('button', 'cw-icon-btn', '×');
    removeBtn.type = 'button';
    removeBtn.title = 'Remove transition';
    removeBtn.disabled = this.isSaving();
    removeBtn.addEventListener('click', () => {
      st.transitions.splice(index, 1);
      this.touchDraft(file.path);
      this.render();
    });
    line.appendChild(removeBtn);
    wrap.appendChild(line);

    // Actions: the messages fired on transition completion (log_event,
    // a scoped/internal event, or a raw action preserved from the YAML).
    const actionsWrap = el('div', 'cw-actions');
    actionsWrap.appendChild(el('span', 'cw-field-label', 'actions'));
    const actionRows = el('div', 'cw-action-rows');
    for (const [actionIndex, action] of transition.actions.entries()) {
      actionRows.appendChild(this.buildSmAction(file, transition, actionIndex, action));
    }
    actionsWrap.appendChild(actionRows);
    const addAction = el('button', 'btn btn-ghost cw-mini cw-add-action', '＋ action');
    addAction.type = 'button';
    addAction.disabled = this.isSaving();
    addAction.addEventListener('click', () => {
      transition.actions.push({ type: 'log_event', value: '' });
      this.touchDraft(file.path);
      this.render();
    });
    actionsWrap.appendChild(addAction);
    wrap.appendChild(actionsWrap);
    return wrap;
  }

  private buildSmAction(
    file: WizardFile,
    transition: DraftTransition,
    index: number,
    action: DraftAction,
  ): HTMLElement {
    const row = el('div', 'cw-action-row');
    const typeSel = document.createElement('select');
    typeSel.className = 'cw-select';
    const kinds: Array<[DraftAction['type'], string]> = [
      ['log_event', 'log event'],
      ['event', 'fire event'],
      ['other', 'raw'],
    ];
    for (const [value, label] of kinds) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = label;
      opt.selected = action.type === value;
      typeSel.appendChild(opt);
    }
    typeSel.addEventListener('change', () => {
      action.type = typeSel.value as DraftAction['type'];
      this.touchDraft(file.path);
      this.render();
    });
    row.appendChild(typeSel);

    if (action.type !== 'log_event') {
      const input = document.createElement('input');
      input.className = 'cw-input';
      input.value = action.value;
      input.placeholder = action.type === 'event' ? 'event name (e.g. all/stop)' : 'JSON action';
      input.addEventListener('input', () => {
        action.value = input.value;
        this.touchDraft(file.path);
      });
      row.appendChild(input);
    }
    const remove = el('button', 'cw-icon-btn', '×');
    remove.type = 'button';
    remove.title = 'Remove action';
    remove.disabled = this.isSaving();
    remove.addEventListener('click', () => {
      transition.actions.splice(index, 1);
      this.touchDraft(file.path);
      this.render();
    });
    row.appendChild(remove);
    return row;
  }

  private addDraftState(file: WizardFile): void {
    const name = this.newStateName.trim();
    if (!/^[A-Za-z0-9_]+$/.test(name)) {
      this.setStatus('State name must use letters, digits or underscores', 'error');
      return;
    }
    const draft = this.getDraft(file);
    if (draft.states.some((s) => s.name === name)) {
      this.setStatus(`State '${name}' already exists`, 'error');
      return;
    }
    draft.states.push({ name, description: '', transitions: [] });
    if (!draft.initial) draft.initial = name;
    this.newStateName = '';
    this.expandedStates.add(name);
    this.touchDraft(file.path);
    this.setStatus(`Added state '${name}'`, 'ok');
    this.render();
  }

  private removeDraftState(file: WizardFile, name: string): void {
    const draft = this.getDraft(file);
    if (draft.states.length <= 1) {
      this.setStatus('A state machine needs at least one state', 'error');
      return;
    }
    draft.states = draft.states.filter((s) => s.name !== name);
    let removedTransitions = 0;
    for (const st of draft.states) {
      const before = st.transitions.length;
      st.transitions = st.transitions.filter((t) => t.target !== name);
      removedTransitions += before - st.transitions.length;
    }
    if (draft.initial === name) draft.initial = draft.states[0]?.name ?? '';
    this.expandedStates.delete(name);
    this.touchDraft(file.path);
    this.setStatus(
      `Removed state '${name}'` +
        (removedTransitions ? ` and ${removedTransitions} transition(s) pointing to it` : ''),
      'info',
    );
    this.render();
  }

  private applyStateMachine(file: WizardFile): void {
    if (this.isSaving()) return;
    const draft = this.getDraft(file);
    const names = draft.states.map((s) => s.name);
    if (names.length === 0) {
      this.setStatus('Add at least one state before applying', 'error');
      return;
    }
    for (const st of draft.states) {
      for (const t of st.transitions) {
        if (!t.event.trim()) {
          this.setStatus(`State '${st.name}': every transition needs an event name`, 'error');
          return;
        }
        if (!names.includes(t.target)) {
          this.setStatus(`State '${st.name}': transition target '${t.target}' is unknown`, 'error');
          return;
        }
        for (const a of t.actions) {
          if (a.type === 'event' && !a.value.trim()) {
            this.setStatus(`State '${st.name}': an event action needs a name`, 'error');
            return;
          }
          if (a.type === 'other') {
            try {
              JSON.parse(a.value);
            } catch {
              this.setStatus(`State '${st.name}': raw action is not valid JSON`, 'error');
              return;
            }
          }
        }
      }
    }
    if (!draft.initial || !names.includes(draft.initial)) {
      this.setStatus('Choose an initial state', 'error');
      return;
    }
    this.smRenderPending.add(file.path);
    this.send('render_state_machine', {
      path: file.path,
      content: file.content,
      states: this.draftToStates(draft),
      initial_state: draft.initial,
    });
    this.setStatus(`Applying the state machine of ${file.path}…`, 'info');
  }

  private buildCreateForm(detail: HTMLElement): void {
    detail.appendChild(el('h4', 'cw-detail-title', 'New subsystem'));
    detail.appendChild(
      el(
        'p',
        'cw-form-hint',
        'Generates a commented subsystem YAML with the standard initializing → ready → work → ready state machine. ' +
          'The simulator script makes it SIM/REAL-toggleable in simulation mode.',
      ),
    );
    const spec = this.newSubsystemSpec;
    const field = (key: string, label: string, placeholder = ''): HTMLElement => {
      const input = document.createElement('input');
      input.className = 'cw-input';
      input.value = str(spec[key] ?? '');
      input.placeholder = placeholder;
      input.addEventListener('input', () => {
        spec[key] = input.value;
      });
      return fieldRow(label, input);
    };
    const form = el('div', 'cw-form');
    const fields = el('div', 'cw-fields');
    fields.appendChild(field('name', 'Name', 'e.g. laser_scanner'));
    fields.appendChild(field('display_name', 'Display name', 'e.g. Laser Scanner'));
    fields.appendChild(field('type', 'Type', 'analyzer / robot / camera / generic'));
    fields.appendChild(field('description', 'Description', 'human-readable role'));
    fields.appendChild(field('init_duration', 'Init duration (s)', '1.5'));
    fields.appendChild(field('work_state', 'Work state', 'working'));
    fields.appendChild(field('work_event', 'Work event', 'start'));
    form.appendChild(fields);

    const requiredLabel = el('label', 'cw-check');
    const requiredBox = document.createElement('input');
    requiredBox.type = 'checkbox';
    requiredBox.checked = spec['required'] !== false;
    requiredBox.addEventListener('change', () => {
      spec['required'] = requiredBox.checked;
    });
    requiredLabel.appendChild(requiredBox);
    requiredLabel.appendChild(document.createTextNode('Required for the workflow to start'));
    form.appendChild(requiredLabel);

    const simLabel = el('label', 'cw-check');
    const simBox = document.createElement('input');
    simBox.type = 'checkbox';
    simBox.checked = this.newSubsystemSim;
    simBox.addEventListener('change', () => {
      this.newSubsystemSim = simBox.checked;
    });
    simLabel.appendChild(simBox);
    simLabel.appendChild(document.createTextNode('Create the simulator script (<name>_sim.py)'));
    form.appendChild(simLabel);

    const createBtn = el('button', 'btn btn-primary cw-mini', 'Create subsystem');
    createBtn.type = 'button';
    createBtn.addEventListener('click', () => {
      this.send('create_subsystem', {
        spec: {
          name: str(spec['name']),
          display_name: str(spec['display_name']),
          type: str(spec['type']),
          description: str(spec['description']),
          init_duration: spec['init_duration'] ? Number(spec['init_duration']) : 1.5,
          work_state: str(spec['work_state']) || 'working',
          work_event: str(spec['work_event']) || 'start',
          required: spec['required'] !== false,
        },
      });
      this.setStatus('Creating subsystem…', 'info');
    });
    form.appendChild(createBtn);
    detail.appendChild(form);
  }

  private findReferences(name: string): Array<{ path: string; line: number; text: string }> {
    if (!name) return [];
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = new RegExp(`(^|[^\\w/])${escaped}([^\\w]|$)`);
    const out: Array<{ path: string; line: number; text: string }> = [];
    for (const f of this.files) {
      if (f.deleted) continue;
      if (f.category === 'subsystem' && f.name === name) continue;
      f.content.split('\n').forEach((line, idx) => {
        if (pattern.test(line)) out.push({ path: f.path, line: idx + 1, text: line.trim() });
      });
    }
    return out.slice(0, 10);
  }

  private buildRawEditor(file: WizardFile, rows: number): HTMLElement {
    const wrap = el('div', 'cw-editor');
    const head = el('div', 'cw-editor-head');
    head.appendChild(el('span', 'cw-editor-label', file.path));
    const validate = el('button', 'btn btn-ghost cw-mini', 'Validate');
    validate.type = 'button';
    validate.addEventListener('click', () => {
      this.send('validate_config', { path: file.path, content: file.content });
      this.setStatus(`Validating ${file.path}…`, 'info');
    });
    head.appendChild(validate);
    if (isDirty(file)) head.appendChild(el('span', 'cw-chip dirty', 'unsaved'));
    wrap.appendChild(head);

    const ta = document.createElement('textarea');
    ta.className = 'cw-textarea';
    ta.rows = rows;
    ta.spellcheck = false;
    ta.value = file.content;
    ta.addEventListener('input', () => {
      file.content = ta.value;
    });
    wrap.appendChild(ta);

    if (file.errors && file.errors.length > 0) {
      const errs = el('div', 'cw-errors');
      for (const e of file.errors) errs.appendChild(el('div', 'cw-error', e));
      wrap.appendChild(errs);
    }
    return wrap;
  }

  // ------------------------------------------------------------------
  // Step ③ — MQTT message formats
  // ------------------------------------------------------------------
  private renderMessagesStep(): void {
    this.bodyEl.appendChild(
      el(
        'p',
        'cw-intro',
        'Field names used in every MQTT payload. Dotted paths reach nested fields ' +
          '(e.g. data.state). Leave a field empty to inherit the default value.',
      ),
    );
    const doc = this.messageFormats;
    const form = el('div', 'cw-form');
    form.appendChild(el('h4', 'cw-form-title', 'Default formats (all subsystems)'));
    form.appendChild(this.buildFormatFields('default', doc.default, doc.default));
    this.bodyEl.appendChild(form);

    const override = el('div', 'cw-form');
    override.appendChild(el('h4', 'cw-form-title', 'Per-subsystem overrides'));
    override.appendChild(
      el('p', 'cw-form-hint', 'Select a subsystem to declare fields that differ from the default format.'),
    );
    const select = document.createElement('select');
    select.className = 'cw-select';
    const emptyOpt = document.createElement('option');
    emptyOpt.value = '';
    emptyOpt.textContent = '— select a subsystem —';
    select.appendChild(emptyOpt);
    for (const f of this.files.filter((f) => f.category === 'subsystem')) {
      const opt = document.createElement('option');
      opt.value = f.name || f.label;
      opt.textContent = f.name || f.label;
      opt.selected = this.overrideName === opt.value;
      select.appendChild(opt);
    }
    select.addEventListener('change', () => {
      this.overrideName = select.value;
      this.render();
    });
    override.appendChild(select);

    if (this.overrideName) {
      const current = doc.subsystems[this.overrideName] || {};
      override.appendChild(this.buildFormatFields('override', current, doc.default));
      const actions = el('div', 'cw-inline-actions');
      const removeBtn = el('button', 'btn btn-ghost cw-mini', 'Remove override');
      removeBtn.type = 'button';
      removeBtn.addEventListener('click', () => {
        delete doc.subsystems[this.overrideName];
        this.setStatus(`Override for '${this.overrideName}' removed (unsaved)`, 'info');
        this.render();
      });
      actions.appendChild(removeBtn);
      override.appendChild(actions);
    }
    this.bodyEl.appendChild(override);

    this.bodyEl.appendChild(
      el(
        'p',
        'cw-form-hint',
        'Applying the forms regenerates the raw YAML below from these values; ' +
          'raw YAML edits are kept only if you save without pressing Apply formats.',
      ),
    );
    const applyBtn = el('button', 'btn btn-primary cw-mini', 'Apply formats');
    applyBtn.type = 'button';
    applyBtn.addEventListener('click', () => this.applyMessageFormats());
    this.bodyEl.appendChild(applyBtn);

    const file = this.files.find((f) => f.category === 'message_formats');
    if (file) this.bodyEl.appendChild(this.buildRawEditor(file, 14));
  }

  private buildFormatFields(
    scope: 'default' | 'override',
    group: MqttFormatGroup,
    inherited: MqttFormatGroup,
  ): HTMLElement {
    const wrap = el('div', 'cw-format-groups');
    const kinds: Array<[keyof MqttFormatGroup, string]> = [
      ['command', 'Command (orchestrator → subsystem /event)'],
      ['hardware_command', 'Hardware command (pause / resume)'],
      ['status', 'Status report (subsystem → orchestrator /status)'],
      ['response', 'Response (subsystem → orchestrator /response)'],
      ['points_command', 'Points push (orchestrator → consumer /points, R12)'],
    ];
    for (const [kind, label] of kinds) {
      const fieldset = el('div', 'cw-fieldset');
      fieldset.appendChild(el('h5', 'cw-form-title', label));
      const rows = el('div', 'cw-fields');
      const current = (group[kind] ?? {}) as Record<string, unknown>;
      const fallback = (inherited[kind] ?? {}) as Record<string, unknown>;
      const builtins = FORMAT_FALLBACKS[kind] ?? {};
      for (const fieldName of Object.keys({ ...builtins, ...fallback, ...current })) {
        // static_fields is a free-form mapping: edit it in the raw YAML.
        if (fieldName === 'static_fields') continue;
        const value = current[fieldName];
        const placeholder = str(fallback[fieldName] ?? builtins[fieldName] ?? '');
        if (fieldName === 'include_timestamp') {
          const input = document.createElement('input');
          input.type = 'checkbox';
          input.dataset.mf = `${scope}.${kind}.${fieldName}`;
          input.dataset.mfKind = 'bool';
          input.checked = value === undefined ? Boolean(fallback[fieldName] ?? true) : Boolean(value);
          rows.appendChild(fieldRow('Include timestamp', input));
          continue;
        }
        const input = document.createElement('input');
        input.className = 'cw-input';
        input.value = str(value);
        input.placeholder = placeholder;
        input.dataset.mf = `${scope}.${kind}.${fieldName}`;
        input.dataset.mfKind = 'text';
        rows.appendChild(fieldRow(fieldName.replace(/_/g, ' '), input));
      }
      fieldset.appendChild(rows);
      wrap.appendChild(fieldset);
    }
    return wrap;
  }

  private applyMessageFormats(): void {
    // Deep-copy the current document so fields not rendered in the form
    // (e.g. static_fields) survive the round trip to the backend.
    const doc: MessageFormatsDocument = {
      default: JSON.parse(JSON.stringify(this.messageFormats.default || {})),
      subsystems: JSON.parse(JSON.stringify(this.messageFormats.subsystems || {})),
    };
    this.bodyEl.querySelectorAll<HTMLInputElement>('input[data-mf]').forEach((input) => {
      const [scope, kind, fieldName] = (input.dataset.mf ?? '').split('.');
      if (!scope || !kind || !fieldName) return;
      const group = scope === 'default' ? doc.default : this.overrideGroup(doc, this.overrideName);
      if (!group) return;
      const kindGroup = (group[kind as keyof MqttFormatGroup] ?? {}) as Record<string, unknown>;
      let value: unknown;
      if (input.dataset.mfKind === 'bool') value = input.checked;
      else value = input.value.trim();
      if (value === '') delete kindGroup[fieldName];
      else kindGroup[fieldName] = value;
      (group as Record<string, unknown>)[kind] = kindGroup;
      if (this.overrideName && scope === 'override') {
        doc.subsystems[this.overrideName] = group;
      }
    });
    this.messageFormats = doc;
    this.send('render_message_formats', { formats: doc });
    this.setStatus('Rendering mqtt_formats.yaml…', 'info');
  }

  private overrideGroup(doc: MessageFormatsDocument, name: string): MqttFormatGroup {
    if (!name) return {};
    if (!doc.subsystems[name]) doc.subsystems[name] = {};
    return doc.subsystems[name];
  }

  // ------------------------------------------------------------------
  // Step ④ — Review & save
  // ------------------------------------------------------------------
  private renderReviewStep(): void {
    this.bodyEl.appendChild(
      el(
        'p',
        'cw-intro',
        'Review the pending changes. Saving writes the files and creates/deletes subsystems. ' +
          'The backend validates every file first; nothing invalid is written.',
      ),
    );
    const pending = this.files.filter((f) => this.isPendingFile(f));
    const list = el('div', 'cw-review-list');
    if (pending.length === 0) {
      list.appendChild(el('div', 'cw-empty', 'No unsaved changes.'));
    }
    for (const f of pending) {
      const row = el('div', 'cw-review-item');
      const state = f.deleted ? 'delete' : f.created ? 'new' : 'modified';
      row.appendChild(el('span', `cw-review-status ${state}`, state));
      row.appendChild(el('span', 'cw-review-path', f.path));
      if (f.errors && f.errors.length > 0) row.appendChild(el('span', 'cw-chip err', 'invalid'));
      list.appendChild(row);
    }
    this.bodyEl.appendChild(list);

    const note = el('div', 'cw-note');
    note.appendChild(
      el(
        'span',
        undefined,
        'After saving, restart the GUI (Ctrl+C, then relaunch) so the state machines, ' +
          'subsystems and MQTT formats are reloaded.',
      ),
    );
    this.bodyEl.appendChild(note);

    if (this.files.some((f) => f.deleted)) {
      const refs = this.files
        .filter((f) => f.deleted && f.name)
        .flatMap((f) => this.findReferences(f.name || '').map((r) => `${f.name} → ${r.path}:${r.line}`));
      if (refs.length > 0) {
        const refBox = el('div', 'cw-errors');
        refBox.appendChild(el('div', 'cw-error', 'Removed subsystems are still referenced by:'));
        for (const r of refs.slice(0, 10)) refBox.appendChild(el('div', 'cw-error', r));
        this.bodyEl.appendChild(refBox);
      }
    }

    const actions = el('div', 'cw-inline-actions');
    const validateAll = el('button', 'btn btn-ghost cw-mini', 'Validate all');
    validateAll.type = 'button';
    validateAll.addEventListener('click', () => {
      const edited = this.files.filter((f) => !f.deleted && isDirty(f));
      for (const f of edited) {
        this.send('validate_config', { path: f.path, content: f.content });
      }
      this.setStatus(`Validating ${edited.length} file(s)…`, 'info');
    });
    actions.appendChild(validateAll);
    this.bodyEl.appendChild(actions);
  }

  // ------------------------------------------------------------------
  // Save queue
  // ------------------------------------------------------------------
  private startSaveAll(): void {
    if (this.isSaving()) return;
    const ops: PendingOp[] = [];
    for (const f of this.files) {
      if (f.deleted) {
        // A file created in this wizard session and then removed was never
        // written, so there is nothing to delete on disk.
        if (!f.created) {
          ops.push({ kind: 'delete', name: f.name, deleteSimulator: true });
        }
        continue;
      }
      if (this.smDirtyPaths.has(f.path)) {
        // A pending state-machine draft is rendered to YAML first; the
        // save (and simulator creation) is queued when the render returns.
        ops.push({ kind: 'render_sm', path: f.path, name: f.name });
        continue;
      }
      if (!isDirty(f)) continue;
      ops.push({ kind: 'save', path: f.path, name: f.name, content: f.content });
      if (f.created && f.simRequested && f.name) {
        ops.push({ kind: 'sim', name: f.name });
      }
    }
    if (ops.length === 0) {
      this.setStatus('Nothing to save', 'info');
      return;
    }
    this.pendingOps = ops;
    this.totalOps = ops.length;
    this.currentOp = null;
    this.setStatus(`Saving 0/${this.totalOps}…`, 'info');
    this.advanceQueue();
  }

  private advanceQueue(): void {
    if (this.currentOp !== null) return;
    const op = this.pendingOps.shift();
    if (!op) {
      this.setStatus(
        'All changes saved. Restart the GUI (Ctrl+C, relaunch) for them to take effect.',
        'ok',
      );
      this.render();
      return;
    }
    this.currentOp = op;
    const done = this.totalOps - this.pendingOps.length;
    if (op.kind === 'save') {
      this.setStatus(`Saving ${done}/${this.totalOps}: ${op.path}…`, 'info');
      this.send('save_config', { path: op.path, content: op.content });
    } else if (op.kind === 'render_sm') {
      const file = this.files.find((f) => f.path === op.path);
      const draft = file ? this.smDrafts.get(file.path) : undefined;
      if (!file || !draft) {
        // Draft gone (e.g. snapshot reload): skip this op.
        this.currentOp = null;
        this.advanceQueue();
        return;
      }
      this.setStatus(`Applying the state machine of ${file.path}…`, 'info');
      this.smRenderPending.add(file.path);
      this.send('render_state_machine', {
        path: file.path,
        content: file.content,
        states: this.draftToStates(draft),
        initial_state: draft.initial,
      });
    } else if (op.kind === 'sim') {
      this.setStatus(`Creating simulator for '${op.name}'…`, 'info');
      this.send('write_simulator', { name: op.name });
    } else {
      this.setStatus(`Deleting subsystem '${op.name}'…`, 'info');
      this.send('delete_subsystem', { name: op.name, delete_simulator: op.deleteSimulator });
    }
  }
}
