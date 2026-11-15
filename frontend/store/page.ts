/**
 * 当前该显示哪一页。纯函数，单独拿出来是为了能测"上传复用了一个已完成的工程"这类边角。
 *
 *   landing  没有打开的工程
 *   loading  打开了工程，但视图还没取回来
 *   init     初始化页（步骤清单 + 终端日志）
 *   editor   编辑器
 *
 * `starting` / `preparing` 为真时，即使服务端说 `ready` 也要停在初始化页：
 * 上传按 MD5 复用时，这个工程可能是个已经完成的老工程；浏览器端人声分离（几分钟）和
 * `setup` 都发生在服务端把阶段改成 `initializing` 之前，那一段时间里服务端视图是旧的
 * `ready`，不能因此闪出一个编辑器。
 */
import type { Stage } from '../net/types';

export type Page = 'landing' | 'loading' | 'init' | 'editor';

export function pageFor(a: {
  videoId: string | null;
  stage: Stage | null;       // null = 视图还没取回来
  starting: boolean;
  preparing: boolean;
}): Page {
  if (!a.videoId) return 'landing';
  if (a.stage === null) return 'loading';
  if (a.stage !== 'ready' || a.starting || a.preparing) return 'init';
  return 'editor';
}
