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
  quality: "720p",
  skip_youtube_startup_warm: false,
  archive_vod_keep_count: 5,
  whisper_model_cache: null,
  yt_subtitles_first: true,
};

function stubFetch(cookieStatus?: object) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      let body: unknown;
      if (url.includes("/api/settings/recommended")) {
        body = { download_threads: 10, max_cache_mb: 1000 };
      } else if (url.includes("/api/disks")) {
        body = {
          drives: [
            { drive: "C:\\", label: "System", total_bytes: 500 * 1024 ** 3, free_bytes: 90 * 1024 ** 3, media_type: "NVMe", bus_type: "NVMe", speed_rank: 1 },
            { drive: "I:\\", label: "Archive", total_bytes: 2000 * 1024 ** 3, free_bytes: 344 * 1024 ** 3, media_type: "NVMe", bus_type: "NVMe", speed_rank: 1 },
          ],
          fastest: "I:\\",
          model_cache: "I:\\",
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

/** Click a card's header to expand/collapse it (all cards start collapsed). */
function expandCard(title: string) {
  fireEvent.click(screen.getByText(title));
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
  it("renders all grouped sections, collapsed by default", () => {
    stubFetch();
    render(<Harness />);
    for (const title of ["General", "Transcription", "Disk & Storage", "Cookie Bridge", "Updates", "Experimental", "Danger Zone"]) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
    expect(screen.getByText("Save Settings")).toBeInTheDocument();
    // Content is collapsed until the header is clicked.
    expect(screen.queryByLabelText("download threads")).not.toBeInTheDocument();
    expect(screen.queryByText("Exit VOD.RIP")).not.toBeInTheDocument();
    expandCard("General");
    expandCard("Danger Zone");
    expect(screen.getByLabelText("download threads")).toBeInTheDocument();
    expect(screen.getByText("Exit VOD.RIP")).toBeInTheDocument();
  });

  it("toggles card content on header click independently", () => {
    stubFetch();
    render(<Harness />);
    expandCard("General");
    expandCard("Danger Zone");
    expect(screen.getByLabelText("download threads")).toBeInTheDocument();
    expect(screen.getByText("Exit VOD.RIP")).toBeInTheDocument();
    // Collapsing General hides only its own content.
    expandCard("General");
    expect(screen.queryByLabelText("download threads")).not.toBeInTheDocument();
    expect(screen.getByText("Exit VOD.RIP")).toBeInTheDocument();
    expandCard("Danger Zone");
    expect(screen.queryByText("Exit VOD.RIP")).not.toBeInTheDocument();
  });

  it("fills threads and cache from the recommended endpoint", async () => {
    stubFetch();
    render(<Harness />);
    expandCard("General");
    fireEvent.click(await screen.findByLabelText("recommended resource defaults"));
    expect(screen.getByLabelText("download threads")).toHaveValue(10);
    expect(screen.getByLabelText("max cache mb")).toHaveValue(1000);
  });

  it("renders the Storage disk pickers with drive options", async () => {
    stubFetch();
    render(<Harness />);
    expandCard("Disk & Storage");
    expect(await screen.findByLabelText("heavy cache disk")).toBeInTheDocument();
    expect(screen.getByLabelText("transcripts and chat data disk")).toBeInTheDocument();
    expect(screen.getByLabelText("ai models folder")).toBeInTheDocument();
    expect(screen.getAllByRole("option", { name: "I: (344 GB free, NVMe)" })).toHaveLength(3);
    expect(screen.getByRole("option", { name: "Auto (fastest: I:)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Auto (best fit: I:)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "transcripts, chat data & preview cache — takes effect after restart (moves the database)" })).toBeInTheDocument();
  });

  it("keeps option rows compact: descriptions live on the (i) affordance, not as text", () => {
    stubFetch();
    render(<Harness />);
    expandCard("General");
    expandCard("Disk & Storage");
    // Descriptions must not render as body text...
    expect(screen.queryByText("Pre-loads preview data for faster first play (uses ~500MB download at boot)")).not.toBeInTheDocument();
    expect(screen.queryByText("transcripts, chat data & preview cache — takes effect after restart (moves the database)")).not.toBeInTheDocument();
    expect(screen.queryByText("Exits VOD.RIP — cancels all downloads and closes the app.")).not.toBeInTheDocument();
    // ...but stay reachable via the hover/info buttons.
    expect(screen.getByRole("button", { name: "Pre-loads preview data for faster first play (uses ~500MB download at boot)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "transcripts, chat data & preview cache — takes effect after restart (moves the database)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Exits VOD.RIP — cancels all downloads and closes the app." })).toBeInTheDocument();
  });

  it("shows unsaved-changes chip on edit and clears it after save", async () => {
    stubFetch();
    render(<Harness />);
    expandCard("General");
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
    expandCard("General");
    fireEvent.click(screen.getByLabelText("download threads plus"));
    expect(screen.getByLabelText("download threads")).toHaveValue(9);
    fireEvent.click(screen.getByLabelText("max cache mb minus"));
    expect(screen.getByLabelText("max cache mb")).toHaveValue(462);
  });

  it("confirms before exiting and posts /api/exit", () => {
    stubFetch();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Harness />);
    expandCard("Danger Zone");
    fireEvent.click(screen.getByText("Exit VOD.RIP"));
    expect(confirmSpy).toHaveBeenCalled();
    const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u.includes("/api/exit"))).toBe(true);
    confirmSpy.mockRestore();
  });

  it("puts Cookie Bridge first while the extension is not installed", async () => {
    stubFetch({ paired: false, enabled: true, platforms: {} });
    render(<Harness />);
    expandCard("Cookie Bridge");
    await screen.findByText(/not paired/);
    const sections = [...document.querySelectorAll("section")];
    expect(sections[0].textContent).toContain("Cookie Bridge");
    expect(sections[0].textContent).not.toContain("General");
  });

  it("puts Cookie Bridge first while cookies are not detected yet", async () => {
    stubFetch({ paired: true, enabled: true, platforms: { kick: { count: 0, lastGrabAt: null, expiredCount: 0 } } });
    render(<Harness />);
    expandCard("Cookie Bridge");
    await screen.findByText(/paired/);
    const sections = [...document.querySelectorAll("section")];
    expect(sections[0].textContent).toContain("Cookie Bridge");
  });

  it("moves Cookie Bridge to second-to-last once cookies are detected", async () => {
    stubFetch({ paired: true, enabled: true, platforms: { kick: { count: 3, lastGrabAt: null, expiredCount: 0 } } });
    render(<Harness />);
    expandCard("Cookie Bridge");
    await screen.findByText(/paired/);
    const sections = [...document.querySelectorAll("section")];
    // Danger Zone renders directly above the Save row (user request);
    // Cookie Bridge stays among the last cards (Experimental now sits
    // between Cookie Bridge and Danger Zone).
    const last = sections[sections.length - 1];
    expect(last.textContent).toContain("Danger Zone");
    expect(last.textContent).not.toContain("Cookie Bridge");
    const secondToLast = sections[sections.length - 2];
    expect(secondToLast.textContent).toContain("Experimental");
    expect(secondToLast.textContent).not.toContain("Cookie Bridge");
    const thirdToLast = sections[sections.length - 3];
    expect(thirdToLast.textContent).toContain("Cookie Bridge");
    // The Save button must come AFTER the Danger Zone card in the DOM.
    const saveBtn = screen.getByText("Save Settings");
    expect(last.compareDocumentPosition(saveBtn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // i18n: a Language card now sits at the top, before General.
    expect(sections[0].textContent).toContain("Language");
    expect(sections[1].textContent).toContain("General");
  });

  it("renders the Experimental AI card with the toggle off by default", () => {
    stubFetch();
    render(<Harness />);
    expandCard("Experimental");
    expect(screen.getByLabelText("experimental ai enabled")).not.toBeChecked();
    // Password input masks; the bullet placeholder only appears once a key
    // is reported set.
    expect(screen.getByLabelText("ai api key")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("ai api key")).toHaveAttribute("placeholder", "");
  });

  it("shows the bullet placeholder when a key is set (never the key itself)", () => {
    stubFetch();
    render(<Harness initial={{ ...BASE, ai_api_key_set: true }} />);
    expandCard("Experimental");
    expect(screen.getByLabelText("ai api key")).toHaveAttribute("placeholder", "••••••••");
    expect(screen.getByText(/never shown/)).toBeInTheDocument();
  });

  it("shows an inline error when the toggle is on without a key", () => {
    stubFetch();
    render(<Harness />);
    expandCard("Experimental");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("experimental ai enabled"));
    expect(screen.getByLabelText("experimental ai enabled")).toBeChecked();
    expect(screen.getByRole("alert")).toHaveTextContent(/API key first/);
    // Typing a key clears the inline error.
    fireEvent.change(screen.getByLabelText("ai api key"), { target: { value: "sk-abc" } });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("posts a typed AI key (write-only) and marks it set", async () => {
    stubFetch();
    render(<Harness />);
    expandCard("Experimental");
    fireEvent.change(screen.getByLabelText("ai api key"), { target: { value: "sk-abc-123" } });
    fireEvent.click(screen.getByText("Save Settings"));
    await waitFor(() => {
      const posts = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls
        .filter(([url]) => String(url) === "/api/settings")
        .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
      expect(posts.some((b) => b.ai_api_key === "sk-abc-123")).toBe(true);
    });
    // After saving, the placeholder reflects the stored key.
    expect(screen.getByLabelText("ai api key")).toHaveAttribute("placeholder", "••••••••");
  });
});
