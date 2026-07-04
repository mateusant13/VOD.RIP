/**
 * Platform-specific Tailwind CSS class generators.
 */

export function actionBtnHover(platform: 'kick' | 'twitch' | null): string {
  if (platform === 'kick') {
    return 'hover:bg-[#53fc18] hover:text-black hover:border-[#53fc18] hover:shadow-[4px_4px_0px_0px_#53fc18]';
  }
  if (platform === 'twitch') {
    return 'hover:bg-[#9146FF] hover:text-black hover:border-[#9146FF] hover:shadow-[4px_4px_0px_0px_#9146FF]';
  }
  return 'hover:bg-white hover:text-black hover:border-white';
}

export function platformCardShadow(platform: 'kick' | 'twitch' | null, compact = false): string {
  if (platform === 'kick') {
    return compact ? 'shadow-[4px_4px_0px_0px_#53fc18]' : 'shadow-[6px_6px_0px_0px_#53fc18]';
  }
  if (platform === 'twitch') {
    return compact ? 'shadow-[4px_4px_0px_0px_#9146FF]' : 'shadow-[6px_6px_0px_0px_#9146FF]';
  }
  return compact
    ? 'shadow-[4px_4px_0px_0px_#53fc18,6px_6px_0px_0px_#9146FF]'
    : 'shadow-[6px_6px_0px_0px_#53fc18,12px_12px_0px_0px_#9146FF]';
}
