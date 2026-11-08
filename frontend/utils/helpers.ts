
export const formatTime = (seconds: number): string => {
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.floor(seconds % 60);
  return `${minutes.toString().padStart(2, '0')}:${remainingSeconds.toString().padStart(2, '0')}`;
};

const SPEAKER_COLORS = [
  '#2563eb', // blue
  '#16a34a', // green
  '#d97706', // amber
  '#dc2626', // red
  '#9333ea', // purple
  '#0891b2', // cyan
];

export const getSpeakerColor = (id: string): string => {
  if (!id) return '#9ca3af';
  let hash = 0;
  for (let i = 0; i < id.length; i++) {
    hash = id.charCodeAt(i) + ((hash << 5) - hash);
  }
  return SPEAKER_COLORS[Math.abs(hash) % SPEAKER_COLORS.length];
};

/**
 * 触发浏览器下载一个 URL 或 Blob。
 *
 * `download` 属性很重要：没有它浏览器可能直接导航，或者从 URL 里取文件名，丢掉后端选的扩展名
 * （样式化导出是 .mkv，不是 .mp4）。Blob 的情况是浏览器端烧录——文件在这个标签页里生成，
 * 没有服务器地址。合成一个 helper 是为了锚点和 revoke 只写在一处。
 */
export function downloadFile(source: string | Blob, filename: string): void {
  const isBlob = typeof source !== 'string';
  const url = isBlob ? URL.createObjectURL(source) : source;
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  if (isBlob) {
    // 足够久：点击一定已处理完；又足够短：不让整段视频在内存里挂一整个会话。
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}
