import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import EditableHmsTime from "./EditableHmsTime";

describe("EditableHmsTime", () => {
  it("renders time in HH:MM:SS format", () => {
    render(
      <EditableHmsTime valueSec={3661} minSec={0} maxSec={7200} onChange={() => {}} />
    );
    expect(screen.getByText("01:01:01")).toBeInTheDocument();
  });

  it("renders zero-padded time", () => {
    render(
      <EditableHmsTime valueSec={5} minSec={0} maxSec={7200} onChange={() => {}} />
    );
    expect(screen.getByText("00:00:05")).toBeInTheDocument();
  });

  it("enters edit mode on click", async () => {
    const user = userEvent.setup();
    render(
      <EditableHmsTime valueSec={100} minSec={0} maxSec={7200} onChange={() => {}} />
    );
    const display = screen.getByRole("button");
    await user.click(display);
    // After click, it should show a contentEditable span
    const editable = document.querySelector('[contenteditable="true"]');
    expect(editable).toBeInTheDocument();
  });

  it("calls onChange with clamped value on Enter", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <EditableHmsTime valueSec={100} minSec={10} maxSec={200} onChange={onChange} />
    );
    const display = screen.getByRole("button");
    await user.click(display);
    const editable = document.querySelector('[contenteditable="true"]');
    expect(editable).toBeInTheDocument();
    if (editable) {
      // Clear and type new value
      editable.textContent = "00:05:00";
      await user.keyboard("{Enter}");
    }
    // 5 minutes = 300s, but clamped to maxSec=200
    expect(onChange).toHaveBeenCalledWith(200);
  });

  it("exits edit mode on Escape without calling onChange", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <EditableHmsTime valueSec={100} minSec={0} maxSec={7200} onChange={onChange} />
    );
    const display = screen.getByRole("button");
    await user.click(display);
    const editable = document.querySelector('[contenteditable="true"]');
    expect(editable).toBeInTheDocument();
    if (editable) {
      editable.textContent = "99:99:99";
    }
    await user.keyboard("{Escape}");
    expect(onChange).not.toHaveBeenCalled();
  });
});
