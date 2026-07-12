import { Check, Circle } from "lucide-react";

interface StatusRailProps {
  stages: string[];
  currentStatus: string;
}

const STAGE_ORDER = [
  "ingested",
  "transcribed",
  "highlights_ready",
  "clips_ready",
  "done",
];

export function StatusRail({ currentStatus }: StatusRailProps) {
  const currentIdx = STAGE_ORDER.indexOf(currentStatus);
  const isFailed = currentStatus === "failed";

  const formatStage = (s: string) =>
    s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  return (
    <div className="flex items-center gap-0">
      {STAGE_ORDER.map((stage, i) => {
        const done = i <= currentIdx && !isFailed;
        const active = i === currentIdx && !isFailed;

        return (
          <div key={stage} className="flex items-center">
            <div className="flex flex-col items-center gap-1">
              <div
                className={`w-3 h-3 rounded-full flex items-center justify-center transition-colors ${
                  done
                    ? "bg-good"
                    : active
                    ? "bg-accent animate-pulse"
                    : "bg-ink-raised border border-line"
                }`}
              >
                {done && <Check className="w-2 h-2 text-ink" />}
                {active && <Circle className="w-2 h-2 text-paper fill-paper" />}
              </div>
              <span
                className={`font-mono text-[10px] whitespace-nowrap ${
                  done ? "text-good" : active ? "text-accent" : "text-muted"
                }`}
              >
                {formatStage(stage)}
              </span>
            </div>
            {i < STAGE_ORDER.length - 1 && (
              <div
                className={`w-8 h-px mx-1 ${
                  i < currentIdx && !isFailed ? "bg-good" : "bg-line"
                }`}
              />
            )}
          </div>
        );
      })}
      {isFailed && (
        <div className="ml-2 text-destructive font-mono text-xs">FAILED</div>
      )}
    </div>
  );
}
