import path from 'path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Backend port (see backend/start_server.py and backend/nginx.conf).
const BACKEND_ORIGIN = process.env.VITE_BACKEND_ORIGIN || 'http://localhost:9100';

export default defineConfig({
  server: {
    port: 3100,
    host: '0.0.0.0',
    allowedHosts: ['video-voice-translator.idealbroker.cn'],
    /**
     * Forward /api to the FastAPI backend.
     *
     * Without this, running the dev server directly (http://localhost:3100)
     * sends every /api request to Vite itself, which answers 404. That silently
     * breaks feature detection — e.g. GET /api/models/separator fails, so the
     * browser separation backend is reported as unavailable and its button is
     * greyed out, with no obvious cause.
     *
     * Harmless behind nginx: its `location /api/` takes precedence, so these
     * requests never reach Vite in the deployed setup.
     */
    proxy: {
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
      },
    },
  },
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '.'),
    }
  }
});
