import { useEffect, useRef } from 'react';

export type DownloadSseEvent = { type: string; data: unknown };

export type DownloadStreamRow = {
  download_id: string;
  status: string;
  progress: number;
  error: string | null;
};

export function applyDownloadSseEvent<T extends DownloadStreamRow>(
  dl: T,
  event: DownloadSseEvent,
): T {
  switch (event.type) {
    case 'progress': {
      const pct = Number(event.data);
      if (!Number.isFinite(pct)) return dl;
      return {
        ...dl,
        progress: pct,
        status:
          dl.status === 'Paused'
            ? dl.status
            : pct > 0
              ? `Downloading ${pct}%`
              : dl.status,
      };
    }
    case 'status':
      return { ...dl, status: String(event.data) };
    case 'complete':
      return { ...dl, progress: 100, status: 'Completed' };
    case 'error':
      return { ...dl, error: String(event.data), status: 'Error' };
    default:
      return dl;
  }
}

/** Live download progress via SSE (replaces 1 Hz polling). */
export function useDownloadStreams(
  activeIds: string[],
  onEvent: (id: string, event: DownloadSseEvent) => void,
  onTerminal: (id: string) => void,
) {
  const sourcesRef = useRef<Map<string, EventSource>>(new Map());
  const onEventRef = useRef(onEvent);
  const onTerminalRef = useRef(onTerminal);
  onEventRef.current = onEvent;
  onTerminalRef.current = onTerminal;

  const idsKey = activeIds.slice().sort().join(',');

  useEffect(() => {
    const wanted = new Set(idsKey ? idsKey.split(',') : []);
    const sources = sourcesRef.current;

    for (const [id, es] of [...sources.entries()]) {
      if (!wanted.has(id)) {
        es.close();
        sources.delete(id);
      }
    }

    for (const id of wanted) {
      if (sources.has(id)) continue;
      const es = new EventSource(`/api/download/${encodeURIComponent(id)}/stream`);
      es.onmessage = (msg) => {
        try {
          const event = JSON.parse(msg.data) as DownloadSseEvent;
          onEventRef.current(id, event);
          if (event.type === 'complete' || event.type === 'error') {
            es.close();
            sources.delete(id);
            onTerminalRef.current(id);
          }
        } catch {
          /* malformed SSE payload */
        }
      };
      es.onerror = () => {
        es.close();
        sources.delete(id);
      };
      sources.set(id, es);
    }

    return () => {
      for (const es of sources.values()) es.close();
      sources.clear();
    };
  }, [idsKey]);
}
