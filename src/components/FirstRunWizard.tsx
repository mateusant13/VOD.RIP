import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { FEATURE_MANIFEST } from '../lib/featureManifest';
import { getEnabledFeatures, hasCompletedOnboarding, markOnboardingDone, setFeaturesBulk } from '../lib/featureFlags';
import { notifyFeatureChange } from '../hooks/useFeature';
import { useI18n } from '../i18n';

const LABELS: Record<string, string> = {
  'core-download': 'Downloads',
  'transcribe-vod': 'Transcripts for saved videos',
  'live-captions': 'Live subtitles',
  'live-preview': 'Live previews',
  'chat-live': 'Live chat',
};

function CostBadge({ cost }: { cost: string }) {
  const { t } = useI18n();
  const heavy = cost === 'heavy';
  return (
    <span className={`text-[9px] font-mono px-1.5 py-0.5 border ${heavy ? 'border-amber-500 text-amber-400 bg-amber-950/30' : 'border-zinc-600 text-zinc-400'}`}>
      {t(heavy ? 'Uses more power' : 'Light')}
    </span>
  );
}

export default function FirstRunWizard({ onDone }: { onDone?: () => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState<Record<string, boolean>>(() => getEnabledFeatures());

  useEffect(() => {
    if (!hasCompletedOnboarding()) setOpen(true);
  }, []);

  if (!open) return null;

  const apply = (next: Record<string, boolean>) => {
    setFeaturesBulk(next);
    notifyFeatureChange();
    markOnboardingDone();
    setOpen(false);
    onDone?.();
  };

  const presetCore = () => {
    const n: Record<string, boolean> = {};
    for (const f of FEATURE_MANIFEST) n[f.id] = f.cost === 'light';
    n['transcribe-vod'] = false;
    n['live-captions'] = false;
    n['live-preview'] = false;
    setSel(n);
  };
  const presetFull = () => {
    const n: Record<string, boolean> = {};
    for (const f of FEATURE_MANIFEST) n[f.id] = true;
    setSel(n);
  };

  const body = (
    <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/70 p-4">
      <div className="w-full max-w-[560px] bg-zinc-900 border-2 border-zinc-700 p-5 flex flex-col gap-4 max-h-[90vh] overflow-auto">
        <h2 className="text-sm font-black uppercase tracking-widest text-white">{t('Welcome to VOD.RIP — choose your setup')}</h2>
        <p className="text-xs font-mono text-zinc-400">{t('Some extra tools use more power and are off at first. You can turn them on anytime in Settings → Features.')}</p>
        <div className="flex gap-2">
          <button type="button" onClick={presetCore} className="px-3 py-1.5 text-xs font-black uppercase border-2 border-zinc-600 bg-zinc-800 text-white hover:border-white">{t('Just the basics')}</button>
          <button type="button" onClick={presetFull} className="px-3 py-1.5 text-xs font-black uppercase border-2 border-zinc-600 bg-zinc-800 text-white hover:border-white">{t('Everything')}</button>
        </div>
        <div className="flex flex-col gap-2">
          {FEATURE_MANIFEST.map(f => (
            <label key={f.id} className="flex items-start gap-3 bg-zinc-800 border border-zinc-700 p-2 cursor-pointer">
              <input type="checkbox" checked={!!sel[f.id]} onChange={e => setSel(s => ({ ...s, [f.id]: e.target.checked }))} className="mt-1" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-mono font-bold text-white">{t(LABELS[f.id] ?? f.id)}</span>
                  <CostBadge cost={f.cost} />
                </div>
                <div className="text-[11px] font-mono text-zinc-400">{t(f.description)}</div>
              </div>
            </label>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <button type="button" onClick={() => apply(sel)} className="px-4 py-2 text-xs font-black uppercase bg-white text-black hover:bg-zinc-200">
            {t('Save & continue')}
          </button>
        </div>
      </div>
    </div>
  );
  return createPortal(body, document.body);
}
