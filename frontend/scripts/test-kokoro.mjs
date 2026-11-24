/**
 * 本地配音（Kokoro）的文本层与引擎测试。  node scripts/test-kokoro.mjs
 *
 * ① 纯逻辑（任何机器都跑）：分句、中文数字读法、中文 G2P、音色排序、语言矩阵与后端一致
 * ② 提交进库的资源：manifest 的大小和 sha256、文件名与前端常量一致、音色齐全无多余、体积不超 GitHub 限制
 * ③ 真模型：经 features/kokoro/engine.ts（与生产同一条装配路径）加载提交进库的 model_int8，在 Node 里合成，
 *    验证出声、不是静音 / NaN——尤其是"中文里夹英文词 / 数字"这类之前会产生乱码音素的句子。
 *    （WebGPU 只能在真实浏览器里验证，见 docs/dubbing-modes.md 的实测记录。）
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { loadTs } from './_bundle.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MODELS = path.resolve(ROOT, '../backend/models/kokoro-82m');

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail !== undefined ? '  ' + JSON.stringify(detail).slice(0, 300) : ''}`);
}

const text = await loadTs('features/kokoro/text.ts');
const zh = await loadTs('features/kokoro/zhG2P.ts');
const catalog = await loadTs('features/kokoro/catalog.ts');

// ---------------------------------------------------------------- ① 分句
{
  const S = text.splitSentences;
  const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  check('① 分句：域名、小数、版本号里的点不是句号',
    eq(S('Visit thirdweb.com to learn more. It is 3.5 times faster than v2.0.', 'en'),
      ['Visit thirdweb.com to learn more.', 'It is 3.5 times faster than v2.0.']), S('Visit thirdweb.com to learn more. It is 3.5 times faster than v2.0.', 'en'));
  check('① 分句：缩写（Dr. Smith）不断', eq(S('Dr. Smith said no. Then he left.', 'en'), ['Dr. Smith said no. Then he left.']));
  check('① 分句：缩写（e.g. / etc.）不断；etc. 后接大写才断',
    eq(S('We use e.g. React and Vue, etc. and more. Next one.', 'en'), ['We use e.g. React and Vue, etc. and more.', 'Next one.']));
  check('① 分句：省略号后小写接着说，后接大写才断', eq(S('Wait... what happened?', 'en'), ['Wait... what happened?']) && eq(S('Wait... What happened?', 'en'), ['Wait...', 'What happened?']));
  check('① 分句：人名缩写（J. Smith）不断', eq(S('Ask J. Smith about it. Done.', 'en'), ['Ask J. Smith about it.', 'Done.']));
  check('① 分句：引号跟着它收尾的那句', eq(S('He said "Stop." Then left.', 'en'), ['He said "Stop."', 'Then left.']));
  check('① 分句：连串标点（?! / !!!）算一个句尾', eq(S('Hello!!! Are you ok?! Yes.', 'en'), ['Hello!!!', 'Are you ok?!', 'Yes.']));
  check('① 分句：法语的小数点也不断', eq(S("Voilà thirdweb.com, c'est 3.5 fois plus rapide. Bien sûr !", 'fr'), ["Voilà thirdweb.com, c'est 3.5 fois plus rapide.", 'Bien sûr !']));
  check('① 分句：中文句号、感叹号、省略号、换行', eq(S('访问 thirdweb.com。很快！真的？是的……好\n再见', 'zh'), ['访问 thirdweb.com。', '很快！', '真的？', '是的……', '好', '再见']));
  const long = 'a'.repeat(10) + ', ' + 'b'.repeat(400);
  const pieces = S(long, 'en');
  check('① 分句：超长句在子句处切开，每段不超过上限，内容不丢', pieces.every((p) => p.length <= text.maxSentenceChars('en')) && pieces.join('').replace(/[\s,]/g, '') === long.replace(/[\s,]/g, ''), pieces.map((p) => p.length));
  const zhLong = '这是一个很长的句子，' .repeat(20);
  check('① 分句：超长中文也被切开', S(zhLong, 'zh').every((p) => p.length <= text.maxSentenceChars('zh')) && S(zhLong, 'zh').length > 1);
  check('① 分句：空串 / 纯标点不产生空句', eq(S('', 'en'), []) && S('...', 'en').length <= 1);
}

// ---------------------------------------------------------------- ① 中文数字
{
  const N = zh.normalizeZh;
  const cases = {
    '100': '一百', '10': '十', '12': '十二', '110': '一百一十', '200': '两百', '2000': '两千', '12000': '一万两千',
    '10000': '一万', '100000': '十万', '1002': '一千零二', '10005': '一万零五', '100000005': '一亿零五',
    '2024年发布': '二零二四年发布', '增长了 35.5%': '增长了 百分之三十五点五', '只有2个': '只有两个', '价格 $99.5': '价格 九十九点五美元',
    '1,000,000 次': '一百万 次', '下午3:05开会': '下午三点零五分开会', '2020-2024': '二零二零到二零二四', '2020-2024 年，涨了': '二零二零到二零二四 年，涨了', '2024年1月': '二零二四年一月', '3-5 天': '三到五 天',
    '电话 13812345678': '电话 一三八一二三四五六七八', '007': '零零七', '１２３': '一百二十三', '5G 网络': '五G 网络',
    'v2.0 版本': 'v2.0 版本', 'Web3 的未来': 'Web3 的未来', 'Web 2 带来': 'Web 二 带来', '3.14': '三点一四',
  };
  const bad = Object.entries(cases).filter(([i, o]) => N(i) !== o).map(([i, o]) => [i, N(i), o]);
  check(`① 中文数字：${Object.keys(cases).length} 个用例（数字 / 年份 / 百分号 / 金额 / 时间 / 区间 / 电话 / 英文词内的数字不动）`, bad.length === 0, bad);
}

// ---------------------------------------------------------------- ① 中文 G2P
{
  const fake = (r) => `<${r}>`;
  const r = zh.zhG2P('我们打造了 Thirdweb', fake);
  check('① G2P：英文词交给英文音素化，不再把原字母塞进音素串', r.phonemes.includes('<Thirdweb>') && !/Thirdweb(?!>)/.test(r.phonemes.replace('<Thirdweb>', '')), r.phonemes);
  check('① G2P：没有英文音素化器时跳过英文词（并记录），不输出原字母', (() => { const x = zh.zhG2P('我们打造了 Thirdweb'); return !x.phonemes.includes('Thirdweb') && x.skipped.some((s) => s.char === 'Thirdweb'); })());
  check('① G2P：词边界按位置——相同的字出现在相邻两个词里不错位（学生|生活）',
    zh.zhG2P('学生生活').phonemes === 'ɕɥe↗ʂə→ŋ ʂə→ŋxwo↗', zh.zhG2P('学生生活').phonemes);
  check('① G2P：同一个词重复出现各自成词', zh.zhG2P('他们喜欢他们的家').phonemes === 'tʰa→mən ɕi↓xwan tʰa→mən tɤ ʨja→', zh.zhG2P('他们喜欢他们的家').phonemes);
  check('① G2P：数字读成汉字再注音（一百 = 一 + 百，不再是逐位"一零零"）', zh.zhG2P('100个').phonemes === 'i↘pai↓kɤ↘' && !zh.zhG2P('100个').phonemes.includes('li↗ŋ'), zh.zhG2P('100个').phonemes);
  check('① G2P：Latin 词清单（给 worker 预先音素化）去重', JSON.stringify(zh.latinRunsZh('Web 2 带来 Thirdweb 和 Web')) === JSON.stringify(['Web', 'Thirdweb']), zh.latinRunsZh('Web 2 带来 Thirdweb 和 Web'));
  const aw = await zh.zhG2PWith('用 AI 做 Web3', async (w) => `[${w}]`);
  check('① G2P：zhG2PWith 逐个调用异步音素化器', aw.phonemes.includes('[AI]') && aw.phonemes.includes('[Web3]'), aw.phonemes);
}

// ---------------------------------------------------------------- ① 音色目录
{
  const en = catalog.voicesFor('English');
  check('① 音色排序：英语第一个是评级最好的（A），最差的在最后', en[0].grade.startsWith('A') && catalog.gradeRank(en.at(-1).grade) >= catalog.gradeRank(en[0].grade), [en[0].key, en[0].grade, en.at(-1).key, en.at(-1).grade]);
  const ranks = en.map((v) => catalog.gradeRank(v.grade));
  check('① 音色排序：整个英语列表按评级单调不降', ranks.every((x, i) => i === 0 || ranks[i - 1] <= x), en.map((v) => v.grade).join(' '));
  check('① 音色排序：A > A− > B− > C+ > C > D > F+，未评级最后', catalog.gradeRank('A') < catalog.gradeRank('A−') && catalog.gradeRank('A−') < catalog.gradeRank('B−')
    && catalog.gradeRank('C+') < catalog.gradeRank('C') && catalog.gradeRank('D') < catalog.gradeRank('F+') && catalog.gradeRank('F+') < catalog.gradeRank(''));
  check('① 默认音色是该语言评级最好的之一（分配时排第一）', catalog.defaultVoiceFor('English') === 'af_heart' && en[0].key === 'af_heart', en[0].key);
  check('① 葡萄牙语（无评级）保持目录顺序、默认音色仍在前', catalog.voicesFor('Portuguese')[0].key === 'pf_dora');
  check('① 没有音色的语言（德 / 越 / 日 / 韩 / 印尼）返回空', ['German', 'Vietnamese', 'Japanese', 'Korean', 'Indonesian'].every((l) => catalog.voicesFor(l).length === 0 && catalog.defaultVoiceFor(l) === null));
  check('① 每个有音色的语言，默认音色确实在它的列表里', Object.keys(catalog.LANGUAGES).every((l) => catalog.voicesFor(l).some((v) => v.key === catalog.defaultVoiceFor(l))));
}

// ---------------------------------------------------------------- ① 语言矩阵与后端一致
{
  let backend = null;
  try {
    backend = JSON.parse(execFileSync('python3', ['-c',
      'import json,sys;sys.path.insert(0,"../backend");import os;os.environ.setdefault("DASHSCOPE_API_KEY","t");os.environ.setdefault("LLM_API_KEY","t");from app import config;print(json.dumps(config.BROWSER_LANGUAGES))'],
    { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }));
  } catch { /* 没有 python / 依赖：下面报 SKIP */ }
  if (!backend) console.log('SKIP  ① 语言矩阵与后端一致（没法导入 backend/app/config.py）');
  else {
    const front = JSON.parse(JSON.stringify(catalog.LANGUAGES));
    const same = JSON.stringify(Object.fromEntries(Object.entries(front).sort())) === JSON.stringify(Object.fromEntries(Object.entries(backend).sort()));
    check('① 语言矩阵：前端 LANGUAGES 与后端 config.BROWSER_LANGUAGES 完全一致（默认音色 + 前缀）', same, { front, backend });
    const orphan = Object.values(front).flatMap((l) => l.prefixes).filter((p) => !catalog.voicesFor(Object.keys(front).find((k) => front[k].prefixes.includes(p))).some((v) => v.key.startsWith(p)));
    check('① 语言矩阵：每个前缀在目录里都有音色（没有写了前缀却没有音色的）', orphan.length === 0, orphan);
  }
}

// ---------------------------------------------------------------- ② 提交进库的资源完整、文件名一致
{
  const MANIFEST = path.join(MODELS, 'manifest.json');
  check('② 资源已随代码库提供：manifest.json 在 backend/models/kokoro-82m/', fs.existsSync(MANIFEST));
  const files = fs.existsSync(MANIFEST) ? JSON.parse(fs.readFileSync(MANIFEST, 'utf8')).files : {};

  const bad = [];
  for (const [rel, meta] of Object.entries(files)) {
    const p = path.join(MODELS, rel);
    if (!fs.existsSync(p)) { bad.push(`缺失 ${rel}`); continue; }
    if (fs.statSync(p).size !== meta.size) { bad.push(`大小不符 ${rel}`); continue; }
    if (createHash('sha256').update(fs.readFileSync(p)).digest('hex') !== meta.sha256) bad.push(`sha256 不符 ${rel}`);
  }
  check(`② ${Object.keys(files).length} 个文件的大小和 sha256 都与 manifest 一致（没有损坏、没有被悄悄换掉）`, bad.length === 0, bad.slice(0, 4));

  const onDisk = [];
  const walk = (d) => { for (const f of fs.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, f.name); f.isDirectory() ? walk(p) : onDisk.push(path.relative(MODELS, p)); } };
  walk(MODELS);
  const stray = onDisk.filter((f) => f !== 'manifest.json' && !(f in files));
  check('② 目录里没有 manifest 之外的文件（没有留下的调试模型 / 临时文件）', stray.length === 0, stray.slice(0, 5));

  const assets = await loadTs('features/kokoro/assets.ts');
  const prepare = fs.readFileSync(path.resolve(ROOT, '../backend/scripts/prepare_kokoro_models.py'), 'utf8');
  const pyFile = /^MODEL_FILE = "([^"]+)"/m.exec(prepare)?.[1];
  check('② 前端 KOKORO_MODEL_FILE 与 prepare_kokoro_models.py 的 MODEL_FILE 一致', pyFile === assets.KOKORO_MODEL_FILE, [pyFile, assets.KOKORO_MODEL_FILE]);
  check('② 前端要加载的模型文件确实在目录里', fs.existsSync(path.join(MODELS, 'onnx', `${assets.KOKORO_MODEL_FILE}.onnx`)));
  check('② onnx/ 里只有这一个模型（fp16 / 官方 int8 / q8f16 等备选不进库：体积，且 q8f16 在 WebGPU 上输出错音频）',
    fs.readdirSync(path.join(MODELS, 'onnx')).join() === `${assets.KOKORO_MODEL_FILE}.onnx`, fs.readdirSync(path.join(MODELS, 'onnx')));
  const biggest = Math.max(...onDisk.map((f) => fs.statSync(path.join(MODELS, f)).size));
  check('② 最大文件 < 100MB（GitHub 单文件上限）', biggest < 100 * 1024 * 1024, biggest);

  // 音色：应用里能选到的每个都有文件，目录里的每个文件都能被选到
  const selectable = Object.keys(catalog.LANGUAGES).flatMap((l) => catalog.voicesFor(l).map((v) => v.key)).sort();
  const shipped = onDisk.filter((f) => f.startsWith('voices/')).map((f) => path.basename(f, '.bin')).sort();
  check(`② 音色：可选的 ${selectable.length} 个都随库提供，且没有多余的`, JSON.stringify(selectable) === JSON.stringify(shipped),
    { missing: selectable.filter((v) => !shipped.includes(v)), extra: shipped.filter((v) => !selectable.includes(v)) });
  check('② espeak 的许可文本（GPL-3.0）随文件提供', fs.existsSync(path.join(MODELS, 'espeak', 'COPYING')));
}

// ---------------------------------------------------------------- ③ 真模型：和生产同一条装配路径
{
  // transformers.js 按 <localModelPath>/<模型 id>/ 找文件；模型 id 是 'kokoro'，目录叫 kokoro-82m，
  // 线上由 /api/models/kokoro 路由对应，这里用符号链接模拟同样的对应关系。
  const assets = await loadTs('features/kokoro/assets.ts');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'vvt-kokoro-'));
  fs.symlinkSync(MODELS, path.join(root, assets.KOKORO_MODEL));

  const { env } = await import('@huggingface/transformers');
  env.allowRemoteModels = false;
  env.allowLocalModels = true;
  env.localModelPath = `${root}/`;

  const engine = await loadTs('features/kokoro/engine.ts', { external: ['kokoro-js', '@huggingface/transformers'] });
  const { phonemize } = await import('phonemizer');
  let tts = null;
  try {
    tts = await engine.loadKokoro('cpu');
    check('③ 真模型：engine.loadKokoro 只靠提交进库的文件就能加载（model_int8 + tokenizer）', true);
  } catch (e) {
    check('③ 真模型：engine.loadKokoro 只靠提交进库的文件就能加载', false, String(e?.message ?? e));
  }

  if (tts) {
    const vocab = catalog.KOKORO_VOCAB;
    const keep = (p) => [...p].filter((c) => vocab.has(c)).join('');
    const english = async (w) => keep((await phonemize(w, 'en-us')).join(' ').replace(/ʲ/g, 'j').replace(/r/g, 'ɹ').replace(/x/g, 'k').replace(/ɬ/g, 'l'));
    const stats = (audio) => {
      let max = 0, nan = 0, nz = 0;
      for (const v of audio) { if (Number.isNaN(v)) nan++; else { max = Math.max(max, Math.abs(v)); if (v !== 0) nz++; } }
      return { seconds: audio.length / 24000, max, nan, nonzero: nz / audio.length };
    };

    {
      const a = await tts.generate('Thirdweb is 3.5 times faster than v2.', { voice: 'af_heart' });
      const s = stats(a.audio);
      check('③ 真模型：英语合成出声（不是静音 / NaN）', s.seconds > 1 && s.max > 0.02 && s.nan === 0 && s.nonzero > 0.5, s);
    }
    for (const [label, sentence] of [
      ['中文夹英文品牌词', '我们打造了 Thirdweb，让开发者更轻松。'],
      ['中文带数字和百分号', '用户增长了 35.5%，一共有 100 个团队。'],
      ['中文带年份区间与金额', '2020-2024 年，价格从 $99 涨到 $120。'],
    ]) {
      const { phonemes } = await zh.zhG2PWith(sentence, english);
      const ph = keep(phonemes);
      const raw = [...phonemes].filter((c) => !vocab.has(c));
      check(`③ 真模型：${label} → 音素全在词表内`, raw.length === 0, { stray: raw.join(''), phonemes });
      const ids = tts.tokenizer(ph, { truncation: false }).input_ids;
      check(`③ 真模型：${label} → token 数在模型窗口内`, ids.dims.at(-1) > 0 && ids.dims.at(-1) < 511, ids.dims.at(-1));
      const a = await tts.generate_from_ids(tts.tokenizer(ph, { truncation: true, max_length: 511 }).input_ids, { voice: 'zf_xiaobei', speed: 1 });
      const s = stats(a.audio);
      check(`③ 真模型：${label} → 合成出声（改过图的 int8 模型在 CPU 上数值正常）`, s.seconds > 1.5 && s.max > 0.02 && s.nan === 0 && s.nonzero > 0.5, { ...s, phonemes });
    }
  }
}

console.log(failures === 0 ? '\n合计 全部通过' : `\n合计 ${failures} 项失败`);
// 不用 process.exit()：onnxruntime-node 的会话还活着时强制退出会在静态析构里崩溃（SIGABRT，退出码 134），
// 官方 int8 模型和我们的模型一样；让事件循环自然结束就是 0。
process.exitCode = failures === 0 ? 0 : 1;
