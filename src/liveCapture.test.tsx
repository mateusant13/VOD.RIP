import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LiveBadge, LiveWatchButton, type LiveEntry } from "./components/LiveBadge";

const mkEntry = (overrides?: Partial<LiveEntry>): LiveEntry => ({
  platform: "kick",
  is_live: true,
  title: "Test stream",
  url: "https://kick.com/test",
  headers: {},
  type: "hls",
  ...overrides,
});

describe("LiveBadge", () => {
  it("renders LIVE badge when entries are live", () => {
    render(<LiveBadge entries={[mkEntry()]} />);
    expect(screen.getByText("LIVE")).toBeInTheDocument();
  });

  it("returns null when no entries", () => {
    const { container } = render(<LiveBadge entries={[]} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("LiveWatchButton", () => {
  it("calls onWatch with the entry when clicked (single entry)", async () => {
    const onWatch = vi.fn();
    const entry = mkEntry({ platform: "kick" });
    render(
      <LiveWatchButton entries={[entry]} onWatch={onWatch} />,
    );
    await userEvent.click(screen.getByRole("button"));
    expect(onWatch).toHaveBeenCalledTimes(1);
    expect(onWatch).toHaveBeenCalledWith(entry);
  });

  it("calls onShowPicker when multiple entries", async () => {
    const onShowPicker = vi.fn();
    render(
      <LiveWatchButton
        entries={[mkEntry({ platform: "kick" }), mkEntry({ platform: "twitch" })]}
        onWatch={vi.fn()}
        onShowPicker={onShowPicker}
      />,
    );
    await userEvent.click(screen.getByRole("button"));
    expect(onShowPicker).toHaveBeenCalledTimes(1);
    expect(onShowPicker).toHaveBeenCalledWith();
  });
});
