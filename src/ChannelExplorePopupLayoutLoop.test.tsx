/** The SECOND render loop in ChannelExplorePopup, not the PanelResizeHandles one.
 *
 * THE CRASH THIS PINS. In the running app this threw
 *   "Maximum update depth exceeded"
 * with the stack ending at ChannelExplorePopup, and the owner hit it again on
 * 2026-10-06 after the PanelResizeHandles loop (pinned by
 * PanelResizeHandles.test.tsx) had already been fixed. So the first fix was
 * real and was not the whole defect: there were two cycles, and only one had
 * been closed.
 *
 * THE CHAIN, read off both ends rather than guessed:
 *   PreviewChatPanel computes `renderedW = open ? Math.min(width, widthCap) : 0`
 *     -> its useEffect (PreviewChatPanel.tsx:1211) reports
 *        onLayoutChange({ open, width: renderedW })
 *     -> the host passed the RAW setter, so every call stored a NEW object
 *     -> chatInfo -> chatTotal -> containerW -> layoutExplorePopupWindow
 *     -> setPos / setPanelWidth -> the maxWidth prop -> widthCap -> renderedW
 *
 * Every hop manufactures a fresh value, so the effect's deps change on every
 * pass and the cycle never closes. React bails out only when the STATE is
 * unchanged, and here it changes identity each time.
 *
 * THE FIX, and why it is not a weakening: the writer returns `prev` untouched
 * when the reported geometry is identical - the same guard this component
 * already used for setPos. A genuinely different width still lands, so layout
 * behaviour is unchanged; what stops is the identity churn.
 *
 * The mock reproduces the mechanism, not a mock of it: the panel reports the
 * SAME payload from a layout effect with NO dependency array, which is what an
 * oscillating measurement does. With the raw setter that never settles; with
 * the guard it settles on the first pass.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { useLayoutEffect } from "react";
import ChannelExplorePopup, { type ExplorePopupVod } from "./ChannelExplorePopup";

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof globalThis.ResizeObserver === "undefined") {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = ResizeObserverStub;
}

vi.mock("./components/PreviewChatPanel", async () => {
  const actual =
    await vi.importActual<typeof import("./components/PreviewChatPanel")>(
      "./components/PreviewChatPanel"
    );
  return {
    ...actual,
    default: (props: { onLayoutChange?: (i: { open: boolean; width: number }) => void }) => {
      // No dependency array: fires after EVERY render, reporting the SAME
      // geometry. This is the pathological measurement.
      useLayoutEffect(() => {
        props.onLayoutChange?.({ open: true, width: 320 });
      });
      return null;
    },
  };
});

const VOD: ExplorePopupVod = {
  url: "https://www.twitch.tv/videos/123456",
  title: "loop regression",
  platform: "twitch",
  durationSec: 3600,
  platformListIndex: 1,
  isClip: false,
  channel: "cellbit",
  videoId: "123456",
};

const SESSION = {
  session_id: "s1",
  master_url: "https://example.com/video.mp4",
  playback_url: "https://example.com/video.mp4",
  kind: "progressive",
  variant_heights: [],
  quality_labels: [],
  active_height: 720,
  duration_sec: 3600,
  trim_timeline: false,
  cached_progressive: true,
};

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/preview/session") {
        return new Response(JSON.stringify(SESSION), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    })
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("ChannelExplorePopup chat layout loop", () => {
  it("settles when the panel reports an unchanged width every render", async () => {
    stubFetch();
    // With the loop closed this renders and reaches its header. With the raw
    // setter it throws "Maximum update depth exceeded" during render().
    render(
      <ChannelExplorePopup
        id="popup-loop"
        vod={VOD}
        zIndex={100}
        stackIndex={0}
        onClose={() => {}}
        onHandoffToMain={() => {}}
        onRegisterPause={() => {}}
        onUnregisterPause={() => {}}
        onBringToFront={() => {}}
        onOpenHit={() => {}}
      />
    );
    await waitFor(() =>
      expect(screen.getByText("Channel VOD explore")).toBeInTheDocument()
    );
  });
});