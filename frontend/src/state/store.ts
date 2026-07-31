import type {
  WorkflowStateType,
  WorkflowStatus,
  SubsystemStatus,
  SubsystemConfig,
} from '../contracts.js';

export interface UiState {
  workflowState: WorkflowStateType;
  progress: number;
  totalObjects: number;
  currentObject: string | null;
  message: string;
  error: string | null;
  subsystems: Record<string, SubsystemStatus>;
  subsystemsConfig: SubsystemConfig[];
  connectionStatus: 'connected' | 'disconnected';
  logs: Array<{ time: string; source: string; message: string; level: 'info' | 'error' | 'success' | 'warning' }>;
}

function now(): string {
  const d = new Date();
  return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`;
}

export function createInitialState(): UiState {
  return {
    workflowState: 'idle',
    progress: 0,
    totalObjects: 0,
    currentObject: null,
    message: 'Waiting for operator input',
    error: null,
    subsystems: {},
    subsystemsConfig: [],
    connectionStatus: 'disconnected',
    logs: [],
  };
}

export function reduceState(state: UiState, msg: unknown): UiState {
  const updated = { ...state };
  const logs = [...state.logs];
  const m = msg as Record<string, unknown>;

  // Handle subsystem configuration (top-level or within details)
  if (m.subsystems && Array.isArray(m.subsystems)) {
    updated.subsystemsConfig = m.subsystems as SubsystemConfig[];
    logs.push({
      time: now(),
      source: 'config',
      message: `Loaded ${updated.subsystemsConfig.length} subsystem config(s)`,
      level: 'info',
    });
  }
  if (m.subsystems_config && Array.isArray(m.subsystems_config)) {
    updated.subsystemsConfig = m.subsystems_config as SubsystemConfig[];
    logs.push({
      time: now(),
      source: 'config',
      message: `Loaded ${updated.subsystemsConfig.length} subsystem config(s)`,
      level: 'info',
    });
  }

  if (m.new_state) {
    const newState = m.new_state as WorkflowStateType;
    updated.workflowState = newState;

    const messages: Record<WorkflowStateType, string> = {
      idle: 'Waiting for operator input',
      starting: 'Initializing subsystems...',
      running: 'Workflow in progress',
      paused: 'Workflow paused',
      stopping: 'Stopping workflow...',
      stopped: 'Workflow stopped by operator',
      completing: 'Finalizing and uploading data...',
      error: `Error: ${state.error || 'Workflow interrupted'}`,
    };
    updated.message = messages[newState] || '';
    logs.push({ time: now(), source: 'workflow', message: `State → ${newState}`, level: 'info' });
  }

  if (m.event === 'workflow_state_changed' && m.details) {
    const d = m.details as Record<string, unknown>;
    if (d.new_state) {
      const newState = d.new_state as WorkflowStateType;
      updated.workflowState = newState;
      updated.message = d.message as string || '';
      logs.push({ time: now(), source: 'workflow', message: `State → ${newState}`, level: 'info' });
    }
    // Subsystem config may be included in the details
    if (d.subsystems_config && Array.isArray(d.subsystems_config)) {
      updated.subsystemsConfig = d.subsystems_config as SubsystemConfig[];
      if (updated.subsystemsConfig.length > 0) {
        logs.push({
          time: now(),
          source: 'config',
          message: `Loaded ${updated.subsystemsConfig.length} subsystem config(s)`,
          level: 'info',
        });
      }
    }
  }

  if (m.event === 'progress_updated') {
    if (m.details) {
      const d = m.details as Record<string, unknown>;
      updated.progress = (d.progress as number) ?? updated.progress + 1;
    } else {
      updated.progress += 1;
    }
    logs.push({ time: now(), source: 'progress', message: `Progress: ${updated.progress}`, level: 'success' });
  }

  if (m.event === 'error_reported') {
    updated.error = m.message as string || 'Unknown error';
    logs.push({ time: now(), source: 'error', message: updated.error, level: 'error' });
  }

  if (m.event === 'subsystem_status_updated' && m.details) {
    const d = m.details as Record<string, unknown>;
    const name = d.subsystem as string;
    if (name) {
      updated.subsystems = { ...updated.subsystems, [name]: d as unknown as SubsystemStatus };
    }
  }

  // Also handle top-level status updates
  if (m.state) {
    const s = m as unknown as WorkflowStatus & { subsystems_config?: SubsystemConfig[] };
    updated.workflowState = s.state;
    updated.progress = s.progress;
    updated.totalObjects = s.total_objects;
    updated.currentObject = s.current_object;
    updated.error = s.error;
    if (s.subsystems) {
      updated.subsystems = s.subsystems;
    }
    if (s.subsystems_config && Array.isArray(s.subsystems_config)) {
      updated.subsystemsConfig = s.subsystems_config;
      logs.push({
        time: now(),
        source: 'config',
        message: `Loaded ${updated.subsystemsConfig.length} subsystem config(s)`,
        level: 'info',
      });
    }
  }

  // Trim logs
  if (logs.length > 200) {
    logs.splice(0, logs.length - 200);
  }
  updated.logs = logs;

  return updated;
}
