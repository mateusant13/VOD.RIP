import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import CookieBridgeSection from "./CookieBridgeSection";

const STATUS = {
  paired: true,
  enabled: true,
  platforms: {
    youtube: { count: 3, lastGrabAt: "2026-08-03T01:00:00Z", expiredCount: 0 },
    twitch: { count: 0, lastGrabAt: null, expiredCount: 2 },
  },
};

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes("/api/session/cookies/status")
        ? STATUS
        : url.includes("/api/session/cookies/token")
          ? { token: "tok-1" }
          : { extension_id: "abc" };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    })
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("CookieBridgeSection", () => {
  it("renders per-platform count, last grab time and expired warning", async () => {
    stubFetch();
    render(<CookieBridgeSection />);
    await waitFor(() =>
      expect(screen.getByText(/● paired/)).toBeInTheDocument()
    );
    expect(screen.getByText(/YouTube: 3/)).toBeInTheDocument();
    expect(screen.getByText(/Twitch: 0/)).toBeInTheDocument();
    expect(screen.getByText(/2 expired/)).toBeInTheDocument();
    expect(screen.getByText(/abc/)).toBeInTheDocument();
  });
});
