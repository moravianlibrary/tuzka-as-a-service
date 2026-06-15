// Unit tests for the dashboard's pure duration formatters, run with Node:
//   node tests/dashboard_format.test.mjs
// dashboard.js is a browser script (top-level DOM bootstrap), so we extract just
// the two pure functions from source and evaluate them in an isolated vm context
// — no source changes, and we test the real shipped code.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const src = readFileSync(new URL("../app/static/dashboard.js", import.meta.url), "utf8");

function extractFn(name) {
  const start = src.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} not found in dashboard.js`);
  let depth = 0;
  let end = -1;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) { end = i + 1; break; }
  }
  assert.ok(end > start, `could not find end of ${name}`);
  return src.slice(start, end);
}

const ctx = { Date, Math, isNaN };
vm.createContext(ctx);
vm.runInContext(
  `${extractFn("fmtDuration")}\n${extractFn("fmtTimeInSystem")}\n` +
    "this.fmtDuration = fmtDuration; this.fmtTimeInSystem = fmtTimeInSystem;",
  ctx,
);
const { fmtDuration, fmtTimeInSystem } = ctx;

// missing endpoints -> em-dash
assert.equal(fmtDuration(null, "2026-06-15T10:00:00Z"), "—");
assert.equal(fmtDuration("2026-06-15T10:00:00Z", null), "—");
assert.equal(fmtDuration(null, null), "—");

// negative span (clock skew / inverted) -> em-dash, never a bogus negative
assert.equal(fmtDuration("2026-06-15T10:00:02Z", "2026-06-15T10:00:00Z"), "—");

// sub-minute -> "N.Ns"
assert.equal(fmtDuration("2026-06-15T10:00:00Z", "2026-06-15T10:00:02.5Z"), "2.5s");
assert.equal(fmtDuration("2026-06-15T10:00:00Z", "2026-06-15T10:00:00Z"), "0.0s");

// >= 60s -> "Xm Ys"
assert.equal(fmtDuration("2026-06-15T10:00:00Z", "2026-06-15T10:01:30Z"), "1m 30s");
assert.equal(fmtDuration("2026-06-15T10:00:00Z", "2026-06-15T10:02:00Z"), "2m 0s");

// time-in-system = submitted -> stored
assert.equal(
  fmtTimeInSystem({ submitted_at: "2026-06-15T10:00:00Z", stored_at: "2026-06-15T10:00:03Z" }),
  "3.0s",
);
assert.equal(fmtTimeInSystem({ submitted_at: "2026-06-15T10:00:00Z", stored_at: null }), "—");

console.log("OK: dashboard formatter tests passed");
