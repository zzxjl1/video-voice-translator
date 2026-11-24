/**
 * 这一批修复的前端测试：401 统一处理、日志脱敏、本地配音的逐句容错与取消、i18n 完整性。
 * 纯逻辑，不开浏览器。  node scripts/test-fixes.mjs
 */
import fs from 'node:fs';
import { EventClient } from '../net/events.ts';
import { onUnauthorized, notifyUnauthorized } from '../net/session.ts';
import { safeUrl } from '../utils/log.ts';

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail !== undefined ? '  ' + JSON.stringify(detail).slice(0, 300) : ''}`);
}
const r = (p) => fs.readFileSync(new URL(`../${p}`, import.meta.url), 'utf8');

// ---------------------------------------------------------------- ① 会话过期的统一通知
{
  let hits = 0;
  const off = onUnauthorized(() => hits++);
  notifyUnauthorized(); notifyUnauthorized();
  off(); notifyUnauthorized();
  check('① onUnauthorized：订阅后收到通知，取消订阅后不再收到', hits === 2, hits);
}
{
  // SSE 遇到 401：不能再无限退避重连（重连一万次也是 401），要通知上层回登录页
  let calls = 0, notified = 0, localHit = 0;
  const off = onUnauthorized(() => notified++);
  const client = new EventClient({
    url: () => '/e', onEvent: () => {}, onUnauthorized: () => localHit++,
    fetchImpl: async () => { calls++; return { ok: false, status: 401, body: null }; },
    backoff: { initial: 1, max: 2 },
  });
  await client.run();
  off();
  check('① SSE 401 → 只请求一次、不重连', calls === 1, calls);
  check('① SSE 401 → 全局会话过期通知 + 连接自己的回调都触发', notified === 1 && localHit === 1, { notified, localHit });
}
{
  let gone = 0, notified = 0;
  const off = onUnauthorized(() => notified++);
  const client = new EventClient({
    url: () => '/e', onEvent: () => {}, onGone: () => gone++,
    fetchImpl: async () => ({ ok: false, status: 404, body: null }), backoff: { initial: 1, max: 2 },
  });
  await client.run(); off();
  check('① SSE 404 仍然是"工程不存在"，不会被当成会话过期', gone === 1 && notified === 0, { gone, notified });
}

// ---------------------------------------------------------------- ② 日志脱敏
{
  const bad = ['/a?token=SECRET', '/a?sig=SECRET', '/a?X-Amz-Signature=SECRET', '/a?api_key=SECRET', '/a?auth_continue=SECRET', '/a?code=SECRET', '/a?state=SECRET'];
  check('② safeUrl：token / sig / signature / key / auth_continue / code / state 都被遮', !bad.some((u) => safeUrl(u).includes('SECRET')), bad.map(safeUrl));
  const plain = '/projects/abc/subtitles?track=bilingual';
  check('② safeUrl：普通参数原样保留', safeUrl(plain) === plain, safeUrl(plain));
  check('② safeUrl：?v=<指纹> 也遮', !safeUrl('/tts/x?v=abcdef').includes('abcdef'));
}

// ---------------------------------------------------------------- ③ 本地配音：源码级保证（真实引擎在 test-kokoro.mjs；浏览器里的 worker 只能源码级检查）
{
  const dub = r('features/browserDubbing.ts');
  check('③ 一句失败收集起来，不整批中止', /result\.failed\.push/.test(dub) && /catch \(e\)/.test(dub));
  check('③ 每句开始前检查取消信号', /opts\.signal\?\.aborted/.test(dub));
  check('③ 整句只有语气标签 → 不送去合成，记为失败原因', /cleanTtsText\(line\.translation\)/.test(dub));
  check('③ 上传带合成时用的译文和音色', /uploadBrowserTts\(project\.id, line\.id, blob, line\.translation, voice\)/.test(dub));
  check('③ 支持 force（行菜单里重做新鲜的一句）', /opts\.force/.test(dub));
  const client = r('net/client.ts');
  check('③ uploadBrowserTts：text / voice 随表单上传（服务端靠它核对）', /form\.append\('text'/.test(client) && /form\.append\('voice'/.test(client));
  check('③ API 层：401 通知会话过期（/auth/ 本身除外）', /notifyUnauthorized\(\)/.test(client) && /startsWith\('\/auth\/'\)/.test(client));
}

// ---------------------------------------------------------------- ③b 本地配音引擎：worker / 资源 / 排队
{
  const worker = r('features/kokoro/kokoroWorker.ts');
  const client = r('features/kokoro/client.ts');
  const espeak = r('features/kokoro/espeakFull.ts');
  const assets = r('features/kokoro/assets.ts');
  const posts = [...worker.matchAll(/post\(\{([^}]*)\}/g)].map((m) => m[1]);
  check('③b worker：每条消息都带请求 id（缺了的话 client 找不到回调，下载进度永远丢）', posts.length > 0 && posts.every((p) => /\bid\b/.test(p)), posts.filter((p) => !/\bid\b/.test(p)));
  check('③b worker：只有引擎自己出错（EngineError）才重建成 WASM', /instanceof EngineError/.test(worker) && /class EngineError/.test(worker));
  check('③b worker：取不到文件 / 未登录不触发 WASM 降级', /isAssetError\(e\)/.test(worker));
  check('③b worker：WebGPU 用 dtype fp16（q8f16 在 WebGPU 上输出错音频；model_file_name 会被 from_pretrained 悄悄丢掉）', /device: 'webgpu', dtype: 'fp16'/.test(worker) && !/model_file_name:/.test(worker.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')));
  check('③b worker：请求串行处理（队列），不会并发加载模型', /queue = queue\.then\(/.test(worker));
  check('③b worker：禁用远程模型，本地路径指向自己的服务器', /allowRemoteModels = false/.test(worker) && /localModelPath = `\$\{KOKORO_BASE\}\/`/.test(worker));
  check('③b worker：ORT 的 wasm 显式指向打包产物（否则 transformers.js 默认去 jsDelivr）', /wasmPaths = \{ mjs: ortMjs, wasm: ortWasm \}/.test(worker));
  check('③b worker：音色请求改写到自己的服务器', /voiceUrl\(/.test(worker));
  check('③b worker：中文里的英文词走英文音素化；音素过滤到词表', /zhG2PWith\(text, englishWord\)/.test(worker) && /keepVocab/.test(worker));
  check('③b worker：超出 511 token 的句子二分重试，不靠 tokenizer 截断', /bisect\(sentence\)/.test(worker) && /truncation: false/.test(worker));
  const src = [worker, espeak, assets, client].join('\n');
  const hosts = src.match(/https?:\/\/[a-z0-9.-]+\.[a-z]{2,}[^\s'"`)]*/gi) ?? [];
  check('③b 引擎源码里没有第三方站点地址（HF / jsDelivr / 镜像）', !hosts.some((h) => /huggingface|hf-mirror|jsdelivr|unpkg|cdnjs/i.test(h)), hosts);
  const dubbing = r('features/browserDubbing.ts');
  check('③b fillBrowserDubbing：排队执行，一批失败不卡住后面的', /tail\.then\(/.test(dubbing) && /run\.catch\(/.test(dubbing));
  check('③b fillBrowserDubbing：排队期间已被取消的不再开始', /排队等待期间就被取消/.test(dubbing));
  const vite = r('vite.config.ts');
  const pkg = JSON.parse(r('package.json'));
  // ORT 去重靠 package.json overrides（transformers.js 钉的是旧 1.22 dev build，不 override
  // 就会嵌套出第二份 ort、打包两份 wasm）。kokoro-ort 解析器把 wasm 指到唯一那份构建。
  check('③b vite：ORT 的 wasm 与 JS 取自同一份构建（overrides 去重 + kokoro-ort 解析器）', /name: 'kokoro-ort'/.test(vite) && pkg.overrides?.['onnxruntime-web'] === '$onnxruntime-web');
}

// ---------------------------------------------------------------- ③c 分离模型已并入 features/
{
  check('③c 分离模型在 features/separation/，旧的 utils/mdx 已不存在', fs.existsSync(new URL('../features/separation/separatorClient.js', import.meta.url)) && !fs.existsSync(new URL('../utils/mdx', import.meta.url)));
  check('③c App 从 features/separation 引入', /from '\.\/features\/separation'/.test(r('App.tsx')));
}

// ---------------------------------------------------------------- ④ 窗口布局不再带旧版兼容
{
  const w = await import('../store/windows.ts');
  check('④ windows：LEGACY_CHAT_KEY 已移除', !('LEGACY_CHAT_KEY' in w));
  check('④ windows：restore 只有两个参数', w.restore.length === 2, w.restore.length);
}

// ---------------------------------------------------------------- ⑤ i18n 完整性
{
  const read = (n) => JSON.parse(r(`i18n/${n}.json`));
  const en = read('en');
  const langs = ['de', 'fr', 'id', 'it', 'ja', 'ko', 'pt', 'vi'];
  const ph = (s) => (s.match(/\{\w+\}/g) ?? []).sort().join();
  for (const l of langs) {
    const d = read(l);
    const miss = Object.keys(en).filter((k) => !(k in d));
    const extra = Object.keys(d).filter((k) => !(k in en));
    const badPh = Object.keys(en).filter((k) => k in d && ph(k) !== ph(d[k]));
    const empty = Object.keys(d).filter((k) => !String(d[k]).trim());
    check(`⑤ ${l}.json：与 en.json 的 key 完全一致、占位符一致、没有空值`, !miss.length && !extra.length && !badPh.length && !empty.length,
      { miss: miss.slice(0, 3), extra: extra.slice(0, 3), badPh: badPh.slice(0, 3), empty: empty.slice(0, 3) });
  }
  const mustHave = ['没问题', '一个问题', '两个问题', '点击补齐', '点一下补齐缺失或过期的内容', '固定工具栏', '译文和配音都已就绪', '关闭通知', '问题', '导出中', '本文档仅提供英文版。', '本地配音正在进行，请稍候'];
  check('⑤ 之前漏掉的界面文案现在都有翻译（en 和 ja）', mustHave.every((k) => en[k] && read('ja')[k]), mustHave.filter((k) => !en[k] || !read('ja')[k]));
  const dyn = read('dynamic');
  check('⑤ dynamic.json：10 种语言都有 "{0} issues" 模板', ['en', 'zh', ...langs].every((l) => dyn[l]?.['{0} issues']));
  check('⑤ i18n：有 "N 个问题" 的动态规则', /个问题/.test(r('i18n/index.tsx')));
  check('⑤ ja.json：字幕内容 不再是中文原文', read('ja')['字幕内容'] === '字幕の内容', read('ja')['字幕内容']);
}

// ---------------------------------------------------------------- ⑥ 其它源码级保证
{
  const toast = r('components/ui/Toast.tsx');
  check('⑥ Toast：onClose 走 ref，计时器不被父组件重渲染重置', /closeRef\.current\(\)/.test(toast) && /\[message, durationMs\]/.test(toast));
  check('⑥ AuthProvider：日志里不再出现邮箱', !/user\.email/.test(r('auth/AuthProvider.tsx')));
  check('⑥ AuthProvider：订阅会话过期通知，把 user 置空', /onUnauthorized\(/.test(r('auth/AuthProvider.tsx')));
  check('⑥ SSE 日志：只记事件类型和序号，不记事件体', !/seq \?\? '\?'\}`, event\)/.test(r('net/events.ts')));
  check('⑥ 命令日志：只记参数的键，不记值', /args: Object\.keys\(args\)/.test(r('hooks/useProject.ts')));
  check('⑥ SettingsPanel：音色池不再只在 browser 模式下才加载', !/dubbingMode !== 'browser'\) return;/.test(r('components/editor/SettingsPanel.tsx')));
  const app = r('App.tsx');
  check('⑥ App：浏览器配音状态随工程切换清零', /browserAbort\.current\?\.abort\(\);[\s\S]{0,300}setBrowserError\(null\)/.test(app));
  check('⑥ App：取消后不再自动开始本地配音', /browserInitKey\.current = `\$\{view\.id\}/.test(app));
  const legal = r('legal/LegalPage.tsx');
  check('⑥ LegalPage：声明仅英文，正文不被 DOM 翻译改写', /data-i18n-skip/.test(legal) && /仅提供英文版/.test(legal));
  check('⑥ index.tsx：/privacy/ 带尾斜杠也能进法务页', /replace\(\/\\\/\+\$\//.test(r('index.tsx')));
  check('⑥ relativeTime：日期按界面语言格式化，不是浏览器默认', /toLocaleDateString\(tag\)/.test(r('utils/relativeTime.ts')));
}

console.log(failures === 0 ? '\n合计 全部通过' : `\n合计 ${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
