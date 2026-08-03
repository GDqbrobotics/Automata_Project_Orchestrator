import type {
  WorkflowStateType,
  WorkflowStatus,
  SubsystemStatus,
  SubsystemConfig,
  ObjectInfo,
} from '../contracts.js';
import { getStateMessage } from '../config/state-messages.js';

export interface UiState {
  workflowState: WorkflowStateType;
  progress: number;
  totalObjects: number;
  currentObject: string | null;
  message: string;
  error: string | null;
  subsystems: Record<string, SubsystemStatus>;
  subsystemsConfig: SubsystemConfig[];
  objects: ObjectInfo[];
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
    objects: [],
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

    updated.message = getStateMessage(newState, state.error || undefined);
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

  // Handle subsystem state changes from state machine transitions
  if (m.event === 'subsystem_state_changed' && m.details) {
    const d = m.details as Record<string, unknown>;
    const name = d.subsystem as string;
    const newState = d.new_state as string;
    const source = d.source as string;
    if (name && newState) {
      // Update or create subsystem status
      const existing = updated.subsystems[name] || { name };
      updated.subsystems = {
        ...updated.subsystems,
        [name]: {
          ...existing,
          name,
          state: newState,
          ready: newState === 'ready',
        } as SubsystemStatus,
      };
      logs.push({
        time: now(),
        source: 'subsystem',
        message: `${name}: ${existing.state || 'unknown'} → ${newState} (${source})`,
        level: 'info',
      });
    }
  }

  // Handle subsystem state changes from state machine transitions
  if (m.event === 'subsystem_state_changed' && m.details) {
    const d = m.details as Record<string, unknown>;
    const name = d.subsystem as string;
    const newState = d.new_state as string;
    if (name && newState) {
      // Update or create subsystem status
      const existing = updated.subsystems[name] || { name };
      updated.subsystems = {
        ...updated.subsystems,
        [name]: {
          ...existing,
          name,
          state: newState,
          ready: newState === 'ready',
        } as SubsystemStatus,
      };
      logs.push({
        time: now(),
        source: 'subsystem',
        message: `${name}: ${existing.state || 'unknown'} → ${newState}`,
        level: 'info',
      });
    }
  }

  // Also handle top-level status updates
  if (m.state) {
    const s = m as unknown as WorkflowStatus & { subsystems_config?: SubsystemConfig[]; objects?: ObjectInfo[] };
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
    if (s.objects && Array.isArray(s.objects)) {
      updated.objects = s.objects as ObjectInfo[];
      if (updated.objects.length > 0) {
        logs.push({
          time: now(),
          source: 'objects',
          message: `Loaded ${updated.objects.length} object(s)`,
          level: 'info',
        });
      }
    }
  }

  // Handle object list updates from status
  if (m.event === 'workflow_state_changed' && m.details) {
    const d = m.details as Record<string, unknown>;
    if (d.objects && Array.isArray(d.objects)) {
      updated.objects = d.objects as ObjectInfo[];
      if (updated.objects.length > 0) {
        logs.push({
          time: now(),
          source: 'objects',
          message: `Updated ${updated.objects.length} object(s)`,
          level: 'info',
        });
      }
    }
  }

  // Handle individual object state changes
  if (m.event === 'object_state_changed' && m.details) {
    const d = m.details as Record<string, unknown>;
    const objectId = d.object_id as string;
    const newState = d.new_state as string;
    if (objectId && newState) {
      // Update or add object in the list
      const existingIdx = updated.objects.findIndex(o => o.object_id === objectId);
      const objInfo = updated.objects[existingIdx] || { object_id: objectId };
      const updatedObj = {
        ...objInfo,
        object_id: objectId,
        state: newState,
        last_state_change: now(),
      } as ObjectInfo;

      if (existingIdx >= 0) {
        updated.objects = [...updated.objects];
        updated.objects[existingIdx] = updatedObj;
      } else {
        updated.objects = [...updated.objects, updatedObj];
      }

      logs.push({
        time: now(),
        source: 'object',
        message: `${objectId}: ${objInfo.state || 'unknown'} → ${newState}`,
        level: 'info',
      });
    }
  }

  // Handle object added
  if (m.event === 'object_added' && m.details) {
    const d = m.details as Record<string, unknown>;
    const objectId = d.object_id as string;
    if (objectId) {
      const objInfo = {
        object_id: objectId,
        state: d.state as string || 'identified_in_box',
        state_description: d.state_description as string || '',
        created: now(),
        last_state_change: now(),
        transitions: [],
      } as ObjectInfo;
      updated.objects = [...updated.objects, objInfo];
      logs.push({
        time: now(),
        source: 'object',
        message: `Object added: ${objectId} (${objInfo.state})`,
        level: 'success',
      });
    }
  }

  // Handle object removed
  if (m.event === 'object_removed' && m.details) {
    const d = m.details as Record<string, unknown>;
    const objectId = d.object_id as string;
    if (objectId) {
      updated.objects = updated.objects.filter(o => o.object_id !== objectId);
      logs.push({
        time: now(),
        source: 'object',
        message: `Object removed: ${objectId}`,
        level: 'warning',
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
