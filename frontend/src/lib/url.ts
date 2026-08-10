/** True iff `url` parses as an absolute `http://` or `https://` URL --
 * the gate every backend-supplied URL must pass before it's ever put into
 * an anchor's `href` (RecordingsDrawer's `source.url`, DetailPanel's
 * `recording.url`). Review fix (M3.5c): the backend's `classify_url`
 * (media/detect.py) now also enforces this same scheme allowlist before
 * persisting a `media_sources.url` row, but this is frontend
 * defense-in-depth -- a `javascript://zoom.us/rec/share/x` (or any other
 * non-http(s) scheme that still parses a real-looking host/path) must
 * never reach an `href` regardless of how it got into the database, now or
 * from an older row written before that backend fix existed.
 *
 * Uses the `URL` constructor rather than a `startsWith("http")` check
 * (DetailPanel's older convention for `sourceUrl`): `startsWith` also
 * passes a scheme-relative string like "httpjunk://…", and doesn't catch
 * malformed input the way a real parse does. */
export function isSafeHttpUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}
