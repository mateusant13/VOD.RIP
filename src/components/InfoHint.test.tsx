import { describe, expect, it, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import InfoHint from "./InfoHint";

describe("InfoHint", () => {
  afterEach(() => {
    document.body.innerHTML = "";
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
});
