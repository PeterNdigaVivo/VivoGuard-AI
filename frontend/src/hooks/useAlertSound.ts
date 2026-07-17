import { useCallback, useRef } from 'react'

// Synthesizes a fire-truck-style two-tone horn via the Web Audio API — no
// external audio file. Returns a play() to invoke on a new urgent alert.
//
// Browser autoplay policy: audio can't start until the page has had a user
// gesture. play() resume()s the context, but the very first blast may be
// silent until the operator has clicked somewhere (e.g. the mute button /
// "Test sound"). After that it works from any page.
export function useAlertSound() {
  const ctxRef = useRef<AudioContext | null>(null)

  const play = useCallback(() => {
    try {
      const AC: typeof AudioContext | undefined =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!AC) return
      let ctx = ctxRef.current
      if (!ctx) { ctx = new AC(); ctxRef.current = ctx }
      if (ctx.state === 'suspended') void ctx.resume()

      const t0 = ctx.currentTime
      // One 1.5s blast: frequency sweep 800 → 400 → 800 Hz.
      const blast = (start: number) => {
        const osc = ctx!.createOscillator()
        const gain = ctx!.createGain()
        osc.type = 'sawtooth'
        osc.frequency.setValueAtTime(800, start)
        osc.frequency.linearRampToValueAtTime(400, start + 0.75)
        osc.frequency.linearRampToValueAtTime(800, start + 1.5)
        gain.gain.setValueAtTime(0.0001, start)
        gain.gain.exponentialRampToValueAtTime(0.3, start + 0.05)
        gain.gain.setValueAtTime(0.3, start + 1.4)
        gain.gain.exponentialRampToValueAtTime(0.0001, start + 1.5)
        osc.connect(gain)
        gain.connect(ctx!.destination)
        osc.start(start)
        osc.stop(start + 1.55)
      }
      blast(t0)          // repeat twice for urgency
      blast(t0 + 1.6)
    } catch {
      /* audio unavailable — never throw */
    }
  }, [])

  return play
}
