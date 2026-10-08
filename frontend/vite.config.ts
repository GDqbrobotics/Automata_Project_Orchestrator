import { defineConfig } from 'vite';

// Dev-server port. Must track backend/config/global.yaml `frontend.dev_port`
// (launch_gui.py reads that value to detect an already-running dev server);
// keep the two in sync when either changes. The UI itself connects directly
// to the backend WebSocket (VITE_WS_URL, injected by launch_gui.py from
// global.yaml `frontend.port`) — no proxying is involved.
export default defineConfig({
  server: {
    port: 3000,
  },
});
