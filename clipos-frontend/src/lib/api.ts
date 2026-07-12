const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export interface Job {
  job_id: string;
  source_filename: string;
  status: string;
  duration_seconds: number | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface HighlightClip {
  clip_id: string;
  final_score: number;
  title: string;
  final_hook_text: string;
  highlight_index: number;
}

export interface QueueStatus {
  job_id: string;
  current_stage: string;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, options);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

export const api = {
  listJobs: () => request<Job[]>("/jobs"),

  getJob: (jobId: string) => request<Job>(`/jobs/${jobId}`),

  getHighlights: (jobId: string) => request<HighlightClip[]>(`/jobs/${jobId}/highlights`),

  getQueueStatus: (jobId: string) => request<QueueStatus>(`/jobs/${jobId}/queue_status`),

  upload: async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    const res = await fetch(`${API_BASE}/ingest/upload`, {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || res.statusText);
    }
    return res.json() as Promise<{ job_id: string }>;
  },

  youtube: (url: string) =>
    request<{ job_id: string }>("/ingest/youtube", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }),

  runPipeline: (jobId: string) =>
    request<{ rq_job_id: string; status: string }>(`/jobs/${jobId}/run`, {
      method: "POST",
    }),

  burnCaptions: (jobId: string, clipId: string) =>
    request<void>(`/jobs/${jobId}/clips/${clipId}/burn`, { method: "POST" }),

  videoUrl: (jobId: string, clipId: string, filename: string) =>
    `${API_BASE}/files/clips/${jobId}/${clipId}/${filename}`,
};
