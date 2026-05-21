// App router. A logged-in user lands on /cameras by default.
// Routes that need auth are wrapped in <Protected />.
//
// Performance — every page is lazily code-split via React.lazy() so
// the initial bundle is small (just the auth shell + Layout). Vite
// chunks each route into its own JS file; the browser only fetches
// what the user actually opens.

import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { lazy, Suspense, type ReactNode } from 'react'

import { AuthProvider, useAuth } from '@/auth/AuthContext'
import { ToastProvider } from '@/components/ui/Primitives'
import Layout from '@/components/Layout'

// Auth screen is eager — every other user hits it before reaching
// the protected app, so lazy-loading it would just add a flash.
import LoginPage from '@/auth/LoginPage'

// Every protected page = its own chunk. Order matches the route
// table below for readability.
const CamerasPage          = lazy(() => import('@/pages/CamerasPage'))
const AddCameraWizard      = lazy(() => import('@/pages/AddCameraWizard'))
const LiveViewPage         = lazy(() => import('@/pages/LiveViewPage'))
const AlertsPage           = lazy(() => import('@/pages/AlertsPage'))
const ReportsPage          = lazy(() => import('@/pages/ReportsPage'))
const DetectionConfigPage  = lazy(() => import('@/pages/DetectionConfigPage'))
const TrainingStudioPage   = lazy(() => import('@/pages/TrainingStudioPage'))
const AnnotationPage       = lazy(() => import('@/pages/AnnotationPage'))
const TrainingDashboardPage= lazy(() => import('@/pages/TrainingDashboardPage'))
const ModelsPage           = lazy(() => import('@/pages/ModelsPage'))
const SystemHealthPage     = lazy(() => import('@/pages/SystemHealthPage'))
const StoresPage           = lazy(() => import('@/pages/StoresPage'))
const StoreDashboardPage   = lazy(() => import('@/pages/StoreDashboardPage'))
const StoreDetailPage      = lazy(() => import('@/pages/StoreDetailPage'))
const MultiStorePage       = lazy(() => import('@/pages/MultiStorePage'))
const HeatmapPage          = lazy(() => import('@/pages/HeatmapPage'))
const StockroomLogPage     = lazy(() => import('@/pages/StockroomLogPage'))
const CampaignsPage        = lazy(() => import('@/pages/CampaignsPage'))
const SearchPage           = lazy(() => import('@/pages/SearchPage'))
const ComparePage          = lazy(() => import('@/pages/ComparePage'))
const CameraSetupPage      = lazy(() => import('@/pages/CameraSetupPage'))
const StoreHeatmapsPage    = lazy(() => import('@/pages/StoreHeatmapsPage'))
const DetectorsPage        = lazy(() => import('@/pages/DetectorsPage'))
const UsersPage            = lazy(() => import('@/pages/UsersPage'))


function Protected({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const loc = useLocation()
  if (loading) return <div className="p-8 text-slate-500">Loading…</div>
  if (!user) return <Navigate to="/login" state={{ from: loc.pathname }} replace />
  return <>{children}</>
}

// Suspense fallback for code-split routes. Same skeleton used inside
// Layout's <Outlet/> while a chunk loads.
function RouteFallback() {
  return <div className="p-6 text-slate-400 text-sm">Loading…</div>
}

export default function App() {
  return (
    <AuthProvider>
      <ToastProvider>
      <BrowserRouter>
        <Suspense fallback={<RouteFallback />}>
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
        </Suspense>
      </BrowserRouter>
      </ToastProvider>
    </AuthProvider>
  )
}
