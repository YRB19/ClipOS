import { useState, useCallback, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Upload, Link as LinkIcon, Loader2, FileVideo, Sparkles } from "lucide-react";
import { useDropzone } from "react-dropzone";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/Tabs";
import { api } from "@/lib/api";

export function UploadPage() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"file" | "youtube" | "local">("file");
  const [file, setFile] = useState<File | null>(null);
  const [youtubeUrl, setYoutubeUrl] = useState("");
  const [library, setLibrary] = useState<{ configured: boolean; files: string[] } | null>(null);
  const [localPath, setLocalPath] = useState("");
  const [tracks, setTracks] = useState<{ stream_index: number; title?: string; codec: string }[]>([]);
  const [audioTrack, setAudioTrack] = useState<number | null>(null);
  useEffect(() => {
    if (mode !== "local") return;
    let cancelled = false;
    api.library().then(data => { if (!cancelled) setLibrary(data); }).catch(err => { if (!cancelled) setError(String(err)); });
    return () => { cancelled = true; };
  }, [mode]);
  useEffect(() => {
    setTracks([]); setAudioTrack(null);
    if (!localPath) return;
    let cancelled = false;
    api.probeLibrary(localPath).then(data => { if (!cancelled) setTracks(data.audio_tracks); }).catch(err => { if (!cancelled) setError(String(err)); });
    return () => { cancelled = true; };
  }, [localPath]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onDrop = useCallback((acceptedFiles: File[]) => {
    const droppedFile = acceptedFiles[0];
    if (droppedFile && (droppedFile.type.startsWith("video/") || /\.(mp4|mkv|mov|webm|m4v)$/i.test(droppedFile.name))) {
      setFile(droppedFile);
      setError(null);
    } else {
      setError("Please drop an MP4, MKV, MOV, WebM or M4V file");
    }
  }, []);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { "video/*": [".mp4", ".mkv", ".mov", ".webm", ".m4v"] },
    maxFiles: 1,
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (mode === "file" && !file) {
      setError("Please select a video file");
      return;
    }
    if (mode === "youtube" && !youtubeUrl.trim()) {
      setError("Please enter a YouTube URL");
      return;
    }

    if (mode === "local" && !localPath) { setError("Select a local recording"); return; }
    setUploading(true);
    try {
      let jobId: string;
      if (mode === "file") {
        const res = await api.upload(file!);
        jobId = res.job_id;
      } else if (mode === "local") {
        const res = await api.importLocal(localPath, audioTrack);
        jobId = res.job_id;
      } else {
        const res = await api.youtube(youtubeUrl.trim());
        jobId = res.job_id;
      }
      if (mode !== "youtube") await api.runPipeline(jobId);
      navigate(`/jobs/${jobId}/progress`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-2xl">
        <div className="text-center mb-10">
          <h1 className="font-display text-4xl font-medium tracking-tight text-paper">ClipOS</h1>
          <p className="text-muted mt-2">Turn long videos into viral shorts</p>
        </div>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-lg">New Job</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSubmit}>
              <Tabs value={mode} onValueChange={(v) => setMode(v as "file" | "youtube" | "local")} className="w-full mb-6">
                <TabsList className="grid w-full grid-cols-3">
                  <TabsTrigger value="file">
                    <FileVideo className="mr-2 h-4 w-4" />
                    Upload File
                  </TabsTrigger>
                  <TabsTrigger value="youtube">
                    <LinkIcon className="mr-2 h-4 w-4" />
                    YouTube URL
                  </TabsTrigger>
                  <TabsTrigger value="local">Local library</TabsTrigger>
                </TabsList>

                <TabsContent value="file" className="space-y-4">
                  <div
                    {...getRootProps()}
                    className={`border-2 border-dashed rounded-lg p-8 text-center transition-colors cursor-pointer ${
                      isDragActive ? "border-accent bg-accent/5" : "border-line hover:border-accent/50"
                    }`}
                  >
                    <input {...getInputProps()} type="file" accept="video/*,.mp4,.mkv,.mov,.webm,.m4v" className="hidden" disabled={uploading} />
                    <Upload className="mx-auto h-12 w-12 text-muted mb-4" />
                    <p className="text-lg font-medium text-paper">
                      {file ? file.name : "Drag & drop a video file, or click to browse"}
                    </p>
                    <p className="text-xs text-muted mt-1">MP4, MKV, MOV, WebM or M4V · originals preserved</p>
                  </div>
                  {file && (
                    <div className="p-3 bg-ink-raised rounded border border-line flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs text-muted">Selected:</span>
                        <span className="text-sm text-paper">{file.name}</span>
                        <span className="text-xs text-muted">
                          ({(file.size / 1024 / 1024).toFixed(1)} MB)
                        </span>
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => setFile(null)}
                      >
                        Remove
                      </Button>
                    </div>
                  )}
                </TabsContent>

                <TabsContent value="youtube" className="space-y-3">
                  <div className="space-y-2">
                    <label htmlFor="youtube-url" className="text-sm font-medium text-paper">
                      YouTube URL
                    </label>
                    <div className="relative">
                      <LinkIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-5 w-5 text-muted" />
                      <Input
                        id="youtube-url"
                        type="url"
                        placeholder="https://youtube.com/watch?v=..."
                        value={youtubeUrl}
                        onChange={(e) => setYoutubeUrl(e.target.value)}
                        className="pl-10"
                        disabled={uploading}
                      />
                    </div>
                    <p className="text-xs text-muted">Public videos only</p>
                  </div>
                </TabsContent>
                <TabsContent value="local" className="space-y-3">
                  {library?.configured ? <>
                    <label htmlFor="local-file" className="text-sm text-paper">Recording</label>
                    <select id="local-file" value={localPath} onChange={e => setLocalPath(e.target.value)} className="w-full bg-ink p-3 border border-line rounded text-paper">
                      <option value="">Choose a recording</option>
                      {library.files.map(path => <option key={path} value={path}>{path}</option>)}
                    </select>
                    <label htmlFor="microphone" className="text-sm text-paper">Microphone for captions</label>
                    <select id="microphone" value={audioTrack ?? "auto"} onChange={e => setAudioTrack(e.target.value === "auto" ? null : Number(e.target.value))} className="w-full bg-ink p-3 border border-line rounded text-paper">
                      <option value="auto">Automatic (prefer a named microphone track)</option>
                      {tracks.map(track => <option key={track.stream_index} value={track.stream_index}>{track.title || `Audio stream ${track.stream_index}`} ({track.codec})</option>)}
                    </select>
                    <p className="text-xs text-muted">Imports reference your original recording without copying it.</p>
                  </> : <p className="text-sm text-muted">{library ? "Set MEDIA_ROOT in the backend environment to choose your recordings folder." : "Loading local library…"}</p>}
                </TabsContent>
              </Tabs>

              {error && (
                <div className="mb-4 p-3 bg-accent-dim border border-accent/20 rounded text-accent text-sm">
                  {error}
                </div>
              )}

              <Button type="submit" className="w-full py-3 text-lg" size="lg" disabled={uploading}>
                {uploading ? (
                  <>
                    <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                    Processing...
                  </>
                ) : (
                  <>
                    <Sparkles className="mr-2 h-5 w-5" />
                    Start Pipeline
                  </>
                )}
              </Button>
            </form>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
