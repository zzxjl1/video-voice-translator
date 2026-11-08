/**
 * 聊天状态：纯 reducer。
 *
 * 一轮 AI 回复在界面上的样子：
 *   start        开出一个"正在输入"的气泡（还没有文字）
 *   delta…       文字增量，追加到这个气泡
 *   tool_call    气泡里多一条"正在做：xxx"的步骤
 *   tool_result  这一步的结果（成功 / 失败的原因）
 *   message      最终落盘的消息（assistant）—— 用它替换流式气泡，内容以它为准
 *   idle         这一轮结束
 *
 * 流式气泡是**临时**的：权威内容是落盘后的 `message`。所以即使中途丢了 delta，最后那条
 * message 也会把气泡纠正过来。
 */
import type { ChatEvent, ChatMessage } from '../net/types';

export interface ToolStep {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: 'running' | 'ok' | 'failed';
  error?: string | null;
}

export interface Streaming {
  id: string;
  text: string;
  steps: ToolStep[];
}

export interface ChatState {
  messages: ChatMessage[];
  /** 正在流式生成的那一轮；没有就是 null。 */
  streaming: Streaming | null;
  /** 有一轮对话在进行（含等用户确认）：输入框据此显示"停止"。 */
  busy: boolean;
}

export const emptyChat = (): ChatState => ({ messages: [], streaming: null, busy: false });

export function loadChat(messages: ChatMessage[], running: boolean): ChatState {
  return { messages, streaming: null, busy: running };
}

export function reduceChat(state: ChatState, event: ChatEvent): ChatState {
  switch (event.kind) {
    case 'start':
      return { ...state, busy: true, streaming: { id: event.id, text: '', steps: [] } };

    case 'delta': {
      const s = state.streaming;
      if (!s || s.id !== event.id) return state;           // 属于已经结束的那一轮的迟到事件
      return { ...state, streaming: { ...s, text: s.text + event.text } };
    }

    case 'tool_call': {
      const s = state.streaming;
      if (!s) return state;
      if (s.steps.some((x) => x.id === event.id)) return state;   // 重复
      return {
        ...state,
        streaming: { ...s, steps: [...s.steps, { id: event.id, name: event.name, args: event.args, status: 'running' }] },
      };
    }

    case 'tool_result': {
      const s = state.streaming;
      if (!s) return state;
      return {
        ...state,
        streaming: {
          ...s,
          steps: s.steps.map((x) =>
            x.id === event.id ? { ...x, status: event.ok ? 'ok' : 'failed', error: event.error } : x,
          ),
        },
      };
    }

    case 'message': {
      const m = event.message;
      if (state.messages.some((x) => x.id === m.id)) {
        // 同 id 的消息：用落盘版本替换（流式气泡用的就是这个 id）
        return {
          ...state,
          messages: state.messages.map((x) => (x.id === m.id ? m : x)),
          streaming: state.streaming?.id === m.id ? null : state.streaming,
        };
      }
      return {
        ...state,
        messages: [...state.messages, m],
        // 这条就是流式气泡对应的最终消息：气泡完成使命
        streaming: m.role === 'assistant' && state.streaming?.id === m.id ? null : state.streaming,
      };
    }

    case 'confirm_resolved':
      return {
        ...state,
        messages: state.messages.map((x) =>
          x.id === event.id ? { ...x, status: event.approved ? 'approved' : 'rejected' } : x,
        ),
      };

    case 'idle':
      return { ...state, busy: false, streaming: null };
  }
}

/** 还在等用户点的确认卡片。 */
export function pendingConfirms(state: ChatState): ChatMessage[] {
  return state.messages.filter((m) => m.role === 'confirm' && m.status !== 'approved' && m.status !== 'rejected');
}

/** 命令名 → 给人看的动作名（工具步骤卡片用）。 */
export const COMMAND_LABEL: Record<string, string> = {
  get_overview: '查看项目概况', list_lines: '查看句子', get_lines: '查看句子详情', search_lines: '搜索句子',
  list_voices: '查看音色', listen: '听原声', get_recent_changes: '查看最近改动', get_cost: '查看花费',
  get_settings: '查看设置', get_subtitle_style: '查看字幕样式', list_jobs: '查看任务',
  edit_source: '改原文', split_line: '拆句', merge_lines: '合并句子', retime_line: '调时间',
  mark_non_speech: '标记非语音', set_speaker: '改说话人', rename_speaker: '给说话人改名',
  insert_line: '插入句子', delete_lines: '删除句子', rerun_asr: '重新识别',
  set_translation: '改译文', retranslate: '重新翻译', set_custom_prompt: '设置翻译要求',
  set_dub_style: '调配音', redub: '重新配音', set_muted: '静音', set_hidden: '隐藏字幕',
  set_speaker_voice: '换音色', clone_voice: '克隆音色', preview_voice: '试听音色',
  set_settings: '改设置', run_initialize: '运行初始化', cancel_job: '取消任务', undo: '撤销',
  export_video: '导出成片', focus_line: '定位到句子', open_panel: '打开面板',
  set_subtitle_style: '改字幕样式', reset_subtitle_style: '重置字幕样式', set_export_plan: '改导出方案',
  request_browser_action: '请求浏览器操作',
};

export function stepLabel(step: Pick<ToolStep, 'name' | 'args'>): string {
  const base = COMMAND_LABEL[step.name] ?? step.name;
  const lines = (step.args.lines as number[] | undefined) ?? (step.args.line != null ? [step.args.line as number] : []);
  return lines.length ? `${base}（第 ${lines.slice(0, 5).join('、')} 句${lines.length > 5 ? '…' : ''}）` : base;
}
