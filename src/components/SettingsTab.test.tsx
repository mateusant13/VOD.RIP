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

function stubFetch(cookieStatus?: object) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      let body: unknown;
      if (url.includes("/api/disks")) {
        body = {
          drives: [
            { drive: "C:\\", label: "System", total_bytes: 500 * 1024 ** 3, free_bytes: 90 * 1024 ** 3, media_type: "NVMe", bus_type: "NVMe", speed_rank: 1 },
            { drive: "I:\\", label: "Archive", total_bytes: 2000 * 1024 ** 3, free_bytes: 344 * 1024 ** 3, media_type: "NVMe", bus_type: "NVMe", speed_rank: 1 },
          ],
          fastest: "I:\\",
        };
      } else if (cookieStatus !== undefined && url.includes("/api/session/cookies/status")) {
        body = cookieStatus;
      } else {
        body = {};
      }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    })
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

  it("renders the Storage disk pickers with drive options", async () => {
    stubFetch();
    render(<Harness />);
    expect(await screen.findByLabelText("heavy cache disk")).toBeInTheDocument();
    expect(screen.getByLabelText("transcripts and chat data disk")).toBeInTheDocument();
    expect(screen.getAllByRole("option", { name: "I: (344 GB free, NVMe)" })).toHaveLength(2);
    expect(screen.getByRole("option", { name: "Auto (fastest: I:)" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "transcripts, chat data & preview cache — takes effect after restart (moves the database)",
      ),
    ).toBeInTheDocument();
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

  it("puts Cookie Bridge first while the extension is not installed", async () => {
    stubFetch({ paired: false, enabled: true, platforms: {} });
    render(<Harness />);
    await screen.findByText(/not paired/);
    const sections = [...document.querySelectorAll("section")];
    expect(sections[0].textContent).toContain("Cookie Bridge");
    expect(sections[0].textContent).not.toContain("General");
  });

  it("puts Cookie Bridge first while cookies are not detected yet", async () => {
    stubFetch({ paired: true, enabled: true, platforms: { kick: { count: 0, lastGrabAt: null, expiredCount: 0 } } });
    render(<Harness />);
    await screen.findByText(/paired/);
    const sections = [...document.querySelectorAll("section")];
    expect(sections[0].textContent).toContain("Cookie Bridge");
  });

  it("moves Cookie Bridge to the very end once cookies are detected", async () => {
    stubFetch({ paired: true, enabled: true, platforms: { kick: { count: 3, lastGrabAt: null, expiredCount: 0 } } });
    render(<Harness />);
    await screen.findByText(/paired/);
    const sections = [...document.querySelectorAll("section")];
    const last = sections[sections.length - 1];
    expect(last.textContent).toContain("Cookie Bridge");
    expect(last.textContent).not.toContain("Danger Zone");
    expect(sections[0].textContent).toContain("General");
  });
});
