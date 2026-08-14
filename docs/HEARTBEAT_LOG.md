# Pipeline Heartbeat Log — 2026-08-14

The dead-man's switch: `heartbeat.py`, `.github/workflows/heartbeat.yml`,
`tests/test_heartbeat.py`. Ported from the plant tracker
(`project_gaia/.github/workflows/heartbeat.yml`) and rebuilt rather than
copied — the divergences and the reasons are in §5.

Built from commit `82ab5ea` (clean tree). **No live HTTP was issued.** The
GitHub API path was exercised against saved payloads (`--runs-json`); every
other path was exercised against this repository's real git history and
against throwaway repositories in a scratch directory. `docs/PLAN.md` is
unchanged.

**Standing tension, recorded rather than resolved:** PLAN.md §5 lists
"heartbeat" among the Phase B non-goals. That listing predates the site
going live and the 2x-daily cron being enabled at `82ab5ea`. A scheduled
pipeline nobody watches is the condition this monitor exists for, so the
work was done on instruction; the plan line was NOT edited, because editing
a plan to match what was built is how a plan stops being a check on the
build. Brandon reconciles it.

## 1. The question it answers

One question, asked independently of the pipeline: **has the pipeline
published recently?**

Every other guard in this repo — the runner's dead-retailer alarm, the data
gate, `audit.py`, `scrape.yml`'s "Raise alarms" step — runs *as part of* a
scrape. A scrape that never happens trips none of them. The site keeps
serving the last good build with prices ageing quietly behind it, every
check reports green because no check ran, and the failure is invisible
precisely because it is total. gaia's `heartbeat.yml` opens with the same
observation; it is the reason both exist.

## 2. What makes it red, and in which window

`WINDOW_HOURS = 30` in `heartbeat.py` is the only number that decides
anything. The workflow's prose is pinned to it by a test, so the two cannot
drift.

| Code | Condition | Window / source | Young-repo grace |
|---|---|---|---|
| `DATA_STALE` | age of the last commit touching `data/prices/` ≥ window | 30h, git | n/a (a data commit cannot predate its repo) |
| `NO_DATA_COMMIT` | no commit in history has ever touched `data/prices/` | git, full history | **yes** — silent while repo age < 30h |
| `MANIFEST_STALE` | `data/last_manifest.json` `timestamp` age ≥ window | 30h, file | n/a (same reason as `DATA_STALE`) |
| `MANIFEST_UNREADABLE` | manifest missing, corrupt, or not an object | file | **yes** |
| `MANIFEST_NO_TIMESTAMP` | manifest has no parseable `timestamp` | file | **yes** (downgraded to a notice) |
| `MANIFEST_DISAGREES` | manifest age < window **and** data age ≥ window (or absent) — the half-run | 30h both sides | **yes** |
| `SCRAPE_RUN_FAILED` | newest **completed** run of `scrape.yml` concluded `failure`, `timed_out`, `startup_failure`, `action_required`, or `stale` | GitHub API, latest run only | **no, deliberately** |
| `SCRAPE_WORKFLOW_MISSING` | HTTP 404 for `scrape.yml` — renamed, deleted, or never there | GitHub API | **no** |
| `NO_COMPLETED_SCRAPE_RUN` | the API answered and reported **zero** completed runs of `scrape.yml` | GitHub API | **yes** |
| `HISTORY_INCOMPLETE` | the clone is shallow, so freshness was not measured at all | git | **no** |
| `CLOCK_SKEW` | a data or manifest timestamp sits in the future by more than the tolerance | 5 min tolerance | **no** |
| `FORCED_ALERT` | `workflow_dispatch` with `force_alert: true` | n/a | n/a |

Warnings (printed, never red): `MANIFEST_DEGRADED`, `SCRAPE_RUNS_UNKNOWN`,
`SCRAPE_RUN_SOFT` (cancelled/skipped/neutral), `SCRAPE_RUN_UNRECOGNISED`,
`REPO_AGE_UNKNOWN`, and `CLOCK_SKEW` **within** the 5-minute tolerance.
Notices: the young-repo grace messages.

Three of these were warnings until a red team proved each one a
failure-to-verify that read as health. They are recorded in §8 rather than
quietly corrected, because "we checked X" and "X could not be checked, so we
said nothing" are the same sentence to an operator reading a green run.

### Why 30h

Inherited from gaia, where it is calibrated: 204 gaps between commits
touching `data/prices/` since 2026-05-01, median 12.70h, p95 15.87h, max
34.51h — one gap over 30h in three and a half months, and that one was a
true positive (two consecutive scheduled runs that published nothing).

It transfers because the cadence transfers: Helios scrapes at 11:00 and
21:30 UTC exactly as gaia does, so healthy spacing is 13.5h and 10.5h, one
missed cycle (~24h) passes in silence, and two consecutive misses (34.5h or
37.5h plus jitter) alarm.

**It is a borrowed prior, not a measurement of this repo.** The cron was
enabled at `82ab5ea`; Helios has no gap distribution of its own yet.
Re-derive after ~30 days of real runs:

```
git log --format=%cI -- data/prices/
```

If the observed p95 approaches 30h, the window is wrong — not the pipeline.
This is written down here because gaia's own worst heartbeat defect was a
calibration paragraph that had been measured on a different metric than the
check used, and nobody noticed for months.

### Why the manifest is checked in both directions

`data/last_manifest.json` is the pipeline's account of itself, so it is
read as a **claim** and cross-examined against git:

- **fresh manifest + stale/absent price data = `MANIFEST_DISAGREES`.** The
  half-run. This is reachable today, not hypothetical: `scrape.yml` git-adds
  the manifest and `data/prices/` in one command, so a run that collects
  zero rows still produces a commit and still writes a `"healthy"` manifest.
  A monitor that read the manifest — or that asked only "was there a recent
  commit?" — would call that healthy.
- **stale manifest + fresh price data = `MANIFEST_STALE`.** Prices moved
  without the runner (a hand commit, a backfill). The pipeline of record
  still has not run.

Measuring the commit age of `data/prices/` specifically, rather than of the
repo, is the same discipline: a docs or config push would otherwise reset
the clock and mask a scraper dead for days.

### Why a failed scrape run is its own condition

The crash-loop: `scrape.yml` fails twice a day, publishes nothing, and the
"your workflow failed" mail lands in a folder nobody reads. The data-age
checks would catch it too, but only after 30h. Reading the run conclusion
catches it on the next heartbeat — inside 13.5h — and names the run URL.

Queried by workflow **filename** (`scrape.yml`) via the REST API with the
run's own `GITHUB_TOKEN` and `permissions: actions: read`. No secret. Only
**completed** runs are considered, selected by timestamp rather than by
trusting API order, so a scrape running right now cannot mask the previous
run's failure.

## 3. What is deliberately NOT red

Both of these are decisions with a stated cost, not oversights. gaia's R14:
*an alarm that cannot be cleared is not a control* — a check that goes red
on a condition nothing scheduled fixes converts the owner's only monitoring
signal into noise. gaia's own `CORRECTNESS_ACTIONS.md` B-2 puts it in
measurable terms: a channel that goes red twice a day from day one has been
pre-broken.

- **`pipeline_status: "degraded"` → warning.** The scrape run that wrote a
  degraded manifest already failed its own job (`scrape.yml`'s alarm step
  exits 1 on any `degraded_retailers`), and this heartbeat re-reports that
  as `SCRAPE_RUN_FAILED`. Raising it a second time adds a red light that no
  scheduled action can clear. The status is still read, still printed, and
  still feeds `MANIFEST_DISAGREES`.
- **GitHub API unreachable → warning.** The git-side checks answer the
  primary question without the API. A transient `api.github.com` 5xx that
  pages the owner teaches the owner to ignore the page. *Cost, stated: an
  attacker or an outage that suppresses the API check leaves only the 30h
  data-age path, which is slower.*

  **Correction (red team, this session).** An earlier version of this
  paragraph ended "the answer is `None` — 'could not ask' — never `[]`, so
  silence is never read as success." That was false, and the red team's own
  live fetch is the counterexample: the real API returns
  `{"total_count": 0, "workflow_runs": []}` for this repository. An empty
  list is exactly what GitHub returns when a workflow has never run, and the
  code then treated it as a NOTICE and printed "the pipeline is publishing".
  The line that actually holds is narrower, and is now the rule: **"could
  not ask" is a warning; every ANSWER is taken at face value.** A 404 and an
  empty list are both definite statements, and both are red past grace.

## 4. The young-repo case

A repository younger than the window cannot have missed two publish cycles,
so "no data yet" and "no manifest yet" are not evidence of anything. Without
the grace, the first heartbeat after `git init` alarms — and an alarm that
fires on day one is pre-broken.

Grace covers **absence only** (`NO_DATA_COMMIT`, `MANIFEST_UNREADABLE`,
`MANIFEST_NO_TIMESTAMP`, `MANIFEST_DISAGREES`, `NO_COMPLETED_SCRAPE_RUN`).
It does not cover `SCRAPE_RUN_FAILED`, `HISTORY_INCOMPLETE` or `CLOCK_SKEW`:
a scrape that failed on day one failed, and a measurement that could not be
made is not made truer by the repository being young.

Repository age comes from the **first root commit**, which is why the
workflow checks out with `fetch-depth: 0`.

**Correction (red team, this session).** This section previously claimed
that a shallow clone makes `heartbeat.py` "refuse to claim youth, warn
`HISTORY_INCOMPLETE`, and fail loudly (verified: §6.5)". The verification
was invalid: §6.5's exit 1 came from `NO_DATA_COMMIT` on a **newborn**
shallow repo, not from shallowness detection. The truth was worse than the
claim in both directions:

- shallowness is not only a *missing* first commit, it **corrupts the
  freshness metric**. At a shallow graft boundary git has no parent to diff
  against and reports that commit as touching every path, so
  `git log -1 -- data/prices/` returns the boundary commit's date whether or
  not it touched price data. The check silently becomes "age of the last
  commit of any kind" — the exact metric this monitor's header says it must
  never use. Proven on a real `--depth 1` clone of this repository: 9.1h of
  freshness that did not exist, and it fails **fresh**, i.e. silently.
- `HISTORY_INCOMPLETE` was a *warning*, so that corrupt reading exited 0.

Both are fixed. Shallowness is now an **error**, and the git-derived
freshness verdict is **suppressed entirely** rather than reported from a
measurement known to be wrong — reporting no number beats reporting one that
fails in the healthy direction. The regression test builds a real two-commit
repo (a price commit, then a docs-only commit 24h later), clones it at
`--depth 1`, and asserts that the pathspec query returns the docs commit,
that the full clone alarms `DATA_STALE` at the same instant, and that the
shallow clone exits 1 without ever issuing a freshness verdict.

The repo's `.git` is ~2 MB, so full history costs nothing worth optimising.

Green under grace prints a different closing line — *"nothing judged... not
a statement that the pipeline works"* — because a true exit code carrying a
false sentence is this project's defining defect class.

## 5. How this diverges from gaia's heartbeat, and why

| | gaia | Helios | why |
|---|---|---|---|
| logic lives in | ~290 lines of inline bash in the yml | `heartbeat.py`, pure `evaluate(Observation) -> Verdict` | gaia's version is untestable; its one test can only assert on the yml as text. Here every failure mode — including ones that take days of a dead pipeline to produce — is a unit test that runs offline in ~1 s |
| SMTP email step | present, `dawidd6/action-send-mail`, gated on secrets | **absent** | gaia's own notes record the secrets were never set and the path has plausibly never delivered a message. A mail step that silently sends nothing is worse than none: it gets counted as coverage. The failed job is the proven channel; the yml documents how to add Slack/email later, with the rule that additions are `continue-on-error` and never replace it |
| any secret at all | 5 (`SMTP_*`) | **zero** | nothing to misconfigure, nothing that can silently stop working |
| manifest | not consulted | consulted, both directions, plus `pipeline_status` | catches the half-run, which the commit-age check alone reads as healthy |
| scrape run conclusion | not consulted | newest completed run via REST API | catches the crash-loop ~16h sooner than data ageing does |
| young repo | no handling | explicit grace bounded by the window | gaia was months old when its heartbeat landed; Helios is one day old |
| checkout | `fetch-depth: 100` | `fetch-depth: 0` | 100 makes repository age unknowable, and "no data commit in 100 commits" conflates "dead pipeline" with "busy week" |
| schedule | every 6h (`0 1,7,13,19 * * *`) | 13:00 + 23:30 UTC | on instruction: 2h after each scrape, which also clears `scrape.yml`'s 60-minute timeout cap with an hour to spare. **Cost, stated:** worst-case detection latency is 30h + 13.5h = 43.5h vs gaia's ~36h. Adding `0 5,17 * * *` closes most of it |
| dependencies | `actions/checkout` only (bash) | stdlib Python, **no `pip install` step** | a broken `requirements.txt` install is one of the failures the monitor must survive. Pinned by a test that reads `heartbeat.py`'s imports out of its AST |
| step ordering | mail step, then a separate `if: always()` alarm step | one step, one exit code | fewer places for the alarm to be swallowed; the workflow has no `continue-on-error` and no conditional step, asserted by test |

Kept from gaia, unchanged and deliberately: the 30h window and its meaning;
measuring `data/prices/` rather than any commit; `::error::` + a failed job
as the alert of record; the `force_alert` drill; and the honest disclosure
that a monitor running inside GitHub Actions shares fate with what it
watches (including that GitHub disables scheduled workflows after 60 days of
no commit activity — i.e. exactly when a dead pipeline needs this most), so
an external monitor such as healthchecks.io remains the stronger design.

## 6. What was run

`ruff check .` clean, `pytest` green (see the session report for pasted
output). Beyond the suite, the script was exercised end to end:

1. **Real repo, offline** — `heartbeat.py --no-api` → exit 0, data 9.3h old,
   manifest 9.7h old, grace ON (repo 17.5h old).
2. **The drill** — `--force-alert` → exit 1, `::error::HEARTBEAT
   FORCED_ALERT`.
3. **Time travel** — `--now 2026-08-17T00:00:00Z` → exit 1,
   `DATA_STALE` (68.0h) + `MANIFEST_STALE` (68.4h).
4. **Half-run + crash-loop** — real git history, a manifest timestamped 2.4h
   before `now`, and a saved API payload whose newest completed run failed
   (with a newer in-progress run above it) → exit 1, `DATA_STALE` +
   `MANIFEST_DISAGREES` + `SCRAPE_RUN_FAILED`; the in-progress run did not
   mask the failure.
5. **A real newborn repo** (`git init`, one commit, no data) → exit 0, both
   grace notices, no false "the pipeline is publishing". A **shallow clone**
   of that same repo also exited 1 — but from `NO_DATA_COMMIT`, because the
   repo was newborn and empty. *That run proved nothing about shallowness*
   (red team; the claim it was used to support is corrected in §4). The real
   shallow-clone behaviour is now covered by a test that builds a repo where
   the two cases give different answers.
6. **Invalid input** — `--now yesterday` → exit 2; `--window-hours 0`, `nan`,
   `inf`, `-1` → exit 2; unreadable `--runs-json` → warning, not a false
   green.
7. **The workflow step's own shell**, extracted from the yml and run with
   `FORCE_ALERT` empty (the schedule-event rendering), `false`, and `true` →
   exit 0, 0, 1.

## 7. Known weaknesses

1. **It shares fate with what it monitors.** Inside GitHub Actions: if
   Actions is down, billing lapses, the repo is disabled, or scheduled
   workflows stop firing, it reports nothing at the moment it is needed.
   GitHub disables scheduled workflows after 60 days without commit
   activity. An external cron monitor is the real fix.
2. **The 30h window is borrowed, not measured** (§2). Until this repo has
   its own gap distribution, the false-positive rate is unknown.
3. **Detection latency is 43.5h worst case** with a twice-daily schedule
   (§5). `SCRAPE_RUN_FAILED` is not subject to it; data-ageing is.
4. **An API check that cannot be MADE degrades to a warning** (§3). Correct
   for a 5xx blip; it also means a token-permission regression (e.g.
   `actions: read` removed) shows up only as a warning nobody reads. Note
   the boundary: an API that *answers* is now always believed — 404 and
   empty-list are both red.
5. **`MANIFEST_DISAGREES` assumes the manifest is committed.** It reads the
   checked-out file, i.e. the last committed manifest, not the runner's
   in-flight one. A pipeline that stops committing the manifest entirely
   makes this check read `MANIFEST_STALE` instead — still red, but with the
   wrong diagnosis attached.
6. **Nothing checks the heartbeat itself is still scheduled.** A workflow
   file deleted, renamed, or disabled in the Actions tab produces silence,
   and silence is indistinguishable from health from inside. Only an
   external monitor closes this.
7. **The scrape-run check reads one workflow by filename.** A 404 is now
   separated from transient API failure and raised as
   `SCRAPE_WORKFLOW_MISSING` (red), so a renamed `scrape.yml` cannot silently
   disable the check. Residual: if scraping is ever split across a second
   workflow file, only `scrape.yml` is watched, and nothing notices the
   omission.
8. **`--runs-json` accepts any payload.** It exists so the API path can be
   exercised offline; it also means a hand-written file can make the check
   say anything. Operator tool, not a trust boundary.
9. **Every window comparison uses `>=` against wall-clock UTC** on the
   runner. Forward skew beyond 5 minutes is now detected on both primary
   timestamps and is an error. **Backward** skew is not detectable from
   inside: a runner clock set into the past makes everything look newer, and
   there is no second clock here to check it against. An external monitor is
   the only real answer.
10. **The 5-minute skew tolerance is a judgement, not a measurement.** It is
   sized for NTP jitter between a runner and a committer. Nothing in this
   repo has measured actual observed skew.

## 8. Red team round 1 — findings and fixes

Verdict NO-GO. Core detection (half-run, crash-loop, git corruption) was
confirmed sound; three separate paths let *failure to verify* read as
health, and two claims in this log were false. All fixed in the same
session. The pattern is worth stating on its own, because all three code
defects were the same mistake wearing different clothes: **the code
distinguished "bad" from "good" but not "unknown" from "good".**

| # | Finding | Fix |
|---|---|---|
| F3 | The live API returns `total_count=0` for `scrape.yml` — **CI has never run the scrape**; every data commit in this repo was made from a laptop. The tool printed "the pipeline is publishing" over that, because zero completed runs was a NOTICE | `NO_COMPLETED_SCRAPE_RUN` is an ERROR past grace. An answered query is a fact; only "could not ask" stays a warning |
| F2 | `CLOCK_SKEW` sat in an `elif` in front of the staleness test, so a future-dated commit removed the staleness question from the run *and* printed a negative age beside the word "publishing". A +48h skew bought a dead pipeline 78h of asserted health | Skew is evaluated as an additional condition on both timestamps; beyond a 5-minute tolerance it is an ERROR; the healthy sentence can never print a negative age |
| F1 | In a shallow clone the graft boundary defeats the pathspec, so the data-age check silently became "age of the last commit of any kind" (9.1h of false freshness on a real clone), and `HISTORY_INCOMPLETE` was only a warning. This log's §4 claimed the opposite and cited a run that proved nothing | `HISTORY_INCOMPLETE` is an ERROR and the freshness verdict is suppressed entirely when history is shallow; §4 and §6.5 corrected |
| F4 | `--window-hours nan` passed the `<= 0` guard and disabled every comparison — exit 0 on a repo 72 years stale | `math.isfinite` guard, exit 2, with a test that pins *why* (a nan window really does disable the check) |
| F6 | The window pin covered the yml only, so this log's table cells and CLAUDE.md could drift from `WINDOW_HOURS` | The pin now covers the yml, CLAUDE.md and this file, including the table's window column parsed structurally |

**The live finding is the operationally important one.** The scheduled
pipeline this monitor was built around has never fired in CI. Nothing is
"wrong" with the heartbeat as a result — it is now the thing that will say
so out loud, as soon as this repository is older than the window. Brandon
should expect `NO_COMPLETED_SCRAPE_RUN` to go red before the first
successful scheduled scrape, and that is the check working.
