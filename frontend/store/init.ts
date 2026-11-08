/**
 * 初始化进度：把任务里的细粒度 `progress` 事件变成"步骤清单 + 终端日志"。
 *
 * 取代旧版 350 行、既写日志文本又改状态的 `applyPipelineEvent`：这里**只**负责展示进度，
 * 不改项目状态（项目状态走 change 事件）。所以它可以放心地被丢弃、重放，不会和状态打架。
 *
 * 步骤 = 后端 `jobs.initialize` 的几个阶段；每个阶段有一个状态和一句话进度。
 */
import type { ProgressEvent } from '../net/types';

export type StepId = 'separation' | 'asr' | 'mm_enhance' | 'translation' | 'voice_clone' | 'tts';
export type StepStatus = 'pending' | 'running' | 'done' | 'skipped' | 'failed';

export interface InitStep {
  id: StepId;
  label: string;
  status: StepStatus;
  detail: string;
  progress?: { done: number; total: number };
}

export interface InitState {
  steps: InitStep[];
  /** 终端风格的日志行（只增不减，进度行会被原地改写）。 */
  log: LogLine[];
  finished: boolean;
  failed: string | null;
}

export interface LogLine {
  key: string;                  // 同 key 的行原地改写（进度条）
  text: string;
  level: 'info' | 'ok' | 'warn' | 'error';
}

const STEPS: Pick<InitStep, 'id' | 'label'>[] = [
  { id: 'separation', label: '人声分离' },
  { id: 'asr', label: '语音识别' },
  { id: 'mm_enhance', label: '审听纠错' },
  { id: 'translation', label: '翻译' },
  { id: 'voice_clone', label: '声音克隆' },
  { id: 'tts', label: '配音合成' },
];

export const emptyInit = (): InitState => ({
  steps: STEPS.map((s) => ({ ...s, status: 'pending', detail: '' })),
  log: [],
  finished: false,
  failed: null,
});

function setStep(state: InitState, id: StepId, patch: Partial<InitStep>): InitState {
  return { ...state, steps: state.steps.map((s) => (s.id === id ? { ...s, ...patch } : s)) };
}

function pushLog(state: InitState, line: LogLine): InitState {
  const index = state.log.findIndex((l) => l.key === line.key);
  if (index >= 0) {
    const log = state.log.slice();
    log[index] = line;
    return { ...state, log };
  }
  return { ...state, log: [...state.log, line] };
}

let counter = 0;
const uniq = (prefix: string) => `${prefix}#${(counter += 1)}`;

/** 一条进度事件 → 新状态。不认识的事件原样返回（后端加了阶段，前端不会炸）。 */
export function reduceInit(state: InitState, event: ProgressEvent): InitState {
  const phase = event.phase as StepId | 'init' | undefined;
  const status = event.status as string | undefined;
  if (!phase) return state;

  if (phase === 'init') {
    return status === 'done' ? { ...state, finished: true } : state;
  }

  switch (phase) {
    case 'separation': {
      if (status === 'skipped') return setStep(state, 'separation', { status: 'skipped', detail: '未使用' });
      if (status === 'done') return setStep(state, 'separation', { status: 'done', detail: '已完成' });
      return state;
    }

    case 'asr': {
      if (status === 'started') {
        return pushLog(setStep(state, 'asr', { status: 'running', detail: '正在识别说话和说话人…' }),
          { key: 'asr', text: '识别语音…', level: 'info' });
      }
      if (status === 'skipped') {
        return setStep(state, 'asr', { status: 'skipped', detail: `沿用已有转写（${event.lines} 句）` });
      }
      if (status === 'done') {
        return pushLog(setStep(state, 'asr', { status: 'done', detail: `${event.lines} 句` }),
          { key: 'asr', text: `识别完成：${event.lines} 句`, level: 'ok' });
      }
      return state;
    }

    case 'mm_enhance': {
      if (status === 'started') {
        return setStep(state, 'mm_enhance', { status: 'running', detail: `${event.count} 句` });
      }
      if (status === 'listening') {
        return pushLog(setStep(state, 'mm_enhance', { status: 'running', detail: 'AI 正在听原声、核对转写…' }),
          { key: 'mm', text: 'AI 听原声，核对转写、标注语气…', level: 'info' });
      }
      if (status === 'done') {
        const n = Number(event.adjusted ?? 0);
        return pushLog(setStep(state, 'mm_enhance', { status: 'done', detail: n ? `纠正/标注了 ${n} 句` : '没有需要改的' }),
          { key: 'mm', text: n ? `审听完成：纠正/标注了 ${n} 句` : '审听完成：转写无需修改', level: 'ok' });
      }
      if (status === 'failed') {
        return pushLog(setStep(state, 'mm_enhance', { status: 'failed', detail: '失败，沿用原始转写' }),
          { key: 'mm', text: `审听失败（${event.error}），沿用原始转写`, level: 'warn' });
      }
      if (status === 'skipped') return setStep(state, 'mm_enhance', { status: 'skipped', detail: '已有审听结果' });
      return state;
    }

    case 'translation': {
      if (status === 'started') {
        return pushLog(setStep(state, 'translation', { status: 'running', detail: `${event.count} 句` }),
          { key: 'tr', text: `翻译 ${event.count} 句…`, level: 'info' });
      }
      if (status === 'done') {
        const failed = Number(event.failed ?? 0);
        return pushLog(
          setStep(state, 'translation', { status: failed ? 'failed' : 'done', detail: failed ? `${failed} 句失败` : `${event.count} 句` }),
          { key: 'tr', text: failed ? `翻译完成，但 ${failed} 句失败` : `翻译完成：${event.count} 句`, level: failed ? 'warn' : 'ok' });
      }
      if (status === 'repairing' || status === 'repair_done') {
        return pushLog(state, { key: uniq('tr'), text: status === 'repairing' ? '补翻失败的句子…' : '补翻完成', level: 'info' });
      }
      return state;
    }

    case 'voice_clone': {
      if (status === 'started') {
        return setStep(state, 'voice_clone', { status: 'running', detail: `${event.total} 位说话人` });
      }
      if (status === 'complete') {
        return setStep(state, 'voice_clone', { status: 'done', detail: '完成' });
      }
      const who = `说话人 ${event.speaker_id}`;
      const text =
        status === 'picking' ? `${who}：AI 挑选参考片段…` :
        status === 'picked' ? `${who}：已挑选 ${(event.picked as string[] | undefined)?.length ?? 0} 句作参考` :
        status === 'pick_failed' ? `${who}：智能选材失败，改用规则（${event.error}）` :
        status === 'sampling' ? `${who}：拼接参考音频（${event.seconds}s）` :
        status === 'enrolling' ? `${who}：注册音色…` :
        status === 'cloning' ? `${who}：开始克隆` :
        status === 'done' ? `${who}：${event.reused ? '复用已有音色' : '克隆完成'}` :
        status === 'failed' ? `${who}：克隆失败（${event.error}），改用系统音色` : '';
      if (!text) return state;
      const progress = event.total ? { done: Number(event.progress ?? 0), total: Number(event.total) } : undefined;
      return pushLog(setStep(state, 'voice_clone', { status: 'running', ...(progress ? { progress } : {}) }),
        { key: uniq('vc'), text, level: status === 'failed' || status === 'pick_failed' ? 'warn' : status === 'done' ? 'ok' : 'info' });
    }

    case 'tts': {
      if (status === 'started') {
        return pushLog(setStep(state, 'tts', { status: 'running', progress: { done: 0, total: Number(event.total) }, detail: '' }),
          { key: 'tts', text: `配音 0/${event.total}`, level: 'info' });
      }
      if (status === 'done') {
        const failed = Number(event.failed ?? 0);
        const step = state.steps.find((s) => s.id === 'tts')!;
        return pushLog(
          setStep(state, 'tts', { status: failed ? 'failed' : 'done', detail: failed ? `${failed} 句失败` : '完成',
            progress: step.progress && { done: step.progress.total - failed, total: step.progress.total } }),
          { key: 'tts', text: failed ? `配音完成，${failed} 句失败：${event.lastError ?? ''}` : '配音完成', level: failed ? 'warn' : 'ok' });
      }
      if (event.progress != null) {
        const done = Number(event.progress), total = Number(event.total);
        const line: LogLine = event.tts_error
          ? { key: uniq('tts-err'), text: `配音失败：${event.tts_error}`, level: 'error' }
          : { key: 'tts', text: `配音 ${done}/${total}`, level: 'info' };
        const next = setStep(state, 'tts', { status: 'running', progress: { done, total } });
        return event.tts_error ? pushLog(pushLog(next, { key: 'tts', text: `配音 ${done}/${total}`, level: 'info' }), line) : pushLog(next, line);
      }
      return state;
    }
  }
  return state;
}

export function failInit(state: InitState, message: string): InitState {
  return { ...state, failed: message, finished: true };
}

/** 总进度 0..1（给进度条）。 */
export function overallProgress(state: InitState): number {
  const active = state.steps.filter((s) => s.status !== 'skipped');
  if (!active.length) return 1;
  const score = active.reduce((sum, s) => {
    if (s.status === 'done' || s.status === 'failed') return sum + 1;
    if (s.status === 'running') {
      return sum + (s.progress && s.progress.total ? s.progress.done / s.progress.total : 0.4);
    }
    return sum;
  }, 0);
  return Math.min(1, score / active.length);
}
