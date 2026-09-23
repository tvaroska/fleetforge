// Who this bundle is. Mirrors `fleetforge/buildinfo.py` on the API side so the two
// halves of the footer are directly comparable.

export const UNKNOWN = 'unknown'

export type BuildInfo = {
  version: string
  commit: string
  builtAt: string
}

// `typeof x === 'undefined'` rather than a bare read: the identifiers are substituted
// by vite's `define`, and anything that loads a source file without that substitution
// (a bare `tsc`, an editor, a test runner configured without the config) would throw a
// ReferenceError on import. A version line must never be the thing that breaks a page.
function defined(value: string, fallback: string): string {
  return typeof value === 'undefined' || value === '' ? fallback : value
}

export const buildInfo: BuildInfo = {
  version: defined(typeof __FF_VERSION__ === 'undefined' ? '' : __FF_VERSION__, UNKNOWN),
  commit: defined(typeof __FF_COMMIT__ === 'undefined' ? '' : __FF_COMMIT__, UNKNOWN),
  builtAt: defined(typeof __FF_BUILT_AT__ === 'undefined' ? '' : __FF_BUILT_AT__, UNKNOWN),
}

/** `0.4.0 · 815596b2`, or just `0.4.0` when built without provenance. */
export function describeBuild(version: string, commit: string): string {
  return commit === UNKNOWN || commit === '' ? version : `${version} · ${commit.slice(0, 8)}`
}
