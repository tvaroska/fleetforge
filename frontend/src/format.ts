// Timestamp rendering, shared by every table on the page.
//
// Both functions are pure and take `now` as an argument rather than calling
// `Date.now()`: a relative label that reads the clock internally cannot be tested
// without a fake timer, and the fleet table re-renders its "last seen" column once a
// second from a tick counter that already knows what time it is.

/** An absolute local timestamp. An unparseable value is shown verbatim, not as "Invalid Date". */
export function formatWhen(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleString()
}

/**
 * A relative age: `just now` / `12 s ago` / `4 min ago` / `2 h ago` / `3 d ago`.
 *
 * `null` is `never` — a board that has enrolled but has never published. A clock skew
 * that puts `iso` in the future reads as `just now` rather than as a negative age.
 */
export function formatAgo(iso: string | null, now: number): string {
  if (iso === null) return 'never'
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return iso

  const seconds = Math.floor((now - at.getTime()) / 1000)
  // Seconds are shown one by one on purpose. A wider "just now" bucket would swallow a
  // whole heartbeat interval — at the 5 s the simulator and `spec/prd.md` use, the
  // column would read "just now" forever and an operator could not tell a board that is
  // still beating from a page that has stopped updating.
  if (seconds <= 0) return 'just now'
  if (seconds < 60) return `${seconds} s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`
  return `${Math.floor(seconds / 86400)} d ago`
}
