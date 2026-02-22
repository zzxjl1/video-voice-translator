
function computePeaks(channelData: Float32Array, points: number): number[] {
  const step = Math.floor(channelData.length / points);
  if (step <= 0) return new Array(points).fill(0.1);

  const peaks: number[] = [];

  for (let i = 0; i < points; i++) {
    let max = 0;
    const offset = i * step;
    for (let j = 0; j < step; j++) {
      const idx = offset + j;
      if (idx >= channelData.length) break;
      const val = Math.abs(channelData[idx]);
      if (val > max) max = val;
    }
    peaks.push(max);
  }

  // Normalize peaks to 0-1 range so waveform always fills the timeline
  const maxPeak = Math.max(...peaks);
  if (maxPeak > 0) {
    for (let i = 0; i < peaks.length; i++) {
      peaks[i] = peaks[i] / maxPeak;
    }
  }

  return peaks;
}

export const getAudioWaveform = async (file: File, points: number = 200): Promise<number[]> => {
  const audioContext = new (window.AudioContext || (window as any).webkitAudioContext)();
  const arrayBuffer = await file.arrayBuffer();

  try {
    const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);
    const channelData = audioBuffer.getChannelData(0);
    return computePeaks(channelData, points);
  } catch (e) {
    console.error("Audio processing failed:", e);
    return new Array(points).fill(0.1);
  } finally {
    audioContext.close();
  }
};

export const getAudioWaveformFromUrl = async (url: string, points: number = 200): Promise<number[]> => {
  const audioContext = new (window.AudioContext || (window as any).webkitAudioContext)();

  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Failed to fetch audio: ${response.status}`);
    const arrayBuffer = await response.arrayBuffer();
    const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);
    const channelData = audioBuffer.getChannelData(0);
    return computePeaks(channelData, points);
  } catch (e) {
    console.error("Audio waveform from URL failed:", e);
    return new Array(points).fill(0.1);
  } finally {
    audioContext.close();
  }
};
