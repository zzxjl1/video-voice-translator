/**
 * 会话过期的统一通知。
 *
 * 任何接口返回 401 都说明登录态没了（过期、被撤销、在别处登出）。net 层不依赖 React，所以这里是一个
 * 极小的发布订阅：`client.ts` / `events.ts` 发现 401 就 `notifyUnauthorized()`，`AuthProvider`
 * 订阅后把 `user` 置空，界面自然回到登录页，而不是一个个接口各自报"重连中"或"请求失败"。
 */
type Listener = () => void;
const listeners = new Set<Listener>();

export function onUnauthorized(fn: Listener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export function notifyUnauthorized(): void {
  for (const fn of [...listeners]) fn();
}
