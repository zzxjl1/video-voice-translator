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
import type { ChatSummary } from '../net/client';
import { EventClient } from '../net/events';
import type { ChatEvent, ChangeEvent, CommandResult, ConversationEvent, JobEvent, ProjectView, ServerEvent, UiEvent } from '../net/types';
import { applyChange } from '../store/project';
import { chatEventMatches, emptyChat, loadChat, reduceChat, type ChatState } from '../store/chat';
import { emptyInit, failInit, reduceInit, type InitState } from '../store/init';

export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'closed';

/** 刚被改过的行（界面做一下高亮用）：id → 谁改的。几秒后自己清掉。 */
export type Flash = Record<string, 'user' | 'ai' | 'system'>;

const FLASH_MS = 2600;

function chatErrorText(e: unknown): string {
  if (e instanceof api.ApiError && e.status === 404) {
    // 后端进程比前端旧（没有 /chats）时最常见的症状：点"新对话"毫无反应。
    return '后端没有会话接口（/chats 404）。请重启后端服务，让新代码生效后重试。';
  }
  return `会话操作失败：${e instanceof Error ? e.message : String(e)}`;
}

export interface ProjectApi {
  project: ProjectView | null;
  loadError: string | null;
  /** 只有 `getProject` 明确 404 才为 true；可选接口失败不能把用户踢回首页。 */
  notFound: boolean;
  /** 会话接口的失败原因（例如后端版本过旧）；聊天面板里直接显示。 */
  chatError: string | null;
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
  reload: () => Promise<ProjectView | null>;
  /** 会话列表（新的在前）与当前会话。 */
  conversations: ChatSummary[];
  currentConv: string;
  /** 开一个新对话；旧的仍在历史里。 */
  startNewChat: () => Promise<void>;
  /** 切到某个历史会话。 */
  openChat: (conv: string) => Promise<void>;
  /** 删掉一个会话（服务端会切到剩下的会话，并给空会话补默认消息）。 */
  deleteChat: (conv: string) => Promise<void>;
}

export function useProject(id: string | null): ProjectApi {
  const [project, setProject] = useState<ProjectView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [chat, dispatchChat] = useReducer(reduceChatAction, undefined, emptyChat);
  const [init, setInit] = useState<InitState>(emptyInit);
  const [connection, setConnection] = useState<ConnectionState>('closed');
  const [flash, setFlash] = useState<Flash>({});
  const [ui, setUi] = useState<UiEvent | null>(null);
  const [conversations, setConversations] = useState<ChatSummary[]>([]);
  const [currentConv, setCurrentConv] = useState('');
  // 事件回调要知道"此刻界面在看哪个会话"，又不能因它变化而重建 SSE 订阅。
  const currentConvRef = useRef('');
  const setConv = useCallback((conv: string) => {
    currentConvRef.current = conv;
    setCurrentConv(conv);
  }, []);

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

  /**
   * 整份重取：首次加载、漏了事件、重连之后。
   *
   * 只有 `getProject` 是必需的。聊天记录/会话列表是**可选**的附加信息：某个接口
   * 暂时失败（旧后端没有 /chats、服务器重启窗口、网络抖动）不能让整个工程视图
   * 加载失败，更不能因为一句 "Not Found" 就把用户踢回首页 —— 真实复现过：
   * 上传后 `/chats` 404 → Promise.all reject → 页面回落地页，而后端 job 还在刷进度。
   */
  const reload = useCallback(async (): Promise<ProjectView | null> => {
    if (!id) return null;
    let view: ProjectView;
    try {
      view = await api.getProject(id);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
      setNotFound(e instanceof api.ApiError && e.status === 404);
      return null;                       // 不清空已加载的视图，避免闪回"加载中"
    }
    setProject(view);
    setLoadError(null);
    setNotFound(false);

    const [history, chats] = await Promise.all([
      api.getChat(id).catch(() => null),
      api.listChats(id).catch((e) => { setChatError(chatErrorText(e)); return null; }),
    ]);
    if (history) dispatchChat({ type: 'load', messages: history.messages, running: history.running });
    if (chats) {
      setChatError(null);
      setConversations(chats.conversations);
      setConv(chats.current || view.chatConv || '');
    } else {
      setConv(view.chatConv || '');
    }
    return view;
  }, [id, setConv]);

  const refreshChats = useCallback(async () => {
    if (!id) return;
    try {
      const r = await api.listChats(id);
      setConversations(r.conversations);
      if (r.current) setConv(r.current);
      setChatError(null);
    } catch (e) {
      setChatError(chatErrorText(e));
    }
  }, [id, setConv]);

  const deleteChat = useCallback(async (conv: string) => {
    if (!id) return;
    try {
      await api.deleteChat(id, conv);
      setChatError(null);
      // 服务端可能把当前会话切到了另一个（或新开一个），消息与列表都要跟着换。
      const [h] = await Promise.all([api.getChat(id), refreshChats()]);
      dispatchChat({ type: 'load', messages: h.messages, running: h.running });
    } catch (e) {
      setChatError(chatErrorText(e));
    }
  }, [id, refreshChats]);

  useEffect(() => {
    setProject(null);
    setLoadError(null);
    dispatchChat({ type: 'reset' });
    setInit(emptyInit());
    setFlash({});
    setUi(null);
    setConversations([]);
    setNotFound(false);
    setChatError(null);
    setConv('');
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
        case 'chat': {
          const e = event as ChatEvent;
          // 切会话后旧一轮的 delta/message/idle 不能污染新会话（服务端已给事件打上
          // 所属 conv）。没有 conv 的老事件按当前会话处理，保持兼容。
          if (!chatEventMatches(e, currentConvRef.current)) return;
          dispatchChat({ type: 'event', event: e });
          return;
        }
        case 'conversation': {
          // 服务端切换会话（含别的标签页）：同步当前会话、消息与列表。
          const conv = (event as ConversationEvent).conv;
          setConv(conv);
          void api.getChat(id, conv).then((h) => {
            if (currentConvRef.current === conv) {
              dispatchChat({ type: 'load', messages: h.messages, running: h.running });
            }
          }).catch(() => {});
          void refreshChats();
          return;
        }
        case 'ui':
          setUi(event as UiEvent);
          return;
      }
    };

    void (async () => {
      const view = await reload();
      if (cancelled) return;
      // 用这次重取拿到的 seq，而不是 render 里才赋值的 ref —— 首帧它是 null，
      // 会让 SSE 以 since=0 连上，把历史事件（含 export_ready 下载）重放一遍。
      const startSeq = view?.seq ?? projectRef.current?.seq ?? 0;
      client = new EventClient({
        url: (since) => `${api.API_BASE}/projects/${id}/events?since=${since}`,
        onEvent: handle,
        onState: (s) => !cancelled && setConnection(s),
        onGone: () => { if (!cancelled) { setLoadError('这个工程已经不存在了'); setNotFound(true); } },
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
  }, [id, markFlash, reload, refreshChats, setConv]);

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

  const startNewChat = useCallback(async () => {
    if (!id) return;
    try {
      const { conv } = await api.newChat(id);
      setConv(conv);
      setChatError(null);
      // 服务端会给新会话写一条"初始化完成…"；从服务端取回来，别用空数组
      // 覆盖掉可能已经随 SSE 到达的那条消息。
      const [h] = await Promise.all([api.getChat(id, conv), refreshChats()]);
      dispatchChat({ type: 'load', messages: h.messages, running: h.running });
    } catch (e) {
      // 不能在本地假装开了一个新会话：消息随后仍会落到服务端的旧会话里。
      setChatError(chatErrorText(e));
    }
  }, [id, setConv, refreshChats]);

  const openChat = useCallback(async (conv: string) => {
    if (!id || conv === currentConv) return;
    try {
      const r = await api.selectChat(id, conv);
      dispatchChat({ type: 'load', messages: r.messages, running: false });
      setConv(r.conv);
      setChatError(null);
    } catch (e) {
      setChatError(chatErrorText(e));
    }
  }, [id, currentConv, setConv]);

  // 一轮对话结束时刷新：新会话的第一个问题、以及条数/标题都只有这时候才会变，
  // 不刷新的话历史菜单里会一直是打开页面时的旧列表（刚发的消息看不到）。
  useEffect(() => {
    if (!chat.busy) void refreshChats();
  }, [chat.busy, refreshChats]);

  const clearUi = useCallback(() => setUi(null), []);

  const say = useCallback(async (text: string) => {
    if (id) await api.sendChat(id, text);
  }, [id]);
  const stop = useCallback(async () => { if (id) await api.stopChat(id); }, [id]);
  const confirm = useCallback(async (cardId: string, approve: boolean) => {
    if (id) await api.confirmChat(id, cardId, approve).catch(() => {});
  }, [id]);

  return {
    project, loadError, notFound, chatError, chat, init, connection, flash, ui, clearUi, run, say, stop, confirm, reload,
    conversations, currentConv, startNewChat, openChat, deleteChat,
  };
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
