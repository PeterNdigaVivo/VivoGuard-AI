// Tiny auth context. We don't store the user payload server-side — JWT is
// the source of truth. We do keep the parsed payload in state so the UI
// can show role + email without a round-trip after login.

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, getToken, setToken } from '@/api/client'

export interface CurrentUser {
  id: number
  email: string
  full_name: string | null
  role: 'admin' | 'operator' | 'viewer'
  is_active: boolean
}

interface AuthCtx {
  user: CurrentUser | null
  loading: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => void
}

const Ctx = createContext<AuthCtx>({} as AuthCtx)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [loading, setLoading] = useState(true)

  // On boot: if a token exists, hydrate /auth/me. Failing that, drop it.
  useEffect(() => {
    const tok = getToken()
    if (!tok) { setLoading(false); return }
    api<CurrentUser>('/auth/me')
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false))
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const res = await api<{ access_token: string; refresh_token: string; role: string }>(
      '/auth/login', { method: 'POST', body: { email, password } },
    )
    setToken(res.access_token)
    const me = await api<CurrentUser>('/auth/me')
    setUser(me)
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  const value = useMemo(() => ({ user, loading, login, logout }), [user, loading, login, logout])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useAuth() {
  return useContext(Ctx)
}
