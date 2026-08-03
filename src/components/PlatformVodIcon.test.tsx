import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import PlatformVodIcon from "./PlatformVodIcon";

describe("PlatformVodIcon", () => {
  it("renders Twitch as inline SVG (no img)", () => {
    const { container } = render(<PlatformVodIcon platform="Twitch" />);
    const svg = screen.getByLabelText("Twitch");
    expect(svg.tagName).toBe("svg");
    expect(svg.querySelector("path")).not.toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("renders Kick as inline SVG (no img)", () => {
    const { container } = render(<PlatformVodIcon platform="Kick" />);
    const svg = screen.getByLabelText("Kick");
    expect(svg.tagName).toBe("svg");
    expect(svg.querySelector("path")).not.toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("renders YouTube icon for YouTube platform", () => {
    render(<PlatformVodIcon platform="YouTube" />);
    expect(screen.getByLabelText("YouTube")).toBeInTheDocument();
  });
});
