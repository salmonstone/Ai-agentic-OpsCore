// Status tones — the one rule the whole UI keeps: "unk" (couldn't check)
// looks different from "ok" (dashed, grey, question mark) and is never
// counted as healthy.
export const TC = {
  ok: 'var(--st-ok)', warn: 'var(--st-warn)', crit: 'var(--st-crit)', unk: 'var(--st-unk)',
  neutral: 'color-mix(in srgb, var(--color-text) 70%, transparent)', accent: 'var(--color-accent)',
}
export const TI = {
  ok: 'ph-check-circle', warn: 'ph-warning', crit: 'ph-x-circle', unk: 'ph-question',
  neutral: 'ph-circle', accent: 'ph-circle',
}
export const TL = { ok: 'OK', warn: 'Needs attention', crit: 'Critical', unk: "Couldn't check", neutral: '', accent: '' }

export const tone = t => ({
  t, c: TC[t], icon: TI[t], label: TL[t],
  tint: `color-mix(in srgb, ${TC[t]} 14%, transparent)`,
  line: `color-mix(in srgb, ${TC[t]} 40%, transparent)`,
  bs: t === 'unk' ? 'dashed' : 'solid',
})

// daily_summary sections report ok / warn / error — error means couldn't check.
export const sectionTone = s => ({ ok: 'ok', warn: 'warn', error: 'unk' }[s] || 'unk')

export const POD_TONE = {
  CrashLoopBackOff: 'crit', OOMKilled: 'crit', Error: 'crit', ImagePullBackOff: 'warn',
  ErrImagePull: 'warn', Pending: 'warn', CreateContainerConfigError: 'crit', Evicted: 'warn',
}

export const BUILD_TONE = { SUCCESS: 'ok', FAILURE: 'crit', UNSTABLE: 'warn', ABORTED: 'neutral', RUNNING: 'accent', NOT_BUILT: 'neutral' }

/** Colour for one line of streamed command output, by its leading glyph. */
export function lineColor(s) {
  const h = String(s).trimStart()[0]
  if (h === '✓') return TC.ok
  if (h === '✗') return TC.crit
  if (h === '!' || h === '⚠') return TC.warn
  if (h === '→' || h === '…') return 'var(--muted)'
  if (h === '$' || h === '▶') return TC.accent
  if (/^(ERROR|Error|error)/.test(String(s).trimStart())) return TC.crit
  return 'var(--color-text)'
}
