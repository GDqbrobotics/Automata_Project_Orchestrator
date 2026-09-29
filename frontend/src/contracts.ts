/** Shared contract types – mirrors shared/contracts.py */

export type WorkflowCommand =
  | 'start_workflow'
  | 'pause_workflow'
  | 'resume_workflow'
  | 'stop_workflow'
  | 'request_status'
  | 'reset_workflow'
  | 'assign_alias'
  | 'toggle_subsystem_simulation'
  | 'debug_send_subsystem_message';

export type WorkflowEvent =
  | 'workflow_state_changed'
  | 'subsystem_status_updated'
  | 'subsystem_state_changed'
  | 'error_reported'
  | 'progress_updated'
  | 'object_state_changed'
  | 'object_added'
  | 'object_removed'
  | 'object_alias_assigned'
  | 'subsystem_simulation_toggled'
  | 'subsystem_message'
  | 'object_message'
  | 'messages_cleared';

export type WorkflowStateType =
  | 'idle'
  | 'starting'
  | 'running'
  | 'paused'
  | 'stopping'
  | 'stopped'
  | 'error';

export interface WorkflowStatus {
  state: WorkflowStateType;
  progress: number;
  total_objects: number;
  current_object: string | null;
  subsystems: Record<string, SubsystemStatus>;
  error: string | null;
  last_update: string;
}

export interface SubsystemStatus {
  name: string;
  display_name: string;
  type: string;
  state: string;
  state_description: string;
  ready: boolean;
  required: boolean;
  error: string | null;
  metadata: Record<string, unknown>;
}

export interface SubsystemConfig {
  name: string;
  display_name: string;
  type: string;
  mqtt_topic: string;
  initial_state: string;
  required: boolean;
  metadata: Record<string, unknown>;
}

export interface ObjectInfo {
  object_id: string;
  state: string;
  state_description: string;
  created: string;
  last_state_change: string;
  transitions: Array<{ from: string; to: string; event: string; timestamp: string }>;
  /** Operator-assigned alias ID used by the local database ('' = none). */
  alias?: string;
}

/** One transition edge in a state machine definition (from YAML). */
export interface SmTransition {
  event: string;
  target: string;
}

/** One state in a state machine definition (from YAML). */
export interface SmStateDef {
  description: string;
  transitions: SmTransition[];
}

/** Full state machine graph definition, as serialized by the backend. */
export interface StateMachineDef {
  name: string;
  initial_state: string;
  states: Record<string, SmStateDef>;
}

/** One MQTT message seen on a subsystem's topics. */
export interface SubsystemMessage {
  direction: 'sent' | 'received';
  topic: string;
  payload: unknown;
  timestamp: string;
  /** Consecutive-duplicate count (e.g. repeated heartbeats). */
  count?: number;
}

/** One MQTT topic a real subsystem is expected to publish (debug mode). */
export interface DebugTopicDescriptor {
  /** Full topic, e.g. 'automata/subsystem/xrf/status'. */
  topic: string;
  description: string;
  /** Sensible starting payload, editable in the inspector. */
  default_payload: Record<string, unknown>;
}

/**
 * Simulation info sent with status broadcasts when the backend runs
 * --simulation: which subsystems are currently served by their simulated
 * process (the operator can toggle each one from its card before the
 * workflow starts).
 */
export interface SimulationInfo {
  enabled: boolean;
  /** Subsystem names currently served by a simulated process. */
  simulated: string[];
  /** Subsystem names for which a simulator exists (all toggled-off ones can be re-enabled). */
  available?: string[];
}

/** Debug config sent with status broadcasts when the backend runs --debug. */
export interface DebugConfig {
  enabled: boolean;
  /** Subsystem name -> its outgoing MQTT topics. */
  topics: Record<string, DebugTopicDescriptor[]>;
}

/** One internal message of an object's state machine. */
export interface ObjectMessage {
  kind: 'transition' | 'event';
  /** kind=transition: source state */
  from?: string;
  /** kind=transition: target state */
  to?: string;
  /** The event name (transition event, or fired scoped event). */
  event: string;
  /** kind=event: where the fired event went (subsystem / pipeline / broadcast). */
  target?: string;
  timestamp: string;
}

export interface FrontendMessage {
  type?: string;
  command?: WorkflowCommand;
  event?: WorkflowEvent;
  received?: boolean;
  error?: string;
  new_state?: string;
  message?: string;
  details?: Record<string, unknown>;
  subsystems?: SubsystemConfig[];
  objects?: ObjectInfo[];
}
