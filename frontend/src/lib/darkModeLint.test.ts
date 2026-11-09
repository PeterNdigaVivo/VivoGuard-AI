import { describe, expect, it } from 'vitest'

import { EXEMPT_FILES, findDarkTextIssues, findOnWhiteCardIssues, formatIssues } from './darkModeLint'

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

describe('findOnWhiteCardIssues', () => {
  const card = 'components/AlertCard.tsx'
  const lint = (src: string, file = card) => findOnWhiteCardIssues(file, src)

  it('flags light dark:text and dark dark:bg colours inside the white card', () => {
    const issues = lint(`
      <div className="bg-sky-50 dark:bg-sky-950/30 text-slate-700 dark:text-slate-200">
      <span className='text-sky-900 dark:text-sky-200 dark:hover:text-white' />`)
    expect(issues.map(i => [i.line, i.classes])).toEqual([
      [2, ['dark:text-slate-200', 'dark:bg-sky-950/30']],
      [3, ['dark:text-sky-200', 'dark:hover:text-white']],
    ])
  })

  it('accepts light-mode colours and dark:text-black', () => {
    expect(lint(`<div className="bg-sky-50 text-sky-900 dark:text-black dark:border-sky-200" />`)).toEqual([])
    expect(lint(`<div className="dark:text-amber-300 dark:bg-amber-100" />`)).toEqual([])
  })

  it('only applies to always-white files', () => {
    expect(lint(`<div className="dark:bg-slate-900 dark:text-white" />`, 'pages/X.tsx')).toEqual([])
  })

  it('skips listed dark insets', () => {
    expect(lint(`<div className="rounded-lg border bg-slate-50 dark:bg-slate-800 p-2 text-center" />`)).toEqual([])
  })
})
