/** Vite `?url` imports of the ONNX Runtime files that ship with transformers.js (resolved by the `kokoro-ort` plugin in vite.config.ts). */
declare module 'kokoro-ort:*' {
  const url: string;
  export default url;
}
