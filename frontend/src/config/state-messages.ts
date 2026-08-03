/**
 * Workflow state display messages.
 *
 * Maps workflow states to human-readable messages shown in the UI.
 * These should stay in sync with the workflow_states section in
 * backend/config/global.yaml.
 */

import type { WorkflowStateType } from '../contracts.js';

/** Default state messages, matching global.yaml workflow_states. */
export const DEFAULT_STATE_MESSAGES: Record<WorkflowStateType, string | ((err?: string) => string)> = {
  idle: 'Waiting for operator input',
  starting: 'Initializing subsystems...',
  running: 'Workflow in progress',
  paused: 'Workflow paused',
  stopping: 'Stopping workflow...',
  stopped: 'Workflow stopped by operator',
  completing: 'Finalizing and uploading data...',
  error: (error?: string) => `Error: ${error || 'Workflow interrupted'}`,
};

/**
 * Get the display message for a workflow state.
 *
 * Supports both string and function message values.
 *
 * @param state - The workflow state
 * @param errorMessage - Optional error message (used when state is 'error')
 * @returns The human-readable message
 */
export function getStateMessage(
  state: WorkflowStateType,
  errorMessage?: string,
): string {
  const defaultMsg = DEFAULT_STATE_MESSAGES[state];
  if (typeof defaultMsg === 'function') {
    return defaultMsg(errorMessage);
  }
  return defaultMsg;
}
