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
  it("shows the resolved model as a read-only value, no free-text input", () => {
    render(
      <TranscriptionSection
        settings={BASE_SETTINGS}
        setSettings={() => {}}
      />,
    );
    // The model id is displayed, but there is no input the user can type into.
    expect(screen.getByText("large-v3-turbo")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    const model = screen.getByLabelText("whisper model id (read-only)");
    expect(model.tagName).toBe("SPAN");
  });

  it("falls back to large-v3-turbo when the setting is blank", () => {
    render(
      <TranscriptionSection
        settings={{ ...BASE_SETTINGS, whisper_model: "" }}
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
