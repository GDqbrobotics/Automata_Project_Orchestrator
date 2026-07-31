/** Shared contract types – mirrors shared/contracts.py */

export type WorkflowCommand =
  | 'start_workflow'
  | 'pause_workflow'
  | 'resume_workflow'
  | 'stop_workflow'
  | 'request_status';

export type WorkflowEvent =
  | 'workflow_state_changed'
  | 'subsystem_status_updated'
  | 'error_reported'
  | 'progress_updated';

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
  state: string;
  ready: boolean;
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
}
