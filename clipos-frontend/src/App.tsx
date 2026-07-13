import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import JobListView from "./components/JobListView";
import UploadView from "./components/UploadView";
import JobProgressView from "./components/JobProgressView";
import ClipGalleryView from "./components/ClipGalleryView";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<JobListView />} />
        <Route path="/upload" element={<UploadView />} />
        <Route path="/jobs/:jobId" element={<JobProgressView />} />
        <Route path="/jobs/:jobId/clips" element={<ClipGalleryView />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
