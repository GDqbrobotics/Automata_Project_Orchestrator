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
  /** Object workflow state display order, sent by backend from global.yaml */
  objectStateOrder: string[];
  /** Object workflow state colors, sent by backend from global.yaml */
  objectStateColors: Record<string, string>;
  /** Workflow state display messages, sent by backend from global.yaml */
  workflowStateMessages: Record<string, string>;
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
    objectStateOrder: [],
    objectStateColors: {},
    workflowStateMessages: {},
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
      // The backend does not send a display message; derive it so the
      // stored message never degrades to an empty string.
      updated.message = getStateMessage(newState, state.error || undefined);
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
      if (d.progress !== undefined) {
        updated.progress = d.progress as number;
      } else {
        updated.progress += 1;
      }
    } else {
      updated.progress += 1;
    }
    logs.push({ time: now(), source: 'progress', message: `Progress: ${updated.progress}`, level: 'success' });
  }

  if (m.event === 'error_reported') {
    // Backend sends errors as {event: 'error_reported', details: {message: '...'}}
    const errDetails = m.details as Record<string, unknown> | undefined;
    updated.error = (errDetails && (errDetails.message as string)) || 'Unknown error';
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

  // Handle object list updates from workflow_state_changed details
  if (m.event === 'workflow_state_changed' && m.details) {
    const d = m.details as Record<string, unknown>;
    // Update objects list from status
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
    // Update progress from status
    if (d.progress !== undefined && typeof d.progress === 'number') {
      updated.progress = d.progress;
    }
    if (d.total_objects !== undefined) {
      updated.totalObjects = d.total_objects as number;
    }
    // Update subsystems from status
    if (d.subsystems && typeof d.subsystems === 'object') {
      updated.subsystems = d.subsystems as Record<string, SubsystemStatus>;
    }
    // Update current object from status
    if (d.current_object !== undefined) {
      updated.currentObject = d.current_object as string | null;
    }
    // Update object workflow display config (state order + colors)
    if (d.state_order && Array.isArray(d.state_order)) {
      updated.objectStateOrder = d.state_order as string[];
      logs.push({ time: now(), source: 'config', message: `State order updated (${updated.objectStateOrder.length} states)`, level: 'info' });
    }
    if (d.state_colors && typeof d.state_colors === 'object') {
      updated.objectStateColors = d.state_colors as Record<string, string>;
      logs.push({ time: now(), source: 'config', message: `State colors updated (${Object.keys(updated.objectStateColors).length} colors)`, level: 'info' });
    }
    // Update workflow state display messages (from global.yaml)
    if (d.state_messages && typeof d.state_messages === 'object') {
      updated.workflowStateMessages = d.state_messages as Record<string, string>;
    }
  }

  // Handle top-level status updates (broadcast_status payload)
  if (m.state) {
    const s = m as unknown as WorkflowStatus & { subsystems_config?: SubsystemConfig[]; objects?: ObjectInfo[]; state_order?: string[]; state_colors?: Record<string, string> };
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
    // Update object workflow display config from top-level payload
    if (s.state_order && Array.isArray(s.state_order)) {
      updated.objectStateOrder = s.state_order as string[];
    }
    if (s.state_colors && typeof s.state_colors === 'object') {
      updated.objectStateColors = s.state_colors as Record<string, string>;
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
