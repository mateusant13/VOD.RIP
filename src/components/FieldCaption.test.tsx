import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import FieldCaption from "./FieldCaption";

describe("FieldCaption", () => {
  it("renders children text", () => {
    render(<FieldCaption>Download Settings</FieldCaption>);
    expect(screen.getByText("Download Settings")).toBeInTheDocument();
  });

  it("applies noWrap class when noWrap is true", () => {
    const { container } = render(<FieldCaption noWrap>Short</FieldCaption>);
    const span = container.firstChild as HTMLElement;
    expect(span.className).toContain("whitespace-nowrap");
  });

  it("does not apply noWrap when not set", () => {
    const { container } = render(<FieldCaption>Normal</FieldCaption>);
    const span = container.firstChild as HTMLElement;
    expect(span.className).not.toContain("whitespace-nowrap");
  });
});
