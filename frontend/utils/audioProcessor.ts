
export const getAudioWaveform = async (file: File, points: number = 200): Promise<number[]> => {
  const audioContext = new (window.AudioContext || (window as any).webkitAudioContext)();
  const arrayBuffer = await file.arrayBuffer();
  
  try {
    const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);
    const channelData = audioBuffer.getChannelData(0); // Use first channel
    const step = Math.floor(channelData.length / points);
    const peaks: number[] = [];
    
    for (let i = 0; i < points; i++) {
      let max = 0;
      for (let j = 0; j < step; j++) {
        const val = Math.abs(channelData[i * step + j]);
        if (val > max) max = val;
      }
      peaks.push(max);
    }
    return peaks;
  } catch (e) {
    console.error("Audio processing failed:", e);
    return new Array(points).fill(0.1);
  } finally {
    audioContext.close();
  }
};
