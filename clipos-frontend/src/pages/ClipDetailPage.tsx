import { useState, useEffect } from "react";
import { useParams, Link } from "react-router-dom";
import { Loader2, ArrowLeft, Flame, Download, Sparkles, XCircle } from "lucide-react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/Tabs";
import { Button } from "@/components/ui/Button";
import { Card, CardContent } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { ScoreMeter } from "@/components/ScoreMeter";
import { api, type HighlightClip } from "@/lib/api";

export function ClipDetailPage() {
  const { jobId, clipId } = useParams<{ jobId: string; clipId: string }>();
  const [clip, setClip] = useState<HighlightClip | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [variant, setVariant] = useState<"vertical" | "original" | "captioned" | "final">("vertical");
  const [burning, setBurning] = useState(false);
  const [burned, setBurned] = useState(false);

  useEffect(() => {
    if (!jobId || !clipId) return;
    let cancelled = false;
    api.getHighlights(jobId).then((clips) => {
      if (cancelled) return;
      const found = clips.find((c) => c.clip_id === clipId);
      if (found) {
        setClip(found);
        const hasExport = found.available_variants?.includes("clip_captioned.mp4") ?? false;
        setBurned(hasExport);
        if (found.final_render?.available) setVariant("final");
        else if (hasExport) setVariant("captioned");
      } else {
        setError("Clip not found");
      }
    }).catch((err) => {
      if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load clip");
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [jobId, clipId]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="h-8 w-8 text-accent animate-spin" />
      </div>
    );
  }

  if (error || !clip || !jobId || !clipId) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <div className="bg-ink-raised rounded-xl border border-line p-6 max-w-md text-center">
          <XCircle className="h-10 w-10 mx-auto text-destructive mb-3" />
          <p className="text-muted mb-4">{error || "Clip not found"}</p>
          <Link to={`/jobs/${jobId}`} className="text-accent hover:underline text-sm">
            Back to gallery
          </Link>
        </div>
      </div>
    );
  }

  const verticalUrl = api.videoUrl(jobId, clip.clip_id, "clip_vertical.mp4");
  const originalUrl = api.videoUrl(jobId, clip.clip_id, "clip.mp4");

  const captionedUrl = api.videoUrl(jobId, clip.clip_id, "clip_captioned.mp4");
  const finalUrl = clip.final_render?.url ? api.mediaUrl(clip.final_render.url) : null;
  const variants = clip.available_variants ?? ["clip.mp4", "clip_vertical.mp4"];
  const currentUrl = variant === "final" && finalUrl ? finalUrl : variant === "captioned" ? captionedUrl : variant === "vertical" ? verticalUrl : originalUrl;
  const score = clip.final_score ?? 0;

  const handleBurn = async () => {
    setBurning(true);
    try {
      await api.burnCaptions(jobId, clip.clip_id);
      setBurned(true);
      setVariant("captioned");
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Burn failed");
    } finally {
      setBurning(false);
    }
  };

  return (
    <div className="min-h-screen p-6">
      <div className="max-w-6xl mx-auto">
        <Link
          to={`/jobs/${jobId}`}
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-paper transition-colors mb-6"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to gallery
        </Link>

        <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,400px)_1fr] gap-6">
          {/* LEFT — Video Player */}
          <div className="space-y-3">
            <div className="relative aspect-[9/16] rounded-xl overflow-hidden border border-line bg-ink">
              <video
                key={currentUrl}
                src={currentUrl}
                controls
                className="w-full h-full object-cover"
                playsInline
              />
            </div>
            <Tabs value={variant} onValueChange={(v) => setVariant(v as "vertical" | "original" | "captioned" | "final")}>
              <TabsList className="w-full">
                {finalUrl && <TabsTrigger value="final" className="flex-1">Final edit</TabsTrigger>}
                {burned && <TabsTrigger value="captioned" className="flex-1">Captioned</TabsTrigger>}
                {variants.includes("clip_vertical.mp4") && <TabsTrigger value="vertical" className="flex-1">Vertical</TabsTrigger>}
                {variants.includes("clip.mp4") && <TabsTrigger value="original" className="flex-1">Original</TabsTrigger>}
              </TabsList>
            </Tabs>
          </div>

          {/* RIGHT — Info + Score + Actions */}
          <div className="space-y-5">
            <div>
              {clip.title && (
                <h1 className="font-display text-2xl font-medium text-paper">{clip.title}</h1>
              )}
              {clip.final_hook_text && (
                <p className="text-muted mt-2">{clip.final_hook_text}</p>
              )}
            </div>

            <div className="flex items-center gap-3">
              <Badge variant="success" className="gap-1.5">
                <Sparkles className="h-3 w-3" />
                {Math.round(score * 100)}% match
              </Badge>
              <span className="font-mono text-xs text-muted">
                Highlight #{clip.highlight_index}
              </span>
            </div>

            {/* Score Meter */}
            <Card>
              <CardContent className="p-4">
                <div className="flex items-center gap-4">
                  <ScoreMeter score={score} className="shrink-0" />
                  <div className="space-y-1">
                    <div className="font-mono text-2xl font-medium text-paper">
                      {Math.round(score * 100)}
                    </div>
                    <div className="text-xs text-muted">Virality Score</div>
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* Action Buttons */}
            <div className="space-y-2">
              {!finalUrl && (!burned ? (
                <Button
                  onClick={handleBurn}
                  disabled={burning}
                  variant="outline"
                  className="w-full justify-start gap-3 h-12 border-line hover:bg-accent-dim hover:border-accent/30"
                >
                  <Flame className="h-4 w-4 text-accent" />
                  <span>{burning ? "Burning captions..." : "Burn Captions"}</span>
                </Button>
              ) : (
                <a href={captionedUrl} download className="block">
                  <Button variant="outline" className="w-full justify-start gap-3 h-12 border-good/30 hover:bg-good/10">
                    <Download className="h-4 w-4 text-good" />
                    <span>Download Captioned</span>
                  </Button>
                </a>
              ))}
              {variant !== "captioned" && <a href={currentUrl} download className="block">
                <Button variant="secondary" className="w-full justify-start gap-3 h-12 border border-line">
                  <Download className="h-4 w-4" />
                  <span>Download {variant === "final" ? "Final edit" : variant === "vertical" ? "Vertical" : "Original"}</span>
                </Button>
              </a>}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
