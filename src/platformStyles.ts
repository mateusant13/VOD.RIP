/**
 * Platform-specific Tailwind CSS class generators.
 */

export type PlatformStyleKey = 'kick' | 'twitch' | 'youtube' | null;

export function actionBtnHover(platform: PlatformStyleKey): string {
  if (platform === 'kick') {
    return 'hover:bg-[#53fc18] hover:text-black hover:border-[#53fc18] hover:shadow-[4px_4px_0px_0px_#53fc18]';
  }
  if (platform === 'twitch') {
    return 'hover:bg-[#9146FF] hover:text-black hover:border-[#9146FF] hover:shadow-[4px_4px_0px_0px_#9146FF]';
  }
  if (platform === 'youtube') {
    return 'hover:bg-[#F03030] hover:text-white hover:border-[#F03030] hover:shadow-[4px_4px_0px_0px_#F03030]';
  }
  return 'hover:bg-white hover:text-black hover:border-white';
}

/** Offset shadow for bordered action buttons (fullscreen, etc.). */
export function platformButtonShadow(platform: PlatformStyleKey): string {
  if (platform === 'kick') return 'shadow-[2px_2px_0px_0px_#53fc18]';
  if (platform === 'twitch') return 'shadow-[2px_2px_0px_0px_#9146FF]';
  if (platform === 'youtube') return 'shadow-[2px_2px_0px_0px_#F03030]';
  return 'shadow-[2px_2px_0px_0px_#53fc18,4px_4px_0px_0px_#9146FF,6px_6px_0px_0px_#F03030]';
}

/** Press-in hover — 1px shift matches 2px→1px shadow so the control stays inside its box. */
export function platformButtonPressHover(platform: PlatformStyleKey): string {
  if (platform === 'kick') {
    return 'shadow-[2px_2px_0px_0px_#53fc18] hover:shadow-[1px_1px_0px_0px_#53fc18] hover:translate-x-px hover:translate-y-px';
  }
  if (platform === 'twitch') {
    return 'shadow-[2px_2px_0px_0px_#9146FF] hover:shadow-[1px_1px_0px_0px_#9146FF] hover:translate-x-px hover:translate-y-px';
  }
  if (platform === 'youtube') {
    return 'shadow-[2px_2px_0px_0px_#F03030] hover:shadow-[1px_1px_0px_0px_#F03030] hover:translate-x-px hover:translate-y-px';
  }
  return 'shadow-[2px_2px_0px_0px_#52525b] hover:shadow-[1px_1px_0px_0px_#52525b] hover:translate-x-px hover:translate-y-px';
}

/** Press-in hover for the main download CTA — shadow only (no translate: avoids scrollbar flicker). */
export function platformDownloadBtn(platform: PlatformStyleKey): string {
  const base = 'w-full h-full min-h-[2.25rem] border-2 border-white bg-black flex items-center justify-center gap-2 text-xs font-black uppercase transition-[box-shadow,background-color,color] duration-150 hover:bg-white hover:text-black disabled:opacity-40 disabled:cursor-not-allowed';
  if (platform === 'kick') {
    return `${base} shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18]`;
  }
  if (platform === 'twitch') {
    return `${base} shadow-[3px_3px_0px_0px_#9146FF] hover:shadow-[2px_2px_0px_0px_#9146FF]`;
  }
  if (platform === 'youtube') {
    return `${base} shadow-[3px_3px_0px_0px_#F03030] hover:shadow-[2px_2px_0px_0px_#F03030]`;
  }
  return `${base} shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18]`;
}

/** Selected-VOD panel actions — same look as VOD rip; active = inverted. */
export function platformVodPanelBtn(platform: PlatformStyleKey, active = false): string {
  const base = platformDownloadBtn(platform);
  if (!active) return base;
  return `${base} !bg-white !text-black shadow-[2px_2px_0px_0px_rgba(255,255,255,0.35)] hover:!bg-black hover:!text-white`;
}

/** Open URL / Watch preview — platform colors from VOD rip, mono label typography. */
export function platformVodPanelSecondaryBtn(platform: PlatformStyleKey, active = false): string {
  const base =
    'w-full h-full min-h-[2.25rem] border-2 font-mono uppercase font-bold text-[10px] flex items-center justify-center gap-1.5 transition-[box-shadow,background-color,color] duration-150 hover:bg-white hover:text-black disabled:opacity-40 disabled:cursor-not-allowed border-white bg-black';
  if (active) {
    return `${base} !bg-white !text-black shadow-[2px_2px_0px_0px_rgba(255,255,255,0.35)] hover:!bg-black hover:!text-white`;
  }
  if (platform === 'kick') {
    return `${base} shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18]`;
  }
  if (platform === 'twitch') {
    return `${base} shadow-[3px_3px_0px_0px_#9146FF] hover:shadow-[2px_2px_0px_0px_#9146FF]`;
  }
  if (platform === 'youtube') {
    return `${base} shadow-[3px_3px_0px_0px_#F03030] hover:shadow-[2px_2px_0px_0px_#F03030]`;
  }
  return `${base} shadow-[3px_3px_0px_0px_#53fc18] hover:shadow-[2px_2px_0px_0px_#53fc18]`;
}

export function platformWatchPreviewBtn(platform: PlatformStyleKey, active: boolean): string {
  return platformVodPanelSecondaryBtn(platform, active);
}

/** Preview player transport buttons — platform accent when docked, glass when fullscreen overlay. */
/** Compact bulk-download CTA for channel multi-select. */
export function platformBulkDownloadBtn(platform: PlatformStyleKey, multiPlatform: boolean): string {
  const base = 'px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider flex items-center gap-1 border-2 transition-[transform,box-shadow,background-color,color] duration-150';
  if (multiPlatform || !platform) {
    return `${base} border-white text-white bg-zinc-900/90 shadow-[2px_2px_0px_0px_#53fc18,3px_3px_0px_0px_#9146FF,4px_4px_0px_0px_#F03030] hover:translate-x-px hover:translate-y-px`;
  }
  if (platform === 'kick') {
    return `${base} border-[#53fc18]/80 text-[#53fc18] bg-[#53fc18]/10 hover:bg-[#53fc18]/20 ${platformButtonPressHover('kick')}`;
  }
  if (platform === 'twitch') {
    return `${base} border-[#9146FF]/80 text-[#9146FF] bg-[#9146FF]/10 hover:bg-[#9146FF]/20 ${platformButtonPressHover('twitch')}`;
  }
  return `${base} border-[#F03030]/80 text-[#F03030] bg-[#F03030]/12 hover:bg-[#F03030]/22 ${platformButtonPressHover('youtube')}`;
}

export function platformPreviewCtrlBtn(
  platform: PlatformStyleKey,
  fsOverlay: boolean,
  large = false,
): string {
  const pad = large ? 'p-2' : 'p-1.5';
  if (fsOverlay) {
    return `border border-white/20 bg-black/20 text-zinc-100/90 hover:bg-black/35 hover:border-white/50 ${pad} disabled:opacity-30 backdrop-blur-[1px]`;
  }
  return `border-2 border-white bg-black text-white hover:bg-white hover:text-black ${pad} disabled:opacity-40 ${platformButtonShadow(platform)}`;
}

export function platformCardShadow(platform: PlatformStyleKey, compact = false): string {
  if (platform === 'kick') {
    return compact ? 'shadow-[4px_4px_0px_0px_#53fc18]' : 'shadow-[6px_6px_0px_0px_#53fc18]';
  }
  if (platform === 'twitch') {
    return compact ? 'shadow-[4px_4px_0px_0px_#9146FF]' : 'shadow-[6px_6px_0px_0px_#9146FF]';
  }
  if (platform === 'youtube') {
    return compact ? 'shadow-[4px_4px_0px_0px_#F03030]' : 'shadow-[6px_6px_0px_0px_#F03030]';
  }
  return compact
    ? 'shadow-[4px_4px_0px_0px_#53fc18,6px_6px_0px_0px_#9146FF,8px_8px_0px_0px_#F03030]'
    : 'shadow-[6px_6px_0px_0px_#53fc18,12px_12px_0px_0px_#9146FF,18px_18px_0px_0px_#F03030]';
}
