import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { UploadPage } from "@/pages/UploadPage";
import { ProgressPage } from "@/pages/ProgressPage";
import { ClipDetailPage } from "@/pages/ClipDetailPage";
import { GalleryPage } from "@/pages/GalleryPage";
import { api } from "@/lib/api";
vi.mock("@/lib/api", () => ({ api: {
  library: vi.fn(), probeLibrary: vi.fn(), importLocal: vi.fn(), runPipeline: vi.fn(),
  getQueueStatus: vi.fn(), getHighlights: vi.fn(), videoUrl: vi.fn(), mediaUrl: (s: string) => s,
} }));
afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

describe("Long recording flow", () => {
  it("imports a library MKV with the selected microphone and starts processing", async () => {
    vi.mocked(api.library).mockResolvedValue({ configured: true, files: ["game.mkv"] });
    vi.mocked(api.probeLibrary).mockResolvedValue({ audio_tracks: [
      { stream_index: 1, title: "Game", codec: "aac" }, { stream_index: 2, title: "Microphone", codec: "aac" }] });
    vi.mocked(api.importLocal).mockResolvedValue({ job_id: "local-job" });
    vi.mocked(api.runPipeline).mockResolvedValue({ rq_job_id: "rq", status: "queued" });
    render(<MemoryRouter><Routes><Route path="/" element={<UploadPage />} />
      <Route path="/jobs/:jobId/progress" element={<p>Queued recording</p>} /></Routes></MemoryRouter>);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Local library" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("tab", { name: "Local library" }));
    await screen.findByRole("option", { name: "game.mkv" });
    fireEvent.change(screen.getByLabelText("Recording"), { target: { value: "game.mkv" } });
    await screen.findByRole("option", { name: "Microphone (aac)" });
    fireEvent.change(screen.getByLabelText("Microphone for captions"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Start Pipeline" }));
    await screen.findByText("Queued recording");
    expect(api.importLocal).toHaveBeenCalledWith("game.mkv", 2);
    expect(api.runPipeline).toHaveBeenCalledWith("local-job");
  });
  it("offers the gallery while later sections are analyzed", async () => {
    vi.mocked(api.getQueueStatus).mockResolvedValue({ job_id: "job", current_stage: "transcribed", error_message: null,
      started_at: null, finished_at: null, run_status: "running", run_id: "run", analyzed_chunks: 1,
      total_chunks: 36, completed_units: 3, total_units: 74, execution_stage: "transcribe_chunk" });
    render(<MemoryRouter initialEntries={["/jobs/job/progress"]}><Routes>
      <Route path="/jobs/:jobId/progress" element={<ProgressPage />} /></Routes></MemoryRouter>);
    await screen.findByText(/1 \/ 36 sections analyzed/);
    expect(screen.getByRole("link", { name: /View Gallery/ })).toBeTruthy();
  });
  it("shows an early candidate without requesting a nonexistent video", async () => {
    vi.mocked(api.getHighlights).mockResolvedValue([{ clip_id: "early", title: "", final_score: 0,
      final_hook_text: "", highlight_index: 0, available_variants: [], hook_text: "Early payoff", virality_score: .8 }]);
    render(<MemoryRouter initialEntries={["/jobs/job"]}><Routes>
      <Route path="/jobs/:jobId" element={<GalleryPage />} /></Routes></MemoryRouter>);
    await screen.findByText("Early payoff");
    await waitFor(() => expect(document.querySelector("video")).toBeNull());
    expect(screen.getByText(/awaiting ranking or rendering/)).toBeTruthy();
  });
});


it("keeps early-candidate details free of unavailable downloads", async () => {
  vi.mocked(api.getHighlights).mockResolvedValue([{ clip_id: "early", title: "", final_score: 0,
    final_hook_text: "", highlight_index: 0, available_variants: [], hook_text: "Early payoff" }]);
  render(<MemoryRouter initialEntries={["/jobs/job/clips/early"]}><Routes>
    <Route path="/jobs/:jobId/clips/:clipId" element={<ClipDetailPage />} /></Routes></MemoryRouter>);
  await screen.findByText("Early payoff");
  expect(document.querySelector("video")).toBeNull();
  expect(screen.queryByRole("button", { name: /Download/ })).toBeNull();
  expect(screen.queryByRole("button", { name: "Burn Captions" })).toBeNull();
});
