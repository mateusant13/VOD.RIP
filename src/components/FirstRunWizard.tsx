import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  allOnPatch,
  coreOnlyPatch,
  enabledPlugins,
  setPluginEnabled,
  setPluginsBulk,
  usePlugins,
  type PluginState,
} from '../lib/plugins';
import { hasCompletedOnboarding, markOnboardingDone } from '../lib/featureFlags';
import { MODAL_Z } from '../layoutUtils';
import { useI18n } from '../i18n';

function CostBadge({ cost }: { cost: string }) {
  const { t } = useI18n();
  const heavy = cost === 'pesado';
  return (
    <span className={`text-[9px] font-mono px-1.5 py-0.5 border ${heavy ? 'border-amber-500 text-amber-400 bg-amber-950/30' : 'border-zinc-600 text-zinc-400'}`}>
      {t(heavy ? 'Uses more power' : 'Light')}
    </span>
  );
}

function PluginRow({
  plugin,
  onToggle,
}: {
  plugin: PluginState;
  onToggle: (id: string, enabled: boolean) => void;
}) {
  const { t } = useI18n();
  const core = !!plugin.core;
  return (
    <label className={`flex items-start gap-3 bg-zinc-800 border border-zinc-700 p-2 ${core ? 'opacity-90' : 'cursor-pointer'}`}>
      <input
        type="checkbox"
        checked={plugin.enabled}
        disabled={core}
        onChange={(e) => onToggle(plugin.id, e.target.checked)}
        className="mt-1"
        aria-label={plugin.name}
      />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-mono font-bold text-white">{plugin.name}</span>
          <CostBadge cost={plugin.cost} />
          {core ? (
            <span className="text-[9px] font-mono px-1.5 py-0.5 border border-emerald-600 text-emerald-300 bg-emerald-950/30">
              {t('Always on')}
            </span>
          ) : null}
        </div>
        <div className="text-[11px] font-mono text-zinc-400">{plugin.blurb}</div>
      </div>
    </label>
  );
}

/**
 * First-run plugin picker — replaces the old "BEM-VINDO AO VOD.RIP — ESCOLHA
 * COMO COMEÇAR" feature wizard. Same one-shot contract (localStorage
 * `vodrip.onboardingDone`), now choosing PLUGINS instead of features: the
 * download-by-link and channels plugins are core and stay locked on, every
 * extra tool is a row the user can switch off before the app ever shows it.
 */
export default function FirstRunWizard({ onDone }: { onDone?: () => void }) {
  const { t } = useI18n();
  const plugins = usePlugins();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!hasCompletedOnboarding()) setOpen(true);
  }, []);

  if (!open) return null;

  // Write-through: every choice lands in settings.json immediately, so an
  // install that never returns to this screen keeps the setup it picked.
  const toggle = (id: string, enabled: boolean) => { void setPluginEnabled(id, enabled); };
  const apply = (patch: Record<string, boolean>) => {
    void setPluginsBulk(patch);
    markOnboardingDone();
    setOpen(false);
    onDone?.();
  };

  // A preset is a full selection, not a seed: "SO O ESSENCIAL" = core only,
  // "TUDO" = everything on including the heavy ones.
  const selected = () => enabledPlugins();

  const body = (
    // MODAL_Z, not 9999. 9999 IS EXPLORE_POPUP_Z - the base every floating
    // player is stacked on - so an explore popup opened after the wizard
    // (EXPLORE_POPUP_Z + rank) painted ON TOP of a modal the user has to
    // answer. The wizard is app-level chrome and belongs above the whole
    // popup ladder.
    <div className="fixed inset-0 flex items-center justify-center bg-black/70 p-4"
      style={{ zIndex: MODAL_Z }}
    >
      <div className="w-full max-w-[560px] bg-zinc-900 border-2 border-zinc-700 p-5 flex flex-col gap-4 max-h-[90vh] overflow-auto">
        <h2 className="text-sm font-black uppercase tracking-widest text-white">{t('Welcome to VOD.RIP — choose your setup')}</h2>
        <p className="text-xs font-mono text-zinc-400">{t('Plugins are the extra tools. The heavy ones start off — turn them on whenever you need them, here or in Settings → Plugins.')}</p>
        <div className="flex gap-2">
          <button type="button" onClick={() => apply(coreOnlyPatch())} className="px-3 py-1.5 text-xs font-black uppercase border-2 border-zinc-600 bg-zinc-800 text-white hover:border-white">{t('Just the basics')}</button>
          <button type="button" onClick={() => apply(allOnPatch())} className="px-3 py-1.5 text-xs font-black uppercase border-2 border-zinc-600 bg-zinc-800 text-white hover:border-white">{t('Everything')}</button>
        </div>
        <div className="flex flex-col gap-2">
          {plugins.map((p) => (
            <PluginRow key={p.id} plugin={p} onToggle={toggle} />
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <button type="button" onClick={() => apply(selected())} className="px-4 py-2 text-xs font-black uppercase bg-white text-black hover:bg-zinc-200">
            {t('Save & continue')}
          </button>
        </div>
      </div>
    </div>
  );
  return createPortal(body, document.body);
}
