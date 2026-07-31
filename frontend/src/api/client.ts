import type { FrontendMessage, WorkflowCommand, WorkflowStatus } from '../contracts.js';

export class FrontendClient {
  private ws: WebSocket | null = null;
  private url: string;
  private onMessage: ((msg: FrontendMessage) => void) | null = null;

  constructor(url: string = 'ws://localhost:8080') {
    this.url = url;
  }

  connect(onMessage: (msg: FrontendMessage) => void): void {
    this.onMessage = onMessage;
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      console.log('Frontend client connected');
      // Notify the app that the connection is live
      if (this.onMessage) {
        this.onMessage({ type: 'connected' });
      }
    };

    this.ws.onmessage = (event) => {
      try {
        const msg: FrontendMessage = JSON.parse(event.data);
        if (this.onMessage) {
          this.onMessage(msg);
        }
      } catch (err) {
        console.error('Failed to parse message:', event.data, err);
      }
    };

    this.ws.onclose = () => {
      console.log('Frontend client disconnected');
      // Notify the app that the connection is gone
      if (this.onMessage) {
        this.onMessage({ type: 'disconnected' });
      }
    };

    this.ws.onerror = (err) => {
      console.error('WebSocket error:', err);
      // Notify the app of the error
      if (this.onMessage) {
        this.onMessage({ type: 'error', error: 'WebSocket connection error' });
      }
    };
  }

  send(command: WorkflowCommand, data?: Record<string, unknown>): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('WebSocket not connected, cannot send', command);
      return;
    }
    const msg = { command, ...data };
    this.ws.send(JSON.stringify(msg));
  }

  close(): void {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  get connected(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }
}
