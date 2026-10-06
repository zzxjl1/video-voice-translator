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
  /**
   * Mirror the dev proxy for `vite preview`, so the PRODUCTION build can be
   * exercised locally before it goes anywhere near a server.
   *
   * Without this there is no way to run the built app at all. `dist` is static
   * files with nothing in front of them, so every /api call hits the preview
   * server and 404s — feature detection fails, buttons grey out, and the only
   * remaining way to answer "does the build actually work?" is to deploy it and
   * find out. That is the wrong order, and it is how a missing nginx directive
   * becomes an outage.
   *
   * This is also the closest local stand-in for the nginx setup: the built
   * assets are served from disk with the API proxied, which is exactly what
   * nginx will do.
   */
  preview: {
    port: 3300,
    strictPort: true,
    host: '0.0.0.0',
    allowedHosts: ['video-voice-translator.idealbroker.cn'],
    proxy: {
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
      },
    },
  },
  plugins: [react()],
  /**
   * Do NOT pre-bundle onnxruntime-web. DO NOT remove this.
   *
   * ORT resolves its runtime from `new URL('ort-wasm-simd-threaded.jsep.wasm',
   * import.meta.url)`. Vite's dep optimizer copies the JS of the dependency
   * into `node_modules/.vite/deps/` but does NOT copy the `.wasm` next to it,
   * so that URL then points at a file that does not exist. Vite's SPA fallback
   * answers with index.html, and the failure is anything but obvious in the
   * console:
   *
   *     wasm streaming compile failed: Unexpected response MIME type.
   *       Expected 'application/wasm'
   *     WebAssembly.Module doesn't parse at byte 0:
   *       module doesn't start with '\0asm'
   *
   * `import.meta.url` only lands on the real directory when the module is
   * served from its own location, so it has to stay out of the optimizer.
   * Verified: /node_modules/onnxruntime-web/dist/...wasm -> application/wasm,
   * while the .vite/deps/ copy -> text/html (index.html).
   */
  optimizeDeps: {
    exclude: ['onnxruntime-web'],
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '.'),
    }
  }
});
