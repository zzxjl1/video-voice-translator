/**
 * Main-thread client for the Kokoro worker. One worker per tab; requests are
 * serialized by the worker itself (its engine is single-instance).
 */
import type { EngineDevice } from './types';

interface Pending {
  resolve: (wav: ArrayBuffer, ms: number, device?: string) => void;
  reject: (e: Error) => void;
  onProgress?: (loaded: number, total: number) => void;
  onDevice?: (d: EngineDevice) => void;
}

type WorkerOut =
  | { id: number; type: 'progress'; phase: string; loaded: number; total: number }
  | { id: number; type: 'device'; device: string; dtype?: string; message?: string }
  | { id: number; type: 'ok'; wav: ArrayBuffer; ms: number }
  | { id: number; type: 'error'; message: string };

class KokoroClient {
  private worker: Worker | null = null;
  private seq = 0;
  private pending = new Map<number, Pending>();
  /** Last reported engine, kept so callers can log it once. */
  device: EngineDevice | null = null;

  private ensure(): Worker {
    if (this.worker) return this.worker;
    const w = new Worker(new URL('./kokoroWorker.ts', import.meta.url), { type: 'module' });
    w.onmessage = (ev: MessageEvent<WorkerOut>) => {
      const p = this.pending.get(ev.data.id);
      if (!p) return;
      const msg = ev.data;
      if (msg.type === 'progress') {
        p.onProgress?.(msg.loaded, msg.total);
      } else if (msg.type === 'device') {
        if (msg.device === 'wasm-fallback') {
          this.device = 'wasm';
        } else {
          this.device = (msg.device === 'webgpu' ? 'webgpu' : 'wasm');
        }
        p.onDevice?.(this.device);
      } else if (msg.type === 'ok') {
        this.pending.delete(msg.id);
        p.resolve(msg.wav, msg.ms, this.device ?? undefined);
      } else {
        this.pending.delete(msg.id);
        p.reject(new Error(msg.message));
      }
    };
    w.onerror = (e) => {
      // Worker-level failure (script error): fail everything pending.
      for (const [, p] of this.pending) p.reject(new Error(e.message || 'Kokoro worker crashed'));
      this.pending.clear();
      this.worker = null;
    };
    this.worker = w;
    return w;
  }

  /** Load the engine (and report device/progress) without synthesizing. */
  warmup(opts: { onProgress?: (loaded: number, total: number) => void; onDevice?: (d: EngineDevice) => void } = {}): Promise<void> {
    const w = this.ensure();
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, {
        resolve: () => resolve(),
        reject,
        onProgress: opts.onProgress,
        onDevice: (d) => { this.device = d; opts.onDevice?.(d); },
      });
      w.postMessage({ id, kind: 'warmup' });
    });
  }

  synthesize(text: string, voice: string, opts: { onProgress?: (loaded: number, total: number) => void } = {}): Promise<Blob> {
    const w = this.ensure();
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, {
        resolve: (wav) => resolve(new Blob([wav], { type: 'audio/wav' })),
        reject,
        onProgress: opts.onProgress,
      });
      w.postMessage({ id, kind: 'synth', text, voice });
    });
  }
}

export const kokoro = new KokoroClient();
