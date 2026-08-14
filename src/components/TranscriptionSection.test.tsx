import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import TranscriptionSection from "./TranscriptionSection";
import type { AppSettings } from "../types";

const BASE_SETTINGS: AppSettings = {
  download_folder: "C:\\Downloads",
  download_threads: 4,
  max_cache_mb: 200,
  throttle_kib: 0,
  ffmpeg_path: "",
  temp_folder: "",
  quality: "720p",
  yt_subtitles_first: true,
  asr_language: "auto",
  channel_asr_languages: null,
};

function stubFetch() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/settings") && String(input) === "/api/settings") {
      return new Response(JSON.stringify(BASE_SETTINGS), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  }));
}

beforeEach(() => {
  stubFetch();
  vi.restoreAllMocks();
});

describe("TranscriptionSection", () => {
  it("has no ASR engine selector (parakeet is the only engine)", () => {
    render(
      <TranscriptionSection
        settings={BASE_SETTINGS}
        setSettings={() => {}}
      />,
    );
    expect(screen.queryByLabelText("ASR engine")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("whisper model id (read-only)")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("renders the fixed-engine caption with parakeet", () => {
    render(
      <TranscriptionSection
        settings={BASE_SETTINGS}
        setSettings={() => {}}
      />,
    );
    expect(screen.getByText(/active engine: parakeet/i)).toBeInTheDocument();
  });

  it("saving sends only the surviving ASR fields to the backend", async () => {
    let sentBody = "";
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/settings") {
        sentBody = String(init?.body ?? "");
        return new Response(JSON.stringify(BASE_SETTINGS), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <TranscriptionSection
        settings={{ ...BASE_SETTINGS, asr_language: "pt" }}
        setSettings={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings",
        expect.objectContaining({
          body: expect.stringContaining('"asr_language":"pt"'),
        }),
      );
    });
    expect(sentBody).not.toContain("whisper_model");
    expect(sentBody).not.toContain("asr_engine");
  });
});
