
export interface Speaker {
  id: string;
  name: string;
  color: string;
}

export interface TranscriptionSegment {
  id: string;
  speakerId: string;
  startTime: number;
  endTime: number;
  originalText: string;
  translatedText: string;
  audioUrl?: string;
  actualDuration?: number;
  isTranslating?: boolean;
  isSynthesizing?: boolean;
  status: 'pending' | 'ready' | 'error';
}
