import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/Tabs";
import { ScoreMeter } from "@/components/ScoreMeter";
import { Video, Sparkles } from "lucide-react";
import { api, type HighlightClip } from "@/lib/api";

interface ClipCardProps {
  clip: HighlightClip;
  jobId: string;
}

export function ClipCard({ clip, jobId }: ClipCardProps) {
  const navigate = useNavigate();
  const [variant, setVariant] = useState<"vertical" | "original">("vertical");

  const verticalUrl = api.videoUrl(jobId, clip.clip_id, clip.available_variants?.includes("clip_captioned.mp4") ? "clip_captioned.mp4" : "clip_vertical.mp4");
  const originalUrl = api.videoUrl(jobId, clip.clip_id, "clip.mp4");
  const currentUrl = variant === "vertical" ? verticalUrl : originalUrl;
  const score = clip.final_score ?? 0;

  return (
    <div
      className="group overflow-hidden rounded-xl border border-line bg-ink-raised cursor-pointer hover:border-accent/40 transition-all"
      onClick={() => navigate(`/jobs/${jobId}/clips/${clip.clip_id}`)}
    >
      <div className="relative aspect-[9/16] bg-ink overflow-hidden">
        <video
          src={currentUrl}
          className="w-full h-full object-cover"
          preload="metadata"
          playsInline
          muted
          onMouseEnter={(e) => (e.target as HTMLVideoElement).play().catch(() => {})}
          onMouseLeave={(e) => { const v = e.target as HTMLVideoElement; v.pause(); v.currentTime = 0; }}
        />
        <div className="absolute top-2 left-2 z-10">
          <Tabs value={variant} onValueChange={(v) => setVariant(v as "vertical" | "original")} onClick={(e) => e.stopPropagation()}>
            <TabsList className="bg-ink/80 backdrop-blur-sm rounded border border-line h-7 px-0.5">
              <TabsTrigger value="vertical" className="px-2 py-0.5 text-[10px] h-5">Vertical</TabsTrigger>
              <TabsTrigger value="original" className="px-2 py-0.5 text-[10px] h-5">Original</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
        <div className="absolute top-2 right-2">
          <ScoreMeter score={score} />
        </div>
        <div className="absolute inset-0 bg-gradient-to-t from-ink/60 to-transparent opacity-0 group-hover:opacity-100 transition-opacity flex items-end justify-center pb-3">
          <span className="text-xs font-mono text-paper/80 flex items-center gap-1">
            <Video className="h-3 w-3" /> View Details
          </span>
        </div>
      </div>
      <div className="p-3 space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0 flex-1">
            {clip.title && (
              <h3 className="font-display text-sm font-medium truncate text-paper">{clip.title}</h3>
            )}
            {clip.final_hook_text && (
              <p className="text-xs text-muted mt-0.5 line-clamp-1">{clip.final_hook_text}</p>
            )}
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <Sparkles className="h-3 w-3 text-accent" />
            <span className="font-mono text-xs text-paper">{Math.round(score * 100)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
