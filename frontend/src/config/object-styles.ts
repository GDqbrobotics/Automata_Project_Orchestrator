/**
 * Object workflow state styles configuration.
 *
 * Defines the display order, color classes, and step mapping for object
 * states. The frontend derives these from backend config when available,
 * but falls back to these defaults.
 */

export interface ObjectStateStyle {
  colorClass: string;
  stepIndex: number;
}

/** Default object state display order (mirrors object.yaml). */
export const DEFAULT_STATE_ORDER: string[] = [
  'identified_in_box',
  'waiting_for_disk',
  'moving_to_disk',
  'object_going_second_position',
  'HSI_3DI_analysis',
  'object_going_first_position',
  'ai_processing',
  'XRF_RAMAN_analysis',
  'moving_to_box',
  'object_analyzed',
];

/**
 * Default color class mapping for object states.
 *
 * Color names map to the CSS classes .object-state.state-<color> defined
 * in index.html.
 */
export const DEFAULT_STATE_COLORS: Record<string, string> = {
  identified_in_box: 'identified',
  waiting_for_disk: 'waiting',
  moving_to_disk: 'moving',
  HSI_3DI_analysis: 'imaging',
  'object_going_second_position': 'rotating',
  XRF_RAMAN_analysis: 'measuring',
  'object_going_first_position': 'rotating',
  ai_processing: 'planning',
  moving_to_box: 'moving',
  object_analyzed: 'analyzed',
};

/**
 * Build a style map from state order and colors.
 *
 * @param stateOrder - Ordered list of state names
 * @param stateColors - Map of state name to color class
 * @returns Map of state name to {colorClass, stepIndex}
 */
export function buildStateStyles(
  stateOrder: string[] = DEFAULT_STATE_ORDER,
  stateColors: Record<string, string> = DEFAULT_STATE_COLORS,
): Record<string, ObjectStateStyle> {
  const styles: Record<string, ObjectStateStyle> = {};
  for (let i = 0; i < stateOrder.length; i++) {
    const state = stateOrder[i];
    styles[state] = {
      colorClass: stateColors[state] || 'gray',
      stepIndex: i,
    };
  }
  return styles;
}

/**
 * Get the total number of steps in the object workflow.
 *
 * @param stateOrder - Optional state order (defaults to DEFAULT_STATE_ORDER)
 * @returns The total number of steps
 */
export function getTotalSteps(stateOrder?: string[]): number {
  return (stateOrder || DEFAULT_STATE_ORDER).length;
}
