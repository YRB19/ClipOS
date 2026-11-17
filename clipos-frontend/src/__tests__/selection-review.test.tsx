import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FeedbackButtons, SelectionReview } from "@/components/SelectionReview";
import { api } from "@/lib/api";
vi.mock("@/lib/api", () => ({ api: { saveFeedback: vi.fn(), getCandidates: vi.fn(), addMarker: vi.fn(), resumePipeline: vi.fn() } }));
afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());
const clip = { clip_id: "clip", title: "Win", final_hook_text: "Payoff", final_score: .8, highlight_index: 0,
  start: 10, end: 30, rank_reasoning: "Complete payoff", feedback: "automatic" as const };

describe("Optional selection review", () => {
  it("saves keep/skip feedback without starting another pipeline", async () => {
    vi.mocked(api.saveFeedback).mockResolvedValue({});
    render(<FeedbackButtons jobId="job" clip={clip} />);
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    await screen.findByText(/Saved. Resume processing/);
    expect(api.saveFeedback).toHaveBeenCalledWith("job", "clip", "rejected");
    expect(screen.getByRole("button", { name: "Skip" }).getAttribute("aria-pressed")).toBe("true");
    expect(api.resumePipeline).not.toHaveBeenCalled();
  });
  it("preserves the current choice when an active run blocks feedback", async () => {
    vi.mocked(api.saveFeedback).mockRejectedValue(new Error("Cancel automatic processing first"));
    render(<FeedbackButtons jobId="job" clip={clip} />);
    fireEvent.click(screen.getByRole("button", { name: "Keep" }));
    await screen.findByText("Cancel automatic processing first");
    expect(screen.getByRole("button", { name: "Automatic" }).getAttribute("aria-pressed")).toBe("true");
  });
  it("loads alternatives only on request and marks an absolute source time", async () => {
    vi.mocked(api.getCandidates).mockResolvedValue([clip]);
    vi.mocked(api.addMarker).mockResolvedValue({});
    render(<SelectionReview jobId="job" />);
    expect(api.getCandidates).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Optional selection review" }));
    await screen.findByText("Complete payoff");
    fireEvent.change(screen.getByLabelText("Moment at (seconds)"), { target: { value: "123.5" } });
    fireEvent.change(screen.getByLabelText("Moment note"), { target: { value: "Boss defeated" } });
    fireEvent.click(screen.getByRole("button", { name: "Mark moment" }));
    await waitFor(() => expect(api.addMarker).toHaveBeenCalledWith("job", 123.5, "Boss defeated"));
    expect(api.resumePipeline).not.toHaveBeenCalled();
  });
});
