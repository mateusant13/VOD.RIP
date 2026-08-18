import { FEATURE_MANIFEST, FEATURE_IDS } from './featureManifest';

const LS_FEATURES = 'vodrip.features';
const LS_ONBOARDED = 'vodrip.onboardingDone';

function defaults(): Record<string, boolean> {
  const m: Record<string, boolean> = {};
  for (const f of FEATURE_MANIFEST) m[f.id] = f.defaultEnabled;
  return m;
}

function readLocal(): Record<string, boolean> | null {
  try {
    const raw = localStorage.getItem(LS_FEATURES);
    if (!raw) return null;
    const v = JSON.parse(raw);
    if (v && typeof v === 'object') return v as Record<string, boolean>;
  } catch { /* ignore */ }
  return null;
}

export function getEnabledFeatures(): Record<string, boolean> {
  const d = defaults();
  const stored = readLocal();
  if (!stored) return d;
  const out: Record<string, boolean> = { ...d };
  for (const id of FEATURE_IDS) if (id in stored) out[id] = !!stored[id];
  return out;
}

export function isEnabled(id: string): boolean {
  return !!getEnabledFeatures()[id];
}

export function setFeature(id: string, enabled: boolean): void {
  const cur = getEnabledFeatures();
  cur[id] = !!enabled;
  try { localStorage.setItem(LS_FEATURES, JSON.stringify(cur)); } catch { /* ignore */ }
  // sync to backend (fire-and-forget)
  try {
    fetch('/api/settings/features', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ features: cur }),
    }).catch(() => {});
  } catch { /* ignore */ }
}

export function setFeaturesBulk(patch: Record<string, boolean>): void {
  const cur = getEnabledFeatures();
  for (const [k, v] of Object.entries(patch)) if (FEATURE_IDS.includes(k)) cur[k] = !!v;
  try { localStorage.setItem(LS_FEATURES, JSON.stringify(cur)); } catch { /* ignore */ }
  try {
    fetch('/api/settings/features', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ features: cur }),
    }).catch(() => {});
  } catch { /* ignore */ }
}

export function hasCompletedOnboarding(): boolean {
  try { return localStorage.getItem(LS_ONBOARDED) === '1'; } catch { return false; }
}
export function markOnboardingDone(): void {
  try { localStorage.setItem(LS_ONBOARDED, '1'); } catch { /* ignore */ }
}
export function resetOnboarding(): void {
  try { localStorage.removeItem(LS_ONBOARDED); } catch { /* ignore */ }
}

/** Pull backend truth into localStorage (call on App mount). */
export async function syncFromBackend(): Promise<void> {
  try {
    const r = await fetch('/api/settings/features');
    if (!r.ok) return;
    const j = await r.json() as { features?: Record<string, boolean> };
    if (j.features && typeof j.features === 'object') {
      const before = JSON.stringify(getEnabledFeatures());
      const cur = getEnabledFeatures();
      for (const id of FEATURE_IDS) if (id in j.features) cur[id] = !!j.features[id];
      const after = JSON.stringify(cur);
      if (after !== before) {
        try { localStorage.setItem(LS_FEATURES, after); } catch { /* ignore */ }
        try { const { notifyFeatureChange } = await import('../hooks/useFeature'); notifyFeatureChange(); } catch { /* ignore */ }
      }
    }
    const b = j as unknown as { onboardingDone?: boolean };
    if (b.onboardingDone) markOnboardingDone();
  } catch { /* ignore */ }
}
