import { describe, expect, it, vi, beforeEach } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import SettingsTab from "./SettingsTab";
import type { AppSettings, UpdateInfo } from "../types";

const BASE: AppSettings = {
  download_folder: "C:\\Downloads",
  download_threads: 8,
  max_cache_mb: 512,
  throttle_kib: 0,
  ffmpeg_path: "",
  temp_folder: "",
  oauth: "",
  quality: "720p",
  skip_youtube_startup_warm: false,
  archive_vod_keep_count: 5,
  whisper_model: "large-v3-turbo",
  whisper_model_cache: null,
  yt_subtitles_first: true,
};

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } })
    )
  );
}

/** Stateful wrapper — emulates App.tsx ownership of the settings state. */
function Harness({
  onSave,
  initial = BASE,
}: {
  onSave?: () => Promise<void>;
  initial?: AppSettings;
}) {
  const [settings, setSettings] = useState(initial);
  return (
    <SettingsTab
      settings={settings}
      setSettings={setSettings}
      appVersion="1.2.3"
      updateInfo={null as UpdateInfo | null}
      updateChecking={false}
      updateApplying={false}
      updateMessage={null}
      pickingFolder={false}
      settingsSaved={false}
      onPickFolder={async () => null}
      onSave={onSave ?? (async () => {})}
      onCheckUpdate={async () => {}}
      onApplyUpdate={async () => {}}
      onFlushPanelLayout={() => {}}
    />
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("SettingsTab", () => {
  it("renders all grouped sections", () => {
    stubFetch();
    render(<Harness />);
    for (const title of ["General", "Transcription", "Disk & Storage", "Cookie Bridge", "Updates", "Danger Zone"]) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
    expect(screen.getByText("Save Settings")).toBeInTheDocument();
    expect(screen.getByText("Exit VOD.RIP")).toBeInTheDocument();
  });

  it("shows unsaved-changes chip on edit and clears it after save", async () => {
    stubFetch();
    render(<Harness />);
    expect(screen.queryByText(/unsaved changes/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("download threads"), { target: { value: "4" } });
    expect(screen.getByText(/unsaved changes/)).toBeInTheDocument();

    fireEvent.click(screen.getByText("Save Settings"));
    await waitFor(() =>
      expect(screen.queryByText(/unsaved changes/)).not.toBeInTheDocument()
    );
  });

  it("steppers emit clamped values via setSettings", () => {
    stubFetch();
    render(<Harness />);
    fireEvent.click(screen.getByLabelText("download threads plus"));
    expect(screen.getByLabelText("download threads")).toHaveValue(9);
    fireEvent.click(screen.getByLabelText("max cache mb minus"));
    expect(screen.getByLabelText("max cache mb")).toHaveValue(462);
  });

  it("confirms before exiting and posts /api/exit", () => {
    stubFetch();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Harness />);
    fireEvent.click(screen.getByText("Exit VOD.RIP"));
    expect(confirmSpy).toHaveBeenCalled();
    const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u.includes("/api/exit"))).toBe(true);
    confirmSpy.mockRestore();
  });
});
