/**
 * Operator button enablement, derived from the workflow state machine
 * definition broadcast by the backend (workflow.yaml is the single source
 * of truth – docs/CONFIG_CHANGE_GUIDE.md, F1).
 *
 * A button is enabled in state S iff S has an outgoing transition whose
 * event is one of `metadata.operator_events` values; the button is the
 * inverse lookup (operator event -> button). Renaming a state or an
 * operator event in workflow.yaml is a config-only change: the GUI follows
 * the broadcast definition instead of a hardcoded per-state table.
 */

import type { StateMachineDef } from '../contracts.js';

export interface ButtonRule {
  start: boolean;
  pause: boolean;
  resume: boolean;
  stop: boolean;
  reset: boolean;
}

/** The operator buttons, in the order of workflow.yaml metadata.operator_events. */
const BUTTON_KEYS = ['start', 'pause', 'resume', 'stop', 'reset'] as const;
export type OperatorButton = (typeof BUTTON_KEYS)[number];

/**
 * Derive the operator button matrix for one workflow state.
 *
 * @param def - The workflow state machine definition (broadcast with every
 *              status update), or null before the first broadcast.
 * @param currentState - The current raw workflow state name.
 * @returns Which buttons are enabled right now.
 */
export function deriveButtonRules(
  def: StateMachineDef | null | undefined,
  currentState: string,
): ButtonRule {
  const rules: ButtonRule = {
    start: false,
    pause: false,
    resume: false,
    stop: false,
    reset: false,
  };
  if (!def || !currentState) return rules;

  const operatorEvents = (def.metadata?.operator_events || {}) as Record<string, string>;
  // operator event name -> button key
  const eventToButton = new Map<string, OperatorButton>();
  for (const key of BUTTON_KEYS) {
    const event = operatorEvents[key];
    if (event) eventToButton.set(event, key);
  }
  if (eventToButton.size === 0) return rules;

  const state = def.states[currentState];
  if (!state) return rules;
  for (const t of state.transitions) {
    const button = eventToButton.get(t.event);
    if (button) rules[button] = true;
  }
  return rules;
}
