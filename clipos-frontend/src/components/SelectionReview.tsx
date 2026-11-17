import { useEffect, useState } from "react";
import { api, type HighlightClip } from "@/lib/api";
import { Button } from "@/components/ui/Button";

export function FeedbackButtons({ jobId, clip }: { jobId: string; clip: HighlightClip }) {
  const [value, setValue] = useState(clip.feedback ?? "automatic");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => { setValue(clip.feedback ?? "automatic"); }, [clip.clip_id, clip.feedback]);
  const save = async (decision: "accepted" | "rejected" | "automatic") => {
    setBusy(true); setMessage("");
    try {
      // Page-open time is not measured review time. Benchmark review timing is
      // supplied explicitly through the API/evaluation manifest when available.
      await api.saveFeedback(jobId, clip.clip_id, decision);
      setValue(decision); setMessage("Saved. Resume processing to apply your choices.");
    } catch (err) { setMessage(err instanceof Error ? err.message : "Could not save feedback"); }
    finally { setBusy(false); }
  };
  return <div className="space-y-2">
    <div className="flex flex-wrap gap-2">
      {([['accepted', 'Keep'], ['rejected', 'Skip'], ['automatic', 'Automatic']] as const).map(([decision, label]) =>
        <Button key={decision} type="button" size="sm" variant={value === decision ? "secondary" : "outline"}
          aria-pressed={value === decision} disabled={busy} onClick={() => save(decision)}>{label}</Button>)}
    </div>
    {message && <p role="status" className="text-xs text-muted">{message}</p>}
  </div>;
}

export function SelectionReview({ jobId }: { jobId: string }) {
  const [open, setOpen] = useState(false);
  const [candidates, setCandidates] = useState<HighlightClip[]>([]);
  const [message, setMessage] = useState("");
  const [time, setTime] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api.getCandidates(jobId).then(data => { if (!cancelled) setCandidates(data); })
      .catch(err => { if (!cancelled) setMessage(String(err)); });
    return () => { cancelled = true; };
  }, [jobId, open]);
  const mark = async () => {
    if (!time.trim() || !Number.isFinite(Number(time)) || Number(time) < 0) { setMessage("Enter a time in seconds."); return; }
    setBusy(true);
    try { await api.addMarker(jobId, Number(time), label); setMessage("Moment marked. Resume processing to include it."); setTime(""); setLabel(""); }
    catch (err) { setMessage(err instanceof Error ? err.message : "Could not save marker"); }
    finally { setBusy(false); }
  };
  const resume = async () => {
    setBusy(true);
    try { await api.resumePipeline(jobId); setMessage("Processing resumed with your choices."); }
    catch (err) { setMessage(String(err)); }
    finally { setBusy(false); }
  };
  return <div className="border border-line rounded-xl p-4 space-y-4">
    <Button variant="outline" onClick={() => setOpen(!open)} aria-expanded={open}>Optional selection review</Button>
    {open && <>
      <p className="text-sm text-muted">Keep or skip candidates after processing finishes, or cancel from Progress first. Keep gives a candidate priority within the clip count and duration limits. Clips are selected automatically when you leave these choices unchanged.</p>
      <div className="flex flex-wrap gap-2 items-end">
        <label className="text-sm text-muted">Moment at (seconds)<input aria-label="Moment at (seconds)" type="number" min="0" step="0.1" value={time} onChange={e => setTime(e.target.value)} className="block bg-ink border border-line rounded p-2" /></label>
        <label className="text-sm text-muted">Note<input aria-label="Moment note" value={label} maxLength={240} onChange={e => setLabel(e.target.value)} className="block bg-ink border border-line rounded p-2" /></label>
        <Button disabled={busy} onClick={mark}>Mark moment</Button>
        <Button disabled={busy} variant="outline" onClick={resume}>Resume with choices</Button>
      </div>
      {message && <p role="status" className="text-sm text-muted">{message}</p>}
      <div className="grid gap-3 sm:grid-cols-2">
        {candidates.map(clip => <div key={clip.clip_id} className="border border-line rounded p-3 space-y-2">
          <p className="text-paper">{clip.title || clip.hook_text || "Candidate"}</p>
          <p className="text-xs text-muted">{clip.start?.toFixed(1)}–{clip.end?.toFixed(1)} seconds · {clip.selected ? "In current selection" : "Alternative candidate"}</p>
          <p className="text-sm text-muted">{clip.rank_reasoning || clip.reasoning}</p>
          <FeedbackButtons jobId={jobId} clip={clip} />
        </div>)}
        {!candidates.length && <p className="text-sm text-muted">No candidates available yet.</p>}
      </div>
    </>}
  </div>;
}
