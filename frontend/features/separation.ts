/**
 * 初始化的第一步：在浏览器里做人声分离，把两条音轨上传给服务器。
 *
 * 从旧 `App.tsx` 的 `runClientSeparation` 抽出来，去掉了所有 React 状态：它只管
 * "做完 / 没做完"，进度通过回调往外说。调用方（初始化流程）把这些回调接到进度视图上。
 *
 * 失败不是致命的：分离只是让配音时背景音乐保留得更好；做不成就以"没有分离"继续，
 * 导出时会用原声垫底（并按包络压低）。所以这里**不抛**，返回 `{ok:false, reason}`。
 */
import * as api from '../net/client';
import { isModelCached, openAudioSource, separateAndUpload } from '../utils/mdx/separatorClient';

export interface SeparationCallbacks {
  onLog: (message: string) => void;
  /** 进度行：同一个 key 的行在界面上原地改写（下载 / 分离百分比）。 */
  onProgress: (key: 'model' | 'separate', text: string, fraction: number) => void;
}

export type SeparationOutcome = { ok: true } | { ok: false; reason: string };

export async function separateInBrowser(
  videoId: string,
  file: File | undefined,
  cb: SeparationCallbacks,
): Promise<SeparationOutcome> {
  try {
    const info = await api.getSeparatorInfo();
    if (!info?.available) throw new Error('服务器上没有可用的分离模型');

    const { token } = await api.getSeparatorToken(videoId);

    if (!(await isModelCached(info as never))) {
      cb.onLog(`下载分离模型（${info.size_mb} MB，之后会缓存）…`);
    }

    // 音频从哪来由 openAudioSource 按内存预算决定：短的本地文件直接在这里解码；
    // 长的 / 没有本地文件（刷新过页面）的从服务器的立体声 WAV 按窗口读，内存不随时长增长。
    const audio = await openAudioSource({
      file,
      onLog: cb.onLog,
      fetchServerSource: async () => {
        cb.onLog('向服务器取立体声分离源…');
        const response = await fetch(api.stereoUrl(videoId, token));
        if (!response.ok) throw new Error(`取不到分离源（HTTP ${response.status}）`);
        return response.blob();
      },
    });
    cb.onLog(`音频就绪：${audio.duration.toFixed(1)} 秒 @ ${audio.sampleRate} Hz`);

    await separateAndUpload({
      audio,
      info: info as never,
      token,
      onLog: cb.onLog,
      onProgress: (stage: string, payload: Record<string, number>) => {
        if (stage === 'model') {
          cb.onProgress('model', `模型下载 ${payload.percent.toFixed(0)}%（${(payload.loaded / 1048576).toFixed(1)}/${(payload.total / 1048576).toFixed(1)} MB）`, payload.percent / 100);
        } else if (stage === 'separate') {
          const fraction = payload.done / Math.max(1, payload.total);
          cb.onProgress('separate', `分离 ${Math.round(fraction * 100)}%（窗口 ${payload.window}/${payload.windows}）`, fraction);
        }
      },
      // 长视频分离耗时可能超过令牌有效期：上传前换一个新的
      upload: async (vocals: Blob, background: Blob) => {
        const fresh = await api.getSeparatorToken(videoId);
        return api.uploadStems(videoId, vocals, background, fresh.token);
      },
    });
    cb.onLog('人声分离完成');
    return { ok: true };
  } catch (e) {
    const reason = e instanceof Error ? e.message : String(e);
    cb.onLog(`人声分离失败（${reason}），将不分离继续`);
    return { ok: false, reason };
  }
}
