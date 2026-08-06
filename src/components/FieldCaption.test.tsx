import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import FieldCaption from "./FieldCaption";

describe("FieldCaption", () => {
  it("renders children text", () => {
    render(<FieldCaption>Download Settings</FieldCaption>);
    expect(screen.getByText("Download Settings")).toBeInTheDocument();
  });

  it("applies noWrap class when noWrap is true", () => {
    const { container } = render(<FieldCaption noWrap>Short</FieldCaption>);
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("whitespace-nowrap");
  });

  it("does not apply noWrap when not set", () => {
    const { container } = render(<FieldCaption>Normal</FieldCaption>);
    const root = container.firstChild as HTMLElement;
    expect(root.className).not.toContain("whitespace-nowrap");
  });

  it("renders an info affordance carrying the description", () => {
    render(<FieldCaption info="Explains the field">Label</FieldCaption>);
    expect(screen.getByRole("button", { name: "Explains the field" })).toBeInTheDocument();
  });
});
