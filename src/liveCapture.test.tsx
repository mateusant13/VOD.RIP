// Real Time button live-edge snap is in App.tsx (not an extracted component),
// so it's exercised end-to-end via the preview player flow, not unit-tested here.

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { LiveBadge, type LiveEntry } from "./components/LiveBadge";

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

  it("shows platform and title in tooltip", () => {
    render(<LiveBadge entries={[mkEntry({ platform: "twitch", title: "My live stream" })]} />);
    const badge = screen.getByText("LIVE");
    expect(badge).toHaveAttribute("title", "twitch: My live stream");
  });

  it("is a clickable button when onClick is provided", () => {
    const onClick = vi.fn();
    render(<LiveBadge entries={[mkEntry()]} onClick={onClick} ariaLabel="Live Test" />);
    const badge = screen.getByText("LIVE");
    expect(badge.tagName).toBe("BUTTON");
    expect(badge).toHaveAttribute("aria-label", "Live Test");
    fireEvent.click(badge);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("stays a plain span when no onClick is provided", () => {
    const { container } = render(<LiveBadge entries={[mkEntry()]} />);
    expect(screen.getByText("LIVE").tagName).toBe("SPAN");
    expect(container.querySelector("button")).toBeNull();
  });
});
