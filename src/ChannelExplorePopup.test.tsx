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

/**
 * ONE GET /api/settings on mount, not two.
 *
 * These were two independent useEffects, each firing its own
 * GET /api/settings. Measured 2026-10-14 by counting in-flight /api/ requests
 * at the instant the preview session POST left the browser:
 *
 *   rep1  [/panel?limit=20000, /chat/emotes, /settings, /settings, /timing] = 5
 *   rep2  [/session/cookies/status, /panel?limit=20000, /settings, /settings, /timing] = 5
 *   rep3-5 [/panel?limit=20000, /settings, /settings, /timing] = 4
 *
 * A browser allows 6 connections per HTTP/1.1 origin, and the session POST was
 * the 6th — it sat in the queue behind /api/preview/panel instead of going out.
 * The app own timing: session_created server_ms=172, session_ready open_ms=10470.
 *
 * RED before the fix: this test failed with 2 calls.
 */
it("fetches /api/settings once on mount, not once per flag", () => {
  stubFetch({});
  renderPopup();
  const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
    .map((c) => String(c[0]))
    .filter((u) => u === "/api/settings");
  expect(calls.length).toBe(1);
});

function renderPopup() {
  return render(
    <ChannelExplorePopup
      id="popup-1"
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
}

beforeEach(() => {
  vi.unstubAllGlobals();
  // The plugin store (src/lib/plugins.tsx) keeps its per-plugin booleans in
  // localStorage under 'vodrip.plugins'. Clearing it here keeps one test's
  // plugin choice from leaking into the next one.
  localStorage.clear();
});

/** localStorage seed for the popup's gates, everything switched ON. Only
 *  'ai-ask' is still a real plugin; 'channel-explore', 'clip-editor' and
 *  'live-chat' are ALWAYS_ON, and the tests below store them false to prove
 *  that a stored false no longer gates them. */
const ALL_PLUGINS_ON: Record<string, boolean> = {
  "channel-explore": true,
  "clip-editor": true,
  "ai-ask": true,
  "live-chat": true,
};

/** Write the plugin store's localStorage copy before rendering. Ids missing
 *  from the patch fall back to their catalog defaultOn. */
function setPlugins(patch: Record<string, boolean>) {
  localStorage.setItem("vodrip.plugins", JSON.stringify(patch));
}

describe("ChannelExplorePopup AI ask", () => {
  // The row needs BOTH gates: the ai-ask plugin ON and the backend
  // experimental_ai_enabled setting ON.
  beforeEach(() => {
    setPlugins({ ...ALL_PLUGINS_ON });
  });

  it("hides the ask row when the experimental AI toggle is off", async () => {
    stubFetch({ aiEnabled: false });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(screen.queryByText("Ask about this channel")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("ai ask question")).not.toBeInTheDocument();
  });

  // The owner: "botoes que necessitam de plugins pra fazer algo, deve aparecer
  // uma mensagem pedindo pra habilitar o plugin". With ai-ask OFF the panel used
  // to vanish and the user never learned the feature existed or how to get it.
  it("an ai-ask OFF shows the enable prompt instead of hiding the feature", async () => {
    setPlugins({ ...ALL_PLUGINS_ON, "ai-ask": false });
    stubFetch({ aiEnabled: true }); // the OTHER gate is satisfied on purpose
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    // the prompt is present and its Enable button targets ai-ask
    expect(document.querySelector('[data-enable-plugin="ai-ask"]')).not.toBeNull();
    expect(
      document.querySelector('[data-enable-plugin-button="ai-ask"]'),
    ).not.toBeNull();
    // and the real row is not merely hidden: it is not rendered
    expect(screen.queryByText("Ask about this channel")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("ai ask question")).not.toBeInTheDocument();
  });

  it("an ai-ask ON shows the row and NO prompt", async () => {
    stubFetch({ aiEnabled: true });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Ask about this channel")).toBeInTheDocument());
    expect(document.querySelector('[data-enable-plugin="ai-ask"]')).toBeNull();
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
        onClose={() => {}}
        onHandoffToMain={() => {}}
        onRegisterPause={() => {}}
        onUnregisterPause={() => {}}
        onBringToFront={() => {}}
        onOpenHit={() => {}}
      />
    );
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(screen.queryByText("Ask about this channel")).not.toBeInTheDocument();
  });
});

describe("ChannelExplorePopup mini-preview transport buttons", () => {
  it("renders the clip and Download buttons icon-only with a11y labels", async () => {
    stubFetch();
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());

    const clipBtn = screen.getByRole("button", {
      name: "Open the Twitch clip mini-preview at the playhead",
    });
    expect(clipBtn.querySelector("svg")).toBeTruthy();
    expect(clipBtn.textContent?.trim()).toBe("");
    expect(clipBtn).toHaveAttribute("title", "Open the Twitch clip mini-preview at the playhead");
    expect(clipBtn).toHaveAttribute("aria-label", "Open the Twitch clip mini-preview at the playhead");

    const dlBtn = screen.getByRole("button", { name: "Open in main preview to download" });
    expect(dlBtn.querySelector("svg")).toBeTruthy();
    expect(dlBtn.textContent?.trim()).toBe("");
    expect(dlBtn).toHaveAttribute("title", "Open in main preview to download");
    expect(dlBtn).toHaveAttribute("aria-label", "Open in main preview to download");
  });

  it("clip button still opens the Twitch clip mini-preview popup", async () => {
    stubFetch();
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", {
      name: "Open the Twitch clip mini-preview at the playhead",
    }));
    await waitFor(() =>
      expect(document.querySelector("[data-twitch-clip-popup]")).toBeTruthy()
    );
  });

  it("Download button still hands the VOD to the main preview on click", async () => {
    stubFetch();
    const onHandoffToMain = vi.fn();
    render(
      <ChannelExplorePopup
        id="popup-1"
        vod={VOD}
        zIndex={100}
        stackIndex={0}
        onClose={() => {}}
        onHandoffToMain={onHandoffToMain}
        onRegisterPause={() => {}}
        onUnregisterPause={() => {}}
        onBringToFront={() => {}}
        onOpenHit={() => {}}
      />
    );
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Open in main preview to download" }));
    expect(onHandoffToMain).toHaveBeenCalledTimes(1);
  });
});

describe("ChannelExplorePopup hover volume", () => {
  it("shows the volume slider on mouseEnter and hides on mouseLeave", async () => {
    stubFetch();
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());

    const volWrapper = document.querySelector("[data-volume-menu]") as HTMLElement;
    expect(volWrapper).toBeTruthy();

    // Slider hidden by default
    expect(volWrapper.querySelector("input[type='range']")).toBeNull();

    // mouseEnter → slider visible
    fireEvent.mouseEnter(volWrapper);
    expect(volWrapper.querySelector("input[type='range']")).toBeTruthy();

    // mouseLeave → slider hidden
    fireEvent.mouseLeave(volWrapper);
    expect(volWrapper.querySelector("input[type='range']")).toBeNull();
  });

  it("click speaker toggles mute", async () => {
    stubFetch();
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());

    const volWrapper = document.querySelector("[data-volume-menu]") as HTMLElement;
    const speakerBtn = volWrapper.querySelector("button") as HTMLButtonElement;
    expect(speakerBtn).toBeTruthy();

    // Initially unmuted — icon should be Volume2 (no VolumeX path)
    expect(speakerBtn.querySelector("svg")).toBeTruthy();

    // Click to mute
    fireEvent.click(speakerBtn);
    // Hover to reveal and check slider value
    fireEvent.mouseEnter(volWrapper);
    const slider = volWrapper.querySelector("input[type='range']") as HTMLInputElement;
    expect(slider).toBeTruthy();
    expect(Number(slider.value)).toBe(0);
  });
});

/**
 * Plugin gates — the 2026-10-15 model: PLUGINS is ONLY the four heavy tools
 * (transcribe, live-preview, live-subtitles, ai-ask). 'channel-explore',
 * 'clip-editor' and 'live-chat' are ALWAYS_ON surfaces, and usePlugin() answers
 * TRUE for any id outside the catalogue, so storing one of those ids as false
 * cannot suppress anything — the surface renders anyway. These tests pin that
 * rule: they FAIL if a gate starts honouring the stored false again (the
 * regression that would make an ALWAYS_ON surface vanish).
 *
 * RED ARM: reintroduce a gate in ChannelExplorePopup.tsx that reads the stored
 * value for one of those ids and the matching "renders even while ... false"
 * case below fails, because the block leaves the DOM.
 *
 * 'ai-ask' is one of the four real plugins, so its gate still removes the row —
 * kept here as the contrast proving not every gate became a no-op.
 */
describe("ChannelExplorePopup plugin gates", () => {
  it("renders even while 'channel-explore' is stored as false, because it is ALWAYS ON", async () => {
    stubFetch();
    setPlugins({ ...ALL_PLUGINS_ON, "channel-explore": false });
    const { container } = renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(container.innerHTML).not.toBe("");
  });

  it("opens while 'channel-explore' is on", async () => {
    stubFetch();
    setPlugins({ ...ALL_PLUGINS_ON });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
  });

  it("renders the mini editor even while 'clip-editor' is stored as false, because it is ALWAYS ON", async () => {
    stubFetch();
    setPlugins({ ...ALL_PLUGINS_ON, "clip-editor": false });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(document.querySelector("[data-preview-mini-editor]")).toBeTruthy();
  });

  it("shows the mini editor while 'clip-editor' is on", async () => {
    stubFetch();
    setPlugins({ ...ALL_PLUGINS_ON });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(document.querySelector("[data-preview-mini-editor]")).toBeTruthy();
  });

  it("hides the ask row while 'ai-ask' is off even when the setting is on", async () => {
    stubFetch({ aiEnabled: true });
    setPlugins({ ...ALL_PLUGINS_ON, "ai-ask": false });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(screen.queryByText("Ask about this channel")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("ai ask question")).not.toBeInTheDocument();
  });

  it("renders the CHAT button and the chat panel even while 'live-chat' is stored as false, because it is ALWAYS ON", async () => {
    stubFetch();
    setPlugins({ ...ALL_PLUGINS_ON, "live-chat": false });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(screen.getByTitle("Open preview chat panel")).toBeInTheDocument();
    expect(document.querySelector("[data-preview-chat-panel]")).toBeTruthy();
  });

  it("shows the CHAT button and mounts the chat panel while 'live-chat' is on", async () => {
    stubFetch();
    setPlugins({ ...ALL_PLUGINS_ON });
    renderPopup();
    await waitFor(() => expect(screen.getByText("Channel VOD explore")).toBeInTheDocument());
    expect(screen.getByTitle("Open preview chat panel")).toBeInTheDocument();
    expect(document.querySelector("[data-preview-chat-panel]")).toBeTruthy();
  });
});
