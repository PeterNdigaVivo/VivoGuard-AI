// Login page — single email/password form.

import { useState, type FormEvent } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from './AuthContext'

export default function LoginPage() {
  const { login } = useAuth()
  const nav = useNavigate()
  const loc = useLocation() as { state?: { from?: string } }

  const [email, setEmail] = useState('admin@example.com')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      await login(email, password)
      nav(loc.state?.from ?? '/cameras', { replace: true })
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-900 text-slate-100">
      <form onSubmit={onSubmit} className="bg-slate-800 p-8 rounded-xl shadow w-full max-w-sm">
        <div className="text-2xl font-semibold mb-1">VivoGuard AI</div>
        <div className="text-slate-400 text-sm mb-6">Sign in to continue</div>

        <label className="block mb-3">
          <div className="text-sm mb-1">Email</div>
          <input className="w-full bg-slate-700 px-3 py-2 rounded outline-none focus:ring-2 ring-sky-500"
                 type="email" value={email} onChange={e => setEmail(e.target.value)} required />
        </label>
        <label className="block mb-4">
          <div className="text-sm mb-1">Password</div>
          <input className="w-full bg-slate-700 px-3 py-2 rounded outline-none focus:ring-2 ring-sky-500"
                 type="password" value={password} onChange={e => setPassword(e.target.value)} required />
        </label>

        {error && <div className="text-red-400 text-sm mb-3">{error}</div>}

        <button disabled={busy}
          className="w-full bg-sky-600 hover:bg-sky-500 disabled:opacity-60 py-2 rounded font-medium">
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
