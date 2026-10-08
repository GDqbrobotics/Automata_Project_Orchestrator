/**
 * Workflow state display messages.
 *
 * The per-state messages are sent by the backend from global.yaml
 * (`workflow_states`, broadcast in the display config as `state_messages`)
 * — the frontend keeps only the error interpolation, which needs the live
 * error detail (docs/CONFIG_CHANGE_GUIDE.md, F5). Which state is the error
 * state comes from the workflow definition's metadata (error_state).
 */

import type { WorkflowStateType } from '../contracts.js';

/** Format the message shown while the workflow is in its error state. */
export function formatErrorMessage(errorMessage?: string | null): string {
  return `Error: ${errorMessage || 'Workflow interrupted'}`;
}

/**
 * Get the display message for a workflow state.
 *
 * Prefers the message sent by the backend (global.yaml `workflow_states`);
 * the only frontend-side knowledge is how to interpolate the error detail
 * into the error-state message.
 *
 * @param state - The workflow state
 * @param backendMessages - Messages from the display config (if any)
 * @param isErrorState - True when `state` is the workflow's error state
 *                       (workflow.yaml metadata.error_state)
 * @param errorMessage - Optional error detail
 * @returns The human-readable message
 */
export function getStateMessage(
  state: WorkflowStateType,
  backendMessages: Record<string, string> | undefined,
  isErrorState: boolean,
  errorMessage?: string | null,
): string {
  if (isErrorState) return formatErrorMessage(errorMessage);
  const msg = backendMessages?.[state];
  if (typeof msg === 'string') return msg;
  return state;
}
