// Click-to-label helpers for the Evaluation page (pure, unit tested).

/** A click inside the displayed image as a point in [0..1] image space,
 *  or null when the click falls outside the image. */
export function clickToPoint(clientX: number, clientY: number,
                             rect: { left: number; top: number; width: number; height: number },
                             ): [number, number] | null {
  if (rect.width <= 0 || rect.height <= 0) return null
  const x = (clientX - rect.left) / rect.width
  const y = (clientY - rect.top) / rect.height
  if (x < 0 || x > 1 || y < 0 || y > 1) return null
  return [Math.round(x * 10000) / 10000, Math.round(y * 10000) / 10000]
}

/** Label progress for one group, e.g. "12 of 400 labelled (3 skipped)". */
export function progressText(counts: Record<string, number>, target: number): string {
  const done = counts.labelled ?? 0
  const skipped = counts.skipped ?? 0
  const pending = counts.pending ?? 0
  return `${done} of ${target} labelled` + (skipped ? ` (${skipped} skipped)` : '')
    + (pending ? ` · ${pending} waiting` : '')
}
