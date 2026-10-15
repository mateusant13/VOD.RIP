/**
 * EnablePluginPrompt — the VISIBLE half of a plugin gate.
 *
 * The rule this exists for: when a plugin is off, a surface that needs it must
 * not simply vanish. The user never learns the feature exists nor how to turn
 * it on. A gate that would otherwise render `null` renders this instead: the
 * plugin's name, what it costs (disk / VRAM, read from the catalog MIRROR —
 * `src/lib/plugins.tsx` locally, `GET /api/plugins` when the API answers),
 * and one button that calls `setPluginEnabled(id, true)`.
 *
 * NOT for always-on ids: a feature that is not in the plugin catalog is not a
 * plugin and has nothing to enable, so this renders nothing for it. That also
 * keeps it correct under REGRA-NUCLEO, where `usePlugin(unknownId)` is TRUE
 * (an id that left the catalog is always on) — the first line already returns
 * null in that case.
 *
 * Reuse: `variant` picks the chrome (a card, or a one-line strip that sits
 * where a video overlay would), and `onDismiss` adds a close X when the owning
 * surface has a close of its own to forward.
 */
import { useState } from 'react';
import { X } from 'lucide-react';
import { useI18n } from '../i18n';
import { pluginCatalog, setPluginEnabled, usePlugin } from '../lib/plugins';

/**
 * The cost fields as they arrive from the mirror. Snake case is the backend
 * contract (`plugin_registry.PluginDef.to_dict`); the camel-case aliases only
 * tolerate a mirror that renamed them, so a real cost is never silently
 * dropped and a prompt never claims a plugin is free because of a spelling.
 */
interface CatalogCostView {
  name?: string;
  disk_mb?: number;
  vram_mb?: number;
  diskMb?: number;
  vramMb?: number;
}

function positive(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0;
}

/** MB → the shortest honest label (2048 → "2 GB", 397 → "397 MB"). */
export function formatPluginSize(mb: number): string {
  if (mb >= 1024) {
    const gb = mb / 1024;
    return `${Number.isInteger(gb) ? gb : gb.toFixed(1)} GB`;
  }
  return `${Math.round(mb)} MB`;
}

export interface EnablePluginPromptProps {
  /** Catalog id of the plugin the surface needs (e.g. 'transcribe'). */
  pluginId: string;
  /** 'block' (default) = a card; 'strip' = a compact one-line overlay. */
  variant?: 'block' | 'strip';
  /** When given, a dismiss X is rendered (the surface forwards its own close). */
  onDismiss?: () => void;
  className?: string;
}

export function EnablePluginPrompt({
  pluginId,
  variant = 'block',
  onDismiss,
  className = '',
}: EnablePluginPromptProps) {
  const { t } = useI18n();
  const on = usePlugin(pluginId);
  const [busy, setBusy] = useState(false);

  // Plugin on (or an id that is not a plugin at all — see the header): nothing
  // to ask for.
  if (on) return null;
  const def = pluginCatalog().find((p) => p.id === pluginId);
  if (!def) return null;

  const view = def as unknown as CatalogCostView;
  const name = view.name || pluginId;
  const disk = positive(view.disk_mb ?? view.diskMb);
  const vram = positive(view.vram_mb ?? view.vramMb);
  const costs = [
    disk > 0 ? t('plugins.cost.disk', { size: formatPluginSize(disk) }) : '',
    vram > 0 ? t('plugins.cost.vram', { size: formatPluginSize(vram) }) : '',
  ].filter(Boolean);
  const costText = costs.length > 0 ? costs.join(' · ') : t('plugins.cost.none');

  const enable = () => {
    setBusy(true);
    // Fire-and-forget by contract: setPluginEnabled applies locally (instant
    // UI) and pushes to the backend; a failure only desyncs settings.json.
    void setPluginEnabled(pluginId, true);
  };

  const enableButton = (
    <button
      type="button"
      onClick={enable}
      disabled={busy}
      data-enable-plugin-button={pluginId}
      className="border-2 border-white bg-white px-2.5 py-1 text-[10px] font-black uppercase text-black hover:bg-zinc-200 disabled:opacity-60"
    >
      {t('plugins.enable')}
    </button>
  );

  const dismiss = onDismiss ? (
    <button
      type="button"
      onClick={onDismiss}
      aria-label={t('Close')}
      className="absolute top-1 right-1 p-0.5 text-zinc-500 hover:text-white"
    >
      <X size={12} aria-hidden />
    </button>
  ) : null;

  if (variant === 'strip') {
    return (
      <div
        data-enable-plugin={pluginId}
        data-enable-plugin-variant="strip"
        className={`pointer-events-auto relative flex items-center gap-2.5 border-2 border-zinc-700 bg-zinc-950/95 px-2.5 py-1.5 ${className}`}
      >
        <div className="min-w-0">
          <div className="truncate text-[10px] font-black uppercase tracking-wide text-white">
            {t('plugins.prompt.title', { name })}
          </div>
          <div className="truncate font-mono text-[9px] text-zinc-400" data-plugin-cost>
            {costText}
          </div>
        </div>
        {enableButton}
        {dismiss}
      </div>
    );
  }

  return (
    <div
      data-enable-plugin={pluginId}
      data-enable-plugin-variant="block"
      className={`relative flex flex-col gap-1.5 border-2 border-zinc-700 bg-zinc-900 px-3 py-2.5 text-left ${className}`}
    >
      <div className="pr-5 text-[11px] font-black uppercase tracking-wide text-white">
        {t('plugins.prompt.title', { name })}
      </div>
      <div className="font-mono text-[10px] text-zinc-400">{t('plugins.prompt.body')}</div>
      <div className="font-mono text-[10px] text-zinc-500" data-plugin-cost>
        {costText}
      </div>
      <div className="mt-0.5 self-start">{enableButton}</div>
      {dismiss}
    </div>
  );
}

export default EnablePluginPrompt;
