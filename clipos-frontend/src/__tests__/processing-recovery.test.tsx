import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ProgressPage } from "@/pages/ProgressPage";
import { ClipDetailPage } from "@/pages/ClipDetailPage";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({ api: {
  getQueueStatus: vi.fn(), resumePipeline: vi.fn(), cancelPipeline: vi.fn(),
  getHighlights: vi.fn(), burnCaptions: vi.fn(), videoUrl: (_j: string, _c: string, name: string) => `/files/${name}`,
} }));

afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

function progress() {
  return render(<MemoryRouter initialEntries={["/jobs/job/progress"]}><Routes>
    <Route path="/jobs/:jobId/progress" element={<ProgressPage />} />
  </Routes></MemoryRouter>);
}

const base = { job_id: "job", current_stage: "failed", error_message: "Worker lost", started_at: null,
  finished_at: null, run_id: "run", run_status: "failed", execution_stage: "reframe",
  completed_units: 3, total_units: 11, completed_clips: 1, failed_clips: 1 };

describe("Processing recovery", () => {
  it("shows durable partial progress and resumes saved work", async () => {
    vi.mocked(api.getQueueStatus).mockResolvedValue(base);
    vi.mocked(api.resumePipeline).mockResolvedValue({});
    progress();
    await screen.findByText(/3 \/ 11 steps complete/);
    expect(screen.getByText("Worker lost")).toBeTruthy();
    expect(screen.getByRole("link", { name: /View Gallery/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry / resume saved work" }));
    await waitFor(() => expect(api.resumePipeline).toHaveBeenCalledWith("job"));
    await waitFor(() => expect(api.getQueueStatus).toHaveBeenCalledTimes(2));
  });

  it("requests cancellation at the current step boundary", async () => {
    vi.mocked(api.getQueueStatus).mockResolvedValue({ ...base, current_stage: "clips_ready", run_status: "running" });
    vi.mocked(api.cancelPipeline).mockResolvedValue({});
    progress();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel after current step" }));
    await waitFor(() => expect(api.cancelPipeline).toHaveBeenCalledWith("job"));
  });

  it("restores the captioned player and download after reload", async () => {
    vi.mocked(api.getHighlights).mockResolvedValue([{ clip_id: "clip", title: "Title", final_score: 0.8,
      final_hook_text: "Hook", highlight_index: 1, available_variants: ["clip.mp4", "clip_vertical.mp4", "clip_captioned.mp4"] }]);
    render(<MemoryRouter initialEntries={["/jobs/job/clips/clip"]}><Routes>
      <Route path="/jobs/:jobId/clips/:clipId" element={<ClipDetailPage />} />
    </Routes></MemoryRouter>);
    await screen.findByText("Download Captioned");
    expect(document.querySelector("video")?.getAttribute("src")).toBe("/files/clip_captioned.mp4");
    expect(screen.queryByRole("button", { name: "Burn Captions" })).toBeNull();
  });
});
