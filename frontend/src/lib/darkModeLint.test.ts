import { describe, expect, it } from 'vitest'

import { EXEMPT_FILES, findDarkTextIssues, formatIssues } from './darkModeLint'

const lint = (src: string, file = 'pages/X.tsx') => findDarkTextIssues(file, src)

describe('findDarkTextIssues', () => {
  it('flags dark grey, black and dark coloured text without a dark variant', () => {
    const issues = lint(`
      <div className="text-sm text-slate-600 mt-1">a</div>
      <span className='font-bold text-black'>b</span>
      <p className={\`x \${y} text-red-700\`}>c</p>`)
    expect(issues.map(i => [i.line, i.classes])).toEqual([
      [2, ['text-slate-600']], [3, ['text-black']], [4, ['text-red-700']],
    ])
  })

  it('accepts a dark variant in the same string, incl. multi-line template literals', () => {
    expect(lint(`<div className="text-slate-700 dark:text-white" />`)).toEqual([])
    expect(lint('<div className={`text-slate-800\n   dark:text-slate-100 p-2`} />')).toEqual([])
    expect(lint(`<div className="text-red-700 dark:text-red-300" />`)).toEqual([])
  })

  it('ignores light text, hover/dark/arbitrary variants and other utilities', () => {
    expect(lint(`<div className="text-slate-300 text-sky-400 text-white text-xs text-center" />`)).toEqual([])
    expect(lint(`<div className="hover:text-slate-700 dark:text-slate-700 group-hover:text-black" />`)).toEqual([])
    expect(lint(`<div className="bg-slate-700 border-slate-800 text-slate-950x" />`)).toEqual([])
    // An opacity suffix is still dark text.
    expect(lint(`<div className="text-slate-700/80" />`)[0].classes).toEqual(['text-slate-700'])
  })

  it('exempts elements marked as sitting on a light background', () => {
    expect(lint(`<span className="vg-on-light bg-amber-100 text-amber-800" />`)).toEqual([])
  })

  it('exempts whole files listed with a reason', () => {
    const file = Object.keys(EXEMPT_FILES)[0]
    expect(EXEMPT_FILES[file].length).toBeGreaterThan(10)
    expect(lint(`<div className="text-slate-700" />`, file)).toEqual([])
  })

  it('explains how to fix', () => {
    const text = formatIssues(lint(`<div className="text-slate-700" />`))
    expect(text).toContain('pages/X.tsx:1  text-slate-700')
    expect(text).toContain('dark:text-white')
    expect(text).toContain('vg-on-light')
  })
})
