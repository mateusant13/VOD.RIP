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

const DISKS = [
  { drive: "C:\\", label: "System", total_bytes: 500 * 1024 ** 3, free_bytes: 90 * 1024 ** 3, media_type: "NVMe", bus_type: "NVMe", speed_rank: 1 },
  { drive: "D:\\", label: "Data", total_bytes: 1000 * 1024 ** 3, free_bytes: 402 * 1024 ** 3, media_type: "SSD", bus_type: "SATA", speed_rank: 2 },
  { drive: "I:\\", label: "Archive", total_bytes: 2000 * 1024 ** 3, free_bytes: 344 * 1024 ** 3, media_type: "NVMe", bus_type: "NVMe", speed_rank: 1 },
];

function stubFetch(status: {
  low: boolean;
  free_bytes: number;
  keep_count: number;
  cache_dir?: string;
  cache_free_bytes?: number;
  biggest_drive?: string;
}) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/api/disk/status")
      ? status
      : url.includes("/api/disk/cleanup")
        ? { freed_bytes: 1024 ** 3 }
        : url.includes("/api/disks")
          ? { drives: DISKS, fastest: "I:\\", model_cache: "I:\\" }
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

  it("shows the effective cache location and free space from status", async () => {
    stubFetch({
      low: false,
      free_bytes: 120 * 1024 ** 3,
      keep_count: 5,
      cache_dir: "I:\\VOD.RIP-cache",
      cache_free_bytes: 402 * 1024 ** 3,
      biggest_drive: "I:\\",
    });
    render(<DiskSection settings={BASE_SETTINGS} setSettings={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("heavy cache disk")).toBeInTheDocument());
    expect(screen.getByText(/I:\\VOD.RIP-cache/)).toBeInTheDocument();
    expect(screen.getByText(/402.00 GB free/)).toBeInTheDocument();
    expect(screen.getByText(/auto pick: I:\\/)).toBeInTheDocument();
  });

  it("renders the storage selects with drive options (free space + type)", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    render(<DiskSection settings={BASE_SETTINGS} setSettings={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("heavy cache disk")).toBeInTheDocument());
    expect(screen.getByLabelText("transcripts and chat data disk")).toBeInTheDocument();
    expect(screen.getByLabelText("ai models folder")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "transcripts, chat data & preview cache — takes effect after restart (moves the database)" })).toBeInTheDocument();
    // Drive labels appear once per select (cache + data + model pickers).
    expect(screen.getAllByRole("option", { name: "I: (344 GB free, NVMe)" })).toHaveLength(3);
    expect(screen.getAllByRole("option", { name: "D: (402 GB free, SSD)" })).toHaveLength(3);
    expect(screen.getByRole("option", { name: "Auto (biggest free space)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Auto (fastest: I:)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Auto (best fit: I:)" })).toBeInTheDocument();
  });

  it("selecting a model cache disk writes <drive>\\VOD.RIP-models", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    const setSettings = vi.fn();
    render(<DiskSection settings={BASE_SETTINGS} setSettings={setSettings} />);
    await waitFor(() => expect(screen.getByLabelText("ai models folder")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("ai models folder"), { target: { value: "D:\\VOD.RIP-models" } });
    expect(setSettings).toHaveBeenCalledWith(expect.objectContaining({ whisper_model_cache: "D:\\VOD.RIP-models" }));
  });

  it("selecting Auto for the model cache clears it (empty = auto)", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    const setSettings = vi.fn();
    render(
      <DiskSection
        settings={{ ...BASE_SETTINGS, whisper_model_cache: "D:\\VOD.RIP-models" }}
        setSettings={setSettings}
      />
    );
    await waitFor(() => expect(screen.getByLabelText("ai models folder")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("ai models folder"), { target: { value: "" } });
    expect(setSettings).toHaveBeenCalledWith(expect.objectContaining({ whisper_model_cache: "" }));
  });

  it("surfaces a legacy custom model cache path as a Custom option", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    render(
      <DiskSection settings={{ ...BASE_SETTINGS, whisper_model_cache: "Z:\\hub" }} setSettings={() => {}} />
    );
    await waitFor(() => expect(screen.getByLabelText("ai models folder")).toBeInTheDocument());
    expect(screen.getByRole("option", { name: "Custom (Z:\\hub)" })).toBeInTheDocument();
    expect(screen.getByLabelText("ai models folder")).toHaveValue("Z:\\hub");
  });

  it("selecting a cache disk writes <drive>\\VOD.RIP-cache", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    const setSettings = vi.fn();
    render(<DiskSection settings={BASE_SETTINGS} setSettings={setSettings} />);
    await waitFor(() => expect(screen.getByLabelText("heavy cache disk")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("heavy cache disk"), { target: { value: "D:\\VOD.RIP-cache" } });
    expect(setSettings).toHaveBeenCalledWith(expect.objectContaining({ cache_dir: "D:\\VOD.RIP-cache" }));
  });

  it("selecting Auto returns cache_dir to auto (empty)", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    const setSettings = vi.fn();
    render(
      <DiskSection
        settings={{ ...BASE_SETTINGS, cache_dir: "D:\\VOD.RIP-cache" }}
        setSettings={setSettings}
      />
    );
    await waitFor(() => expect(screen.getByLabelText("heavy cache disk")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("heavy cache disk"), { target: { value: "" } });
    expect(setSettings).toHaveBeenCalledWith(expect.objectContaining({ cache_dir: "" }));
  });

  it("selecting a data disk writes <drive>\\VOD.RIP-data", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    const setSettings = vi.fn();
    render(<DiskSection settings={BASE_SETTINGS} setSettings={setSettings} />);
    await waitFor(() => expect(screen.getByLabelText("transcripts and chat data disk")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("transcripts and chat data disk"), { target: { value: "I:\\VOD.RIP-data" } });
    expect(setSettings).toHaveBeenCalledWith(expect.objectContaining({ data_dir: "I:\\VOD.RIP-data" }));
  });

  it("surfaces a legacy custom cache path as a Custom option", async () => {
    stubFetch({ low: false, free_bytes: 120 * 1024 ** 3, keep_count: 5 });
    render(
      <DiskSection settings={{ ...BASE_SETTINGS, cache_dir: "D:\\caches" }} setSettings={() => {}} />
    );
    await waitFor(() => expect(screen.getByLabelText("heavy cache disk")).toBeInTheDocument());
    expect(screen.getByRole("option", { name: "Custom (D:\\caches)" })).toBeInTheDocument();
    expect(screen.getByLabelText("heavy cache disk")).toHaveValue("D:\\caches");
  });
});
