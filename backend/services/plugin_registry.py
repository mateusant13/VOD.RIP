"""Plugin registry — single source of truth for the app's optional plugins.

Mirrored in src/lib/plugins.tsx (keep in sync; same contract shape as
services/feature_registry.py <-> src/lib/featureManifest.ts).

A plugin is an optional slice of the app the user can turn on/off (Settings →
Plugins, or the first-run wizard). The two CORE plugins — link download and the
channels section — are always on and cannot be disabled; the bonus plugins
ship core ON / light ON / heavy OFF (see the defaultOn rule at PLUGINS), so an
upgrade changes nothing an existing user sees and a new install reaches the
minimal experience in one wizard click.

Persistence (tolerant migration, by design):
  * one boolean per plugin, stored in settings.json under the plugin's
    settingsKey ("plugin_<id>") inside the "plugins" object;
  * a settings.json from an older build has NO "plugins" key at all — every
    plugin then resolves to defaultOn, EXCEPT the four that were features
    first (PLUGIN_FEATURE_ALIAS), which resolve to the stored FEATURE flag so
    a choice the user already made is never overridden by a default;
  * unknown/legacy keys inside "plugins" are ignored on read and kept on
    write (never destroyed — a renamed id must not lose the user's choice).

Storage lives on the AppSettings "plugins" field (models/schemas.py), NOT as
loose top-level settings.json keys: measured (tmp/measure_settings_extras.py),
a flat top-level key is silently DROPPED by AppSettings' strict pydantic model
on the next save of any kind — the toggle would revert itself. The dict is a
model field, so SettingsManager.save()'s three-way CAS merge protects it from
concurrent whole-object writers (same guarantee "features" has).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class PluginDef:
    id: str
    name: str          # pt-BR
    blurb: str         # 1 linha pt-BR: o que faz
    settingsKey: str   # chave booleana em settings.json (plugins[settingsKey])
    # ── O que ele CUSTA. Medido, nao inventado: ver as evidencias no comentario
    # de cada plugin abaixo. disk_mb e o download proprio; vram_mb/ram_mb sao o
    # pico enquanto roda. shares_with nomeia outro plugin cujo modelo este
    # reaproveita, para a UI nao somar o mesmo download duas vezes.
    disk_mb: int = 0
    vram_mb: int = 0
    ram_mb: int = 0
    shares_with: str = ""
    recommended: bool = False  # ordem no seletor: recomendados primeiro
    defaultOn: bool = False    # ligado por default numa instalacao nova

    def to_dict(self, enabled: Optional[bool] = None) -> Dict:
        """The public shape. `enabled` is filled by get_manifest() so a caller
        never has to re-resolve the map to render a row."""
        out = {
            "id": self.id,
            "name": self.name,
            "blurb": self.blurb,
            "settingsKey": self.settingsKey,
            # Cost is what makes this a plugin at all, so it is never omitted.
            "disk_mb": self.disk_mb,
            "vram_mb": self.vram_mb,
            "ram_mb": self.ram_mb,
            # Transcribe and live-subtitles run the SAME 397 MB of weights;
            # the UI must show one download, not two.
            "shares_with": self.shares_with,
            "recommended": self.recommended,
            "defaultOn": self.defaultOn,
        }
        if enabled is not None:
            out["enabled"] = bool(enabled)
        return out


# Catalog of everything ADDITIONAL to the essential experience (paste a link
# -> download; the CANAIS section). The ids/settingsKeys mirror
# src/lib/plugins.ts one-for-one — the frontend renders the mirror before the
# API answers, so a divergence here is a toggle the UI cannot find.
# Evidence = where each surface shows up in the UI (measured 2026-10-14):
#   link-download   src/App.tsx:6398 input "PASTE VOD OR CLIP LINK...", :6412
#                   extract button, tab bar src/App.tsx:7374-7388
#   channels        src/App.tsx:7431-7463 CHANNELS TAB, ChannelLinkCard :7446
#   transcribe      src/components/SettingsTab.tsx:409 TranscriptionSection
#                   (legacy feature 'transcribe-vod')
#   live-subtitles  src/components/LivePlayerPopup.tsx:2908 captions overlay
#                   (legacy feature 'live-captions')
#   live-preview    src/App.tsx:378 LiveBadge (legacy feature 'live-preview')
#   live-chat       src/components/LiveChatPanel.tsx:134 (LivePlayerPopup
#                   :2961), src/App.tsx:7238 PreviewChatPanelTime
#                   (legacy feature 'chat-live')
#   clip-editor     src/ChannelExplorePopup.tsx:1795 PreviewMiniEditor,
#                   src/App.tsx:6543 ClipDurationAdjustButtons
#   twitch-clip     src/App.tsx:8151-8176 TwitchClipPopup
#   archive-search  src/App.tsx:7365 "SEARCH CHAT" button -> :8180
#                   ArchiveSearchPopup
#   channel-explore src/App.tsx:8070 ChannelExplorePopup
#   local-file      src/App.tsx:8092-8100 LocalFilePopup
#   ai-ask          src/ChannelExplorePopup.tsx:413 POST /api/ai/ask
# ─────────────────────────────────────────────────────────────────────────────
# A PLUGIN IS SOMETHING HEAVY. If it costs no disk, no VRAM and no real RAM, it
# is not a plugin - it is part of the app and it is always on. That line is the
# owner's rule ("os plugins devem ser plugins apenas aqueles que sao pesados de
# algum jeito, tanto em disco, cpu, gpu, ram etc"), and it is why only four
# entries live here while eight features moved to ALWAYS_ON below.
#
# Every cost below is either MEASURED on this box or marked as an estimate.
# An invented number in a settings dialog is worse than no number: the user
# cannot tell which one to trust.
# ─────────────────────────────────────────────────────────────────────────────
PLUGINS: List[PluginDef] = [
    PluginDef(
        id="transcribe",
        name="Transcrição",
        blurb="Escreve o que foi dito nos VODs arquivados para você pesquisar depois.",
        settingsKey="plugin_transcribe",
        # MEASURED 2026-10-15: H:\VOD.RIP-models\parakeet-models\...-redux is
        # 397.2 MB on disk (encoder.onnx 327.9 + decoder 45.1 + joiner 24.1),
        # and that is the whole model - the 4-bit weights ARE the encoder.
        disk_mb=397,
        # MEASURED on this box's RTX 5080 (docs in photon_asr.py): 1.75 GB
        # resident after load, 2.26 GiB peak reserved. 2 GB is the ask.
        vram_mb=2048,
        ram_mb=2560,
        recommended=True,
    ),
    PluginDef(
        id="live-preview",
        name="Prévia ao vivo",
        blurb="Vê de relance quem está ao vivo e abre a prévia na hora.",
        settingsKey="plugin_live_preview",
        disk_mb=0,
        # ESTIMATE, not measured: one live video decode on the GPU plus its
        # network stream. No model is downloaded.
        vram_mb=400,
        ram_mb=600,
        recommended=True,
    ),
    PluginDef(
        id="live-subtitles",
        name="Legendas ao vivo",
        blurb="Mostra legendas enquanto a transmissão está acontecendo.",
        settingsKey="plugin_live_subtitles",
        # Same parakeet weights as transcribe: shares_with says so, and the
        # settings UI must not add 397 MB twice for a user who has both on.
        disk_mb=397,
        vram_mb=2048,
        ram_mb=1536,
        shares_with="transcribe",
    ),
    PluginDef(
        id="ai-ask",
        name="Perguntar à IA",
        blurb="Perguntas respondidas só com o arquivo local, usando sua chave de API.",
        settingsKey="plugin_ai_ask",
        # Nothing local: the model runs behind the user's own API key, so this
        # costs no disk and no VRAM. It is a plugin because it is a whole
        # feature with its own setup, not because it is heavy.
        disk_mb=0,
        vram_mb=0,
        ram_mb=128,
    ),
]


# ── ALWAYS ON: not plugins, because nothing here is heavy ───────────────────
# These used to be plugin rows with a toggle. Measured reason they are not:
# none of them downloads a model, owns a GPU slot or holds meaningful RAM.
# They are the app. Keeping a toggle for them was the bloat the owner asked to
# remove, and `usePlugin(id)` / `is_enabled(id)` both answer TRUE for any id
# that is not in PLUGINS above, so the existing gates stay correct without
# being rewired.
ALWAYS_ON: List[Dict] = [
    {"id": "link-download", "name": "Baixar por link",
     "blurb": "Cole um link de VOD ou clipe e baixe em vídeo, áudio ou trecho."},
    {"id": "channels", "name": "Canais",
     "blurb": "Acompanhe canais salvos, veja o que saiu novo e arquive VODs."},
    {"id": "channel-explore", "name": "Explorar canais",
     "blurb": "Abre o painel de um canal com VODs, clipes e ao-vivo."},
    {"id": "clip-editor", "name": "Editor de clipes",
     "blurb": "Corta um trecho do VOD e baixa só ele."},
    {"id": "twitch-clip", "name": "Clipes do Twitch",
     "blurb": "Cria um clipe do Twitch sem sair do app."},
    {"id": "archive-search", "name": "Busca no arquivo",
     "blurb": "Pesquisa transcrições e chat dos VODs já arquivados."},
    {"id": "local-file", "name": "Arquivo local",
     "blurb": "Assiste um arquivo de vídeo que já está no seu disco."},
    {"id": "live-chat", "name": "Chat ao vivo",
     "blurb": "Mostra o chat ao lado do vídeo e salva junto com o download."},
]

_BY_ID: Dict[str, PluginDef] = {p.id: p for p in PLUGINS}
# Plugins that existed as FEATURES first (services/feature_registry.py).
    # Tolerant migration: a settings.json that has no "plugins" key (every
    # existing install) resolves THESE four from the stored feature flag
    # instead of defaultOn — the owner's live-captions:false must not come
    # back as live-subtitles:true just because the manifest default says so.
    # Once a plugin toggle writes its own key, that key wins forever.
PLUGIN_FEATURE_ALIAS: Dict[str, str] = {
    "transcribe": "transcribe-vod",
    "live-subtitles": "live-captions",
    "live-preview": "live-preview",
    # "live-chat" was here while chat was a plugin. It is ALWAYS_ON now, so an
    # alias would migrate a feature flag into a settingsKey nothing reads -
    # the owner's chat-live choice is simply obeyed by the feature being on.
}
_BY_KEY: Dict[str, PluginDef] = {p.settingsKey: p for p in PLUGINS}
# defaultOn por settingsKey — o que uma instalação nova (settings.json sem a
# chave "plugins") deve responder.
_DEFAULT_BY_KEY: Dict[str, bool] = {p.settingsKey: p.defaultOn for p in PLUGINS}


def get_manifest() -> List[Dict]:
    """The plugin catalog, ORDERED for display.

    Order is the owner's rule ("os ligados/recomendados devem ficar em
    ordem"): enabled first, then recommended, then by cost ascending, then by
    id so the order is stable across runs rather than dict-insertion luck.
    """
    enabled = enabled_map()

    def key(p: PluginDef) -> tuple:
        return (not enabled.get(p.id, False), not p.recommended, p.disk_mb + p.vram_mb, p.id)

    return [p.to_dict(enabled=enabled.get(p.id, False)) for p in sorted(PLUGINS, key=key)]


def get_always_on() -> List[Dict]:
    """Features that are not plugins because none of them is heavy.

    The UI lists these so a user can see the app is not missing things, but
    they carry no toggle: there is nothing to switch off.
    """
    return [dict(a) for a in ALWAYS_ON]


def is_plugin(feature_id: str) -> bool:
    """True only for the heavy, toggleable entries."""
    return feature_id in _BY_ID


def cost_bundle(ids: List[str]) -> Dict[str, int]:
    """Total disk/VRAM/RAM for a SET of enabled plugin ids.

    Honours shares_with, because transcribe and live-subtitles run the SAME
    parakeet weights: enabling both must not report 397 MB twice.

    Charged at the OWNER, not at first sight. The previous version charged
    whoever came first in the list, so the answer depended on ORDER: with
    [live-subtitles, transcribe] the sharer paid, and then transcribe paid
    again because its own shares_with is empty. A cost that changes when you
    reorder a list is not a cost. This is order-independent by construction:
      * if the owner is enabled, it pays and the sharers do not;
      * if the owner is NOT enabled, a sharer brings its own copy, and the
        first sharer pays for it;
      * RAM is every plugin own peak, always, because a second consumer of
        the same model still costs its own working set.
    """
    want = {i for i in ids if i in _BY_ID}
    paid: set = set()
    out = {"disk_mb": 0, "vram_mb": 0, "ram_mb": 0}
    # Sorted, so the result is deterministic even if two sharers of one model
    # are both present with the owner missing.
    for pid in sorted(want):
        p = _BY_ID[pid]
        owner = p.shares_with or p.id
        if owner in want:
            # The owner is enabled and will pay; a sharer adds only its RAM.
            provides = p.id == owner and owner not in paid
        else:
            # Nobody owns the model in this selection, so this plugin carries
            # its own copy - counted once even if several sharers need it.
            provides = owner not in paid
        if provides:
            out["disk_mb"] += p.disk_mb
            out["vram_mb"] += p.vram_mb
            paid.add(owner)
        out["ram_mb"] += p.ram_mb
    return out

def defaults() -> Dict[str, bool]:
    """settingsKey -> defaultOn. What a fresh install resolves to."""
    return dict(_DEFAULT_BY_KEY)


def is_core(plugin_id: str) -> bool:
    """Always False, and that is the honest answer now.

    "Core" used to mean "a catalogued plugin that cannot be switched off".
    Nothing is both any more: the always-on features (link-download, channels,
    the eight in ALWAYS_ON) are not plugins, so there is no plugin a caller
    could be forbidden to disable. The function stays because a name that
    disappears breaks importers, and a caller asking "is this core?" deserves
    False rather than an AttributeError.
    """
    _ = _BY_ID.get(plugin_id)  # keep the lookup so an unknown id is not special
    return False


def _stored_features() -> Dict[str, bool]:
    """The persisted features dict (feature id -> bool) for the migration
    fallback below. Lazy import of deps, same as _stored_plugins."""
    try:
        from deps import settings_mgr

        s = settings_mgr.get()
        stored = getattr(s, "features", None)
        if isinstance(stored, dict):
            return {str(k): bool(v) for k, v in stored.items()}
    except Exception:
        pass
    return {}


def _stored_plugins() -> Dict[str, bool]:
    """The persisted plugins dict (settingsKey -> bool), unknown keys kept.

    Lazy import of deps (module-level would be a cycle: deps -> services.*).
    settings_mgr.get() is an in-memory model_copy under the manager lock —
    cheap and always fresh, so there is deliberately NO memo cache here
    (feature_registry needs one because it re-parses settings on a cold call;
    this reads the already-loaded model).
    """
    try:
        from deps import settings_mgr

        s = settings_mgr.get()
        stored = getattr(s, "plugins", None)
        if isinstance(stored, dict):
            return {str(k): bool(v) for k, v in stored.items()}
    except Exception:
        pass
    return {}


def enabled_map() -> Dict[str, bool]:
    """plugin id -> enabled, with tolerant migration.

    Resolution order per plugin:
      1. the plugin's own settingsKey (a toggle the user made) — wins always;
      2. the legacy FEATURE flag, for the four aliased plugins only — so an
         existing install keeps the choice it already made;
      3. defaultOn.
    Unknown/legacy keys in the stored dict are ignored here.
    """
    stored = _stored_plugins()
    features = _stored_features()
    out: Dict[str, bool] = {}
    for p in PLUGINS:
        if p.settingsKey in stored:
            out[p.id] = bool(stored[p.settingsKey])
            continue
        alias = PLUGIN_FEATURE_ALIAS.get(p.id)
        if alias and alias in features:
            # Migration: the feature flag the user already chose decides.
            out[p.id] = bool(features[alias])
            continue
        out[p.id] = p.defaultOn
    return out


def is_enabled(plugin_id: str) -> bool:
    """Is this feature usable?

    TRUE for anything that is not a plugin. ALWAYS_ON features (and every id a
    caller happens to invent) resolve here, which is what keeps the existing
    gates correct after eight features stopped being plugins: a surface wrapped
    in a now-meaningless id still renders instead of vanishing.
    """
    if plugin_id not in _BY_ID:
        return True
    return enabled_map().get(plugin_id, _BY_ID[plugin_id].defaultOn)


def set_plugin(plugin_id: str, enabled: bool) -> Dict[str, bool]:
    """Persist one plugin toggle; returns the new enabled_map (id -> bool).

    Raises ValueError for an unknown id and for disabling a core plugin —
    the routes map that to 400, the tests assert the guard.
    """
    plugin = _BY_ID.get(plugin_id)
    if plugin is None:
        raise ValueError(f"unknown plugin {plugin_id!r}")
    # No core guard: every entry in PLUGINS is a heavy add-on, and the things
    # that cannot be switched off (link-download, channels, ...) are not
    # plugins at all - they are in ALWAYS_ON and never reach this function.
    from deps import settings_mgr

    s = settings_mgr.get()
    current: Dict[str, bool] = dict(getattr(s, "plugins", None) or {})
    # Unknown keys ride along untouched (never destroy a renamed id's value).
    # The no-op check compares the EFFECTIVE value, not defaultOn: a plugin
    # migrating off a legacy feature flag resolves to that flag, and asking
    # for the defaultOn value is then a REAL change the user must see land.
    if bool(enabled_map().get(plugin_id, plugin.defaultOn)) == bool(enabled):
        return enabled_map()  # no-op: do not rewrite settings.json
    current[plugin.settingsKey] = bool(enabled)
    # model_copy carries _vodrip_base (the save() provenance snapshot), so the
    # CAS merge attributes this edit to us and a concurrent whole-object
    # writer cannot revert the plugins dict.
    settings_mgr.save(s.model_copy(update={"plugins": current}))
    return enabled_map()
