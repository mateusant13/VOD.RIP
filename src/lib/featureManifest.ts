/** Feature manifest — SINGLE source of truth (mirror of backend/services/feature_registry.py). */
export type FeatureCost = 'heavy' | 'light';
export interface FeatureDef {
  id: string;
  cost: FeatureCost;
  defaultEnabled: boolean;
  description: string;
}

export const FEATURE_MANIFEST: FeatureDef[] = [
  { id: 'core-download', cost: 'light', defaultEnabled: true, description: 'Core VOD / clip downloads (yt-dlp + ffmpeg)' },
  { id: 'transcribe-vod', cost: 'heavy', defaultEnabled: false, description: 'VOD transcription (parakeet ASR, GPU/VRAM)' },
  { id: 'live-captions', cost: 'heavy', defaultEnabled: false, description: 'Live captions — real-time ASR for live streams' },
  { id: 'live-preview', cost: 'heavy', defaultEnabled: false, description: 'Live preview sessions & channel live-status warm' },
  { id: 'clipping', cost: 'light', defaultEnabled: true, description: 'Clipping & trim tools (timeline, clip editor)' },
  { id: 'chat-live', cost: 'light', defaultEnabled: true, description: 'Live chat capture & overlay for previews' },
];

export const FEATURE_IDS = FEATURE_MANIFEST.map(f => f.id) as string[];
export const HEAVY_IDS = new Set(FEATURE_MANIFEST.filter(f => f.cost === 'heavy').map(f => f.id));
export function isHeavy(id: string): boolean { return HEAVY_IDS.has(id); }
