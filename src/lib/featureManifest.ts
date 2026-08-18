/** Feature manifest — SINGLE source of truth (mirror of backend/services/feature_registry.py). */
export type FeatureCost = 'heavy' | 'light';
export interface FeatureDef {
  id: string;
  cost: FeatureCost;
  defaultEnabled: boolean;
  description: string;
}

export const FEATURE_MANIFEST: FeatureDef[] = [
  { id: 'core-download', cost: 'light', defaultEnabled: true, description: 'Download videos and clips from Kick, Twitch and YouTube' },
  { id: 'transcribe-vod', cost: 'heavy', defaultEnabled: false, description: 'Automatically write out what was said in your saved videos so you can search them' },
  { id: 'live-captions', cost: 'heavy', defaultEnabled: false, description: 'Show subtitles live while a stream is happening' },
  { id: 'live-preview', cost: 'heavy', defaultEnabled: false, description: 'See at a glance who is live and open a preview instantly' },
  { id: 'chat-live', cost: 'light', defaultEnabled: true, description: 'Show live chat next to the video and save it with your download' },
];

export const FEATURE_IDS = FEATURE_MANIFEST.map(f => f.id) as string[];
export const HEAVY_IDS = new Set(FEATURE_MANIFEST.filter(f => f.cost === 'heavy').map(f => f.id));
export function isHeavy(id: string): boolean { return HEAVY_IDS.has(id); }
