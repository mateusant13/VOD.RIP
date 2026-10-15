import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  ALWAYS_ON,
  allOnPatch,
  bundleCost,
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

/** Custo legível, na regra do dono: abaixo de 1024 MB mostra MB inteiro; a
 *  partir disso, GB com 1 casa (2048 -> "2.0 GB", 397 -> "397 MB"). */
function formatMb(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

/** O custo de UM plugin. Quando `shares_with` nomeia outro plugin JÁ LIGADO o
 *  modelo é o mesmo: repetir o número faria o dono somar 397 MB duas vezes, e a
 *  linha vira "incluso (mesmo modelo)". */
function PluginRow({
  plugin,
  enabledIds,
  onToggle,
}: {
  plugin: PluginState;
  enabledIds: Set<string>;
  onToggle: (id: string, enabled: boolean) => void;
}) {
  const { t } = useI18n();
  const shared = !!plugin.shares_with && enabledIds.has(plugin.shares_with);
  const cost = shared
    ? t('included (same model)')
    : `${t('{size} on disk', { size: formatMb(plugin.disk_mb ?? 0) })} · ${t('{size} of VRAM', { size: formatMb(plugin.vram_mb ?? 0) })}`;
  return (
    <label className="flex items-start gap-3 bg-zinc-800 border border-zinc-700 p-2 cursor-pointer">
      <input
        type="checkbox"
        checked={plugin.enabled}
        onChange={(e) => onToggle(plugin.id, e.target.checked)}
        className="mt-1"
        aria-label={plugin.name}
      />
      <div className="flex-1 min-w-0">
        <div className="text-xs font-mono font-bold text-white">{plugin.name}</div>
        <div className="text-[11px] font-mono text-zinc-400">{plugin.blurb}</div>
        <div
          className={`text-[10px] font-mono ${shared ? 'text-emerald-400' : 'text-zinc-500'}`}
          data-testid={`wizard-cost-${plugin.id}`}
        >
          {cost}
        </div>
      </div>
    </label>
  );
}

/**
 * First-run plugin picker — replaces the old "BEM-VINDO AO VOD.RIP — ESCOLHA
 * COMO COMEÇAR" feature wizard. Same one-shot contract (localStorage
 * `vodrip.onboardingDone`), now choosing PLUGINS instead of features: only the
 * four heavy tools carry a toggle, every one of them starts off, and the eight
 * light surfaces are listed under "Sempre ligado" because they are the app.
 *
 * Every row also shows what it would cost in disk and VRAM, and the selection
 * total is live, so "Tudo" is an informed choice rather than a blind one.
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

  // A preset is a full selection, not a seed: "SO O ESSENCIAL" = every plugin
  // off (the always-on app stays), "TUDO" = everything on, heavy included.
  const selected = () => enabledPlugins();

  const enabledIds = new Set(plugins.filter((p) => p.enabled).map((p) => p.id));
  const totals = bundleCost(enabledIds);
  const totalText = `${t('{size} on disk', { size: formatMb(totals.disk_mb) })} · ${t('{size} of VRAM', { size: formatMb(totals.vram_mb) })}`;

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
            <PluginRow key={p.id} plugin={p} enabledIds={enabledIds} onToggle={toggle} />
          ))}
        </div>
        <div className="flex items-center justify-between gap-2" data-testid="wizard-totals">
          <span className="text-[10px] font-black uppercase tracking-wide text-zinc-500">{t('Selected total')}</span>
          <span className="text-[10px] font-mono text-zinc-300 tabular-nums">{totalText}</span>
        </div>
        {/* As 8 superfícies que são o app: sem toggle, e é por isso que "Só o
            essencial" ainda entrega tudo o que não é ferramenta pesada. */}
        <div className="pt-2 border-t-2 border-zinc-800" data-testid="wizard-always-on">
          <div className="text-[10px] font-black uppercase tracking-wide text-zinc-500">{t('Always on')}</div>
          <div className="flex flex-wrap gap-1.5 mt-1.5">
            {ALWAYS_ON.map(a => (
              <span key={a.id} title={a.blurb} className="text-[10px] font-mono px-1.5 py-0.5 border border-zinc-700 text-zinc-400">
                {a.name}
              </span>
            ))}
          </div>
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
