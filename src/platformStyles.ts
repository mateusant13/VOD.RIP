/**
 * Platform-specific Tailwind CSS class generators.
 */

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
    return 'hover:bg-[#E03E3E] hover:text-white hover:border-[#E03E3E] hover:shadow-[4px_4px_0px_0px_#E03E3E]';
  }
  return 'hover:bg-white hover:text-black hover:border-white';
}

export function platformCardShadow(platform: PlatformStyleKey, compact = false): string {
  if (platform === 'kick') {
    return compact ? 'shadow-[4px_4px_0px_0px_#53fc18]' : 'shadow-[6px_6px_0px_0px_#53fc18]';
  }
  if (platform === 'twitch') {
    return compact ? 'shadow-[4px_4px_0px_0px_#9146FF]' : 'shadow-[6px_6px_0px_0px_#9146FF]';
  }
  if (platform === 'youtube') {
    return compact ? 'shadow-[4px_4px_0px_0px_#E03E3E]' : 'shadow-[6px_6px_0px_0px_#E03E3E]';
  }
  return compact
    ? 'shadow-[4px_4px_0px_0px_#53fc18,6px_6px_0px_0px_#9146FF]'
    : 'shadow-[6px_6px_0px_0px_#53fc18,12px_12px_0px_0px_#9146FF]';
}
