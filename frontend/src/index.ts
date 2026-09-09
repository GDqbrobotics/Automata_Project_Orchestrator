import './styles/app.css';
import { App } from './ui/app.js';

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
  btnReset: 'reset_workflow',
});

// Alias assignment (backend endpoint pending — see development journal)
document.getElementById('aliasBtn')?.addEventListener('click', () => {
  const input = document.getElementById('aliasInput') as HTMLInputElement;
  if (input?.value) {
    console.log('Alias assignment:', input.value);
    input.value = '';
  }
});

console.log('AUTOMATA GUI Orchestrator frontend started');
app.start();
