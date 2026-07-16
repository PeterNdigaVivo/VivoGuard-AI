// Light / dark theme. Applies the `dark` class to <html> (Tailwind's
// class strategy) and persists the choice in localStorage. Default is
// 'light' — the app's current look — and the toggle introduces dark.
//
// The initial class is also applied synchronously in main.tsx before the
// first paint to avoid a flash, so this provider only needs to keep it in
// sync afterwards.
import {
  createContext, useCallback, useContext, useEffect, useState,
  type ReactNode,
} from 'react'

export type Theme = 'light' | 'dark'
export const THEME_KEY = 'vg_theme'

export function readStoredTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY)
    if (v === 'light' || v === 'dark') return v
  } catch { /* localStorage unavailable */ }
  return 'light'
}

function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

interface ThemeCtx {
  theme: Theme
  toggle: () => void
  setTheme: (t: Theme) => void
}

const Ctx = createContext<ThemeCtx | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme)

  useEffect(() => {
    applyTheme(theme)
    try { localStorage.setItem(THEME_KEY, theme) } catch { /* ignore */ }
  }, [theme])

  const setTheme = useCallback((t: Theme) => setThemeState(t), [])
  const toggle = useCallback(
    () => setThemeState(t => (t === 'dark' ? 'light' : 'dark')), [])

  return <Ctx.Provider value={{ theme, toggle, setTheme }}>{children}</Ctx.Provider>
}

// Safe default so components render even outside the provider (e.g. tests).
export function useTheme(): ThemeCtx {
  return useContext(Ctx) ?? {
    theme: 'light', toggle: () => {}, setTheme: () => {},
  }
}
