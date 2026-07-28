import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
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
});
