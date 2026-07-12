interface ScoreMeterProps {
  score: number;
  className?: string;
}

export function ScoreMeter({ score, className = "" }: ScoreMeterProps) {
  const clamped = Math.max(0, Math.min(1, score));
  const segments = 6;
  const filledSegments = Math.round(clamped * segments);

  return (
    <div className={`flex flex-col-reverse gap-0.5 ${className}`}>
      {Array.from({ length: segments }).map((_, i) => {
        const filled = i < filledSegments;
        const isTop = i === segments - 1;
        const isHighScore = clamped > 0.8;

        let colorClass = "bg-line";
        if (filled) {
          if (isTop && isHighScore) {
            colorClass = "bg-good";
          } else {
            colorClass = "bg-accent";
          }
        }

        return (
          <div
            key={i}
            className={`w-1.5 h-3 rounded-sm transition-colors ${colorClass}`}
          />
        );
      })}
    </div>
  );
}
