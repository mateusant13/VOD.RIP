import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle, RotateCcw } from 'lucide-react';
import { t } from '../i18n';
import { recordFrontendCrash } from '../crashLog';

/**
 * The app's crash boundary.
 *
 * WHY IT EXISTS: before this, `src/` had no `componentDidCatch`, no
 * `getDerivedStateFromError` and no `window.onerror`. In React 19 an exception
 * thrown while rendering unmounts the whole tree, so every crash reached the
 * user as a blank white screen with nothing on it — which is exactly why a
 * real crash (dragging a preview into a frame cell) stayed undiagnosed: there
 * was no error text anywhere to read, in the UI or in the console.
 *
 * WHAT IT DELIBERATELY DOES NOT DO:
 *  - It does not swallow. The error message, the component stack and the
 *    persisted report are all on screen. A boundary that rendered an empty
 *    div would be WORSE than the crash it replaced, because it converts a
 *    visible failure into an invisible one.
 *  - It does not invent a second logging path. The app's existing error sink
 *    (`GET /api/errors/latest`, backed by backend/services/error_log.py) is
 *    READ-ONLY — there is no client-writable endpoint, and adding one would
 *    mean touching backend/routers/, which this change does not own. So the
 *    report is persisted to one clearly-named localStorage key instead, and
 *    read back into the panel on the next load. See crashLog.ts.
 */
type Props = {
  children: ReactNode;
  /** Shown above the report so a nested boundary is identifiable. */
  label?: string;
};

type State = {
  error: Error | null;
  componentStack: string | null;
  /** Crash reports already on disk, newest first, so a reload keeps the story. */
  previous: { message: string; at: string }[];
};

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: null, previous: [] };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // `t` is a pure function of the module-level language, so it is safe to
    // call from a non-React method (see i18n.tsx).
    const message = error?.message ? String(error.message) : t('crash.unknownError');
    // Persist BEFORE rendering the fallback: if rendering the panel itself
    // fails, the report still exists on disk.
    const previous = recordFrontendCrash({
      message,
      name: error?.name || 'Error',
      stack: error?.stack ?? null,
      componentStack: info?.componentStack ?? null,
      label: this.props.label,
    });
    this.setState({ componentStack: info?.componentStack ?? null, previous });
  }

  private readonly handleReload = (): void => {
    window.location.reload();
  };

  private readonly handleDismiss = (): void => {
    this.setState({ error: null, componentStack: null, previous: [] });
  };

  render(): ReactNode {
    const { error, componentStack, previous } = this.state;
    if (!error) return this.props.children;

    return (
      <div
        data-crash-boundary=""
        role="alert"
        className="flex min-h-screen w-full flex-col items-start gap-3 bg-zinc-950 p-6 font-mono text-[10px] text-zinc-300"
      >
        <div className="flex items-center gap-1.5 text-red-400">
          <AlertTriangle size={14} className="shrink-0" aria-hidden />
          <h1 className="text-[11px] font-bold uppercase tracking-wide">
            {t('crash.title')}
          </h1>
        </div>

        <p className="max-w-prose text-zinc-400">
          {t('crash.body')}
        </p>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={this.handleReload}
            className="inline-flex items-center gap-1.5 border border-white bg-zinc-900 px-2 py-1 font-bold text-white hover:bg-zinc-800"
          >
            <RotateCcw size={12} aria-hidden />
            {t('crash.reload')}
          </button>
          <button
            type="button"
            onClick={this.handleDismiss}
            className="border border-zinc-700 px-2 py-1 font-bold text-zinc-500 hover:text-white"
          >
            {t('crash.dismiss')}
          </button>
        </div>

        <div className="flex w-full min-w-0 flex-col gap-2">
          <p className="uppercase text-zinc-500">{t('crash.errorLabel')}</p>
          {/* The message and the component stack are the whole point of this
              panel: without them a crash is a blank screen. */}
          <pre
            data-crash-message=""
            className="max-h-40 w-full overflow-auto whitespace-pre-wrap break-words border border-zinc-800 bg-zinc-900 p-2 text-red-300"
          >
            {`${error.name}: ${error.message}`}
          </pre>

          <p className="uppercase text-zinc-500">{t('crash.componentStackLabel')}</p>
          <pre
            data-crash-component-stack=""
            className="max-h-60 w-full overflow-auto whitespace-pre-wrap break-words border border-zinc-800 bg-zinc-900 p-2 text-amber-300"
          >
            {componentStack || t('crash.noComponentStack')}
          </pre>

          {previous.length > 1 && (
            <>
              <p className="uppercase text-zinc-500">{t('crash.previousLabel')}</p>
              <ul data-crash-previous="" className="flex w-full flex-col gap-1">
                {previous.slice(1).map((p, i) => (
                  <li key={`${p.at}-${i}`} className="break-words text-zinc-500">
                    <span className="text-zinc-600">{p.at}</span> {p.message}
                  </li>
                ))}
              </ul>
            </>
          )}

          <p className="text-zinc-600">{t('crash.persistedNote')}</p>
        </div>
      </div>
    );
  }
}
