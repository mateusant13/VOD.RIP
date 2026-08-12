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
  whisper_model: "large-v3-turbo",
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
  it("shows parakeet as default engine; whisper model shown when engine=whisper", () => {
    render(
      <TranscriptionSection
        settings={BASE_SETTINGS}
        setSettings={() => {}}
      />,
    );
    // Parakeet is the default engine; the whisper id renders only on whisper.
    const model = screen.getByLabelText("whisper model id (read-only)");
    expect(model.textContent).toContain("Parakeet (default)");
    expect(model.tagName).toBe("SPAN");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("falls back to large-v3-turbo when whisper engine selected and setting blank", () => {
    render(
      <TranscriptionSection
        settings={{ ...BASE_SETTINGS, asr_engine: "whisper", whisper_model: "" }}
        setSettings={() => {}}
      />,
    );
    expect(screen.getByText("large-v3-turbo")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("saving still sends the resolved whisper_model to the backend", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") {
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
        settings={{ ...BASE_SETTINGS, whisper_model: "small" }}
        setSettings={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings",
        expect.objectContaining({
          body: expect.stringContaining('"whisper_model":"small"'),
        }),
      );
    });
  });
});
