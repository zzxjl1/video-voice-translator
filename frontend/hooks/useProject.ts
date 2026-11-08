/**
 * 打开一个工程：加载、订阅事件流、维护状态、提供命令。
 *
 * 这是 UI 的**唯一**数据入口。组件只读 `project`/`chat`/`init`，只通过 `run()` 改东西：
 *   run('edit_source', {line:3, text:'…'})   用户的按钮
 *   （AI 的动作在服务端走同一个命令，沿着事件流回到这里）
 * 所以没有"先改本地再回滚"：状态只由服务端的增量推进，UI 与 AI 的同步是结构性的。
 *
 * 严格模式安全：effect 被调用两次时，第一次的订阅被 cleanup 干净地关掉。
 */
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import * as api from '../net/client';
import { EventClient } from '../net/events';
import type { ChangeEvent, CommandResult, JobEvent, ProjectView, ServerEvent, UiEvent } from '../net/types';
import { applyChange } from '../store/project';
import { emptyChat, loadChat, reduceChat, type ChatState } from '../store/chat';
import { emptyInit, failInit, reduceInit, type InitState } from '../store/init';

export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'closed';

/** 刚被改过的行（界面做一下高亮用）：id → 谁改的。几秒后自己清掉。 */
export type Flash = Record<string, 'user' | 'ai' | 'system'>;

const FLASH_MS = 2600;

export interface ProjectApi {
  project: ProjectView | null;
  loadError: string | null;
  chat: ChatState;
  init: InitState;
  connection: ConnectionState;
  flash: Flash;
  /** 最近一次 AI 让界面做的事（focus_line / open_panel …）。组件消费后调用 clearUi。 */
  ui: UiEvent | null;
  clearUi: () => void;
  /** 执行一个命令。失败时 error 是写给人看的原因。 */
  run: <T = Record<string, unknown>>(name: string, args?: Record<string, unknown>) => Promise<CommandResult<T>>;
  say: (text: string) => Promise<void>;
  stop: () => Promise<void>;
  confirm: (cardId: string, approve: boolean) => Promise<void>;
  reload: () => Promise<void>;
}

export function useProject(id: string | null): ProjectApi {
  const [project, setProject] = useState<ProjectView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [chat, dispatchChat] = useReducer(reduceChatAction, undefined, emptyChat);
  const [init, setInit] = useState<InitState>(emptyInit);
  const [connection, setConnection] = useState<ConnectionState>('closed');
  const [flash, setFlash] = useState<Flash>({});
  const [ui, setUi] = useState<UiEvent | null>(null);

  // 事件回调要读到"最新的 project"，又不想因为它变化而重建订阅 → ref
  const projectRef = useRef<ProjectView | null>(null);
  projectRef.current = project;
  const flashTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const markFlash = useCallback((ids: string[], actor: 'user' | 'ai' | 'system') => {
    if (!ids.length) return;
    setFlash((prev) => {
      const next = { ...prev };
      for (const i of ids) next[i] = actor;
      return next;
    });
    for (const i of ids) {
      const old = flashTimers.current.get(i);
      if (old) clearTimeout(old);
      flashTimers.current.set(i, setTimeout(() => {
        setFlash((prev) => { const { [i]: _drop, ...rest } = prev; return rest; });
        flashTimers.current.delete(i);
      }, FLASH_MS));
    }
  }, []);

  /** 整份重取：首次加载、漏了事件、重连之后。 */
  const reload = useCallback(async () => {
    if (!id) return;
    try {
      const [view, history] = await Promise.all([api.getProject(id), api.getChat(id)]);
      setProject(view);
      dispatchChat({ type: 'load', messages: history.messages, running: history.running });
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, [id]);

  useEffect(() => {
    setProject(null);
    setLoadError(null);
    dispatchChat({ type: 'reset' });
    setInit(emptyInit());
    setFlash({});
    setUi(null);
    if (!id) {
      setConnection('closed');
      return;
    }

    let cancelled = false;
    let client: EventClient | null = null;

    const handle = (event: ServerEvent) => {
      switch (event.type) {
        case 'change': {
          const current = projectRef.current;
          if (!current) return;
          const result = applyChange(current, event as ChangeEvent);
          if (result.kind === 'applied') {
            projectRef.current = result.state;
            setProject(result.state);
            markFlash(result.changed, (event as ChangeEvent).actor);
          } else if (result.kind === 'resync') {
            void reload();
          }
          return;
        }
        case 'job': {
          const e = event as JobEvent;
          // 任务列表是易变状态，不放进 change 事件：这里只维护"有哪些在跑"
          setProject((p) => {
            if (!p) return p;
            if (e.phase === 'started') {
              const info = { id: e.job, kind: e.kind, label: e.label, actor: e.actor, step: null, done: 0, total: 0, cancelling: false, elapsedS: 0 };
              return p.jobs.some((j) => j.id === e.job) ? p : { ...p, jobs: [...p.jobs, info] };
            }
            return { ...p, jobs: p.jobs.filter((j) => j.id !== e.job) };
          });
          if (e.kind === 'init' && e.phase === 'failed') {
            setInit((s) => failInit(s, e.error ?? '初始化失败'));
          }
          // 初始化结束 → 取一次完整视图（阶段、报告、说话人音色都要对上）
          if (e.kind === 'init' && e.phase !== 'started') void reload();
          return;
        }
        case 'progress':
          setInit((s) => reduceInit(s, event as never));
          return;
        case 'chat':
          dispatchChat({ type: 'event', event });
          return;
        case 'ui':
          setUi(event as UiEvent);
          return;
      }
    };

    void (async () => {
      await reload();
      if (cancelled) return;
      const startSeq = projectRef.current?.seq ?? 0;
      client = new EventClient({
        url: (since) => `${api.API_BASE}/projects/${id}/events?since=${since}`,
        onEvent: handle,
        onState: (s) => !cancelled && setConnection(s),
        onGone: () => !cancelled && setLoadError('这个工程已经不存在了'),
        startSeq,
      });
      void client.run();
    })();

    return () => {
      cancelled = true;
      client?.close();
      for (const t of flashTimers.current.values()) clearTimeout(t);
      flashTimers.current.clear();
    };
  }, [id, markFlash, reload]);

  // 断线重连成功后，事件可能漏了：保险起见按 rev 再核对一次
  const wasReconnecting = useRef(false);
  useEffect(() => {
    if (connection === 'reconnecting') wasReconnecting.current = true;
    if (connection === 'open' && wasReconnecting.current) {
      wasReconnecting.current = false;
      void reload();
    }
  }, [connection, reload]);

  const run = useCallback(
    async <T = Record<string, unknown>>(name: string, args: Record<string, unknown> = {}): Promise<CommandResult<T>> => {
      if (!id) return { ok: false, error: '没有打开的工程' };
      try {
        return await api.command<T>(id, name, args);
      } catch (e) {
        return { ok: false, error: e instanceof Error ? e.message : String(e) };
      }
    },
    [id],
  );

  const say = useCallback(async (text: string) => {
    if (id) await api.sendChat(id, text);
  }, [id]);
  const stop = useCallback(async () => { if (id) await api.stopChat(id); }, [id]);
  const confirm = useCallback(async (cardId: string, approve: boolean) => {
    if (id) await api.confirmChat(id, cardId, approve).catch(() => {});
  }, [id]);

  return { project, loadError, chat, init, connection, flash, ui, clearUi: () => setUi(null), run, say, stop, confirm, reload };
}

type ChatAction =
  | { type: 'reset' }
  | { type: 'load'; messages: ChatState['messages']; running: boolean }
  | { type: 'event'; event: Extract<ServerEvent, { type: 'chat' }> };

function reduceChatAction(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'reset': return emptyChat();
    case 'load': return loadChat(action.messages, action.running);
    case 'event': return reduceChat(state, action.event);
  }
}
