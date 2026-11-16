import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { Loader2, XCircle, CheckCircle2, ArrowRight } from "lucide-react";
import { StatusRail } from "@/components/StatusRail";
import { Card, CardContent } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { api, type QueueStatus } from "@/lib/api";

export function ProgressPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const [status, setStatus] = useState<QueueStatus | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [acting, setActing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;

    let cancelled = false;

    let stopped = false;

    const poll = async () => {
      if (stopped) return;
      try {
        const data = await api.getQueueStatus(jobId);
        if (!cancelled) {
          setStatus(data);
          setError(null);

          if (data.run_status ? ["completed", "completed_with_errors", "failed", "cancelled"].includes(data.run_status) : ["done", "failed"].includes(data.current_stage)) {
            stopped = true;
            clearInterval(interval);
          }
        }
      } catch (err) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : "Failed to fetch status";
          setError(msg);
          if (msg.includes("404") || msg.includes("Not Found")) {
            stopped = true;
            clearInterval(interval);
          }
        }
      }
    };

    poll();
    const interval = setInterval(poll, 2000);
    return () => { cancelled = true; stopped = true; clearInterval(interval); };
  }, [jobId, refreshKey]);

  if (!jobId) return <div className="p-8 text-center text-muted">No job ID</div>;

  const currentStage = status?.current_stage || "ingested";
  const runStatus = status?.run_status;
  const terminal = runStatus ? ["completed", "completed_with_errors", "failed", "cancelled"].includes(runStatus) : ["done", "failed"].includes(currentStage);
  const isFailed = runStatus ? ["failed", "completed_with_errors"].includes(runStatus) : currentStage === "failed";
  const isDone = runStatus === "completed" || (!runStatus && currentStage === "done");
  const isCancelled = runStatus === "cancelled";
  const act = async (action: "resume" | "cancel") => {
    setActing(true);
    try {
      await (action === "resume" ? api.resumePipeline(jobId) : api.cancelPipeline(jobId));
      setError(null);
      setRefreshKey((key) => key + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally { setActing(false); }
  };

  return (
    <div className="min-h-screen p-6">
      <div className="max-w-4xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="font-display text-3xl font-medium tracking-tight text-paper">Processing</h1>
            <p className="font-mono text-sm text-muted mt-1">{jobId}</p>
          </div>
          {(isDone || (status?.analyzed_chunks ?? 0) > 0 || (status?.completed_clips ?? 0) > 0) && (
            <Link to={`/jobs/${jobId}`}>
              <Button className="gap-2">
                View Gallery
                <ArrowRight className="h-4 w-4" />
              </Button>
            </Link>
          )}
        </div>

        <Card>
          <CardContent className="p-8">
            <div className="flex justify-center mb-8">
              <StatusRail stages={[]} currentStatus={currentStage} executionStage={status?.execution_stage} runStatus={runStatus ?? undefined} />
            </div>

            <div className="text-center">
              {!terminal && (
                <div className="flex items-center justify-center gap-2 text-muted">
                  <Loader2 className="h-5 w-5 animate-spin text-accent" />
                  <span>{status?.rq_state === "scheduled" ? "Waiting to retry" : status?.run_status === "queued" ? "Queued" : status?.cancel_requested ? "Stopping after current step" : `Processing ${status?.execution_stage ?? "video"}…`}</span>
                </div>
              )}
              {isDone && (
                <div className="flex items-center justify-center gap-2 text-good">
                  <CheckCircle2 className="h-5 w-5" />
                  <span>Pipeline complete</span>
                </div>
              )}
              {isFailed && (
                <div className="space-y-2">
                  <div className="flex items-center justify-center gap-2 text-destructive">
                    <XCircle className="h-5 w-5" />
                    <span>{runStatus === "completed_with_errors" ? "Completed with clip failures" : "Pipeline failed"}</span>
                  </div>
                  {status?.error_message && (
                    <p className="text-sm text-muted max-w-md mx-auto">{status.error_message}</p>
                  )}
                </div>
              )}
              {isCancelled && <p className="text-muted">Cancelled. Verified work is saved.</p>}
              {status && !status.run_id && (
                <Button className="mt-5" onClick={() => act("resume")} disabled={acting} variant="outline">Start / resume processing</Button>
              )}
              {status?.run_id && (
                <div className="mt-5 space-y-3 text-sm text-muted">
                  <p>{status.completed_units ?? 0} / {status.total_units ?? 0} steps complete · {status.completed_clips ?? 0} clips ready · {status.failed_clips ?? 0} clips failed</p>
                  <progress className="w-full accent-accent" aria-label="Processing progress" max={status.total_units || 1} value={status.completed_units || 0} />
                  <p className="text-xs">{status.total_chunks ? `${status.analyzed_chunks ?? 0} / ${status.total_chunks} sections analyzed. Early candidates are available while processing continues.` : "Steps expand when highlights are found."}</p>
                  {status.clips?.filter((clip) => clip.status === "failed").map((clip) => (
                    <p key={clip.clip_id} className="text-destructive">{clip.clip_id}: {clip.stage} — {clip.error_message}</p>
                  ))}
                  {terminal ? (
                    <Button onClick={() => act("resume")} disabled={acting} variant="outline">{isDone ? "Verify / resume outputs" : "Retry / resume saved work"}</Button>
                  ) : (
                    <Button onClick={() => act("cancel")} disabled={acting || status.cancel_requested} variant="outline">Cancel after current step</Button>
                  )}
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        {error && (
          <div className="p-4 rounded-lg bg-accent-dim border border-accent/20 text-accent text-sm">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}
