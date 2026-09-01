import { memo } from 'react';
import PreviewChatPanel from './PreviewChatPanel';
import { usePreviewTime } from '../hooks/usePreviewTime';

/**
 * Thin wrapper over PreviewChatPanel that feeds it the ~4 Hz preview playhead
 * from the external store. The panel's own `currentTime` is read at fetch time
 * (never a fetch dependency), so re-rendering this wrapper per tick only
 * drifts its internal ref — App no longer rebuilds the whole preview overlay
 * per tick.
 *
 * ponytail: if the wrapper's own per-tick re-render (and the forwarded props
 * re-render of the panel) ever shows up in profiling, the panel could read
 * the store directly; this seam keeps the (unowned) panel untouched.
 */
const PreviewChatPanelTime = memo(function PreviewChatPanelTime(
  props: Omit<React.ComponentProps<typeof PreviewChatPanel>, 'currentTime'>,
) {
  const currentTime = usePreviewTime();
  return <PreviewChatPanel {...props} currentTime={currentTime} />;
});

export default PreviewChatPanelTime;