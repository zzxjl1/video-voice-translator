
export interface Speaker {
  id: string;
  name: string;
  color: string;
  voice: string; 
}

export interface TranscriptionSegment {
  id: string;
  speakerId: string;
  startTime: number;
  endTime: number;
  originalText: string;
  translatedText: string;
  audioUrl?: string;
  isTranslating?: boolean;
  isSynthesizing?: boolean;
  status: 'pending' | 'ready' | 'error';
  ttsSource: 'gemini' | 'browser';
}

export enum GeminiVoice {
    Kore = 'Kore',
    Puck = 'Puck',
    Zephyr = 'Zephyr',
    Charon = 'Charon',
    Fenrir = 'Fenrir',
}

export interface ModelSettings {
  transcriptionModel: string;
  translationModel: string;
  ttsModel: string;
}

export const DEFAULT_MODEL_SETTINGS: ModelSettings = {
  transcriptionModel: 'gemini-2.0-flash',
  translationModel: 'gemini-2.0-flash',
  ttsModel: 'gemini-2.5-flash-preview-tts',
};
