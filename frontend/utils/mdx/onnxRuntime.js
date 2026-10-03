/**
 * Lazy access point for onnxruntime-web.
 *
 * Keeping the dependency behind this module means:
 *  - the DSP / separation core (`mdxDsp.js`, `mdxSeparator.js`) stays pure JS
 *    and can be executed directly by Node in `scripts/test-*.mjs`,
 *  - the runtime is only fetched when separation is actually used, so the
 *    initial app bundle stays small (onnxruntime-web is ~690 kB of JS), and
 *  - swapping or stubbing the runtime later touches exactly one file.
 *
 * Vite picks up the runtime's `.wasm` assets automatically through the ESM
 * entry's `new URL(..., import.meta.url)` references — the production build
 * emits `assets/ort-wasm-*.wasm`, so `env.wasm.wasmPaths` does not need to be
 * configured. If you ever host the app from a path where that resolution
 * breaks, set `ort.env.wasm.wasmPaths = '/ort/'` and copy
 * `node_modules/onnxruntime-web/dist/*.wasm` into `public/ort/`.
 */

/** @type {Promise<typeof import('onnxruntime-web')> | null} */
let ortPromise = null;

/**
 * Load (once) and return the onnxruntime-web module namespace.
 * @returns {Promise<typeof import('onnxruntime-web')>}
 */
export function loadOrt() {
  if (!ortPromise) {
    ortPromise = import('onnxruntime-web');
  }
  return ortPromise;
}
