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
