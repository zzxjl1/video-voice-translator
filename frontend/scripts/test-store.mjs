/**
 * 前端状态层的测试：增量应用、事件流（重连/去重/缺口）、聊天流式、初始化进度。
 * 纯逻辑，不开浏览器、不起服务。Node 24 直接 import .ts。
 *
 *   node scripts/test-store.mjs
 */
import { applyPatch, applyChange, speakerColor, summarize, lineAt } from '../store/project.ts';
import { reduceChat, emptyChat, loadChat, pendingConfirms, stepLabel, chatEventMatches } from '../store/chat.ts';
import { reduceInit, emptyInit, overallProgress, failInit, replayCeiling, acceptEvent } from '../store/init.ts';
import { pageFor } from '../store/page.ts';
import { EventClient } from '../net/events.ts';

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail !== undefined ? '  ' + JSON.stringify(detail).slice(0, 300) : ''}`);
}

const line = (id, n, over = {}) => ({
  id, line: n, speakerId: '0', start: n, end: n + 1, text: `原${n}`, translation: `T${n}`, reviewedText: '',
  kind: 'speech', muted: false, hidden: false, ttsVoice: null, ttsRate: null, ttsInstruction: null,
  trStatus: 'fresh', trSuspect: false, ttsStatus: 'fresh', audioUrl: `/a/${id}`, audioDuration: 1, ...over,
});
const base = () => ({
  id: 'p', rev: 5, seq: 10, filename: 'f.mp4', stage: 'ready', initError: null, initReport: {},
  settings: { targetLanguage: 'English' }, speakers: [{ id: '0', name: 'A' }, { id: '1', name: 'B' }],
  segments: [line('a', 1), line('b', 2), line('c', 3)], subtitle: { style: {}, export: {} },
  needs: { translate: 0, dub: 0 }, hasStems: false, mix: null, jobs: [], undo: null, export: null, capabilities: {},
});

// ------------------------------------------------------------------ ① applyPatch：行
{
  const s0 = base();
  const s1 = applyPatch(s0, { segments: { upsert: [line('b', 2, { translation: '改了', trStatus: 'stale' })], remove: [], order: null } });
  check('① upsert 只改那一行，别的行引用不变（React 不会重渲染它们）',
    s1.segments[1].translation === '改了' && s1.segments[0] === s0.segments[0] && s1.segments[2] === s0.segments[2]);
  check('① 不改入参（不可变）', s0.segments[1].translation === 'T2');
  check('① 顺序保持', s1.segments.map((x) => x.id).join() === 'a,b,c');

  const s2 = applyPatch(s0, { segments: { upsert: [], remove: ['b'], order: ['a', 'c'] } });
  check('② 删除一行', s2.segments.map((x) => x.id).join() === 'a,c');

  const s3 = applyPatch(s0, { segments: { upsert: [line('n', 2), line('b', 3), line('c', 4), line('a', 1)], remove: [], order: ['a', 'n', 'b', 'c'] } });
  check('③ 插入一行并按服务端给的顺序重排（"第 N 句"编号随之变）',
    s3.segments.map((x) => `${x.id}:${x.line}`).join() === 'a:1,n:2,b:3,c:4', s3.segments.map((x) => x.id));

  const s4 = applyPatch(s0, { segments: { upsert: [line('z', 9)], remove: [], order: null } });
  check('③ 顺序没给但出现了新行：追加在末尾，不丢', s4.segments.map((x) => x.id).join() === 'a,b,c,z');

  const s5 = applyPatch(s0, { segments: { upsert: [], remove: [], order: ['c', 'b', 'a'] } });
  check('③ 只换顺序', s5.segments.map((x) => x.id).join() === 'c,b,a');

  const s6 = applyPatch(s0, { segments: { upsert: [], remove: [], order: ['a', 'ghost', 'b'] } });
  check('③ order 里有不存在的 id：忽略它而不是塞 undefined 进列表', s6.segments.length === 2 && s6.segments.every(Boolean));
}

// ------------------------------------------------------------------ ② applyPatch：整块
{
  const s0 = base();
  const s1 = applyPatch(s0, {
    needs: { translate: 2, dub: 1 }, settings: { targetLanguage: 'Japanese' }, hasStems: true, stage: 'initializing',
    mix: { version: 'v1', plan: { segments: [] } },
  });
  check('④ 整块替换：needs / settings / hasStems / stage / mix',
    s1.needs.translate === 2 && s1.settings.targetLanguage === 'Japanese' && s1.hasStems && s1.stage === 'initializing' && s1.mix.version === 'v1');
  check('④ 没提到的块原样保留', s1.speakers === s0.speakers && s1.filename === 'f.mp4');
  const s2 = applyPatch(s1, { mix: null });
  check('④ mix: null 明确地清掉混音计划（没有可混的配音）', s2.mix === null);
  check('④ 空 patch = 原状', applyPatch(s0, {}).segments === s0.segments);
  check('④ chatConv 通过增量同步（resync 时能拿回当前会话）',
    applyPatch(s0, { chatConv: 'conv-new' }).chatConv === 'conv-new');
}

// ------------------------------------------------------------------ ③ applyChange：连续性
{
  const s0 = base();                                        // rev 5
  const ev = (rev, patch = {}, ids = []) => ({ type: 'change', seq: rev, ts: 0, actor: 'ai', cmd: 'x', summary: 's', rev, ids, patch });
  const ok = applyChange(s0, ev(6, { needs: { translate: 1, dub: 0 } }, ['b']));
  check('⑤ rev = 当前+1 → 应用，rev 前进，带出受影响的行', ok.kind === 'applied' && ok.state.rev === 6 && ok.changed[0] === 'b');
  check('⑤ 重复事件（rev ≤ 当前）→ stale，忽略', applyChange(s0, ev(5)).kind === 'stale' && applyChange(s0, ev(3)).kind === 'stale');
  check('⑤ 跳号（漏了 6）→ resync，不硬套增量（套了就是静默错误的界面）', applyChange(s0, ev(7)).kind === 'resync');
  let s = s0;
  for (const r of [6, 7, 8]) s = applyChange(s, ev(r, { needs: { translate: r, dub: 0 } })).state;
  check('⑤ 连续应用多条', s.rev === 8 && s.needs.translate === 8);
  check('⑤ 同一批事件重放第二遍是幂等的', (() => { let t = s; for (const r of [6, 7, 8]) { const x = applyChange(t, ev(r)); if (x.kind === 'applied') t = x.state; } return t === s; })());
}

// ------------------------------------------------------------------ ④ 派生值
{
  const s = base();
  check('⑥ 说话人颜色稳定且互不相同', speakerColor(s, '0') === speakerColor(s, '0') && speakerColor(s, '0') !== speakerColor(s, '1'));
  check('⑥ 未知说话人有兜底色，不抛', typeof speakerColor(s, 'zzz') === 'string');
  check('⑥ lineAt：播放头所在的句子', lineAt(s, 2.5)?.id === 'b' && lineAt(s, 99) === null);
  s.segments[0] = line('a', 1, { trStatus: 'stale', ttsStatus: 'missing', trSuspect: true });
  const sm = summarize(s);
  check('⑥ summarize', sm.total === 3 && sm.translated === 2 && sm.dubbed === 2 && sm.suspect === 1, sm);
}

// ------------------------------------------------------------------ ⑤ 聊天 reducer
{
  let c = emptyChat();
  const E = (kind, o = {}) => ({ type: 'chat', seq: 1, kind, ...o });
  c = reduceChat(c, E('message', { message: { id: 'u1', role: 'user', ts: 1, text: '改第 2 句' } }));
  c = reduceChat(c, E('start', { id: 'r1' }));
  check('⑦ start：开出流式气泡、进入 busy', c.busy && c.streaming?.id === 'r1' && c.streaming.text === '');
  c = reduceChat(c, E('delta', { id: 'r1', text: '好，' }));
  c = reduceChat(c, E('delta', { id: 'r1', text: '我来改。' }));
  check('⑦ delta 累加成文字', c.streaming.text === '好，我来改。');
  c = reduceChat(c, E('delta', { id: 'OLD', text: '迟到的' }));
  check('⑦ 属于别的（已结束的）一轮的迟到 delta 被丢掉', c.streaming.text === '好，我来改。');
  c = reduceChat(c, E('tool_call', { id: 't1', name: 'set_translation', args: { line: 2, text: 'x' } }));
  c = reduceChat(c, E('tool_call', { id: 't1', name: 'set_translation', args: { line: 2, text: 'x' } }));
  check('⑦ tool_call 出现一个"进行中"的步骤，重复事件不会出现两个', c.streaming.steps.length === 1 && c.streaming.steps[0].status === 'running');
  c = reduceChat(c, E('tool_result', { id: 't1', name: 'set_translation', ok: true, error: null }));
  check('⑦ tool_result 成功 → ok', c.streaming.steps[0].status === 'ok');
  c = reduceChat(c, E('tool_call', { id: 't2', name: 'edit_source', args: { line: 99 } }));
  c = reduceChat(c, E('tool_result', { id: 't2', name: 'edit_source', ok: false, error: '没有第 99 句' }));
  check('⑦ tool_result 失败 → failed，带原因', c.streaming.steps[1].status === 'failed' && c.streaming.steps[1].error === '没有第 99 句');
  c = reduceChat(c, E('message', { message: { id: 'r1', role: 'assistant', ts: 2, text: '改好了。', actions: [] } }));
  check('⑦ 落盘的最终消息替换流式气泡，内容以它为准', c.streaming === null && c.messages.at(-1).text === '改好了。' && c.messages.length === 2);
  c = reduceChat(c, E('idle'));
  check('⑦ idle → 不再 busy', !c.busy);
  const again = reduceChat(c, E('message', { message: { id: 'r1', role: 'assistant', ts: 2, text: '改好了（更新）。' } }));
  check('⑦ 同 id 的消息再来一次 = 替换，不重复', again.messages.length === 2 && again.messages.at(-1).text === '改好了（更新）。');

  // 确认卡片
  let d = emptyChat();
  d = reduceChat(d, E('message', { message: { id: 'k1', role: 'confirm', ts: 1, command: 'redub', status: 'pending', level: 'cost' } }));
  check('⑧ 待确认的卡片能被找出来（输入框据此提示）', pendingConfirms(d).length === 1);
  d = reduceChat(d, E('confirm_resolved', { id: 'k1', approved: true }));
  check('⑧ 确认后卡片变 approved，不再算待确认', d.messages[0].status === 'approved' && pendingConfirms(d).length === 0);
  d = reduceChat(d, E('confirm_resolved', { id: 'nope', approved: false }));
  check('⑧ 对不存在的卡片 resolve 不崩', d.messages.length === 1);

  // 刷新页面：从落盘记录恢复，若有一轮在进行就是 busy
  const loaded = loadChat([{ id: 'a', role: 'user', ts: 1, text: 'hi' }], true);
  check('⑨ 从落盘记录恢复', loaded.messages.length === 1 && loaded.busy && loaded.streaming === null);

  // 会话事件过滤：切会话后旧一轮事件不能污染新会话
  check('⑨a 会话过滤：同 conv 放行、异 conv 丢弃、无 conv 兼容',
    chatEventMatches({ conv: 'A' }, 'A') && !chatEventMatches({ conv: 'A' }, 'B')
    && chatEventMatches({}, 'B') && chatEventMatches({ conv: 'A' }, ''));

  // 有工具调用的那一步：过渡语清掉，但工具步骤卡片要留着
  const withText = reduceChat(reduceChat(loadChat([], true), { type: 'chat', seq: 1, kind: 'start', id: 'r11' }),
    { type: 'chat', seq: 2, kind: 'delta', id: 'r11', text: '我先看一下' });
  const stepped = reduceChat(withText, { type: 'chat', seq: 3, kind: 'tool_call', id: 't11', name: 'get_overview', args: {} });
  const cleared = reduceChat(stepped, { type: 'chat', seq: 4, kind: 'clear', id: 'r11' });
  check('⑨c clear 只清文字、保留工具步骤', cleared.streaming?.text === '' && cleared.streaming?.steps.length === 1,
    cleared.streaming);
  check('⑨c clear 不能误伤别的一轮',
    reduceChat(stepped, { type: 'chat', seq: 5, kind: 'clear', id: 'other' }).streaming?.text === '我先看一下');

  // 输出完还挂着"输出中 + 停止按钮"：GET /chat 的快照（running=true）比事件旧，
  // 落在 message/idle 之后会把 busy 覆盖成 true。判据要落在消息上。
  const asst = { id: 'm1', role: 'assistant', ts: 1, text: '说完了' };
  const usr = { id: 'm2', role: 'user', ts: 2, text: '在吗' };
  check('⑨b 快照说 running，但最后一条是 assistant → 不算进行中',
    loadChat([usr, asst], true).busy === false);
  check('⑨b 最后一条是 user 且 running → 进行中', loadChat([usr], true).busy === true);
  check('⑨b 最后一条是确认卡片且 running → 仍算进行中（等确认）',
    loadChat([usr, { id: 'c1', role: 'confirm', ts: 3, command: 'redub', status: 'pending' }], true).busy === true);

  let firing = loadChat([usr], true);
  let c2 = reduceChat(firing, { type: 'chat', seq: 1, kind: 'start', id: 'r9' });
  c2 = reduceChat(c2, { type: 'chat', seq: 2, kind: 'delta', id: 'r9', text: '好' });
  check('⑨b 流式中的 busy', c2.busy === true && c2.streaming.text === '好');
  c2 = reduceChat(c2, { type: 'chat', seq: 3, kind: 'message', message: { id: 'r9', role: 'assistant', ts: 2, text: '好了' } });
  check('⑨b assistant 消息落地即结束这一轮（idle 丢了也不会卡住）',
    c2.busy === false && c2.streaming === null);
  let system = loadChat([usr], true);
  system = reduceChat(system, { type: 'chat', seq: 1, kind: 'start', id: 'r10' });
  system = reduceChat(system, { type: 'chat', seq: 2, kind: 'message', message: { id: 'w1', role: 'assistant', ts: 3, text: '欢迎', system: true } });
  check('⑨b 系统欢迎消息不算"这一轮结束"（不会误清进行中的流）', system.busy === true && system.streaming?.id === 'r10');
  check('⑨b error 消息也是一轮结束（异常时后端不写 assistant）', loadChat([usr, { id: 'e1', role: 'error', ts: 3, text: '出错了' }], true).busy === false);
  check('⑨b 快照里的 system 欢迎消息不该让进行中的轮次失去停止按钮',
    loadChat([{ id: 'w1', role: 'assistant', ts: 1, text: '欢迎', system: true }], true).busy === true);
  const errTurn = reduceChat(loadChat([usr], true), { type: 'chat', seq: 1, kind: 'message', message: { id: 'e2', role: 'error', ts: 2, text: '出错了' } });
  check('⑨b error 事件也清掉流式气泡', errTurn.busy === false && errTurn.streaming === null);

  check('⑩ 步骤标签：带行号', stepLabel({ name: 'set_muted', args: { lines: [1, 2, 3] } }) === '静音（第 1、2、3 句）');
  check('⑩ 步骤标签：单句', stepLabel({ name: 'edit_source', args: { line: 4 } }) === '改原文（第 4 句）');
  check('⑩ 步骤标签：行太多省略', stepLabel({ name: 'redub', args: { lines: [1, 2, 3, 4, 5, 6, 7] } }).endsWith('…）'));
  check('⑩ 不认识的命令回退为命令名（后端加了命令前端不会炸）', stepLabel({ name: 'brand_new', args: {} }) === 'brand_new');
}

// ------------------------------------------------------------------ ⑥ 初始化进度
{
  let s = emptyInit();
  const P = (phase, status, o = {}) => ({ type: 'progress', seq: 1, ts: 0, phase, status, ...o });
  check('⑪ 初始状态：六个步骤都是 pending', s.steps.length === 6 && s.steps.every((x) => x.status === 'pending') && !s.finished);
  check('⑪ 起点进度 0', overallProgress(s) === 0);

  s = reduceInit(s, P('separation', 'skipped'));
  s = reduceInit(s, P('asr', 'started'));
  check('⑫ asr 开始 → running 并写日志', s.steps[1].status === 'running' && s.log.some((l) => l.text.includes('识别')));
  s = reduceInit(s, P('asr', 'done', { lines: 10 }));
  check('⑫ asr 完成：同一行日志被原地改写，不是追加一行', s.steps[1].status === 'done' && s.steps[1].detail === '10 句' && s.log.filter((l) => l.key === 'asr').length === 1);

  s = reduceInit(s, P('mm_enhance', 'listening'));
  s = reduceInit(s, P('mm_enhance', 'done', { adjusted: 3 }));
  check('⑫ 审听：纠正 3 句', s.steps[2].status === 'done' && s.steps[2].detail.includes('3'));
  s = reduceInit(s, P('translation', 'started', { count: 10 }));
  s = reduceInit(s, P('translation', 'done', { count: 10, failed: 0 }));
  check('⑫ 翻译完成', s.steps[3].status === 'done');

  s = reduceInit(s, P('tts', 'started', { total: 10 }));
  for (let i = 1; i <= 4; i++) s = reduceInit(s, P('tts', undefined, { segment_id: `s${i}`, progress: i, total: 10 }));
  check('⑬ 配音进度逐句推进，日志只有一行进度（原地改写）', s.steps[5].progress.done === 4 && s.log.filter((l) => l.key === 'tts').length === 1 && s.log.find((l) => l.key === 'tts').text === '配音 4/10');
  const mid = overallProgress(s);
  check('⑬ 总进度在 0~1 之间且随步骤推进（跳过的步骤不计入分母）', mid > 0.5 && mid < 1, mid);
  s = reduceInit(s, P('tts', undefined, { segment_id: 's5', tts_error: '额度不足', progress: 5, total: 10 }));
  check('⑬ 单句配音失败：单独一条错误日志，进度照常前进', s.log.some((l) => l.level === 'error' && l.text.includes('额度不足')) && s.steps[5].progress.done === 5);
  s = reduceInit(s, P('tts', 'done', { failed: 1, lastError: '额度不足' }));
  check('⑬ 配音结束带失败 → 步骤标失败，写明几句', s.steps[5].status === 'failed' && s.steps[5].detail.includes('1'));
  s = reduceInit(s, { type: 'progress', seq: 1, ts: 0, phase: 'init', status: 'done' });
  check('⑭ 收到 init done → finished', s.finished);

  // 克隆的细节日志
  let v = emptyInit();
  v = reduceInit(v, P('voice_clone', 'started', { total: 2 }));
  v = reduceInit(v, P('voice_clone', 'picking', { speaker_id: '0' }));
  v = reduceInit(v, P('voice_clone', 'pick_failed', { speaker_id: '0', error: '候选不足' }));
  v = reduceInit(v, P('voice_clone', 'done', { speaker_id: '0', reused: true, progress: 1, total: 2 }));
  v = reduceInit(v, P('voice_clone', 'failed', { speaker_id: '1', error: '超时', progress: 2, total: 2 }));
  check('⑮ 克隆日志：挑选失败 / 复用 / 失败各一条，且失败是 warn 级',
    v.log.length === 4 && v.log[1].level === 'warn' && v.log[2].text.includes('复用') && v.log[3].level === 'warn');

  check('⑯ 不认识的阶段 / 没有 phase 的事件：原样返回，不炸', reduceInit(s, P('future_phase', 'started')) === s && reduceInit(s, { type: 'progress', seq: 1, ts: 0 }) === s);
  const f = failInit(emptyInit(), 'ASR 挂了');
  check('⑯ failInit', f.failed === 'ASR 挂了' && f.finished);
  const allSkipped = { ...emptyInit(), steps: emptyInit().steps.map((x) => ({ ...x, status: 'skipped' })) };
  check('⑯ 全部跳过时总进度 = 1（不除零）', overallProgress(allSkipped) === 1);
}

// ------------------------------------------------------------------ ⑦ EventClient：假流
const enc = new TextEncoder();
function streamOf(chunks, { endWith = 'close' } = {}) {
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) { controller.enqueue(enc.encode(chunks[i++])); return; }
      if (endWith === 'close') controller.close(); else controller.error(new Error('network'));
    },
  });
}
const frame = (o) => `id: ${o.seq}\ndata: ${JSON.stringify(o)}\n\n`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

{
  // 帧被切在任意位置（半个 JSON、半个分隔符）
  const full = frame({ type: 'change', seq: 1 }) + ': ping\n\n' + frame({ type: 'job', seq: 2 });
  const cut = [full.slice(0, 7), full.slice(7, 40), full.slice(40, full.length - 3), full.slice(full.length - 3)];
  const got = [];
  const client = new EventClient({
    url: (since) => `/e?since=${since}`, onEvent: (e) => got.push(e),
    fetchImpl: async () => ({ ok: true, status: 200, body: streamOf(cut) }),
    backoff: { initial: 1, max: 2 },
  });
  const run = client.run(); await sleep(30); client.close(); await run;
  check('⑰ 帧被任意切开也能拼回完整事件，心跳（: ping）被忽略', got.length >= 2 && got[0].seq === 1 && got[1].seq === 2 && got.every((e) => e.type !== undefined), got.length);
}
{
  // 重连：带上最后收到的 seq，边界上重复的事件被丢掉
  const urls = [];
  let attempt = 0;
  const got = [];
  const client = new EventClient({
    url: (since) => { urls.push(since); return `/e?since=${since}`; }, onEvent: (e) => got.push(e.seq),
    fetchImpl: async () => {
      attempt++;
      if (attempt === 1) return { ok: true, status: 200, body: streamOf([frame({ seq: 1, type: 'x' }) + frame({ seq: 2, type: 'x' })], { endWith: 'error' }) };
      if (attempt === 2) return { ok: true, status: 200, body: streamOf([frame({ seq: 2, type: 'x' }) + frame({ seq: 3, type: 'x' })], { endWith: 'error' }) };   // 2 重复
      return { ok: true, status: 200, body: new ReadableStream({ start() {} }) };                  // 之后挂着不动
    },
    backoff: { initial: 1, max: 2 },
  });
  const run = client.run(); await sleep(80); client.close(); await run;
  check('⑱ 断线后用最后收到的 seq 重连', urls[0] === 0 && urls[1] === 2, urls);
  check('⑱ 重连边界上重复的事件按 seq 丢弃（只收到 1、2、3 各一次）', got.join() === '1,2,3', got);
  check('⑱ lastSeq 跟踪到最后一个', client.lastSeq === 3);
}
{
  // 工程不存在（404）：不重连，通知上层
  let calls = 0, gone = 0;
  const client = new EventClient({
    url: () => '/e', onEvent: () => {}, onGone: () => gone++,
    fetchImpl: async () => { calls++; return { ok: false, status: 404, body: null }; },
    backoff: { initial: 1, max: 2 },
  });
  await client.run();
  check('⑲ 404 → 通知 onGone 且不再重连（EventSource 会无限重连）', gone === 1 && calls === 1, { calls, gone });
}
{
  // 服务端 500：退避重试；close() 能打断
  let calls = 0; const states = [];
  const client = new EventClient({
    url: () => '/e', onEvent: () => {}, onState: (s) => states.push(s),
    fetchImpl: async () => { calls++; return { ok: false, status: 500, body: null }; },
    backoff: { initial: 5, max: 20 },
  });
  const run = client.run(); await sleep(120); client.close(); await run;
  check('⑳ 5xx：持续退避重试', calls >= 3, calls);
  check('⑳ close() 之后真的停了，最后状态是 closed', states.at(-1) === 'closed' && states.includes('reconnecting'), states);
  const before = calls; await sleep(60);
  check('⑳ 停了之后不再发请求', calls === before);
}
{
  // 半截 JSON 不会让整条流死掉
  const got = [];
  const client = new EventClient({
    url: () => '/e', onEvent: (e) => got.push(e.seq),
    fetchImpl: async () => ({ ok: true, status: 200, body: streamOf(['data: {"seq": 1, "ty\n\n' + frame({ seq: 2, type: 'x' })]) }),
    backoff: { initial: 1, max: 2 },
  });
  const run = client.run(); await sleep(30); client.close(); await run;
  check('㉑ 一帧坏 JSON 被丢弃，后面的帧照常处理', got.includes(2) && !got.includes(1), got);
}

// ------------------------------------------------------------------ ⑧ 刷新后找回初始化进度
{
  check('㉒ 初始化中 / 失败停留：重放上界 = 快照的 seq', replayCeiling({ stage: 'initializing', seq: 40 }) === 40 && replayCeiling({ stage: 'failed', seq: 9 }) === 9);
  check('㉒ 已就绪 / 刚上传：不重放（0）', replayCeiling({ stage: 'ready', seq: 40 }) === 0 && replayCeiling({ stage: 'uploaded', seq: 3 }) === 0);

  const ev = (type, seq, extra = {}) => ({ type, seq, ...extra });
  const C = 40;
  check('㉒ 重放段里只放行 progress', acceptEvent(ev('progress', 10), C));
  for (const t of ['ui', 'chat', 'change', 'job', 'conversation']) {
    check(`㉒ 重放段里的旧 ${t} 事件被挡掉（ui 会再触发一次下载，chat 会重画旧气泡）`, !acceptEvent(ev(t, 10), C));
  }
  check('㉒ 边界：seq == 上界仍属于重放段', !acceptEvent(ev('ui', C), C) && acceptEvent(ev('progress', C), C));
  for (const t of ['ui', 'chat', 'change', 'job']) {
    check(`㉒ 重放段之后的实时 ${t} 事件照常处理`, acceptEvent(ev(t, C + 1), C));
  }
  check('㉒ 不重放时（上界 0）一切照常', ['ui', 'chat', 'change'].every((t) => acceptEvent(ev(t, 1), 0)));
  check('㉒ 没有 seq 的事件放行（保持与 EventClient 一致）', acceptEvent({ type: 'chat' }, C));

  // 端到端：模拟"刷新前跑到配音 4/10"，重放这些 progress 到一个全新的状态
  const P = (phase, status, o = {}) => ({ type: 'progress', seq: 0, ts: 0, phase, status, ...o });
  const history = [
    P('separation', 'skipped'), P('asr', 'started'), P('asr', 'done', { lines: 10 }),
    P('mm_enhance', 'skipped'), P('translation', 'started', { count: 10 }), P('translation', 'done', { count: 10, failed: 0 }),
    P('voice_clone', 'complete'), P('tts', 'started', { total: 10 }),
    ...[1, 2, 3, 4].map((i) => P('tts', undefined, { segment_id: `s${i}`, progress: i, total: 10 })),
  ].map((e, i) => ({ ...e, seq: i + 1 }));
  const mixedIn = [...history.slice(0, 3), ev('chat', 4, { kind: 'message' }), ev('ui', 5, { action: 'export_ready' }), ...history.slice(3)];
  let live = emptyInit();
  for (const e of mixedIn) if (acceptEvent(e, 60)) live = reduceInit(live, e);
  check('㉓ 重放后：已完成的步骤是完成态，不是“待处理”',
    live.steps.map((x) => x.status).join() === 'skipped,done,skipped,done,done,running', live.steps.map((x) => x.status));
  check('㉓ 重放后：配音进度 4/10', live.steps[5].progress?.done === 4 && live.steps[5].progress?.total === 10);
  check('㉓ 重放后：日志不是空的', live.log.length >= 3 && live.log.some((l) => l.text.includes('识别完成')), live.log.length);
  check('㉓ 重放后接着收实时事件：进度继续往前走', reduceInit(live, P('tts', undefined, { segment_id: 's5', progress: 5, total: 10 })).steps[5].progress.done === 5);
}

// ------------------------------------------------------------------ ⑨ 该显示哪一页
{
  const A = (o) => pageFor({ videoId: 'v', stage: 'ready', starting: false, preparing: false, ...o });
  check('㉔ 没有工程 → 落地页', pageFor({ videoId: null, stage: null, starting: false, preparing: false }) === 'landing');
  check('㉔ 打开了工程但视图没回来 → 加载中', A({ stage: null }) === 'loading');
  check('㉔ uploaded / initializing / failed → 初始化页', ['uploaded', 'initializing', 'failed'].every((st) => A({ stage: st }) === 'init'));
  check('㉔ ready → 编辑器', A({}) === 'editor');
  // 回归：上传按 MD5 复用了一个已完成的工程。分离/setup 期间服务端还是旧的 ready，不能闪出编辑器
  check('㉔ 回归：复用了已完成工程，浏览器分离期间（preparing）停在初始化页，不闪编辑器', A({ preparing: true }) === 'init');
  check('㉔ 回归：setup 请求期间（starting）也停在初始化页', A({ starting: true }) === 'init');
  check('㉔ 分离/启动结束后，ready 才进编辑器', A({ starting: false, preparing: false }) === 'editor');
  check('㉔ 加载中优先于 starting（视图没回来前没得显示）', A({ stage: null, starting: true }) === 'loading');
  check('㉔ 落地页不受 starting 影响（上传中还在落地页上）', pageFor({ videoId: null, stage: null, starting: true, preparing: false }) === 'landing');
}

console.log(failures === 0 ? '\n合计 全部通过' : `\n合计 ${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
