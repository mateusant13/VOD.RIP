import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import DiskSection from "./DiskSection";
import type { AppSettings } from "../types";

const BASE_SETTINGS: AppSettings = {
  download_folder: "C:\\Downloads",
  download_threads: 4,
  max_cache_mb: 200,
  throttle_kib: 0,
  ffmpeg_path: "",
  temp_folder: "",
  oauth: "",
  quality: "720p",
};

const USAGE = {
  archive_vods: 11 * 1024 ** 3,
  whisper_models: 2 * 1024 ** 3,
  db: 55 * 1024 ** 2,
  logs: 200 * 1024,
  preview_cache: 1024 ** 3,
  update_temps: 0,
  total: 14 * 1024 ** 3,
};

function stubFetch(status: { low: boolean; free_bytes: number; keep_count: number }) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/api/disk/status")
      ? status
      : url.includes("/api/disk/cleanup")
        ? { freed_bytes: 1024 ** 3 }
        : USAGE;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }));
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("DiskSection", () => {
  it("shows the low-disk banner when status.low is true", async () => {
    stubFetch({ low: true, free_bytes: 2 * 1024 ** 3, keep_count: 5 });
    render(<DiskSection settings={BASE_SETTINGS} setSettings={() => {}} />);
    await waitFor(() =>
      expect(screen.getByRole("alert")).toBeInTheDocument()
    );
    expect(screen.getByRole("alert").textContent).toContain("LOW DISK SPACE");
  });

  it("does not render the banner when status.low is false", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    render(<DiskSection settings={BASE_SETTINGS} setSettings={() => {}} />);
    await waitFor(() => expect(screen.getByText("FREE")).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders usage rows with CLEAN buttons on cleanable categories only", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    render(<DiskSection settings={BASE_SETTINGS} setSettings={() => {}} />);
    await waitFor(() => expect(screen.getByText("Archive VODs")).toBeInTheDocument());
    expect(screen.getByText("11.00 GB")).toBeInTheDocument();
    expect(screen.getByLabelText("clean archive_vods")).toBeInTheDocument();
    expect(screen.getByLabelText("clean whisper_models")).toBeInTheDocument();
    expect(screen.getByLabelText("clean preview_cache")).toBeInTheDocument();
    expect(screen.getByLabelText("clean update_temps")).toBeInTheDocument();
    expect(screen.queryByLabelText("clean db")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("clean logs")).not.toBeInTheDocument();
  });

  it("defaults the keep input to 5 when the backend field is absent", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    render(<DiskSection settings={BASE_SETTINGS} setSettings={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("archive vods keep count")).toBeInTheDocument());
    expect((screen.getByLabelText("archive vods keep count") as HTMLInputElement).value).toBe("5");
  });

  it("clamps keep input changes to 1-50 via setSettings", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    const setSettings = vi.fn();
    render(<DiskSection settings={{ ...BASE_SETTINGS, archive_vod_keep_count: 5 }} setSettings={setSettings} />);
    await waitFor(() => expect(screen.getByLabelText("archive vods keep count")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("archive vods keep count"), { target: { value: "99" } });
    expect(setSettings).toHaveBeenCalledWith(expect.objectContaining({ archive_vod_keep_count: 50 }));
  });

  it("posts cleanup and refreshes usage", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    render(<DiskSection settings={BASE_SETTINGS} setSettings={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("clean preview_cache")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("clean preview_cache"));
    await waitFor(() =>
      expect(screen.getByText(/freed 1.00 GB/)).toBeInTheDocument()
    );
    // usage re-fetched after cleanup: fetch called for usage at least twice
    const usageCalls = vi.mocked(fetch).mock.calls.filter(([u]) => String(u).includes("/api/disk/usage"));
    expect(usageCalls.length).toBeGreaterThanOrEqual(2);
  });
});
