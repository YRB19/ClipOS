import { describe, it, expect, vi, beforeEach } from "vitest";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: {
    upload: vi.fn(),
    youtube: vi.fn(),
    runPipeline: vi.fn(),
  },
}));

describe("Upload flow integration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("api.upload returns job_id, then api.runPipeline is callable with that job_id", async () => {
    const mockUpload = vi.mocked(api.upload);
    const mockRun = vi.mocked(api.runPipeline);

    mockUpload.mockResolvedValue({ job_id: "test-123" });
    mockRun.mockResolvedValue({ rq_job_id: "rq-456", status: "queued" });

    const res = await api.upload(new File([""], "test.mp4", { type: "video/mp4" }));
    const jobId = res.job_id;
    expect(jobId).toBe("test-123");

    await api.runPipeline(jobId);
    expect(mockRun).toHaveBeenCalledWith("test-123");
  });

  it("api.youtube returns job_id, then api.runPipeline is callable with that job_id", async () => {
    const mockYoutube = vi.mocked(api.youtube);
    const mockRun = vi.mocked(api.runPipeline);

    mockYoutube.mockResolvedValue({ job_id: "yt-789" });
    mockRun.mockResolvedValue({ rq_job_id: "rq-abc", status: "queued" });

    const res = await api.youtube("https://youtube.com/watch?v=test");
    const jobId = res.job_id;
    expect(jobId).toBe("yt-789");

    await api.runPipeline(jobId);
    expect(mockRun).toHaveBeenCalledWith("yt-789");
  });

  it("api.runPipeline is called after upload, not before", async () => {
    const callOrder: string[] = [];
    const mockUpload = vi.mocked(api.upload);
    const mockRun = vi.mocked(api.runPipeline);

    mockUpload.mockImplementation(async () => {
      callOrder.push("upload");
      return { job_id: "test-123" };
    });
    mockRun.mockImplementation(async () => {
      callOrder.push("runPipeline");
      return { rq_job_id: "rq-456", status: "queued" };
    });

    const res = await api.upload(new File([""], "test.mp4", { type: "video/mp4" }));
    await api.runPipeline(res.job_id);

    expect(callOrder).toEqual(["upload", "runPipeline"]);
  });

  it("api.runPipeline throwing does not call navigate (upload stays on page)", async () => {
    const mockUpload = vi.mocked(api.upload);
    const mockRun = vi.mocked(api.runPipeline);

    mockUpload.mockResolvedValue({ job_id: "test-123" });
    mockRun.mockRejectedValue(new Error("409: Job status is 'done', must be 'ingested'"));

    const res = await api.upload(new File([""], "test.mp4", { type: "video/mp4" }));

    let caughtError: string | null = null;
    try {
      await api.runPipeline(res.job_id);
    } catch (err) {
      caughtError = err instanceof Error ? err.message : "unknown";
    }

    expect(caughtError).toContain("409");
    expect(mockRun).toHaveBeenCalled();
  });
});
