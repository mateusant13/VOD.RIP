import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import ChannelListIndexBadge from "./ChannelListIndexBadge";

describe("ChannelListIndexBadge", () => {
  it("renders index number for Kick", () => {
    render(<ChannelListIndexBadge platform="Kick" index={3} />);
    const badge = screen.getByText("3");
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain("text-[#53fc18]");
  });

  it("renders index number for Twitch", () => {
    render(<ChannelListIndexBadge platform="Twitch" index={1} />);
    const badge = screen.getByText("1");
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain("text-[#9146FF]");
  });

  it("shows platform title", () => {
    render(<ChannelListIndexBadge platform="Kick" index={5} />);
    expect(screen.getByTitle("Kick #5")).toBeInTheDocument();
  });

  it("uses sm size by default", () => {
    render(<ChannelListIndexBadge platform="Kick" index={1} />);
    const badge = screen.getByText("1");
    expect(badge.className).toContain("w-4");
  });

  it("uses md size when specified", () => {
    render(<ChannelListIndexBadge platform="Kick" index={1} size="md" />);
    const badge = screen.getByText("1");
    expect(badge.className).toContain("w-5");
  });
});
