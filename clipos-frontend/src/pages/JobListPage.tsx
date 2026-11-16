import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Plus, Loader2, Clock, CheckCircle2, XCircle, Video, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { api, type Job } from "@/lib/api";
import { formatDistanceToNow } from "date-fns";

const STATUS_META: Record<string, { variant: "default" | "secondary" | "destructive" | "outline" | "success" | "warning" | "info"; icon: React.ElementType; label: string }> = {
  ingested: { variant: "secondary", icon: Clock, label: "Ingested" },
  transcribed: { variant: "info", icon: Loader2, label: "Transcribed" },
  highlights_ready: { variant: "warning", icon: Sparkles, label: "Highlights" },
  clips_ready: { variant: "info", icon: Video, label: "Clips Ready" },
  done: { variant: "success", icon: CheckCircle2, label: "Done" },
  failed: { variant: "destructive", icon: XCircle, label: "Failed" },
};

export function JobListPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchJobs = async () => {
      try {
        const data = await api.listJobs();
        if (!cancelled) setJobs(data);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load jobs");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchJobs();
    const interval = setInterval(fetchJobs, 10000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  const getLink = (job: Job) => {
    if (job.run_status && job.run_status !== "completed") return `/jobs/${job.job_id}/progress`;
    if (job.status === "done" || job.status === "clips_ready") return `/jobs/${job.job_id}`;
    return `/jobs/${job.job_id}/progress`;
  };

  const formatDate = (iso: string) => {
    try {
      return formatDistanceToNow(new Date(iso), { addSuffix: true });
    } catch {
      return new Date(iso).toLocaleString();
    }
  };

  return (
    <div className="min-h-screen p-6">
      <div className="max-w-5xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="font-display text-3xl font-medium tracking-tight text-paper">Jobs</h1>
            <p className="text-muted text-sm mt-1 font-mono">{jobs.length} total</p>
          </div>
          <Link to="/upload">
            <Button className="gap-2">
              <Plus className="h-4 w-4" />
              New Job
            </Button>
          </Link>
        </div>

        {error && (
          <div className="p-4 rounded-lg bg-accent-dim border border-accent/20 text-accent text-sm">
            {error}
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-8 w-8 text-accent animate-spin" />
          </div>
        ) : jobs.length === 0 ? (
          <div className="rounded-xl border border-line bg-ink-raised py-16 text-center">
            <Video className="mx-auto h-10 w-10 text-muted mb-4" />
            <p className="text-muted mb-4">No jobs yet</p>
            <Link to="/upload" className="text-accent hover:underline text-sm font-medium">
              Upload your first video
            </Link>
          </div>
        ) : (
          <div className="rounded-xl border border-line bg-ink-raised overflow-hidden">
            <div className="grid grid-cols-[auto_1fr_auto_auto_auto] gap-4 items-center px-4 py-2.5 border-b border-line text-xs font-mono text-muted uppercase tracking-wider">
              <span>Status</span>
              <span>Job</span>
              <span>Duration</span>
              <span>Created</span>
              <span className="w-8" />
            </div>
            {jobs.map((job) => {
              const meta = STATUS_META[job.status] || { variant: "secondary" as const, icon: Loader2, label: job.status };
              const StatusIcon = meta.icon;

              return (
                <Link
                  key={job.job_id}
                  to={getLink(job)}
                  className="grid grid-cols-[auto_1fr_auto_auto_auto] gap-4 items-center px-4 py-3 border-b border-line/50 last:border-0 hover:bg-ink-raised/80 transition-colors group"
                >
                  <Badge variant={meta.variant} className="gap-1.5 w-[110px] justify-center">
                    <StatusIcon className={`h-3 w-3 ${job.status === "transcribed" || job.status === "highlights_ready" || job.status === "clips_ready" ? "animate-spin" : ""}`} />
                    {meta.label}
                  </Badge>
                  <div className="min-w-0">
                    <span className="font-mono text-sm text-paper truncate block">
                      {job.source_filename || job.job_id.slice(0, 8)}
                    </span>
                  </div>
                  <span className="font-mono text-sm text-muted w-16 text-right">
                    {job.duration_seconds != null ? `${Math.round(job.duration_seconds)}s` : "—"}
                  </span>
                  <span className="font-mono text-xs text-muted w-28 text-right">
                    {formatDate(job.created_at)}
                  </span>
                  <div className="w-8 flex justify-end">
                    <svg className="h-4 w-4 text-muted group-hover:text-accent transition-colors" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                    </svg>
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
