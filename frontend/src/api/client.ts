// Tiny fetch wrapper that:
//   - prefixes /api so paths in the rest of the app stay short
//   - injects the JWT access token from localStorage
//   - throws on non-2xx so callers can `try/catch`
//
// This is intentionally NOT a heavyweight library — every shop has a
// preferred React Query / SWR setup and we don't want to fight you on it.

const TOKEN_KEY = 'vg_access_token'
const REFRESH_TOKEN_KEY = 'vg_refresh_token'
export const AUTH_EXPIRED_EVENT = 'vg:auth-expired'

let refreshInFlight: Promise<string | null> | null = null

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(t: string | null): void {
  if (t) localStorage.setItem(TOKEN_KEY, t)
  else localStorage.removeItem(TOKEN_KEY)
}

export function getRefreshToken(): string | null {
  return sessionStorage.getItem(REFRESH_TOKEN_KEY)
}

export function setRefreshToken(t: string | null): void {
  if (t) sessionStorage.setItem(REFRESH_TOKEN_KEY, t)
  else sessionStorage.removeItem(REFRESH_TOKEN_KEY)
}

export function clearTokens(): void {
  setToken(null)
  setRefreshToken(null)
}

function expireSession(): void {
  clearTokens()
  window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
}

async function refreshAccessToken(): Promise<string | null> {
  if (refreshInFlight) return refreshInFlight
  const refreshToken = getRefreshToken()
  if (!refreshToken) return null

  refreshInFlight = fetch('/api/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken }),
  }).then(async response => {
    if (!response.ok) return null
    const tokens = await response.json() as {
      access_token: string
      refresh_token: string
    }
    setToken(tokens.access_token)
    setRefreshToken(tokens.refresh_token)
    return tokens.access_token
  }).catch(() => null).finally(() => {
    refreshInFlight = null
  })
  return refreshInFlight
}

interface RequestOpts {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  // form: send as multipart/form-data instead of JSON.
  form?: FormData
  // returns the raw Response so callers can read blobs/streams.
  raw?: boolean
}

export async function api<T = any>(path: string, opts: RequestOpts = {}): Promise<T> {
  const request = (token: string | null) => {
    const headers: Record<string, string> = {}
    if (!opts.form) headers['Content-Type'] = 'application/json'
    if (token) headers['Authorization'] = `Bearer ${token}`
    return fetch(`/api${path}`, {
      method: opts.method ?? 'GET',
      headers,
      body: opts.form ?? (opts.body !== undefined ? JSON.stringify(opts.body) : undefined),
    })
  }

  const originalToken = getToken()
  let res = await request(originalToken)
  if (res.status === 401 && originalToken && path !== '/auth/refresh') {
    const replacementToken = await refreshAccessToken()
    if (replacementToken) {
      res = await request(replacementToken)
      if (res.status === 401) expireSession()
    } else expireSession()
  }

  if (opts.raw) return res as unknown as T
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${text ? `: ${text}` : ''}`)
  }
  // 204 / empty body
  if (res.status === 204) return undefined as T
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return (await res.json()) as T
  return (await res.text()) as unknown as T
}

// Build a WebSocket URL that points at the same backend as the API client.
// The browser is served by nginx which proxies /ws → backend; in dev,
// vite proxies /ws as well.
export function wsUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}${path}`
}

// Browser WebSockets cannot set an Authorization header. Send the access JWT
// as a secondary WebSocket subprotocol instead of putting it in the URL, where
// it would leak into proxy logs and browser history. The server selects only
// the fixed marker protocol and validates the accompanying token before it
// accepts the connection.
export function authenticatedWebSocket(path: string): WebSocket {
  const token = getToken()
  return token
    ? new WebSocket(wsUrl(path), ['vg-token', token])
    : new WebSocket(wsUrl(path), ['vg-token'])
}
