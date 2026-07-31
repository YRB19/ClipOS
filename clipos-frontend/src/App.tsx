import { BrowserRouter, Routes, Route } from "react-router-dom";
import { JobListPage } from "@/pages/JobListPage";
import { UploadPage } from "@/pages/UploadPage";
import { ProgressPage } from "@/pages/ProgressPage";
import { GalleryPage } from "@/pages/GalleryPage";
import { ClipDetailPage } from "@/pages/ClipDetailPage";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<JobListPage />} />
        <Route path="/upload" element={<UploadPage />} />
        <Route path="/jobs/:jobId/progress" element={<ProgressPage />} />
        <Route path="/jobs/:jobId/clips/:clipId" element={<ClipDetailPage />} />
        <Route path="/jobs/:jobId" element={<GalleryPage />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
