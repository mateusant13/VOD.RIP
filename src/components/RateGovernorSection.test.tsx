/**
 * RateGovernorSection — the honest-rendering contract.
 *
 * LIVE is the exact body returned by GET /api/archive/rate-budget on the
 * running app (2026-10-04), trimmed to the two rows that matter. It is
 * deliberately a real payload, not a tidy fixture: the YouTube row is the
 * interesting one, because its ceiling was learned from saved history
 * (4.8 vs the 20 default) while this session has recorded ZERO events —
 * the exact case where a UI that renders "0 events / 0 seconds since trip"
 * would be lying.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import RateGovernorSection from "./RateGovernorSection";
import type { RateBudgetStatus } from "../types";

const LIVE: RateBudgetStatus = {
  auto_share: 0.7,
  max_auto_wait_s: 30,
  platforms: [
    {
      platform: "youtube",
      ceiling_rpm: 4.802,
      default_ceiling_rpm: 20,
      auto_share: 0.7,
      auto: { tokens: 0, capacity: 3.361, refill_rpm: 3.361, exhausted: true },
      user: { tokens: 4.802, capacity: 4.802, refill_rpm: 4.802 },
      learning: {
        events: 0,
        trip_rpm: 0,
        min_trip_rpm: 6.86,
        seconds_since_event: null,
        ramp_pending: false,
      },
      hot: { calls_last_min: 0, limited_last_min: 0 },
    },
    {
      platform: "kick",
      ceiling_rpm: 20,
      default_ceiling_rpm: 20,
      auto_share: 0.7,
      auto: { tokens: 14, capacity: 14, refill_rpm: 14, exhausted: false },
      user: { tokens: 20, capacity: 20, refill_rpm: 20 },
      learning: {
        events: 0,
        trip_rpm: 0,
        min_trip_rpm: 0,
        seconds_since_event: null,
        ramp_pending: false,
      },
      hot: { calls_last_min: 0, limited_last_min: 0 },
    },
  ],
  scheduler: {
    youtube: { auto_exhausted: true, backoff_s: 17.85 },
    kick: { auto_exhausted: false, backoff_s: 0 },
  },
  recent_decisions: [
    { platform: "youtube", source: "auto", allowed: false, wait_s: 17.85, ceiling_rpm: 4.802, tokens: -196.64, reason: "auto_exhausted" },
  ],
};

function stubRateBudget(body: unknown, status = 200) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("RateGovernorSection", () => {
  it("renders the live values from the endpoint, not recomputed ones", async () => {
    stubRateBudget(LIVE);
    render(<RateGovernorSection />);

    // Kick is untouched: ceiling 20, AUTO capped at 14 (70%), USER full 20.
    expect(await screen.findByText("20 req/min")).toBeInTheDocument();
    expect(screen.getByText("Allowed right now: 20 requests per minute.")).toBeInTheDocument();
    // One AUTO caption per platform row (both are capped at the same 70%).
    expect(screen.getAllByText("May use 70% of the ceiling — never more, so a background storm cannot spend your share.")).toHaveLength(2);
    expect(screen.getByText("Gets all 20 req/min. Background work can never draw from this reserve.")).toBeInTheDocument();
  });

  it("translates the AUTO/USER split into plain language with both pool balances", async () => {
    stubRateBudget(LIVE);
    render(<RateGovernorSection />);
    await screen.findByText("Allowed right now: 20 requests per minute.");

    // Two "of N left" rows per platform (AUTO + USER) — the reserve is visible.
    const leftRows = screen.getAllByText(/of .* left/);
    expect(leftRows.length).toBe(4);
    expect(screen.getByText("14 of 14 left")).toBeInTheDocument(); // kick AUTO, full
    expect(screen.getByText("20 of 20 left")).toBeInTheDocument(); // kick USER reserve
  });

  it("NULL seconds_since_event reads as 'no events yet', never as a zero age", async () => {
    stubRateBudget(LIVE);
    render(<RateGovernorSection />);
    await screen.findByText("Allowed right now: 20 requests per minute.");

    // seconds_since_event is null in the live payload. A fabricated "0s ago"
    // would claim a clean window that was never observed.
    expect(screen.getAllByText("No rate-limit events recorded in this session.")).toHaveLength(2);
    expect(screen.queryByText(/0s ago/)).not.toBeInTheDocument();
    expect(screen.queryByText(/last one/)).not.toBeInTheDocument();
  });

  it("separates 'no events this session' from a ceiling learned from saved history", async () => {
    stubRateBudget(LIVE);
    render(<RateGovernorSection />);
    await screen.findByText("Allowed right now: 20 requests per minute.");

    // YouTube: 0 events, but the ceiling is 4.8 against a 20 default — primed
    // from history. Saying only "no events" would hide the real learning.
    expect(screen.getByText("4.8 req/min")).toBeInTheDocument();
    expect(screen.getByText("Learned down from the 20 req/min default.")).toBeInTheDocument();
    expect(
      screen.getByText("Ceiling learned earlier from saved history (lowest recorded trip 6.9 req/min)."),
    ).toBeInTheDocument();
  });

  it("surfaces a real trip age and trip rate when events exist", async () => {
    stubRateBudget({
      ...LIVE,
      platforms: [
        {
          ...LIVE.platforms[0],
          learning: { events: 3, trip_rpm: 19.2, min_trip_rpm: 9.8, seconds_since_event: 420, ramp_pending: true },
        },
      ],
    });
    render(<RateGovernorSection />);

    expect(await screen.findByText("3 limit events recorded · last one 7min ago.")).toBeInTheDocument();
    expect(screen.getByText("Lowest rate that ever tripped it: 9.8 req/min.")).toBeInTheDocument();
    expect(
      screen.getByText("A clean window has passed — the ceiling will step back up slightly on the next request."),
    ).toBeInTheDocument();
  });

  it("reports a spent AUTO pool with the scheduler's real backoff", async () => {
    stubRateBudget(LIVE);
    render(<RateGovernorSection />);
    await screen.findByText("Allowed right now: 20 requests per minute.");
    expect(screen.getByText("Spent for now — background work retries in about 17.9s.")).toBeInTheDocument();
  });

  it("degrades honestly when the endpoint is unreachable: no values, no zeros", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("Backend not running."); }));
    render(<RateGovernorSection />);

    // apiFetch retries twice with backoff, so the alert lands ~1.2s later.
    const alert = await screen.findByRole("alert", undefined, { timeout: 5000 });
    expect(alert).toHaveTextContent("Could not reach the rate governor");
    expect(alert).toHaveTextContent("No values are shown");
    // Nothing is invented: no ceiling badge, no pool balances.
    expect(screen.queryByText(/req\/min$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/of .* left/)).not.toBeInTheDocument();
  });

  it("fetches once on mount and only again on an explicit Refresh", async () => {
    const fetchMock = stubRateBudget(LIVE);
    render(<RateGovernorSection />);
    await screen.findByText("Allowed right now: 20 requests per minute.");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // No polling loop: idling past several timer intervals adds no requests.
    await new Promise((r) => setTimeout(r, 250));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByLabelText("refresh rate governor"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });
});
