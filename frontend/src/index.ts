import { App } from './ui/app.js';
import type { WorkflowStateType } from './contracts.js';

// launch_gui.py injects VITE_WS_URL so the UI follows --ws-port;
// the default keeps a manual `npm run dev` working out of the box.
const app = new App({
  wsUrl: (import.meta.env.VITE_WS_URL as string | undefined) || 'ws://localhost:8080',
});

// Bind UI buttons to operator commands
app.onBindCommands({
  btnStart: 'start_workflow',
  btnPause: 'pause_workflow',
  btnResume: 'resume_workflow',
  btnStop: 'stop_workflow',
  btnStatus: 'request_status',
});

// Reset button – resets workflow to idle (for error/stopped recovery)
document.getElementById('btnReset')?.addEventListener('click', () => {
  app.sendCommand('reset_workflow');
});

// Alias assignment
document.getElementById('aliasBtn')?.addEventListener('click', () => {
  const input = document.getElementById('aliasInput') as HTMLInputElement;
  if (input?.value) {
    console.log('Alias assignment:', input.value);
    // In a real implementation, this would be a dedicated alias command
    // For now, just log it
    input.value = '';
  }
});

// Handle state changes for UI feedback
app.start();

// Log connection status changes
const originalStart = app.start;
app.start = function() {
  originalStart.call(this);
  console.log('AUTOMATA GUI Orchestrator frontend started');
  console.log('Connecting to backend at ws://localhost:8080');
};
