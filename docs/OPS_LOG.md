# Helios Ops Log — one dated entry per scheduled run, newest last

## 2026-08-15 → 2026-08-20 — seeded by the main session (five-day incident, resolved in this commit)

Written 2026-08-15, shipped 2026-08-20: the main session's host process died
before the fix below finished verification, leaving it UNCOMMITTED in the
working tree for five days. The scheduled agent ran daily throughout and,
per its check-0b rule, correctly voided its fix mandate every run (dirty
tree = a human may be mid-edit) — so CI stayed red and nothing self-repaired
until the tree's owner returned. That rule worked as designed; the gap was
the orchestrator's unshipped work, not the agent. Verification was re-run
in full on 2026-08-20 (11 checks, incl. a simulation of CI's actual
git init + fetch --depth=1 checkout): verdict PASS/GO. A ride-along from
that review: the test's ground-truth probe reads the git COMMON dir
(--git-common-dir), not --absolute-git-dir, so it also holds in linked
worktrees.

The scheduled agent's first run (2026-08-15 15:07 UTC, a catch-up fire when
the app opened) completed check 0 cleanly — tree clean, pulled to 0b9143b —
and the three API fetches, then lost its host ~70 seconds in (app closed).
No repo writes were made by it. This entry seeds the log so check 0a has a
baseline; the sweep below was completed manually in the main session on
2026-08-15.

Sweep results:
- Heartbeat (local, --no-api): exit 0 HEALTHY. Newest price data 6.6h old,
  manifest healthy and agreeing with git, quarantine {}, degraded none.
- Both scheduled scrapes fired and PUBLISHED (9b4221a at 22:09Z Aug 14,
  0b9143b at 11:39Z Aug 15). Audit: CLEAN 9 / NO_BASELINE 1, alarms 0.
  No active pair >= 30h stale. Live site serving fresh prices and articles.
- RED FOUND — scrape.yml runs #2 and #3 concluded FAILURE despite
  publishing: tests/test_heartbeat.py::test_the_git_helpers_read_this_repository
  asserted the live checkout has complete history, but scrape.yml checks out
  fetch-depth-1 (shallow), so the test failed on every CI run containing it
  ("1 failed, 429 passed"). Run #1 predates the test: it started at 13:43Z,
  before 680757f was pushed, and its price commit simply rebased on top.
  heartbeat.yml runs #1 and #2 then went red with SCRAPE_RUN_FAILED — the
  crash-loop check doing its job on a red upstream, not a heartbeat defect.
  Root cause class: the test asserted the SHAPE OF THE CHECKOUT, not the
  correctness of the helper, and all pre-merge verification (builder, red
  team, orchestrator) ran only on complete clones where the assertion is
  vacuously true.
- FIX (ships in the commit carrying this entry): the test now verifies
  git_history_complete() against git's own shallow marker (via
  rev-parse --path-format=absolute --git-common-dir) in whichever
  environment it runs — CI now
  genuinely exercises the shallow branch no local run ever reached — and on
  shallow checkouts declines the data-pathspec assertions for the same
  reason heartbeat.py reports HISTORY_INCOMPLETE instead of trusting
  graft-boundary history. Complete clones still run every original
  assertion. Proven before commit: the old test reproduced the exact CI
  failure on a local depth-1 clone; the new test passes there and the full
  suite passes 430/430 on the complete clone; independent adversarial
  verification ran before commit (verdict recorded in the commit message).
- Expected next: tonight's 21:30 UTC scrape run goes green end to end and
  the 23:30 UTC heartbeat follows. There is no remaining "expected red";
  any red after this fix is a new incident.

Open concerns: standing backlog only (MEDIUM-1 hardcoded capacity
comparative in the head-to-head body; LOW-1 lede figures lack data-field
provenance; LOW-2 the IEA citation URL 403s all bots — exempt it from any
future link-liveness checker).
