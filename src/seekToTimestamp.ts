/**
 * Shared click-to-seek dispatch for timestamped rows (chat history,
 * transcript segments, subtitles). Rows carry archive-relative offset_sec;
 * the host's seek function owns the CURRENT player (main preview or an
 * explore popup) and clamps to its own trim/window range. The row surfaces
 * never reach into the player directly — one helper, one contract.
 */
export function seekToTimestamp(offsetSec: number, seek: (sec: number) => void): void {
  if (!Number.isFinite(offsetSec)) return;
  seek(Math.max(0, offsetSec));
}
