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

          if (data.current_stage === "done" || data.current_stage === "failed") {
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
  }, [jobId]);

  if (!jobId) return <div className="p-8 text-center text-muted">No job ID</div>;

  const currentStage = status?.current_stage || "ingested";
  const isFailed = currentStage === "failed";
  const isDone = currentStage === "done";

  return (
    <div className="min-h-screen p-6">
      <div className="max-w-4xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="font-display text-3xl font-medium tracking-tight text-paper">Processing</h1>
            <p className="font-mono text-sm text-muted mt-1">{jobId}</p>
          </div>
          {isDone && (
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
              <StatusRail stages={[]} currentStatus={currentStage} />
            </div>

            <div className="text-center">
              {!isFailed && !isDone && (
                <div className="flex items-center justify-center gap-2 text-muted">
                  <Loader2 className="h-5 w-5 animate-spin text-accent" />
                  <span>Processing...</span>
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
                    <span>Pipeline failed</span>
                  </div>
                  {status?.error_message && (
                    <p className="text-sm text-muted max-w-md mx-auto">{status.error_message}</p>
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
