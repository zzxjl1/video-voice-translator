/**
 * AI 消息的正文：几种有限的格式 + 可点击的行号。
 *
 * 不引入 markdown 库：AI 只会用到粗体、行内代码、列表、引用、换行，写个几十行的解析比
 * 带一整个库（还要处理 XSS 白名单）更可控。输出的全是 React 节点，**从不**用
 * dangerouslySetInnerHTML —— 模型的输出是不可信文本。
 *
 * 行号：「第 12 句」「#12」「第 3、5、8 句」都会变成可点击标签，点了定位到那一句。
 * 超出范围的数字（比如“第 99 句”而只有 10 句）保持纯文本，不给一个点了没反应的假链接。
 */
import React from 'react';

interface Props {
  text: string;
  lineCount: number;
  onFocusLine: (line: number) => void;
}

type Node = React.ReactNode;

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|第\s*\d+(?:\s*[、,，~-]\s*\d+)*\s*句|#\d+)/g;

function lineChip(n: number, label: string, key: string, lineCount: number, onFocusLine: (n: number) => void): Node {
  if (n < 1 || n > lineCount) return label;
  return (
    <button
      key={key}
      onClick={() => onFocusLine(n)}
      className="inline-flex items-center px-1.5 py-px mx-px rounded-md bg-claude-accent/10 text-claude-accent font-bold text-[12px] hover:bg-claude-accent/20 transition-colors align-baseline"
      title={`定位到第 ${n} 句`}
    >
      {label}
    </button>
  );
}

function inline(text: string, lineCount: number, onFocusLine: (n: number) => void, keyBase: string): Node[] {
  const out: Node[] = [];
  let last = 0;
  let i = 0;
  for (const match of text.matchAll(INLINE)) {
    const start = match.index ?? 0;
    if (start > last) out.push(text.slice(last, start));
    const token = match[0];
    const key = `${keyBase}-${i++}`;
    if (token.startsWith('**')) {
      out.push(<strong key={key} className="font-bold">{token.slice(2, -2)}</strong>);
    } else if (token.startsWith('`')) {
      out.push(<code key={key} className="px-1 py-px rounded bg-claude-paper text-[12px] font-mono">{token.slice(1, -1)}</code>);
    } else if (token.startsWith('#')) {
      out.push(lineChip(Number(token.slice(1)), token, key, lineCount, onFocusLine));
    } else {
      // 「第 3、5、8 句」：每个数字各自一个标签，句式保留
      const nums = token.match(/\d+/g) ?? [];
      if (nums.length === 1) {
        out.push(lineChip(Number(nums[0]), token, key, lineCount, onFocusLine));
      } else {
        const parts = token.split(/(\d+)/);
        out.push(
          <React.Fragment key={key}>
            {parts.map((p, j) => (/^\d+$/.test(p) ? lineChip(Number(p), p, `${key}-${j}`, lineCount, onFocusLine) : p))}
          </React.Fragment>,
        );
      }
    }
    last = start + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export const RichText: React.FC<Props> = ({ text, lineCount, onFocusLine }) => {
  const blocks: Node[] = [];
  const lines = text.split('\n');
  let list: string[] = [];
  let quote: string[] = [];
  let k = 0;

  const flushList = () => {
    if (!list.length) return;
    const items = list;
    list = [];
    const key = `l${k++}`;
    blocks.push(
      <ul key={key} className="my-1.5 space-y-1 pl-4 list-disc marker:text-gray-300">
        {items.map((it, i) => <li key={i}>{inline(it, lineCount, onFocusLine, `${key}-${i}`)}</li>)}
      </ul>,
    );
  };
  const flushQuote = () => {
    if (!quote.length) return;
    const q = quote.join('\n');
    quote = [];
    const key = `q${k++}`;
    blocks.push(
      <blockquote key={key} className="my-1.5 pl-3 border-l-2 border-claude-accent/40 text-gray-600 whitespace-pre-wrap">
        {inline(q, lineCount, onFocusLine, key)}
      </blockquote>,
    );
  };

  for (const raw of lines) {
    const bullet = raw.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    const quoted = raw.match(/^\s*>\s?(.*)$/);
    if (bullet) { flushQuote(); list.push(bullet[1]); continue; }
    if (quoted) { flushList(); quote.push(quoted[1]); continue; }
    flushList(); flushQuote();
    if (!raw.trim()) { blocks.push(<div key={`s${k++}`} className="h-2" />); continue; }
    const key = `p${k++}`;
    blocks.push(<p key={key}>{inline(raw, lineCount, onFocusLine, key)}</p>);
  }
  flushList(); flushQuote();
  return <div className="space-y-0.5 break-words">{blocks}</div>;
};
