// App router. A logged-in user lands on /cameras by default.
// Routes that need auth are wrapped in <Protected />.

import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { type ReactNode } from 'react'

import { AuthProvider, useAuth } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/ui/Primitives'
import LoginPage from '@/auth/LoginPage'
import Layout from '@/components/Layout'

import CamerasPage         from '@/pages/CamerasPage'
import AddCameraWizard     from '@/pages/AddCameraWizard'
import LiveViewPage        from '@/pages/LiveViewPage'
import AlertsPage          from '@/pages/AlertsPage'
import ReportsPage         from '@/pages/ReportsPage'
import DetectionConfigPage from '@/pages/DetectionConfigPage'
import TrainingStudioPage  from '@/pages/TrainingStudioPage'
import AnnotationPage      from '@/pages/AnnotationPage'
import TrainingDashboardPage from '@/pages/TrainingDashboardPage'
import ModelsPage          from '@/pages/ModelsPage'
import SystemHealthPage    from '@/pages/SystemHealthPage'
import StoresPage          from '@/pages/StoresPage'
import StoreDashboardPage  from '@/pages/StoreDashboardPage'
import StoreDetailPage     from '@/pages/StoreDetailPage'
import MultiStorePage      from '@/pages/MultiStorePage'
import HeatmapPage         from '@/pages/HeatmapPage'
import StockroomLogPage    from '@/pages/StockroomLogPage'
import CampaignsPage       from '@/pages/CampaignsPage'
import SearchPage          from '@/pages/SearchPage'
import ComparePage         from '@/pages/ComparePage'
import CameraSetupPage     from '@/pages/CameraSetupPage'
import StoreHeatmapsPage   from '@/pages/StoreHeatmapsPage'
import DetectorsPage       from '@/pages/DetectorsPage'
import UsersPage           from '@/pages/UsersPage'

// Gate: any unauthenticated request is bounced to /login while preserving
// the originally requested path so we can redirect back after sign-in.
function Protected({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const loc = useLocation()
  if (loading) return <div className="p-8 text-slate-500">Loading…</div>
  if (!user) return <Navigate to="/login" state={{ from: loc.pathname }} replace />
  return <>{children}</>
}

export default function App() {
  return (
    <AuthProvider>
      <ToastProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<Protected><Layout /></Protected>}>
            <Route index element={<Navigate to="/cameras" replace />} />
            <Route path="/chain"                    element={<MultiStorePage />} />
            <Route path="/stores"                   element={<StoresPage />} />
            <Route path="/stores/:id"               element={<StoreDetailPage />} />
            <Route path="/stores/:id/analytics"     element={<StoreDashboardPage />} />
            <Route path="/stores/:id/add-camera"    element={<AddCameraWizard />} />
            <Route path="/heatmaps/:id"             element={<StoreHeatmapsPage />} />
            <Route path="/detectors"                element={<DetectorsPage />} />
            <Route path="/users"                    element={<UsersPage />} />
            <Route path="/cameras"                  element={<CamerasPage />} />
            <Route path="/cameras/add"              element={<AddCameraWizard />} />
            <Route path="/cameras/:id/setup"        element={<CameraSetupPage />} />
            <Route path="/cameras/:id/detection"    element={<DetectionConfigPage />} />
            <Route path="/cameras/:id/heatmap"      element={<HeatmapPage />} />
            <Route path="/stockroom"                element={<StockroomLogPage />} />
            <Route path="/campaigns"                element={<CampaignsPage />} />
            <Route path="/search"                   element={<SearchPage />} />
            <Route path="/compare"                  element={<ComparePage />} />
            <Route path="/live"                     element={<LiveViewPage />} />
            <Route path="/alerts"                   element={<AlertsPage />} />
            <Route path="/reports"                  element={<ReportsPage />} />
            <Route path="/training"                 element={<TrainingStudioPage />} />
            <Route path="/training/datasets/:dsId"  element={<AnnotationPage />} />
            <Route path="/training/jobs/:jobId"     element={<TrainingDashboardPage />} />
            <Route path="/models"                   element={<ModelsPage />} />
            <Route path="/system"                   element={<SystemHealthPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
      </ToastProvider>
    </AuthProvider>
  )
}
