/**
 * Turn on pitch preservation explicitly.（本文件剩下的唯一内容）
 *
 * 速率数学（computeSyncRates 等）已删除：那是混音策略的一部分，唯一实现在
 * 后端 mix_service，前端只消费 plan。留一份本地实现 = 回到"预览与成片各一套"。

 *
 * Browsers default `preservesPitch` to true, so this is usually a no-op — but
 * the whole approach depends on the stretch not shifting pitch, and older
 * WebKit only honoured the prefixed spelling. Relying on a default for
 * something this load-bearing is not worth it.
 */
export function setPreservesPitch(audio: HTMLAudioElement, enabled = true): void {
  audio.preservesPitch = enabled;
  // Prefixed spellings for older engines.
  const legacy = audio as HTMLAudioElement & {
    webkitPreservesPitch?: boolean;
    mozPreservesPitch?: boolean;
  };
  if ('webkitPreservesPitch' in legacy) legacy.webkitPreservesPitch = enabled;
  if ('mozPreservesPitch' in legacy) legacy.mozPreservesPitch = enabled;
}
