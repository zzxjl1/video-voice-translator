
export const formatTime = (seconds: number): string => {
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.floor(seconds % 60);
  return `${minutes.toString().padStart(2, '0')}:${remainingSeconds.toString().padStart(2, '0')}`;
};

/**
 * Decodes a base64 string into a Uint8Array.
 * Following @google/genai manual implementation guidelines.
 */
export function decodeBase64(base64: string) {
  const binaryString = atob(base64);
  const len = binaryString.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    bytes[i] = binaryString.charCodeAt(i);
  }
  return bytes;
}

/**
 * Wraps raw 16-bit PCM mono 24kHz audio data in a WAV header 
 * so it can be played by the browser's HTMLAudioElement.
 */
export const createWavUrlFromPcm = (base64Pcm: string): string => {
  const pcmData = decodeBase64(base64Pcm);
  const dataLength = pcmData.length;
  const sampleRate = 24000;
  const numChannels = 1;
  const bitsPerSample = 16;
  
  const header = new Uint8Array(44);
  const view = new DataView(header.buffer);

  // RIFF identifier
  view.setUint32(0, 0x52494646, false); // "RIFF"
  // File length
  view.setUint32(4, 36 + dataLength, true);
  // RIFF type
  view.setUint32(8, 0x57415645, false); // "WAVE"
  // Format chunk identifier
  view.setUint32(12, 0x666d7420, false); // "fmt "
  // Format chunk length
  view.setUint32(16, 16, true);
  // Sample format (1 is PCM)
  view.setUint16(20, 1, true);
  // Channel count
  view.setUint16(22, numChannels, true);
  // Sample rate
  view.setUint32(24, sampleRate, true);
  // Byte rate (sampleRate * numChannels * bitsPerSample/8)
  view.setUint32(28, sampleRate * numChannels * (bitsPerSample / 8), true);
  // Block align (numChannels * bitsPerSample/8)
  view.setUint16(32, numChannels * (bitsPerSample / 8), true);
  // Bits per sample
  view.setUint16(34, bitsPerSample, true);
  // Data chunk identifier
  view.setUint32(36, 0x64617461, false); // "data"
  // Data chunk length
  view.setUint32(40, dataLength, true);

  const wavFile = new Uint8Array(header.length + pcmData.length);
  wavFile.set(header);
  wavFile.set(pcmData, header.length);
  
  const blob = new Blob([wavFile], { type: 'audio/wav' });
  return URL.createObjectURL(blob);
};
