import type {
  WorkflowStateType,
  WorkflowStatus,
  SubsystemStatus,
  SubsystemConfig,
  ObjectInfo,
  StateMachineDef,
  SubsystemMessage,
  ObjectMessage,
  DebugConfig,
  SimulationInfo,
} from '../contracts.js';
import { getStateMessage } from '../config/state-messages.js';

/** Max messages kept per subsystem / object in the inspector history. */
const MESSAGE_HISTORY_LIMIT = 100;

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
  /** Subsystem state machine graph definitions (for the inspector modal). */
  subsystemStateMachines: Record<string, StateMachineDef>;
  /** Object state machine graph definition (shared blueprint). */
  objectStateMachine: StateMachineDef | null;
  /** Debug config (null when the backend is not running with --debug). */
  debug: DebugConfig | null;
  /** Simulation info (null when the backend is not running with --simulation). */
  simulation: SimulationInfo | null;
  /** Recent MQTT messages per subsystem (sent/received). */
  subsystemMessages: Record<string, SubsystemMessage[]>;
  /** Recent internal messages per object (transitions + fired events). */
  objectMessages: Record<string, ObjectMessage[]>;
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
    subsystemStateMachines: {},
    objectStateMachine: null,
    debug: null,
    simulation: null,
    subsystemMessages: {},
    objectMessages: {},
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
    // Incremental events carry new_state; full status broadcasts (and the
    // replay sent to a (re)connecting client) carry state.
    const wsState = d.new_state || d.state;
    if (wsState) {
      const newState = wsState as WorkflowStateType;
      if (newState !== state.workflowState) {
        logs.push({ time: now(), source: 'workflow', message: `State → ${newState}`, level: 'info' });
      }
      updated.workflowState = newState;
      // The backend does not send a display message; derive it so the
      // stored message never degrades to an empty string.
      updated.message = getStateMessage(newState, state.error || undefined);
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
          state_description:
            (d.state_description as string) || existing.state_description || '',
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

  // Subsystem was commanded into a state but never reported it within the
  // timeout: its real state (over MQTT) lags the expectation. Warning only –
  // the card keeps showing the last reported (real) state.
  if (m.event === 'subsystem_state_mismatch' && m.details) {
    const d = m.details as Record<string, unknown>;
    const name = d.subsystem as string;
    const expected = d.expected_state as string;
    const reported = d.reported_state as string;
    const command = d.command as string;
    const message =
      (d.message as string) ||
      `${name}: expected '${expected}' after '${command}', reported '${reported}'`;
    if (name) {
      logs.push({ time: now(), source: 'subsystem', message, level: 'warning' });
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
    // State machine graph definitions (inspector modal)
    if (d.subsystem_state_machines && typeof d.subsystem_state_machines === 'object') {
      updated.subsystemStateMachines = d.subsystem_state_machines as Record<string, StateMachineDef>;
    }
    if (d.object_state_machine && typeof d.object_state_machine === 'object') {
      updated.objectStateMachine = d.object_state_machine as StateMachineDef;
    }
    // Debug config (backend started with --debug)
    if (d.debug && typeof d.debug === 'object') {
      updated.debug = d.debug as DebugConfig;
    }
    // Simulation info (backend started with --simulation)
    if (d.simulation && typeof d.simulation === 'object') {
      updated.simulation = d.simulation as SimulationInfo;
    }
  }

  // Handle top-level status updates (broadcast_status payload)
  if (m.state) {
    const s = m as unknown as WorkflowStatus & {
      subsystems_config?: SubsystemConfig[];
      objects?: ObjectInfo[];
      state_order?: string[];
      state_colors?: Record<string, string>;
      subsystem_state_machines?: Record<string, StateMachineDef>;
      object_state_machine?: StateMachineDef;
      debug?: DebugConfig;
      simulation?: SimulationInfo;
    };
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
    // State machine graph definitions from top-level payload
    if (s.subsystem_state_machines && typeof s.subsystem_state_machines === 'object') {
      updated.subsystemStateMachines = s.subsystem_state_machines as Record<string, StateMachineDef>;
    }
    if (s.object_state_machine && typeof s.object_state_machine === 'object') {
      updated.objectStateMachine = s.object_state_machine as StateMachineDef;
    }
    // Debug config from top-level payload (backend started with --debug)
    if (s.debug && typeof s.debug === 'object') {
      updated.debug = s.debug;
    }
    // Simulation info from top-level payload (backend started with --simulation)
    if (s.simulation && typeof s.simulation === 'object') {
      updated.simulation = s.simulation as SimulationInfo;
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

  // Handle per-subsystem simulation toggle (simulation mode, operator action)
  if (m.event === 'subsystem_simulation_toggled' && m.details) {
    const d = m.details as Record<string, unknown>;
    const name = d.subsystem as string;
    const simulated = Boolean(d.simulated);
    if (name && updated.simulation) {
      const set = new Set(updated.simulation.simulated);
      if (simulated) {
        set.add(name);
      } else {
        set.delete(name);
      }
      updated.simulation = { ...updated.simulation, simulated: [...set].sort() };
    }
    logs.push({
      time: now(),
      source: 'simulation',
      message: `${name}: ${simulated ? 'simulated' : 'real hardware'}`,
      level: 'info',
    });
  }

  // Handle object alias assignment (operator action)
  if (m.event === 'object_alias_assigned' && m.details) {
    const d = m.details as Record<string, unknown>;
    const objectId = d.object_id as string;
    const alias = (d.alias as string) || '';
    if (objectId) {
      const idx = updated.objects.findIndex((o) => o.object_id === objectId);
      if (idx >= 0) {
        updated.objects = [...updated.objects];
        updated.objects[idx] = { ...updated.objects[idx], alias };
      }
      logs.push({
        time: now(),
        source: 'object',
        message: `Alias for ${objectId}: ${alias || '(cleared)'}`,
        level: 'success',
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
        alias: (d.alias as string) || '',
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
      if (objectId in updated.objectMessages) {
        const rest = { ...updated.objectMessages };
        delete rest[objectId];
        updated.objectMessages = rest;
      }
      logs.push({
        time: now(),
        source: 'object',
        message: `Object removed: ${objectId}`,
        level: 'warning',
      });
    }
  }

  // Handle subsystem MQTT message traffic (inspector modal feed)
  if (m.event === 'subsystem_message' && m.details) {
    const d = m.details as Record<string, unknown>;
    const name = d.subsystem as string;
    if (name) {
      const list = [...(updated.subsystemMessages[name] || [])];
      const msg: SubsystemMessage = {
        direction: (d.direction as 'sent' | 'received') || 'received',
        topic: (d.topic as string) || '',
        payload: d.payload,
        timestamp: (d.timestamp as string) || now(),
      };
      // Collapse consecutive duplicates (e.g. status heartbeats with an
      // unchanged payload): bump the counter instead of flooding the feed.
      const last = list[list.length - 1];
      if (
        last &&
        last.direction === msg.direction &&
        last.topic === msg.topic &&
        JSON.stringify(last.payload) === JSON.stringify(msg.payload)
      ) {
        last.count = (last.count || 1) + 1;
        last.timestamp = msg.timestamp;
      } else {
        list.push(msg);
      }
      if (list.length > MESSAGE_HISTORY_LIMIT) {
        list.splice(0, list.length - MESSAGE_HISTORY_LIMIT);
      }
      updated.subsystemMessages = { ...updated.subsystemMessages, [name]: list };
    }
  }

  // Handle object internal messages (inspector modal feed)
  if (m.event === 'object_message' && m.details) {
    const d = m.details as Record<string, unknown>;
    const objectId = d.object_id as string;
    if (objectId) {
      const list = [...(updated.objectMessages[objectId] || [])];
      list.push({
        kind: (d.kind as 'transition' | 'event') || 'transition',
        from: d.from as string | undefined,
        to: d.to as string | undefined,
        event: (d.event as string) || '',
        target: d.target as string | undefined,
        timestamp: (d.timestamp as string) || now(),
      });
      if (list.length > MESSAGE_HISTORY_LIMIT) {
        list.splice(0, list.length - MESSAGE_HISTORY_LIMIT);
      }
      updated.objectMessages = { ...updated.objectMessages, [objectId]: list };
    }
  }

  // Handle message-history clear (workflow reset)
  if (m.event === 'messages_cleared') {
    updated.subsystemMessages = {};
    updated.objectMessages = {};
  }

  // Trim logs
  if (logs.length > 200) {
    logs.splice(0, logs.length - 200);
  }
  updated.logs = logs;

  return updated;
}
