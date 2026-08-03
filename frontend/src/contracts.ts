/** Shared contract types – mirrors shared/contracts.py */

export type WorkflowCommand =
  | 'start_workflow'
  | 'pause_workflow'
  | 'resume_workflow'
  | 'stop_workflow'
  | 'request_status'
  | 'reset_workflow';

export type WorkflowEvent =
  | 'workflow_state_changed'
  | 'subsystem_status_updated'
  | 'subsystem_state_changed'
  | 'error_reported'
  | 'progress_updated'
  | 'object_state_changed'
  | 'object_added'
  | 'object_removed';

export type WorkflowStateType =
  | 'idle'
  | 'starting'
  | 'running'
  | 'paused'
  | 'stopping'
  | 'stopped'
  | 'completing'
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
