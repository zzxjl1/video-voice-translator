/**
 * AI 回复的正文：交给 `streamdown`（专为"边流边渲染"做的 Markdown 渲染器）。
 *
 * 为什么不再自己解析：模型会输出表格、任务列表、代码块、数学式，手写子集永远差一块
 * （表格就是这么丢的）。streamdown 自带 GFM（表格/任务列表/删除线）、对**没写完**的
 * Markdown 有容错（流式输出时半截的表格/代码块不会整段消失），并且用 rehype-harden +
 * rehype-sanitize 做白名单，比手写解析更安全。
 *
 * `mode="streaming"` 时不缓存已解析的块：内容每来一段都要重新解析，缓存反而会让
 * 半截的块卡在旧状态。静态消息用默认模式。
 */
import React from 'react';
import { Streamdown } from 'streamdown';

interface Props {
  text: string;
  /** 这一轮还在输出（streamdown 据此对未闭合的语法做容错处理）。 */
  streaming?: boolean;
}

const Markdown: React.FC<Props> = ({ text, streaming = false }) => (
  <Streamdown
    mode={streaming ? 'streaming' : 'static'}
    isAnimating={streaming}
    className="text-[13px] leading-relaxed [&_table]:my-2"
  >
    {text}
  </Streamdown>
);

export default Markdown;
