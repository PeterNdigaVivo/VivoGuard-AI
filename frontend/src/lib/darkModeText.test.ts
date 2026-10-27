/// <reference types="vite/client" />
// CI guard: every dark text colour in src/ has a dark-mode colour too
// (see darkModeLint.ts for the rule and the exemption mechanism).
import { describe, expect, it } from 'vitest'

import { findDarkTextIssues, formatIssues } from './darkModeLint'

const sources = import.meta.glob(['/src/**/*.{ts,tsx}', '!/src/**/*.test.{ts,tsx}'], {
  query: '?raw', import: 'default', eager: true,
}) as Record<string, string>

describe('dark-mode text colours', () => {
  it('scans the app source', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(20)
  })

  it('every dark text class has a dark: variant or a vg-on-light exemption', () => {
    const issues = Object.entries(sources).flatMap(([path, src]) =>
      findDarkTextIssues(path.replace(/^\/src\//, ''), src))
    expect(issues, formatIssues(issues)).toEqual([])
  })
})
