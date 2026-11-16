const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export interface Job {
  job_id: string;
  source_filename: string;
  run_status?: string | null;
  execution_stage?: string | null;
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
  available_variants?: string[];
  final_render?: RenderOutput | null;
  processing_status?: string | null;
  processing_error?: string | null;
}

export interface QueueStatus {
  job_id: string;
  current_stage: string;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  execution_stage?: string;
  run_id?: string;
  run_status?: string | null;
  rq_state?: string;
  cancel_requested?: boolean;
  completed_units?: number;
  total_units?: number;
  completed_clips?: number;
  failed_clips?: number;
  stages?: { stage: string; clip_id: string | null; status: string; elapsed_seconds: number | null; error_message: string | null }[];
  clips?: { clip_id: string; stage: string; status: string; error_message: string | null }[];
}

export interface SourceRegion { x: number; y: number; width: number; height: number }
export interface EditRecipe {
  schema_version: 1;
  source: { job_id: string; fingerprint: string };
  intervals: { start: number; end: number }[];
  layout: { mode: "fit_background" | "gameplay_facecam" | "manual_crop"; gameplay: SourceRegion;
    facecam: SourceRegion | null; facecam_height: number; background: string;
    crop_keyframes: { time: number; x: number; y: number }[] };
  captions: { enabled: boolean; words: { word: string; start: number; end: number }[];
    style: { font: string; font_size: number; color: string; highlight_color: string; margin_bottom: number } };
  audio: { tracks: { stream_index: number; gain: number }[] };
  export: { width: 1080; height: 1920; frame_rate: string; codec: "libx264";
    pixel_format: "yuv420p"; crf: number; preset: "fast" | "medium" };
}
export interface ClipEdit { revision: number; revision_id: string; recipe: EditRecipe }
export interface RenderOutput {
  render_id: string; revision_id: string; kind: "preview" | "final"; rq_task_id: string;
  status: string; available: boolean; url: string | null; is_current: boolean;
  error_message: string | null; created_at: string; finished_at: string | null;
  metadata?: Record<string, unknown> | null;
}
export interface SourceAssets {
  status: string; fingerprint?: string; metadata?: Record<string, unknown>;
  artifacts: Record<string, string>; error_message?: string | null;
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

  resumePipeline: (jobId: string) => request(`/jobs/${jobId}/resume`, { method: "POST" }),

  cancelPipeline: (jobId: string) => request(`/jobs/${jobId}/cancel`, { method: "POST" }),

  getEdit: (jobId: string, clipId: string) => request<ClipEdit>(`/jobs/${jobId}/clips/${clipId}/edit`),
  saveEdit: (jobId: string, clipId: string, expectedRevision: number, recipe: EditRecipe) =>
    request<ClipEdit>(`/jobs/${jobId}/clips/${clipId}/edit`, { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: expectedRevision, recipe }) }),
  requestRender: (jobId: string, clipId: string, revisionId: string, kind: "preview" | "final") =>
    request<RenderOutput>(`/jobs/${jobId}/clips/${clipId}/renders`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision_id: revisionId, kind }) }),
  getRenders: (jobId: string, clipId: string) => request<RenderOutput[]>(`/jobs/${jobId}/clips/${clipId}/renders`),
  getAssets: (jobId: string) => request<SourceAssets>(`/jobs/${jobId}/assets`),
  mediaUrl: (path: string) => `${API_BASE}${path}`,

  burnCaptions: (jobId: string, clipId: string) =>
    request<void>(`/jobs/${jobId}/clips/${clipId}/burn`, { method: "POST" }),

  videoUrl: (jobId: string, clipId: string, filename: string) =>
    `${API_BASE}/files/clips/${jobId}/${clipId}/${filename}`,
};
