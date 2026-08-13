import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import InfoHint from "./InfoHint";

describe("InfoHint", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("renders a button whose accessible name is the description, kept out of the DOM while idle", () => {
    render(<InfoHint text="Explains the field" />);
    const btn = screen.getByRole("button", { name: "Explains the field" });
    expect(btn).toHaveAttribute("aria-expanded", "false");
    // rows stay compact: the description is not body text until shown
    expect(screen.queryByText("Explains the field")).not.toBeInTheDocument();
  });

  it("shows the description on hover and hides it on leave", () => {
    render(<InfoHint text="Hover hint" />);
    const btn = screen.getByRole("button", { name: "Hover hint" });
    fireEvent.mouseEnter(btn);
    expect(screen.getByText("Hover hint")).toBeInTheDocument();
    fireEvent.mouseLeave(btn);
    expect(screen.queryByText("Hover hint")).not.toBeInTheDocument();
  });

  it("pins the description on click and unpins on re-click", () => {
    render(<InfoHint text="Click hint" />);
    const btn = screen.getByRole("button", { name: "Click hint" });
    fireEvent.click(btn);
    expect(screen.getByText("Click hint")).toBeInTheDocument();
    expect(btn).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(btn);
    expect(screen.queryByText("Click hint")).not.toBeInTheDocument();
    expect(btn).toHaveAttribute("aria-expanded", "false");
  });

  it("closes the pinned popover on outside pointer down", () => {
    render(
      <div>
        <InfoHint text="Outside hint" />
        <button type="button">elsewhere</button>
      </div>
    );
    const btn = screen.getByRole("button", { name: "Outside hint" });
    fireEvent.click(btn);
    expect(screen.getByText("Outside hint")).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByRole("button", { name: "elsewhere" }));
    expect(screen.queryByText("Outside hint")).not.toBeInTheDocument();
  });

  it("has no native tooltip: only the in-DOM box shows on hover", () => {
    render(<InfoHint text="No native tooltip" />);
    const btn = screen.getByRole("button", { name: "No native tooltip" });
    expect(btn).not.toHaveAttribute("title");
  });

  it("clamps into the viewport: flips above and aligns right near the bottom-right edge", () => {
    // jsdom reports 1024x768; pin the button near the bottom-right corner.
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      top: 700, left: 900, right: 916, bottom: 716, width: 16, height: 16,
      x: 900, y: 700, toJSON: () => ({}),
    } as DOMRect);
    render(<InfoHint text="Edge hint" />);
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Edge hint" }));
    const tip = screen.getByRole("tooltip");
    expect(tip.className).toContain("bottom-full");
    expect(tip.className).toContain("right-0");
    expect(tip.className).toContain("z-50");
    expect(tip.className).toContain("pointer-events-none");
    expect(screen.getByText("Edge hint")).toBeInTheDocument();
  });

  it("stays below/left-aligned when the button has room on both sides", () => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      top: 200, left: 300, right: 316, bottom: 216, width: 16, height: 16,
      x: 300, y: 200, toJSON: () => ({}),
    } as DOMRect);
    render(<InfoHint text="Middle hint" />);
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Middle hint" }));
    const tip = screen.getByRole("tooltip");
    expect(tip.className).toContain("top-full");
    expect(tip.className).toContain("left-0");
  });
});
