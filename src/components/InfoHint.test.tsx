import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import InfoHint from "./InfoHint";

const rect = (top: number, left: number, width: number, height: number): DOMRect =>
  ({
    top,
    left,
    right: left + width,
    bottom: top + height,
    width,
    height,
    x: left,
    y: top,
    toJSON: () => ({}),
  }) as DOMRect;

const px = (v: string) => parseInt(v, 10);

/** jsdom reports a 1024x768 viewport. Pin the button's rect for every
 *  element and the scroller's own rect for the .custom-scrollbar wrapper. */
function pinLayout(buttonRect: DOMRect, scrollerRect?: DOMRect) {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    if (scrollerRect && this.classList.contains("custom-scrollbar")) return scrollerRect;
    return buttonRect;
  });
}

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
    // pinned box is interactive so long text can be scrolled inside it
    expect(screen.getByRole("tooltip").className).toContain("pointer-events-auto");
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

  it("closes the pinned popover on Escape", () => {
    render(<InfoHint text="Esc hint" />);
    const btn = screen.getByRole("button", { name: "Esc hint" });
    fireEvent.click(btn);
    expect(screen.getByText("Esc hint")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("Esc hint")).not.toBeInTheDocument();
  });

  it("wires aria-describedby from the button to the tooltip box while visible", () => {
    render(<InfoHint text="Described hint" />);
    const btn = screen.getByRole("button", { name: "Described hint" });
    // Idle: no dangling description reference to a missing node.
    expect(btn).not.toHaveAttribute("aria-describedby");
    fireEvent.mouseEnter(btn);
    const tip = screen.getByRole("tooltip");
    expect(btn.getAttribute("aria-describedby")).toBe(tip.id);
    fireEvent.mouseLeave(btn);
    expect(btn).not.toHaveAttribute("aria-describedby");
  });

  it("Escape dismisses the hover tooltip too (keyboard users cannot mouse-leave)", () => {
    render(<InfoHint text="Hover esc hint" />);
    const btn = screen.getByRole("button", { name: "Hover esc hint" });
    fireEvent.mouseEnter(btn);
    expect(screen.getByText("Hover esc hint")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("Hover esc hint")).not.toBeInTheDocument();
  });

  it("has no native tooltip: only the in-DOM box shows on hover", () => {
    render(<InfoHint text="No native tooltip" />);
    const btn = screen.getByRole("button", { name: "No native tooltip" });
    expect(btn).not.toHaveAttribute("title");
  });

  it("stays inside the viewport near the bottom-right edge: flips above and right-aligns", () => {
    const buttonRect = rect(700, 900, 16, 16);
    pinLayout(buttonRect);
    render(<InfoHint text="Edge hint" />);
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Edge hint" }));
    const tip = screen.getByRole("tooltip");
    expect(tip.className).toContain("bottom-full");
    expect(tip.className).toContain("z-50");
    expect(tip.className).toContain("pointer-events-none");
    // right-aligned: box right edge = button right edge = 916
    expect(px(tip.style.left)).toBe(916 - 224 - 900);
    const boxLeft = 900 + px(tip.style.left);
    expect(boxLeft).toBeGreaterThanOrEqual(0);
    expect(boxLeft + px(tip.style.maxWidth)).toBeLessThanOrEqual(1024);
    expect(screen.getByText("Edge hint")).toBeInTheDocument();
  });

  it("stays below/left-aligned when the button has room on both sides", () => {
    pinLayout(rect(200, 300, 16, 16));
    render(<InfoHint text="Middle hint" />);
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Middle hint" }));
    const tip = screen.getByRole("tooltip");
    expect(tip.className).toContain("top-full");
    expect(px(tip.style.left)).toBe(0);
  });

  it("caps long text to the available room with internal scroll, no layout overflow", () => {
    const LONG = "x".repeat(300);
    const buttonRect = rect(150, 20, 16, 16);
    const scrollerRect = rect(0, 0, 300, 180); // button near the bottom edge
    pinLayout(buttonRect, scrollerRect);
    render(
      <div className="custom-scrollbar">
        <InfoHint text={LONG} />
      </div>
    );
    fireEvent.mouseEnter(screen.getByRole("button", { name: LONG }));
    const tip = screen.getByRole("tooltip");
    // 300 chars need ~164px; only 144px fit above the button → capped, scrolls
    expect(tip.className).toContain("overflow-y-auto");
    expect(tip.className).toContain("bottom-full");
    expect(px(tip.style.maxHeight)).toBe(144 - 2);
    expect(px(tip.style.maxHeight)).toBeLessThan(164);
    // box stays inside the scroller's visible area (no truncation)
    const boxTop = buttonRect.top - 6 - px(tip.style.maxHeight);
    expect(boxTop).toBeGreaterThanOrEqual(0);
    expect(20 + px(tip.style.maxWidth) + 6).toBeLessThanOrEqual(300);
  });

  it("clamps horizontally inside a container narrower than the box", () => {
    const buttonRect = rect(100, 150, 16, 16);
    const scrollerRect = rect(0, 0, 200, 400); // 188px of room, box wants 224
    pinLayout(buttonRect, scrollerRect);
    render(
      <div className="custom-scrollbar">
        <InfoHint text="Narrow hint" />
      </div>
    );
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Narrow hint" }));
    const tip = screen.getByRole("tooltip");
    expect(px(tip.style.maxWidth)).toBe(200 - 12);
    const boxLeft = 150 + px(tip.style.left);
    expect(boxLeft).toBeGreaterThanOrEqual(6);
    expect(boxLeft + px(tip.style.maxWidth)).toBeLessThanOrEqual(200 - 6);
  });
});
