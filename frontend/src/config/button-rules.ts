/**
 * Workflow state button visibility rules.
 *
 * Defines which operator buttons are enabled for each workflow state.
 * This mirrors the state machine transitions defined in workflow.yaml
 * and should stay in sync with the backend configuration.
 */

import type { WorkflowStateType } from '../contracts.js';

export interface ButtonRule {
  start: boolean;
  pause: boolean;
  resume: boolean;
  stop: boolean;
  reset: boolean;
}

/** Button enable/disable rules per workflow state. */
export const BUTTON_RULES: Record<WorkflowStateType, ButtonRule> = {
  idle: {
    start: true,
    pause: false,
    resume: false,
    stop: false,
    reset: false,
  },
  starting: {
    start: false,
    pause: false,
    resume: false,
    stop: true,
    reset: false,
  },
  running: {
    start: false,
    pause: true,
    resume: false,
    stop: true,
    reset: false,
  },
  paused: {
    start: false,
    pause: false,
    resume: true,
    stop: true,
    reset: false,
  },
  stopping: {
    start: false,
    pause: false,
    resume: false,
    stop: false,
    reset: false,
  },
  stopped: {
    start: false,
    pause: false,
    resume: false,
    stop: false,
    reset: true,
  },
  completing: {
    start: false,
    pause: false,
    resume: false,
    stop: false,
    reset: false,
  },
  error: {
    start: false,
    pause: false,
    resume: false,
    stop: false,
    reset: true,
  },
};

/**
 * Get the button rules for a given workflow state.
 *
 * @param state - The current workflow state
 * @returns The button enable/disable rules
 */
export function getButtonRules(state: WorkflowStateType): ButtonRule {
  return BUTTON_RULES[state] || {
    start: false,
    pause: false,
    resume: false,
    stop: false,
    reset: false,
  };
}
