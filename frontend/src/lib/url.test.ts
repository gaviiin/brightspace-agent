import { describe, expect, it } from "vitest";

import { isSafeHttpUrl } from "./url";

describe("isSafeHttpUrl", () => {
  it("accepts absolute http and https URLs", () => {
    expect(isSafeHttpUrl("https://mediasite.example.edu/watch/1")).toBe(true);
    expect(isSafeHttpUrl("http://zoom.us/rec/share/xyz")).toBe(true);
  });

  it("rejects a javascript: URL, even one shaped like a real recording URL", () => {
    // The exact review-flagged shape: parses with a real-looking host/path
    // for classify_url's (now-fixed) matching, but the scheme is the part
    // that makes it dangerous to ever put in an href.
    expect(isSafeHttpUrl("javascript://zoom.us/rec/share/x")).toBe(false);
    expect(isSafeHttpUrl("javascript:alert(1)")).toBe(false);
  });

  it("rejects a data: URL", () => {
    expect(isSafeHttpUrl("data://zoom.us/rec/share/x")).toBe(false);
    expect(isSafeHttpUrl("data:text/html,<script>alert(1)</script>")).toBe(false);
  });

  it("rejects other non-http(s) schemes", () => {
    expect(isSafeHttpUrl("ftp://example.com/x")).toBe(false);
    expect(isSafeHttpUrl("file:///etc/passwd")).toBe(false);
  });

  it("rejects relative and malformed strings", () => {
    expect(isSafeHttpUrl("/relative/path")).toBe(false);
    expect(isSafeHttpUrl("not a url")).toBe(false);
    expect(isSafeHttpUrl("")).toBe(false);
  });
});
