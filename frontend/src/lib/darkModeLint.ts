// Dark-mode text check (used by darkModeText.test.ts in CI).
//
// Rule: a class list that sets a DARK text colour must also say what the
// colour is in dark mode (a `dark:text-…` class in the same string), or
// the text turns dark-on-near-black when the theme is dark.
//
//   dark text  = text-slate/gray/zinc/neutral/stone-500…950, text-black,
//                or a coloured text-<colour>-600…950
//
// Exemptions, for elements that sit on a background that stays LIGHT in
// dark mode (white cards, light badges, amber warning boxes…):
//   • put the marker class `vg-on-light` in the same class list
//     (styles/index.css also gives such areas dark inherited text), or
//   • list the whole file in EXEMPT_FILES with a reason.
//
// Keep a dark variant in the SAME string literal as the colour it pairs
// with; the check reads one string at a time (a double-quoted JSX
// className may span several lines).

export const ON_LIGHT_MARKER = 'vg-on-light'

/** Files whose every element sits on a background that is light in both
 *  themes. Remove an entry once that component gets a real dark style. */
export const EXEMPT_FILES: Record<string, string> = {
  'components/AlertCard.tsx': 'alert cards are white in both themes (bg-white, no dark:bg)',
  'lib/darkModeLint.ts': 'holds the class patterns this check looks for, not UI',
}

const GREYS = 'slate|gray|zinc|neutral|stone'
const COLOURS = 'red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose'
const DARK_TEXT = new RegExp(
  `(?<![\\w:/-])(?:text-(?:${GREYS})-(?:500|600|700|800|900|950)|text-black|`
  + `text-(?:${COLOURS})-(?:600|700|800|900|950))(?![\\w-])`, 'g')
const HAS_DARK_VARIANT = /(?:^|\s|['"`{])dark:text-/
const STRING_LITERAL = /"[^"]*"|'[^'\n]*'|`[^`]*`/g

export interface DarkTextIssue {
  file: string
  line: number
  classes: string[]
  snippet: string
}

/** Every string literal in `source` that sets dark text without a dark
 *  variant or exemption. `file` is the path relative to src/. */
export function findDarkTextIssues(file: string, source: string): DarkTextIssue[] {
  if (file in EXEMPT_FILES) return []
  const issues: DarkTextIssue[] = []
  for (const m of source.matchAll(STRING_LITERAL)) {
    const literal = m[0]
    const classes = [...literal.matchAll(DARK_TEXT)].map(x => x[0])
    if (classes.length === 0) continue
    if (HAS_DARK_VARIANT.test(literal) || literal.includes(ON_LIGHT_MARKER)) continue
    issues.push({
      file,
      line: source.slice(0, m.index ?? 0).split('\n').length,
      classes,
      snippet: literal.replace(/\s+/g, ' ').slice(0, 120),
    })
  }
  return issues
}

export function formatIssues(issues: DarkTextIssue[]): string {
  return issues.map(i => `  ${i.file}:${i.line}  ${i.classes.join(' ')}  ← ${i.snippet}`).join('\n')
    + '\n\nAdd a dark-mode colour in the same class list (normal text: dark:text-white,'
    + ' small notes: dark:text-slate-300, coloured status text: a light shade such as'
    + ' dark:text-red-300), or, if the element sits on a background that stays light in'
    + ` dark mode, add the marker class "${ON_LIGHT_MARKER}".`
}

// ── Always-white cards ───────────────────────────────────────────────
// The alert card is white in BOTH themes (vg-on-light), so inside it a
// dark-mode colour written for a dark card backfires: a light
// `dark:text-…` (white, slate/gray/sky 50–400) is pale-on-white and a
// dark `dark:bg-…` turns a light box murky under its own dark text.
// Keep light-mode colours there, or `dark:text-black` for more contrast.

/** Files whose cards stay white in dark mode. */
export const ALWAYS_WHITE_FILES: Record<string, string> = {
  'components/AlertCard.tsx': 'alert cards are bg-white in both themes',
}

/** Deliberate dark insets inside a white card that read fine on their own
 *  (the class string sets a dark background AND light text, or frames
 *  media). Matched by a distinctive piece of the class string. */
export const ALWAYS_WHITE_EXEMPT: { file: string; contains: string; reason: string }[] = [
  { file: 'components/AlertCard.tsx', contains: 'bg-slate-100 dark:bg-slate-800 border border-slate-300',
    reason: 'snapshot placeholder frame: a dark media slot, its caption is light-on-dark' },
  { file: 'components/AlertCard.tsx', contains: 'bg-slate-50 dark:bg-slate-800 p-2 text-center',
    reason: 'Store Intelligence tile: dark tile with light text, readable as a unit' },
  { file: 'components/AlertCard.tsx', contains: 'text-slate-800 dark:text-slate-100 capitalize truncate',
    reason: 'value inside the dark Store Intelligence tile above' },
  { file: 'components/AlertCard.tsx', contains: 'bg-blue-50 text-blue-900 dark:bg-slate-800 dark:text-slate-200',
    reason: 'Store Intelligence AI summary: dark inset with light text' },
  { file: 'components/AlertCard.tsx', contains: 'bg-emerald-50 text-emerald-900 dark:bg-slate-800 dark:text-emerald-300',
    reason: 'Store Intelligence recommendation: dark inset with light text' },
]

const LIGHT_DARK_TEXT = /(?<![\w-])dark:(?:[\w-]+:)?text-(?:white|(?:slate|gray|sky)-(?:50|100|200|300|400))(?![\w-])/g
const DARK_DARK_BG = new RegExp(
  `(?<![\\w-])dark:(?:[\\w-]+:)?bg-(?:black|(?:${GREYS}|${COLOURS})-(?:600|700|800|900|950))(?:\\/\\d+)?(?![\\w-])`, 'g')

/** Class strings in an always-white card that use dark-card colours. */
export function findOnWhiteCardIssues(file: string, source: string): DarkTextIssue[] {
  if (!(file in ALWAYS_WHITE_FILES)) return []
  const exempt = ALWAYS_WHITE_EXEMPT.filter(e => e.file === file)
  const issues: DarkTextIssue[] = []
  for (const m of source.matchAll(STRING_LITERAL)) {
    const literal = m[0]
    const classes = [...literal.matchAll(LIGHT_DARK_TEXT), ...literal.matchAll(DARK_DARK_BG)].map(x => x[0])
    if (classes.length === 0) continue
    const flat = literal.replace(/\s+/g, ' ')
    if (exempt.some(e => flat.includes(e.contains))) continue
    issues.push({
      file,
      line: source.slice(0, m.index ?? 0).split('\n').length,
      classes,
      snippet: flat.slice(0, 120),
    })
  }
  return issues
}

export function formatOnWhiteCardIssues(issues: DarkTextIssue[]): string {
  return issues.map(i => `  ${i.file}:${i.line}  ${i.classes.join(' ')}  ← ${i.snippet}`).join('\n')
    + '\n\nThis card stays white in dark mode: drop the dark-card colour and keep the'
    + ' light-mode one (or use dark:text-black). A deliberate dark inset that reads'
    + ' on its own can be listed in ALWAYS_WHITE_EXEMPT with a reason.'
}
