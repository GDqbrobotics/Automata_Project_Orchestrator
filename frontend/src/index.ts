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

// Alias ID assignment: the operator picks an object and gives it the ID
// the cloud database will use (the machine keeps working with the
// auto-assigned object_id in the meantime).
const submitAlias = (): void => {
  const select = document.getElementById('aliasObjectSelect') as HTMLSelectElement | null;
  const input = document.getElementById('aliasInput') as HTMLInputElement | null;
  const objectId = select?.value || '';
  const alias = (input?.value || '').trim();
  if (!objectId || !alias) return;
  app.assignAlias(objectId, alias);
  if (input) input.value = '';
};
document.getElementById('aliasBtn')?.addEventListener('click', submitAlias);
document.getElementById('aliasInput')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') submitAlias();
});

console.log('AUTOMATA GUI Orchestrator frontend started');
app.start();
