// Tiny fetch wrapper that:
//   - prefixes /api so paths in the rest of the app stay short
//   - injects the JWT access token from localStorage
//   - throws on non-2xx so callers can `try/catch`
//
// This is intentionally NOT a heavyweight library — every shop has a
// preferred React Query / SWR setup and we don't want to fight you on it.

const TOKEN_KEY = 'vg_access_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(t: string | null): void {
  if (t) localStorage.setItem(TOKEN_KEY, t)
  else localStorage.removeItem(TOKEN_KEY)
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
  const headers: Record<string, string> = {}
  if (!opts.form) headers['Content-Type'] = 'application/json'
  const tok = getToken()
  if (tok) headers['Authorization'] = `Bearer ${tok}`

  const res = await fetch(`/api${path}`, {
    method: opts.method ?? 'GET',
    headers,
    body: opts.form ?? (opts.body !== undefined ? JSON.stringify(opts.body) : undefined),
  })

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
