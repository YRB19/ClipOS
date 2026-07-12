import { useState, useCallback } from "react";
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
  const [mode, setMode] = useState<"file" | "youtube">("file");
  const [file, setFile] = useState<File | null>(null);
  const [youtubeUrl, setYoutubeUrl] = useState("");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onDrop = useCallback((acceptedFiles: File[]) => {
    const droppedFile = acceptedFiles[0];
    if (droppedFile && (droppedFile.type.startsWith("video/") || droppedFile.name.endsWith(".mp4"))) {
      setFile(droppedFile);
      setError(null);
    } else {
      setError("Please drop a valid video file (.mp4)");
    }
  }, []);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { "video/mp4": [".mp4"], "video/*": [] },
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

    setUploading(true);
    try {
      let jobId: string;
      if (mode === "file") {
        const res = await api.upload(file!);
        jobId = res.job_id;
      } else {
        const res = await api.youtube(youtubeUrl.trim());
        jobId = res.job_id;
      }
      await api.runPipeline(jobId);
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
              <Tabs value={mode} onValueChange={(v) => setMode(v as "file" | "youtube")} className="w-full mb-6">
                <TabsList className="grid w-full grid-cols-2">
                  <TabsTrigger value="file">
                    <FileVideo className="mr-2 h-4 w-4" />
                    Upload File
                  </TabsTrigger>
                  <TabsTrigger value="youtube">
                    <LinkIcon className="mr-2 h-4 w-4" />
                    YouTube URL
                  </TabsTrigger>
                </TabsList>

                <TabsContent value="file" className="space-y-4">
                  <div
                    {...getRootProps()}
                    className={`border-2 border-dashed rounded-lg p-8 text-center transition-colors cursor-pointer ${
                      isDragActive ? "border-accent bg-accent/5" : "border-line hover:border-accent/50"
                    }`}
                  >
                    <input {...getInputProps()} type="file" accept="video/mp4,.mp4" className="hidden" disabled={uploading} />
                    <Upload className="mx-auto h-12 w-12 text-muted mb-4" />
                    <p className="text-lg font-medium text-paper">
                      {file ? file.name : "Drag & drop a video file, or click to browse"}
                    </p>
                    <p className="text-xs text-muted mt-1">MP4 format, max 2GB</p>
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
