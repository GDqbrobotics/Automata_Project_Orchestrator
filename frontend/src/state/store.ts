import type {
  WorkflowStateType,
  SubsystemStatus,
  SubsystemConfig,
  ObjectInfo,
  StateMachineDef,
  SubsystemMessage,
  ObjectMessage,
  DebugConfig,
  SimulationInfo,
} from '../contracts.js';

/** Max messages kept per subsystem / object in the inspector history. */
const MESSAGE_HISTORY_LIMIT = 100;

export interface UiState {
  workflowState: WorkflowStateType;
  progress: number;
  totalObjects: number;
  currentObject: string | null;
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
  /** Object journey final states (object.yaml, F4): the "done" styling. */
  objectFinalStates: string[];
  /** Ready state of subsystems (global.yaml states.ready, F5). */
  readyState: string;
  /** Alias input constraint (global.yaml, F7): regex pattern. */
  aliasPattern: string;
  /** Subsystem state machine graph definitions (for the inspector modal). */
  subsystemStateMachines: Record<string, StateMachineDef>;
  /** Object state machine graph definition (shared blueprint). */
  objectStateMachine: StateMachineDef | null;
  /**
   * Workflow state machine definition (broadcast with status): carries the
   * metadata.operator_events map the GUI derives its button matrix from (F1)
   * and metadata.error_state (the error-state message, F5).
   */
  workflowStateMachine: StateMachineDef | null;
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
    error: null,
    subsystems: {},
    subsystemsConfig: [],
    objects: [],
    connectionStatus: 'disconnected',
    objectStateOrder: [],
    objectStateColors: {},
    workflowStateMessages: {},
    objectFinalStates: [],
    readyState: 'ready',
    aliasPattern: '^[A-Za-z0-9_\\-\\. ]{1,64}$',
    subsystemStateMachines: {},
    objectStateMachine: null,
    workflowStateMachine: null,
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
          // Visual tone from the configured vocabulary (F3); the payload
          // carries it, fall back to the last known one otherwise.
          tone: (d.tone as string) || existing.tone || 'busy',
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
    // Object journey final states (object.yaml, F4): the "done" styling
    if (d.final_states && Array.isArray(d.final_states)) {
      updated.objectFinalStates = d.final_states as string[];
    }
    // Shared vocabulary the UI must not re-hardcode (F5/F7)
    if (typeof d.ready_state === 'string' && d.ready_state) {
      updated.readyState = d.ready_state;
    }
    if (typeof d.alias_pattern === 'string' && d.alias_pattern) {
      updated.aliasPattern = d.alias_pattern;
    }
    // State machine graph definitions (inspector modal + button matrix F1)
    if (d.subsystem_state_machines && typeof d.subsystem_state_machines === 'object') {
      updated.subsystemStateMachines = d.subsystem_state_machines as Record<string, StateMachineDef>;
    }
    if (d.object_state_machine && typeof d.object_state_machine === 'object') {
      updated.objectStateMachine = d.object_state_machine as StateMachineDef;
    }
    if (d.workflow_state_machine && typeof d.workflow_state_machine === 'object') {
      updated.workflowStateMachine = d.workflow_state_machine as StateMachineDef;
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
      // Fallback for the initial state: the object blueprint's initial state
      // (object.yaml) — not a hardcoded state name (R6/F4).
      const initialObjectState =
        updated.objectStateMachine?.initial_state || state.objectStateMachine?.initial_state;
      const objInfo = {
        object_id: objectId,
        state: (d.state as string) || initialObjectState || 'unknown',
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
