
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
  /** 该行静音：不参与配音，导出时这一格留白。 */
  muted?: boolean;
  isTranslating?: boolean;
  isSynthesizing?: boolean;
  status: 'pending' | 'ready' | 'error';
}
