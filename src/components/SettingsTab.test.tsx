import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { useState } from "react";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import SettingsTab from "./SettingsTab";
import { getLanguage, setLanguage } from "../i18n";
import type { AppSettings, UpdateInfo } from "../types";

/** i18n is module-global; restore it so a pt-BR test cannot leak into the next. */
const savedLang = getLanguage();

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
  // Plugin flags live in the same localStorage the app syncs from
  // GET /api/plugins. Clearing it keeps every test on the registry defaults
  // (transcribe off, ai-ask off) instead of inheriting the previous test's.
  localStorage.clear();
});

afterEach(() => {
  act(() => setLanguage(savedLang));
});

/** Override the registry defaults with an explicit on/off map, the same way
 *  a synced GET /api/plugins would (plugins.tsx storedState). */
function setPlugins(patch: Record<string, boolean>) {
  localStorage.setItem("vodrip.plugins", JSON.stringify(patch));
}

describe("SettingsTab", () => {
  it("renders all grouped sections, collapsed by default", () => {
    stubFetch();
    // Transcription and Experimental are plugin cards (transcribe / ai-ask);
    // with them ON this list is the full set. The companion tests below pin
    // what happens with them OFF.
    setPlugins({ transcribe: true, "ai-ask": true });
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
    const sections = [...document.querySelectorAll("section")].filter((section) => section.querySelector("[aria-expanded]"));
    expect(sections[0].textContent).toContain("Cookie Bridge");
    expect(sections[0].textContent).not.toContain("General");
  });

  it("puts Cookie Bridge first while cookies are not detected yet", async () => {
    stubFetch({ paired: true, enabled: true, platforms: { kick: { count: 0, lastGrabAt: null, expiredCount: 0 } } });
    render(<Harness />);
    expandCard("Cookie Bridge");
    await screen.findByText(/paired/);
    const sections = [...document.querySelectorAll("section")].filter((section) => section.querySelector("[aria-expanded]"));
    expect(sections[0].textContent).toContain("Cookie Bridge");
  });

  it("moves Cookie Bridge to second-to-last once cookies are detected", async () => {
    stubFetch({ paired: true, enabled: true, platforms: { kick: { count: 3, lastGrabAt: null, expiredCount: 0 } } });
    // The layout this pins includes the two plugin cards, so they are on here;
    // the gated-out layout is asserted in the plugin tests below.
    setPlugins({ transcribe: true, "ai-ask": true });
    render(<Harness />);
    expandCard("Cookie Bridge");
    await screen.findByText(/paired/);
    const sections = [...document.querySelectorAll("section")].filter((section) => section.querySelector("[aria-expanded]"));
    // Danger Zone remains immediately above Save; Updates is a static section
    // after Save and is intentionally excluded from accordion ordering.
    const last = sections[sections.length - 1];
    expect(last.textContent).toContain("Danger Zone");
    expect(last.textContent).not.toContain("Cookie Bridge");
    const secondToLast = sections[sections.length - 2];
    // The old "Features" card is now "Plugins" (same slot, plugin toggles).
    expect(secondToLast.textContent).toContain("Plugins");
    expect(secondToLast.textContent).not.toContain("Cookie Bridge");
    const thirdToLast = sections[sections.length - 3];
    expect(thirdToLast.textContent).toContain("Experimental");
    expect(thirdToLast.textContent).not.toContain("Cookie Bridge");
    const fourthToLast = sections[sections.length - 4];
    expect(fourthToLast.textContent).toContain("Cookie Bridge");
    const saveBtn = screen.getByText("Save Settings");
    expect(last.compareDocumentPosition(saveBtn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // i18n: a Language card now sits at the top, before General.
    expect(sections[0].textContent).toContain("Language");
    expect(sections[1].textContent).toContain("General");
  });

  it("renders the Experimental AI card with the toggle off by default", () => {
    stubFetch();
    // The card is gated by the ai-ask plugin.
    setPlugins({ "ai-ask": true });
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
    setPlugins({ "ai-ask": true });
    render(<Harness initial={{ ...BASE, ai_api_key_set: true }} />);
    expandCard("Experimental");
    expect(screen.getByLabelText("ai api key")).toHaveAttribute("placeholder", "••••••••");
    expect(screen.getByText(/never shown/)).toBeInTheDocument();
  });

  it("shows an inline error when the toggle is on without a key", () => {
    stubFetch();
    setPlugins({ "ai-ask": true });
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
    setPlugins({ "ai-ask": true });
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

  // ── Plugins section ──────────────────────────────────────────────────────
  it("renders a Plugins card listing every plugin with an active counter", () => {
    stubFetch();
    render(<Harness />);
    expandCard("Plugins");
    // Every plugin id is a labelled toggle.
    // Only the four HEAVY tools are plugins now; the eight always-on surfaces
    // have no toggle (asserted in the "Sempre ligado" test below).
    for (const id of ["transcribe", "live-preview", "live-subtitles", "ai-ask"]) {
      expect(screen.getByLabelText(id)).toBeInTheDocument();
    }
    // RED ARM: drop the counter from the SettingsCard `right` slot and this
    // fails (it is the "N of M on" the owner reads at a glance).
    expect(screen.getByTestId("plugins-count").textContent).toMatch(/\d+ (of|de) \d+/);
  });

  it("a plugin toggle in Settings writes through to POST /api/plugins", async () => {
    stubFetch();
    render(<Harness />);
    expandCard("Plugins");
    // ai-ask is a real catalog plugin; archive-search is always-on now and
    // has no toggle to click.
    const before = (screen.getByLabelText("ai-ask") as HTMLInputElement).checked;
    fireEvent.click(screen.getByLabelText("ai-ask"));
    // RED ARM: make the Plugins toggle only set local React state and this
    // POST never happens.
    await waitFor(() => {
      const posts = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls
        .filter(([url]) => String(url) === "/api/plugins")
        .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
      expect(posts).toEqual([{ id: "ai-ask", enabled: !before }]);
    });
    // The local store flipped too — the row re-renders from it.
    expect((screen.getByLabelText("ai-ask") as HTMLInputElement).checked).toBe(!before);
  });

  // ── Plugin-gated sections ──────────────────────────────────────────────────
  // RED ARM for all three: delete the <PluginGate> wrappers in SettingsTab.tsx
  // (render the two cards unconditionally) and the first two fail on the OFF
  // case; the third fails on both the OFF render and the toggle round-trip.
  it("hides the Transcription and Experimental cards when their plugin is off", () => {
    stubFetch();
    setPlugins({ transcribe: false, "ai-ask": false });
    render(<Harness />);
    // The cards are GONE, not collapsed: the registry defaults already put
    // both plugins off on a fresh install.
    expect(screen.queryByText("Transcription")).not.toBeInTheDocument();
    expect(screen.queryByText("Experimental")).not.toBeInTheDocument();
    // Their controls exist nowhere, not merely hidden inside a collapsed card.
    expect(screen.queryByLabelText("default captions language")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("experimental ai enabled")).not.toBeInTheDocument();
    // Only the plugin surfaces disappear — the rest of Settings stays.
    expect(screen.getByText("General")).toBeInTheDocument();
    expect(screen.getByText("Disk & Storage")).toBeInTheDocument();
    expect(screen.getByText("Danger Zone")).toBeInTheDocument();
  });

  it("shows the Transcription and Experimental cards when their plugin is on", () => {
    stubFetch();
    setPlugins({ transcribe: true, "ai-ask": true });
    render(<Harness />);
    expect(screen.getByText("Transcription")).toBeInTheDocument();
    expandCard("Transcription");
    expect(screen.getByLabelText("default captions language")).toBeInTheDocument();
    expect(screen.getByText("Experimental")).toBeInTheDocument();
    expandCard("Experimental");
    expect(screen.getByLabelText("experimental ai enabled")).toBeInTheDocument();
  });

  it("a plugin toggle in the Plugins card makes its section appear and disappear", async () => {
    stubFetch();
    // Defaults: transcribe off — the card must not be there.
    render(<Harness />);
    expect(screen.queryByText("Transcription")).not.toBeInTheDocument();
    expandCard("Plugins");
    fireEvent.click(screen.getByLabelText("transcribe"));
    // Turning it on renders the section immediately — Settings > Plugins is
    // where the owner summons the section back.
    await waitFor(() => expect(screen.getByText("Transcription")).toBeInTheDocument());
    expandCard("Transcription");
    expect(screen.getByLabelText("default captions language")).toBeInTheDocument();
    // And turning it off removes it again.
    fireEvent.click(screen.getByLabelText("transcribe"));
    await waitFor(() => expect(screen.queryByText("Transcription")).not.toBeInTheDocument());
  });

  // ── Custos dos plugins (regra do dono: disco/VRAM legíveis + ordem) ──────
  it("mostra o custo legível por plugin (MB < 1024; senão GB com 1 casa)", () => {
    stubFetch();
    act(() => setLanguage("pt-BR"));
    setPlugins({ transcribe: true });
    render(<Harness />);
    expandCard("Plugins");
    // 397 MB fica em MB; 2048 MB vira "2.0 GB" (1 casa, não "2 GB").
    // RED ARM: remova o toFixed(1) do formatMb e este teste falha.
    expect(screen.getByTestId("plugin-cost-transcribe")).toHaveTextContent(
      "397 MB em disco · 2.0 GB de VRAM",
    );
    expect(screen.getByTestId("plugin-cost-live-preview")).toHaveTextContent(
      "0 MB em disco · 400 MB de VRAM",
    );
  });

  it("troca o número por 'incluso (mesmo modelo)' quando shares_with já está ligado", () => {
    stubFetch();
    act(() => setLanguage("pt-BR"));
    setPlugins({ transcribe: false, "live-subtitles": true });
    render(<Harness />);
    expandCard("Plugins");
    // Só legendas ligadas: ela é a dona do modelo e mostra o número.
    expect(screen.getByTestId("plugin-cost-live-subtitles")).toHaveTextContent(
      "397 MB em disco · 2.0 GB de VRAM",
    );
    // Ligar a transcrição (mesmo modelo) troca a repetição por "incluso".
    // RED ARM: ignore shares_with e o número de 397 MB reaparece.
    fireEvent.click(screen.getByLabelText("transcribe"));
    expect(screen.getByTestId("plugin-cost-live-subtitles")).toHaveTextContent(
      "incluso (mesmo modelo)",
    );
  });

  it("resume o total do selecionado sem contar o mesmo modelo duas vezes", () => {
    stubFetch();
    act(() => setLanguage("pt-BR"));
    setPlugins({ transcribe: true, "live-subtitles": true });
    render(<Harness />);
    expandCard("Plugins");
    const totals = screen.getByTestId("plugins-totals");
    expect(totals).toHaveTextContent("397 MB em disco · 2.0 GB de VRAM");
    // RED ARM: some os plugins sem honrar shares_with e estes dois aparecem.
    expect(totals).not.toHaveTextContent("794 MB");
    expect(totals).not.toHaveTextContent("4.0 GB");
  });

  it("lista os 8 alwaysOn como 'Sempre ligado', sem toggle", () => {
    stubFetch();
    act(() => setLanguage("pt-BR"));
    render(<Harness />);
    expandCard("Plugins");
    const box = screen.getByTestId("plugins-always-on");
    for (const name of [
      "Baixar por link", "Canais", "Explorar canais", "Editor de clipes",
      "Clipes do Twitch", "Busca no arquivo", "Arquivo local", "Chat ao vivo",
    ]) {
      expect(box).toHaveTextContent(name);
    }
    // Sem toggle: nenhum id always-on vira checkbox.
    expect(box.querySelectorAll("input")).toHaveLength(0);
    expect(screen.queryByLabelText("channels")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("link-download")).not.toBeInTheDocument();
  });

  it("não reordena a lista: a ordem que o backend manda é a que aparece", () => {
    stubFetch();
    // Catálogo invertido de propósito. Se a UI reordenasse por conta própria
    // (orderPlugins), o DOM voltaria à ordem do espelho e este teste falharia.
    // RED ARM: enrole a lista em orderPlugins(plugins) e a ordem muda.
    localStorage.setItem("vodrip.plugins.catalog", JSON.stringify([
      { id: "ai-ask", name: "Perguntar à IA", blurb: "b", settingsKey: "plugin_ai_ask", disk_mb: 0, vram_mb: 0, ram_mb: 128, shares_with: "", recommended: false, defaultOn: false, cost: "pesado" },
      { id: "live-subtitles", name: "Legendas ao vivo", blurb: "b", settingsKey: "plugin_live_subtitles", disk_mb: 397, vram_mb: 2048, ram_mb: 1536, shares_with: "transcribe", recommended: false, defaultOn: false, cost: "pesado" },
      { id: "live-preview", name: "Prévia ao vivo", blurb: "b", settingsKey: "plugin_live_preview", disk_mb: 0, vram_mb: 400, ram_mb: 600, shares_with: "", recommended: true, defaultOn: false, cost: "pesado" },
      { id: "transcribe", name: "Transcrição", blurb: "b", settingsKey: "plugin_transcribe", disk_mb: 397, vram_mb: 2048, ram_mb: 2560, shares_with: "", recommended: true, defaultOn: false, cost: "pesado" },
    ]));
    render(<Harness />);
    expandCard("Plugins");
    const ids = [...document.querySelectorAll("[data-testid^='plugin-cost-']")]
      .map((el) => el.getAttribute("data-testid"));
    expect(ids).toEqual([
      "plugin-cost-ai-ask",
      "plugin-cost-live-subtitles",
      "plugin-cost-live-preview",
      "plugin-cost-transcribe",
    ]);
  });

  // ── The OFF case: a plugin card must SAY how to enable it ────────────────
  // The owner: "botoes que necessitam de plugins pra fazer algo, deve
  // aparecer uma mensagem pedindo pra habilitar o plugin". Before this, OFF
  // rendered nothing and the feature was undiscoverable.

  it("transcribe OFF shows the enable prompt, and the section is really gone", () => {
    stubFetch();
    setPlugins({ transcribe: false });
    render(<Harness />);
    const prompt = document.querySelector('[data-enable-plugin="transcribe"]');
    expect(prompt).not.toBeNull();
    // it names the cost, so the user knows what enabling costs
    expect(prompt!.querySelector("[data-plugin-cost]")!.textContent).toMatch(/397 MB/);
    // and the Enable button targets THIS plugin
    expect(
      document.querySelector('[data-enable-plugin-button="transcribe"]'),
    ).not.toBeNull();
    // The real section is absent, asserted on something that EXISTS in it.
    // A selector that matches nothing anywhere would make this line vacuous:
    // the check has to name a control the ON path really renders, and
    // "default captions language" is that control (the ON test above uses it).
    expect(screen.queryByLabelText("default captions language")).not.toBeInTheDocument();
    // and expanding the card name cannot reveal it either
    expect(screen.queryByText("Transcription")).not.toBeInTheDocument();
  });

  it("ai-ask OFF shows the prompt and the Experimental controls are gone", () => {
    stubFetch();
    setPlugins({ "ai-ask": false });
    render(<Harness />);
    expect(document.querySelector('[data-enable-plugin="ai-ask"]')).not.toBeNull();
    expect(
      document.querySelector('[data-enable-plugin-button="ai-ask"]'),
    ).not.toBeNull();
    // the card controls are not merely hidden: they are not rendered
    expect(screen.queryByLabelText("experimental ai enabled")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("ai api key")).not.toBeInTheDocument();
  });

  it("a plugin ON renders the section and NO prompt", () => {
    stubFetch();
    setPlugins({ transcribe: true, "ai-ask": true });
    render(<Harness />);
    expect(document.querySelector('[data-enable-plugin="transcribe"]')).toBeNull();
    expect(document.querySelector('[data-enable-plugin="ai-ask"]')).toBeNull();
    // sanity: the enabling path did not cost us the real cards
    expect(screen.getByText("Transcription")).toBeInTheDocument();
  });

});
