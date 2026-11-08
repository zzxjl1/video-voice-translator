/**
 * 事件流客户端：SSE，带断线重连、按 seq 去重、缺口检测。
 *
 * 不用 `EventSource`：它不能中止、也拿不到 HTTP 状态（工程被删时的 404 会被它无限重连）。
 * 用 fetch + ReadableStream 手读，几十行，行为完全可控。
 *
 * 可靠性约定（与后端 `app/bus.py` 配合）：
 *   · 每个事件有工程内单调递增的 `seq`；客户端记着最后收到的，重连时带 `?since=`；
 *   · 服务端回放内存里最近的事件。重启过的服务端回放不到旧事件 —— 这没关系：客户端以
 *     `rev` 为准，发现自己落后（收到的 change 的 rev 不是 当前+1）就整份重取一次；
 *   · 重复事件（重连边界上会发生）按 seq 丢弃，所以应用增量是幂等的。
 *
 * 纯 TS、不碰 DOM/React：`fetch` 通过参数注入，测试里直接喂一个假的流。
 */
import type { ServerEvent } from './types';

export interface EventClientOptions {
  url: (since: number) => string;
  onEvent: (event: ServerEvent) => void;
  /** 连接状态变化（给界面显示"重连中…"用）。 */
  onState?: (state: 'connecting' | 'open' | 'reconnecting' | 'closed') => void;
  /** 服务端回 404（工程不存在）：不再重连。 */
  onGone?: () => void;
  fetchImpl?: typeof fetch;
  /** 重连退避：首次、上限（毫秒）。 */
  backoff?: { initial: number; max: number };
  startSeq?: number;
}

export class EventClient {
  private seq: number;
  private abort: AbortController | null = null;
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  private closed = false;
  private attempts = 0;
  private readonly opts: Required<Pick<EventClientOptions, 'fetchImpl' | 'backoff'>> & EventClientOptions;

  constructor(opts: EventClientOptions) {
    this.opts = {
      fetchImpl: opts.fetchImpl ?? ((...a) => fetch(...a)),
      backoff: opts.backoff ?? { initial: 500, max: 8000 },
      ...opts,
    };
    this.seq = opts.startSeq ?? 0;
  }

  get lastSeq(): number {
    return this.seq;
  }

  /** 开始。返回一个在连接**彻底结束**（close() 或 404）时 resolve 的 Promise。 */
  async run(): Promise<void> {
    while (!this.closed) {
      this.opts.onState?.(this.attempts === 0 ? 'connecting' : 'reconnecting');
      try {
        const gone = await this.connectOnce();
        if (gone) {
          this.opts.onGone?.();
          break;
        }
        this.attempts = 0;                       // 正常读到过数据：重置退避
      } catch (error) {
        if (this.closed) break;
        this.attempts += 1;
      }
      if (this.closed) break;
      const { initial, max } = this.opts.backoff;
      await this.sleep(Math.min(max, initial * 2 ** Math.min(this.attempts, 5)));
    }
    this.opts.onState?.('closed');
  }

  close(): void {
    this.closed = true;
    this.abort?.abort();
    // 不只依赖 fetch 对 abort 的响应：直接取消 reader，挂在 read() 上的循环立刻醒来。
    this.reader?.cancel().catch(() => {});
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  /** 一次连接。返回 true = 工程不存在（别再重连）。流自然结束或出错都会回到 run 的循环。 */
  private async connectOnce(): Promise<boolean> {
    this.abort = new AbortController();
    const response = await this.opts.fetchImpl(this.opts.url(this.seq), {
      signal: this.abort.signal,
      headers: { Accept: 'text/event-stream' },
    });
    if (response.status === 404) return true;
    if (!response.ok || !response.body) throw new Error(`events: HTTP ${response.status}`);

    this.opts.onState?.('open');
    const reader = (this.reader = response.body.getReader());
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return false;
      this.attempts = 0;
      buffer += decoder.decode(value, { stream: true });
      // SSE 帧以空行分隔；一帧里可能有 id: / data: / 注释（心跳 ": ping"）
      let boundary: number;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        this.handleFrame(frame);
      }
    }
  }

  private handleFrame(frame: string): void {
    const data = frame
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).replace(/^ /, ''))
      .join('\n');
    if (!data) return;                           // 心跳 / retry: 行
    let event: ServerEvent;
    try {
      event = JSON.parse(data);
    } catch {
      return;                                    // 半截 JSON：丢掉，下一帧继续
    }
    // 去重：重连边界上同一个事件可能来两次。seq 缺省的事件（不应出现）照放行。
    if (typeof event.seq === 'number') {
      if (event.seq <= this.seq) return;
      this.seq = event.seq;
    }
    this.opts.onEvent(event);
  }
}
