import { useState, useEffect } from "react";
import { useParams, Link } from "react-router-dom";
import { ArrowLeft, XCircle } from "lucide-react";
import { ClipCard } from "@/components/ClipCard";
import { api, type HighlightClip } from "@/lib/api";

export function GalleryPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const [clips, setClips] = useState<HighlightClip[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;

    api
      .getHighlights(jobId)
      .then((data) => { if (!cancelled) setClips(data.filter((clip) => clip.available_variants === undefined || clip.available_variants.includes("clip_vertical.mp4"))); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load clips"); })
      .finally(() => { if (!cancelled) setLoading(false); });

    return () => { cancelled = true; };
  }, [jobId]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin h-8 w-8 border-2 border-accent border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <div className="bg-ink-raised rounded-xl border border-line p-6 max-w-md text-center">
          <XCircle className="h-12 w-12 mx-auto text-destructive mb-4" />
          <h2 className="font-display text-xl mb-2 text-paper">Failed to load clips</h2>
          <p className="text-muted mb-4">{error}</p>
          <button
            onClick={() => window.location.reload()}
            className="bg-accent text-paper px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-6">
      <div className="max-w-6xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Link to="/" className="text-muted hover:text-paper transition-colors">
              <ArrowLeft className="h-5 w-5" />
            </Link>
            <div>
              <h1 className="font-display text-2xl font-medium tracking-tight text-paper">Clips</h1>
              <p className="font-mono text-xs text-muted mt-0.5">{clips.length} clips generated</p>
            </div>
          </div>
          <span className="font-mono text-xs text-muted">{jobId?.slice(0, 8)}</span>
        </div>

        {clips.length === 0 ? (
          <div className="rounded-xl border border-line bg-ink-raised p-12 text-center text-muted">
            No clips generated
          </div>
        ) : (
          <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {clips.map((clip) => (
              <ClipCard key={clip.clip_id} clip={clip} jobId={jobId!} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
