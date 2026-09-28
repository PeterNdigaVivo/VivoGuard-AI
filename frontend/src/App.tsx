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
import { AlertNotificationProvider } from '@/contexts/AlertNotificationContext'
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
const DetectionConfigPage  = lazy(() => import('@/pages/DetectionConfigPage'))
const TrainingStudioPage   = lazy(() => import('@/pages/TrainingStudioPage'))
const AnnotationPage       = lazy(() => import('@/pages/AnnotationPage'))
const TrainingDashboardPage= lazy(() => import('@/pages/TrainingDashboardPage'))
const ShutterTrainingPage  = lazy(() => import('@/pages/ShutterTrainingPage'))
const UniformTrainingPage  = lazy(() => import('@/pages/UniformTrainingPage'))
const ChainTrainingPage    = lazy(() => import('@/pages/ChainTrainingPage'))
const MissionControlPage   = lazy(() => import('@/pages/MissionControlPage'))
const StoresPage           = lazy(() => import('@/pages/StoresPage'))
const StoreDashboardPage   = lazy(() => import('@/pages/StoreDashboardPage'))
const StoreDetailPage      = lazy(() => import('@/pages/StoreDetailPage'))
const StoreMultiCameraView = lazy(() => import('@/pages/StoreMultiCameraView'))
const HeatmapPage          = lazy(() => import('@/pages/HeatmapPage'))
const SearchPage           = lazy(() => import('@/pages/SearchPage'))
const CameraSetupPage      = lazy(() => import('@/pages/CameraSetupPage'))
const StoreHeatmapsPage    = lazy(() => import('@/pages/StoreHeatmapsPage'))
const DetectorsPage        = lazy(() => import('@/pages/DetectorsPage'))
const UsersPage            = lazy(() => import('@/pages/UsersPage'))
const OdooAssurancePage    = lazy(() => import('@/pages/OdooAssurancePage'))


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
            <Route element={<Protected><AlertNotificationProvider><Layout /></AlertNotificationProvider></Protected>}>
              <Route index element={<Navigate to="/cameras" replace />} />
              <Route path="/chain"                    element={<Navigate to="/stores" replace />} />
              <Route path="/stores"                   element={<StoresPage />} />
              <Route path="/stores/:id"               element={<StoreDetailPage />} />
              <Route path="/stores/:id/analytics"     element={<StoreDashboardPage />} />
              <Route path="/stores/:id/cameras"       element={<StoreMultiCameraView />} />
              <Route path="/stores/:id/add-camera"    element={<AddCameraWizard />} />
              <Route path="/heatmaps/:id"             element={<StoreHeatmapsPage />} />
              <Route path="/detectors"                element={<DetectorsPage />} />
              <Route path="/users"                    element={<UsersPage />} />
              <Route path="/cameras"                  element={<CamerasPage />} />
              <Route path="/cameras/add"              element={<AddCameraWizard />} />
              <Route path="/cameras/:id/setup"        element={<CameraSetupPage />} />
              <Route path="/cameras/:id/detection"    element={<DetectionConfigPage />} />
              <Route path="/cameras/:id/heatmap"      element={<HeatmapPage />} />
              <Route path="/search"                   element={<SearchPage />} />
              <Route path="/compare"                  element={<Navigate to="/stores" replace />} />
              <Route path="/live"                     element={<LiveViewPage />} />
              <Route path="/alerts"                   element={<AlertsPage />} />
              <Route path="/training"                 element={<TrainingStudioPage />} />
              <Route path="/training/shutter"         element={<ShutterTrainingPage />} />
              <Route path="/training/uniform"         element={<UniformTrainingPage />} />
              <Route path="/training/chain"           element={<ChainTrainingPage />} />
              <Route path="/training/datasets/:dsId"  element={<AnnotationPage />} />
              <Route path="/training/jobs/:jobId"     element={<TrainingDashboardPage />} />
              <Route path="/ai-learning"              element={<Navigate to="/training" replace />} />
              <Route path="/ai-progress"              element={<Navigate to="/training" replace />} />
              <Route path="/sprint"                   element={<Navigate to="/training" replace />} />
              <Route path="/models"                   element={<Navigate to="/training" replace />} />
              <Route path="/system"                   element={<MissionControlPage />} />
              <Route path="/system-health"            element={<Navigate to="/system" replace />} />
              <Route path="/system/odoo"              element={<OdooAssurancePage />} />
              <Route path="/odoo-assurance"           element={<Navigate to="/system/odoo" replace />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </BrowserRouter>
      </ToastProvider>
    </AuthProvider>
  )
}
