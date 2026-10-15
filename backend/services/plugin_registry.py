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
from typing import Dict, List


@dataclass(frozen=True)
class PluginDef:
    id: str
    name: str          # pt-BR
    blurb: str         # 1 linha pt-BR: o que faz
    cost: str          # "leve" | "pesado"
    settingsKey: str   # chave booleana em settings.json (plugins[settingsKey])
    core: bool = False        # sempre ligado; nao pode desligar
    defaultOn: bool = False   # ligado por default numa instalacao nova

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "blurb": self.blurb,
            "cost": self.cost,
            "core": self.core,
            "defaultOn": self.defaultOn,
            "settingsKey": self.settingsKey,
        }


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
PLUGINS: List[PluginDef] = [
    # ── CORE: a experiencia essencial. Sempre ligado. ───────────────────────
    PluginDef(
        id="link-download",
        name="Baixar por link",
        blurb="Cole um link de VOD ou clipe e baixe em vídeo, áudio ou trecho.",
        cost="leve",
        settingsKey="plugin_link_download",
        core=True,
        defaultOn=True,
    ),
    PluginDef(
        id="channels",
        name="Canais",
        blurb="Acompanhe canais salvos, veja o que saiu novo e arquive VODs.",
        cost="leve",
        settingsKey="plugin_channels",
        core=True,
        defaultOn=True,
    ),
    # ── BONUS: liga/desliga no wizard ou em Settings → Plugins. ──────────────
    # defaultOn rule — core ON, LIGHT bonus ON, HEAVY bonus OFF. Measured
    # against this box's live settings.json (2026-10-14): the owner's own
    # features are {core-download:true, chat-live:true, clipping:true,
    # transcribe-vod:false, live-captions:false, live-preview:false} — i.e.
    # exactly light-on/heavy-off, and the wizard's own copy promises "the
    # heavy ones start off". An upgrade therefore changes nothing he uses,
    # and a NEW install still reaches the minimal experience in one click:
    # the first-run wizard opens on "SO O ESSENCIAL" (core only), with
    # "TUDO" turning everything on.
    PluginDef(
        id="transcribe",
        name="Transcrição",
        blurb="Escreve o que foi dito nos VODs arquivados para você pesquisar depois.",
        cost="pesado",
        settingsKey="plugin_transcribe",
    ),
    PluginDef(
        id="live-subtitles",
        name="Legendas ao vivo",
        blurb="Mostra legendas enquanto a transmissão está acontecendo.",
        cost="pesado",
        settingsKey="plugin_live_subtitles",
        defaultOn=False,
    ),
    PluginDef(
        id="live-preview",
        name="Prévia ao vivo",
        blurb="Vê de relance quem está ao vivo e abre a prévia na hora.",
        cost="pesado",
        settingsKey="plugin_live_preview",
    ),
    PluginDef(
        id="live-chat",
        name="Chat ao vivo",
        blurb="Mostra o chat ao lado do vídeo e salva junto com o download.",
        cost="leve",
        settingsKey="plugin_live_chat",
        defaultOn=True,
    ),
    PluginDef(
        id="clip-editor",
        name="Editor de clipes",
        blurb="Corta um trecho do VOD e baixa só ele.",
        cost="leve",
        settingsKey="plugin_clip_editor",
        defaultOn=True,
    ),
    PluginDef(
        id="twitch-clip",
        name="Clipes do Twitch",
        blurb="Cria um clipe do Twitch sem sair do app.",
        cost="leve",
        settingsKey="plugin_twitch_clip",
        defaultOn=True,
    ),
    PluginDef(
        id="archive-search",
        name="Busca no arquivo",
        blurb="Pesquisa transcrições e chat dos VODs já arquivados.",
        cost="leve",
        settingsKey="plugin_archive_search",
        defaultOn=True,
    ),
    PluginDef(
        id="channel-explore",
        name="Explorar canais",
        blurb="Abre o painel de um canal com VODs, clipes e ao-vivo.",
        cost="leve",
        settingsKey="plugin_channel_explore",
        defaultOn=True,
    ),
    PluginDef(
        id="local-file",
        name="Arquivo local",
        blurb="Assiste um arquivo de vídeo que já está no seu disco.",
        cost="leve",
        settingsKey="plugin_local_file",
        defaultOn=True,
    ),
    PluginDef(
        id="ai-ask",
        name="Perguntar à IA",
        blurb="Perguntas respondidas só com o arquivo local, usando sua chave de API.",
        cost="pesado",
        settingsKey="plugin_ai_ask",
    ),
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
    "live-chat": "chat-live",
}
_BY_KEY: Dict[str, PluginDef] = {p.settingsKey: p for p in PLUGINS}
# defaultOn por settingsKey — o que uma instalação nova (settings.json sem a
# chave "plugins") deve responder.
_DEFAULT_BY_KEY: Dict[str, bool] = {p.settingsKey: p.defaultOn for p in PLUGINS}


def get_manifest() -> List[Dict]:
    """Catalog as plain dicts (id/name/blurb/cost/core/defaultOn/settingsKey)."""
    return [p.to_dict() for p in PLUGINS]


def defaults() -> Dict[str, bool]:
    """settingsKey -> defaultOn. What a fresh install resolves to."""
    return dict(_DEFAULT_BY_KEY)


def is_core(plugin_id: str) -> bool:
    p = _BY_ID.get(plugin_id)
    return bool(p and p.core)


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
    if plugin_id not in _BY_ID:
        return False
    return enabled_map().get(plugin_id, _BY_ID[plugin_id].defaultOn)


def set_plugin(plugin_id: str, enabled: bool) -> Dict[str, bool]:
    """Persist one plugin toggle; returns the new enabled_map (id -> bool).

    Raises ValueError for an unknown id and for disabling a core plugin —
    the routes map that to 400, the tests assert the guard.
    """
    plugin = _BY_ID.get(plugin_id)
    if plugin is None:
        raise ValueError(f"unknown plugin {plugin_id!r}")
    if plugin.core and not enabled:
        raise ValueError(f"plugin {plugin_id!r} é core e não pode ser desligado")
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
