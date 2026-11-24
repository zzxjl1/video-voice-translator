import path from 'path';
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * There is ONE onnxruntime-web in the app. package.json `overrides` forces
 * @huggingface/transformers (Kokoro) onto the same copy the vocal-separation model uses; left to
 * itself transformers.js pins an old 1.22 dev build, npm nests it, and the bundle then ships TWO
 * wasm runtimes (21.6 MB + 28.3 MB). The Kokoro worker imports the wasm through the `kokoro-ort:`
 * resolver below so it is bundled and handed to ORT explicitly, never fetched from a CDN.
 */
const KOKORO_ORT = path.resolve(__dirname, 'node_modules/onnxruntime-web/dist');

/**
 * `kokoro-ort:<file>?url` -> the ORT file inside KOKORO_ORT (see above). A plugin rather than a
 * resolve.alias: an alias with a package-like name makes Vite's dev optimizer try to pre-bundle the
 * file as a dependency (and choke on its top-level await). It is registered twice — in `plugins`
 * and in `worker.plugins` — because a Worker is bundled by its own plugin pipeline, which does NOT
 * inherit the main one; without the second registration the production build cannot resolve it.
 */
const kokoroOrt = (): Plugin => ({
  name: 'kokoro-ort',
  enforce: 'pre',
  resolveId(id) {
    return id.startsWith('kokoro-ort:') ? path.join(KOKORO_ORT, id.slice('kokoro-ort:'.length)) : null;
  },
});

// Backend port (see backend/start_server.py and backend/nginx.conf).
const BACKEND_ORIGIN = process.env.VITE_BACKEND_ORIGIN || 'http://localhost:9100';

/**
 * Bind to loopback by default, same convention as backend/start_server.py.
 *
 * `0.0.0.0` put the dev server on every interface, so anyone on the same LAN (or
 * on the host's public IP, if the firewall allows it) could reach :3100. The dev
 * server has no auth, serves unbundled sources plus source maps, and will happily
 * proxy /api — including multi-GB uploads — to the backend. Local development
 * never needs that reachability: http://localhost:3100 works the same either way.
 *
 * Set VITE_HOST=0.0.0.0 when a device on the LAN really has to connect (phone
 * testing), which is an explicit, per-run decision rather than the default.
 */
const DEV_HOST = process.env.VITE_HOST || '127.0.0.1';

export default defineConfig({
  server: {
    port: 3100,
    host: DEV_HOST,
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
    // Same reasoning as `server.host` above: preview proxies /api too.
    host: DEV_HOST,
    allowedHosts: ['video-voice-translator.idealbroker.cn'],
    proxy: {
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
      },
    },
  },
  plugins: [react(), kokoroOrt()],
  /**
   * ES worker bundles. The default IIFE format cannot code-split, and the
   * Kokoro worker (features/kokoro/kokoroWorker.ts) pulls in transformers.js
   * and espeak-ng, both of which dynamic-import their own chunks. Instantiated
   * with { type: 'module' } in features/kokoro/client.ts.
   */
  worker: {
    format: 'es',
    plugins: () => [kokoroOrt()],
  },
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
