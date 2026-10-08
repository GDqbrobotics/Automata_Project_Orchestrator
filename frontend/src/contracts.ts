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
  | 'messages_cleared'
  | 'subsystem_state_mismatch';

/**
 * A workflow state name. workflow.yaml is the single source of truth (R3):
 * the backend sends raw state names and the GUI derives its behaviour from
 * the broadcast state machine definition (F1), so this is an open string.
 */
export type WorkflowStateType = string;

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
  /**
   * Visual tone (ready / busy / error / idle / offline / warning) computed
   * by the backend from the configured vocabulary (global.yaml `states:`);
   * the UI renders it as-is instead of enumerating state names (F3).
   */
  tone?: string;
  required: boolean;
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
  /**
   * The YAML `metadata:` section. For the workflow definition it carries
   * the operator-event map the GUI derives its button matrix from (F1);
   * the object definition carries the machine layout / task chains.
   */
  metadata?: Record<string, unknown>;
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
  /** Topic kind: 'status' (state report) or 'response'. Drives the UI. */
  kind?: string;
  /** For kind='status': the payload field carrying the reported state. */
  state_field?: string;
  /** For kind='status': the payload field mirroring the ready flag. */
  ready_field?: string;
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
  /** Client pseudo-events: 'connected' | 'disconnected' | 'error'. */
  type?: string;
  /** Backend event name (see WorkflowEvent). */
  event?: WorkflowEvent;
  /** Client pseudo-event detail (e.g. the WebSocket error message). */
  error?: string;
  /** Backend event details (status broadcasts, object/subsystem events). */
  details?: Record<string, unknown>;
}
