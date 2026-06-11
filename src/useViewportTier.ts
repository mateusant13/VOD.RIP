import { useSyncExternalStore } from 'react';
import type { ViewportTier } from './uiScale';

function getTier(): ViewportTier {
  const tier = document.documentElement.dataset.viewport;
  if (tier === 'narrow' || tier === 'wide') return tier;
  return 'normal';
}

export function useViewportTier(): ViewportTier {
  return useSyncExternalStore(
    (onStoreChange) => {
      window.addEventListener('resize', onStoreChange);
      return () => window.removeEventListener('resize', onStoreChange);
    },
    getTier,
    () => 'normal',
  );
}
