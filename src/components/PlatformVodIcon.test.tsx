import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import PlatformVodIcon from "./PlatformVodIcon";

vi.mock("@/assets/platforms/kick.ico", () => ({ default: "kick.ico" }));
vi.mock("@/assets/platforms/twitch.png", () => ({ default: "twitch.png" }));

describe("PlatformVodIcon", () => {
  it("renders Twitch icon for Twitch platform", () => {
    render(<PlatformVodIcon platform="Twitch" />);
    const img = screen.getByAltText("Twitch");
    expect(img).toBeInTheDocument();
    expect(img).toHaveAttribute("src", "twitch.png");
  });

  it("renders Kick icon for Kick platform", () => {
    render(<PlatformVodIcon platform="Kick" />);
    const img = screen.getByAltText("Kick");
    expect(img).toBeInTheDocument();
    expect(img).toHaveAttribute("src", "kick.ico");
  });

  it("renders YouTube icon for YouTube platform", () => {
    render(<PlatformVodIcon platform="YouTube" />);
    expect(screen.getByLabelText("YouTube")).toBeInTheDocument();
  });
});
