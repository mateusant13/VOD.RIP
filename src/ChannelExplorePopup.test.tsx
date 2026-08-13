import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ChannelExplorePopup, { type ExplorePopupVod } from "./ChannelExplorePopup";

// jsdom has no ResizeObserver; the popup uses it for row-height pinning.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof globalThis.ResizeObserver === "undefined") {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = ResizeObserverStub;
}

const VOD: ExplorePopupVod = {
  url: "https://www.twitch.tv/videos/123456",
  title: "Builds de xerath",
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

function stubFetch(opts: {
  aiEnabled?: boolean;
  askResponse?: unknown;
  askStatus?: number;
} = {}) {
  const { aiEnabled = false, askResponse = null, askStatus = 200 } = opts;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/settings") {
        return new Response(
          JSON.stringify({ experimental_ai_enabled: aiEnabled, ai_api_key_set: aiEnabled }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url === "/api/preview/session") {
        return new Response(JSON.stringify(SESSION), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/ai/ask" && init?.method === "POST") {
        return new Response(JSON.stringify(askResponse), {
          status: askStatus,
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

function renderPopup() {
  return render(
    <ChannelExplorePopup
      id="popup-1"
      vod={VOD}
      zIndex={100}
      stackIndex={0}
      volumeMenuCloseTick={0}
      onClose={() => {}}
      onHandoffToMain={() => {}}
      onRegisterPause={() => {}}
      onUnregisterPause={() => {}}
      onVolumeMenuOpen={() => {}}
      onBringToFront={() => {}}
      onOpenHit={() => {}}
    />
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("ChannelExplorePopup AI ask", () => {
  it("hides the ask row when the experimental AI toggle is off", async () => {
    stubFetch({ aiEnabled: false });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(screen.queryByText("Ask about this channel")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("ai ask question")).not.toBeInTheDocument();
  });

  it("shows the ask row when enabled and posts the question to /api/ai/ask", async () => {
    stubFetch({ aiEnabled: true });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Ask about this channel")).toBeInTheDocument());
    // Collapsed by default: inputs hidden until expanded.
    expect(screen.queryByLabelText("ai ask question")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Ask about this channel"));
    expect(screen.getByLabelText("ai ask question")).toBeInTheDocument();
    expect(screen.getByLabelText("ai ask scope")).toHaveValue("all");
    expect(screen.getByLabelText("ai ask days")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("ai ask question"), {
      target: { value: "quantas vezes ele recomendou a build do xerath?" },
    });
    fireEvent.change(screen.getByLabelText("ai ask scope"), { target: { value: "chat" } });
    fireEvent.change(screen.getByLabelText("ai ask days"), { target: { value: "7" } });
    fireEvent.click(screen.getByText("Ask"));

    await waitFor(() => {
      const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
      const ask = calls.find(
        ([url, init]) => String(url) === "/api/ai/ask" && (init as RequestInit)?.method === "POST"
      );
      expect(ask).toBeTruthy();
      expect(JSON.parse(String((ask![1] as RequestInit).body))).toEqual({
        channel: "cellbit",
        platform: "twitch",
        question: "quantas vezes ele recomendou a build do xerath?",
        scope: "chat",
        days: 7,
      });
    });
  });

  it("renders the answer with cited sources", async () => {
    stubFetch({
      aiEnabled: true,
      askResponse: {
        answer: "Ele recomendou a build do xerath 2 vezes.",
        sources: [
          { video_title: "Recomendação de builds 01", created_at: "2026-08-12T10:00:00Z", matched_text: "recomendei a build do xerath" },
          { video_title: "Builds da semana 02", created_at: "2026-08-11T10:00:00Z", matched_text: "build do xerath de novo" },
        ],
      },
    });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Ask about this channel")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Ask about this channel"));
    fireEvent.change(screen.getByLabelText("ai ask question"), {
      target: { value: "quantas vezes?" },
    });
    fireEvent.click(screen.getByText("Ask"));

    expect(await screen.findByText("Ele recomendou a build do xerath 2 vezes.")).toBeInTheDocument();
    expect(screen.getByText("Sources")).toBeInTheDocument();
    expect(screen.getByText(/Recomendação de builds 01 · 2026-08-12/)).toBeInTheDocument();
    expect(screen.getByText(/Builds da semana 02 · 2026-08-11/)).toBeInTheDocument();
  });

  it("surfaces backend errors (e.g. disabled feature) in the answer box", async () => {
    stubFetch({ aiEnabled: true, askStatus: 403, askResponse: { detail: "Experimental AI is disabled" } });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Ask about this channel")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Ask about this channel"));
    fireEvent.change(screen.getByLabelText("ai ask question"), { target: { value: "quantas vezes?" } });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Experimental AI is disabled/);
  });

  it("hides the ask row when the popup has no channel slug", async () => {
    stubFetch({ aiEnabled: true });
    render(
      <ChannelExplorePopup
        id="popup-2"
        vod={{ ...VOD, channel: undefined }}
        zIndex={100}
        stackIndex={0}
        volumeMenuCloseTick={0}
        onClose={() => {}}
        onHandoffToMain={() => {}}
        onRegisterPause={() => {}}
        onUnregisterPause={() => {}}
        onVolumeMenuOpen={() => {}}
        onBringToFront={() => {}}
        onOpenHit={() => {}}
      />
    );
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(screen.queryByText("Ask about this channel")).not.toBeInTheDocument();
  });
});
