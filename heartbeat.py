"""
heartbeat.py -- the dead-man's switch. Does this pipeline still publish?

THE ONE QUESTION. Every other guard in this repo (the data gate, audit.py,
the runner's per-retailer health, the workflow's alarm step) runs as PART OF
a scrape. A scrape that never happens trips none of them: the site keeps
serving the last good build, silently, forever, and every check reports
green because no check ran. This script answers the only question those
cannot: has the pipeline published recently?

It is deliberately SEPARATE from the pipeline it watches:

  * stdlib only. No requests, no jinja2, nothing from requirements.txt --
    a broken dependency install is one of the failures this must survive,
    so .github/workflows/heartbeat.yml runs it with NO pip install step.
    (tests/test_heartbeat.py pins that coupling: add an import from
    requirements.txt here and the test fails.)
  * it does not import build.py, audit.py or scrapers/. It reads git and
    two files. Sharing code with the thing under test is how a monitor
    inherits the defect it is supposed to report.

WHAT IT CHECKS, and why each one exists (see the table in
docs/HEARTBEAT_LOG.md):

  1. git: the age of the last commit touching data/prices/, against a 30h
     window. NOT the age of the last commit of any kind -- a docs or config
     push would reset that clock and mask a scraper dead for days.
  2. the manifest: data/last_manifest.json's own timestamp and
     pipeline_status. Read as a CLAIM, never as the answer.
  3. git vs the manifest. The manifest can lie in both directions and each
     direction is a distinct real failure:
       - manifest fresh + no new price data = HALF-RUN. The runner started,
         wrote a manifest, and published nothing. This is reachable today:
         scrape.yml git-adds the manifest and data/prices/ together, so a
         run that collects zero rows still produces a commit, and any check
         reading "was there a recent commit?" or "what does the manifest
         say?" alone sees a healthy pipeline.
       - price data fresh + manifest stale = something other than the
         runner wrote prices (a hand commit, a backfill). The pipeline of
         record still has not run.
  4. the GitHub API: the conclusion of the most recent COMPLETED run of the
     Scrape workflow. This catches the crash-loop -- scrape fails twice a
     day, publishes nothing, and the "you have a failed workflow" mail goes
     to a folder nobody reads. Checks 1-3 would eventually catch it too,
     but only after 30h; this catches it on the next heartbeat.

WHAT IT CANNOT DO, stated up front because a monitor believed to cover more
than it does is worse than no monitor. This runs INSIDE GitHub Actions, so
it shares fate with what it watches: if Actions is down, if billing lapses,
if the repo is disabled, or if GitHub disables scheduled workflows (it does
that after 60 days with no commit activity -- i.e. exactly when a dead
pipeline needs this most), the heartbeat goes quiet at the same moment as
the pipeline and reports nothing. An EXTERNAL cron monitor (healthchecks.io
or equivalent) is the strictly stronger design and is the documented
upgrade path in the workflow file. This exists because it needs no account,
no secrets and no signup.

Exit codes:
    0  healthy (warnings and notices are allowed and printed)
    1  ALARM -- at least one error finding; ::error:: annotations emitted
    2  usage error

Usage:
    python -X utf8 heartbeat.py                     # in CI
    python -X utf8 heartbeat.py --no-api            # offline, git+manifest
    python -X utf8 heartbeat.py --now 2026-09-01T00:00:00Z --no-api
    python -X utf8 heartbeat.py --runs-json runs.json   # saved API payload
    python -X utf8 heartbeat.py --force-alert       # prove the alert path
"""

import argparse
import json
import math
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Calibration. WINDOW_HOURS is the only number that decides anything; the
# workflow's prose is pinned to it by tests/test_heartbeat.py so the two can
# never drift (gaia's heartbeat shipped a calibration paragraph measured on
# a different metric than the one the check used -- the sentence was the
# defect, not the code).
#
# 30h is gaia's window, mirrored deliberately, and it means the same thing
# here because the cadence is the same: scrape at 11:00 and 21:30 UTC, so
# healthy spacing is 13.5h and 10.5h. One missed cycle (~24h) is tolerated
# in silence; two consecutive missed cycles (34.5h or 37.5h plus jitter)
# alarm. Gaia measured this against 204 real gaps between commits touching
# data/prices/ since 2026-05-01: median 12.70h, p95 15.87h, max 34.51h --
# one gap over 30h in three and a half months, and that one was a true
# positive (two scheduled runs that published nothing).
#
# HELIOS HAS NO SUCH MEASUREMENT YET. The 2x-daily cron was enabled at
# commit 82ab5ea; this repo's own gap distribution does not exist. 30h is
# an inherited prior from a pipeline with the same schedule, not a measured
# threshold for this one. Re-derive it after ~30 days of real runs:
#     git log --format=%cI -- data/prices/
# and if the observed p95 approaches 30h, the window is wrong, not the
# pipeline. Until then the honest claim is "borrowed, and stated as such".
WINDOW_HOURS = 30

# How far into the future a timestamp may sit before the measurement is
# declared unusable rather than merely odd. Runner and committer clocks are
# NTP-synced, so seconds of disagreement are ordinary and hours are not.
# Anything beyond this is an ERROR: a clock that disagrees makes freshness
# unmeasurable, and unmeasurable must never read as healthy.
SKEW_TOLERANCE_HOURS = 5 / 60

# The path whose commit age IS the heartbeat. Changing this changes what
# "the pipeline published" means.
DATA_PATH = "data/prices/"

MANIFEST_PATH = "data/last_manifest.json"

# Queried by FILENAME, not by name or id: the file cannot be renamed without
# this breaking loudly, whereas `name: Scrape` can be edited in place.
SCRAPE_WORKFLOW_FILE = "scrape.yml"

# A completed run in any of these states means the scrape did not do its
# job. `stale` and `action_required` are included because both mean "this
# run produced nothing" just as surely as `failure` does.
FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "startup_failure", "action_required", "stale"}
)

# Not a success and not proof of a broken pipeline either. A human usually
# caused these; report them, do not alarm on them.
SOFT_CONCLUSIONS = frozenset({"cancelled", "skipped", "neutral"})

ERROR = "error"
WARNING = "warning"
NOTICE = "notice"


class WorkflowNotFound(Exception):
    """HTTP 404 on the workflow itself: it was renamed, deleted, or never
    existed. Structurally different from a transient API failure, and
    treated differently -- see SCRAPE_WORKFLOW_MISSING."""

    def __init__(self, workflow: str):
        super().__init__(f"workflow {workflow} not found (HTTP 404)")


# --------------------------------------------------------------------------
# Pure data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One thing the heartbeat has to say. `code` is the stable identity --
    tests and humans key off it; `message` is prose and may be reworded."""

    level: str
    code: str
    message: str


@dataclass(frozen=True)
class Observation:
    """Everything measured about the world, in one immutable value.

    Collecting and judging are split on purpose: every rule in evaluate()
    is a pure function of this struct, so every failure mode below --
    including ones that take days of a dead pipeline to reach in reality --
    is reachable in a unit test in microseconds, offline.
    """

    now: datetime
    last_data_commit: datetime | None = None
    last_any_commit: datetime | None = None
    first_commit: datetime | None = None
    # False when the clone is shallow (or git could not be asked). The
    # oldest commit in a shallow clone is not the repo's first commit, so
    # repository age -- and therefore the young-repo grace -- is unknowable.
    history_complete: bool = True
    manifest_timestamp: datetime | None = None
    manifest_status: str | None = None
    manifest_degraded: tuple[str, ...] = ()
    manifest_error: str | None = None
    # None means "could not ask", which is NOT the same as "no runs".
    scrape_runs: list[dict] | None = None
    scrape_runs_error: str | None = None
    # True only for a 404 on the workflow itself: structural, not transient.
    scrape_workflow_missing: bool = False


@dataclass
class Verdict:
    findings: list[Finding] = field(default_factory=list)
    window_hours: float = WINDOW_HOURS
    young: bool = False
    repo_age_hours: float | None = None

    def add(self, level: str, code: str, message: str) -> None:
        self.findings.append(Finding(level, code, message))

    def _at(self, level: str) -> list[Finding]:
        return [f for f in self.findings if f.level == level]

    @property
    def errors(self) -> list[Finding]:
        return self._at(ERROR)

    @property
    def warnings(self) -> list[Finding]:
        return self._at(WARNING)

    @property
    def notices(self) -> list[Finding]:
        return self._at(NOTICE)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def codes(self) -> set[str]:
        return {f.code for f in self.findings}

    def error_codes(self) -> set[str]:
        return {f.code for f in self.errors}


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 instant to an aware UTC datetime, or None.

    Handles the three shapes this program meets: git's %cI
    (2026-08-13T23:57:21-04:00), the GitHub API's Z suffix
    (2026-08-14T11:00:03Z), and the manifest's +00:00. A naive string is
    read as UTC -- the alternative, reading it as the runner's local time,
    silently shifts every age by the runner's offset.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def hours_since(moment: datetime | None, now: datetime | None) -> float | None:
    if moment is None or now is None:
        return None
    return (now - moment).total_seconds() / 3600.0


def fmt_age(hours: float | None) -> str:
    return "unknown" if hours is None else f"{hours:.1f}h"


def fmt_time(moment: datetime | None) -> str:
    return "none" if moment is None else moment.isoformat()


def _run_sort_key(run: dict) -> datetime:
    for key in ("created_at", "run_started_at", "updated_at"):
        moment = parse_iso(run.get(key))
        if moment is not None:
            return moment
    return datetime.min.replace(tzinfo=timezone.utc)


def latest_completed_run(runs: list[dict] | None) -> dict | None:
    """The newest run that actually finished.

    In-progress and queued runs are excluded on purpose: a scrape running
    right now must not mask the previous run's failure. API order is not
    trusted -- the newest is selected by timestamp.
    """
    if not runs:
        return None
    completed = [
        run
        for run in runs
        if isinstance(run, dict)
        and str(run.get("status") or "").lower() == "completed"
        and run.get("conclusion")
    ]
    if not completed:
        return None
    return max(completed, key=_run_sort_key)


def describe_run(run: dict | None) -> str:
    if run is None:
        return "none"
    number = run.get("run_number")
    label = f"#{number} " if number is not None else ""
    return (
        f"{label}{run.get('conclusion')} "
        f"({run.get('created_at') or 'unknown time'})"
    )


# --------------------------------------------------------------------------
# The judgement. Pure: Observation in, Verdict out.
# --------------------------------------------------------------------------


def evaluate(
    obs: Observation,
    window_hours: float = WINDOW_HOURS,
    force_alert: bool = False,
) -> Verdict:
    verdict = Verdict(window_hours=window_hours)

    # -- Young-repo grace (requirement 4) ---------------------------------
    # A repository younger than the window cannot have missed two publish
    # cycles, so "no data yet" and "no manifest yet" are not evidence of
    # anything. Without this, the first heartbeat after `git init` alarms,
    # and an alarm that fires on day one is pre-broken: it teaches the owner
    # to ignore the channel before it has ever told the truth.
    #
    # Grace covers ONLY the absence cases. It cannot suppress DATA_STALE --
    # a data commit cannot be older than the repository that contains it --
    # and it deliberately does not cover SCRAPE_RUN_FAILED, because a scrape
    # that failed on day one failed, and no window makes that acceptable.
    repo_age = hours_since(obs.first_commit, obs.now)
    verdict.repo_age_hours = repo_age
    young = (
        obs.history_complete and repo_age is not None and repo_age < window_hours
    )
    verdict.young = young

    # A shallow clone does not merely hide the first commit -- it CORRUPTS
    # the freshness metric, silently and in the direction of looking healthy.
    # At the graft boundary git has no parent to diff against, so it reports
    # the boundary commit as touching every path: `git log -1 -- data/prices/`
    # returns that commit's date whether or not it touched price data, and
    # the check degrades into "age of the last commit of any kind" -- the one
    # metric this file's header says it must never use, because a docs push
    # then masks a dead scraper. Measured on a real --depth 1 clone of this
    # repository: 9.1h of false freshness.
    #
    # So this is an ERROR, not a warning, and the git-derived freshness
    # verdict is SUPPRESSED rather than reported from a broken measurement.
    # Reporting a number known to be wrong is worse than reporting none:
    # DATA_STALE would go quiet exactly when the pipeline died. The workflow
    # checks out with fetch-depth: 0, so this fires only if someone changes
    # that -- which is the regression worth catching.
    git_freshness_readable = obs.history_complete

    if not obs.history_complete:
        verdict.add(
            ERROR,
            "HISTORY_INCOMPLETE",
            "Repository history is shallow (or git could not be queried), so "
            f"freshness was NOT evaluated. At a shallow graft boundary git "
            f"reports that commit as touching every path, which turns "
            f"`git log -1 -- {DATA_PATH}` into 'age of the last commit of any "
            "kind' -- a reading that is FRESHER than the truth, so it fails "
            "silent instead of loud. Checkout must use fetch-depth: 0.",
        )
    elif repo_age is None:
        verdict.add(
            WARNING,
            "REPO_AGE_UNKNOWN",
            "Could not determine the first commit date; young-repo grace is "
            "OFF.",
        )

    # -- 1. git: has price data been committed inside the window? ---------
    data_age = hours_since(obs.last_data_commit, obs.now)

    if not git_freshness_readable:
        pass  # suppressed above, and already red
    elif obs.last_data_commit is None:
        if young:
            verdict.add(
                NOTICE,
                "YOUNG_NO_DATA_YET",
                f"No commit has touched {DATA_PATH} yet, but this repository "
                f"is only {fmt_age(repo_age)} old (window {window_hours:g}h). "
                "Too early to read that as silence.",
            )
        else:
            verdict.add(
                ERROR,
                "NO_DATA_COMMIT",
                f"No commit in this history touches {DATA_PATH}. Either the "
                "pipeline has never published, or the price store moved and "
                "this check is now measuring nothing. Both need a human.",
            )
    else:
        # Skew is evaluated as an ADDITIONAL condition, never as an
        # alternative branch. It used to be an `elif` in front of the
        # staleness test, which meant a future-dated commit removed the
        # staleness question from the run entirely: a +48h skew bought a
        # dead pipeline 78h of green while printing "the pipeline is
        # publishing (last price data -48.0h ago)". A negative age is not
        # freshness -- it is the absence of a usable measurement.
        if data_age is not None and data_age < -SKEW_TOLERANCE_HOURS:
            verdict.add(
                ERROR,
                "CLOCK_SKEW",
                f"UNVERIFIABLE: the last commit touching {DATA_PATH} is dated "
                f"{fmt_time(obs.last_data_commit)}, which is "
                f"{fmt_age(-data_age)} in the FUTURE relative to now "
                f"({fmt_time(obs.now)}). Freshness cannot be measured against "
                "a clock that disagrees, and an unmeasurable pipeline must "
                "never read as a healthy one. Check the committer and runner "
                "clocks.",
            )
        elif data_age is not None and data_age < 0:
            verdict.add(
                WARNING,
                "CLOCK_SKEW",
                f"The last commit touching {DATA_PATH} is dated "
                f"{fmt_age(-data_age)} in the future, within the "
                f"{SKEW_TOLERANCE_HOURS * 60:.0f}-minute tolerance for "
                "ordinary clock jitter. Treated as fresh.",
            )
        if data_age is not None and data_age >= window_hours:
            verdict.add(
                ERROR,
                "DATA_STALE",
                f"PIPELINE DOWN: no price data committed for "
                f"{fmt_age(data_age)} (window {window_hours:g}h). Last commit "
                f"touching {DATA_PATH}: {fmt_time(obs.last_data_commit)}. At "
                "11:00/21:30 UTC that is at least two consecutive scheduled "
                "runs that published nothing.",
            )

    # -- 2. the manifest, read as a claim ---------------------------------
    manifest_age = hours_since(obs.manifest_timestamp, obs.now)

    if obs.manifest_error is not None:
        if young:
            verdict.add(
                NOTICE,
                "YOUNG_NO_MANIFEST",
                f"{MANIFEST_PATH} is unreadable ({obs.manifest_error}), but "
                f"the repository is only {fmt_age(repo_age)} old.",
            )
        else:
            verdict.add(
                ERROR,
                "MANIFEST_UNREADABLE",
                f"{MANIFEST_PATH} is missing or unparseable "
                f"({obs.manifest_error}). The runner writes it on every run, "
                "so its absence means no run has completed -- or the file "
                "was corrupted, which is equally a human's problem.",
            )
    elif obs.manifest_timestamp is None:
        verdict.add(
            ERROR if not young else NOTICE,
            "MANIFEST_NO_TIMESTAMP",
            f"{MANIFEST_PATH} has no parseable `timestamp` field, so the "
            "pipeline's own record of when it last ran cannot be read.",
        )
    else:
        # Same rule as the data commit: a future-dated manifest is not a
        # fresh one. Without this, forward skew makes `manifest_age` a large
        # negative, which reads as "well inside the window" and suppresses
        # MANIFEST_STALE forever.
        if manifest_age is not None and manifest_age < -SKEW_TOLERANCE_HOURS:
            verdict.add(
                ERROR,
                "CLOCK_SKEW",
                f"UNVERIFIABLE: {MANIFEST_PATH} is timestamped "
                f"{fmt_time(obs.manifest_timestamp)}, which is "
                f"{fmt_age(-manifest_age)} in the FUTURE relative to now. The "
                "pipeline's own record of when it ran cannot be believed.",
            )
        if manifest_age is not None and manifest_age >= window_hours:
            verdict.add(
                ERROR,
                "MANIFEST_STALE",
                f"The pipeline's own manifest is {fmt_age(manifest_age)} old "
                f"(window {window_hours:g}h): "
                f"{fmt_time(obs.manifest_timestamp)}. The runner rewrites it "
                "on every run, so this says no run of record has completed "
                "inside the window -- even if something else has been "
                f"committing to {DATA_PATH}.",
            )

    # -- 3. git vs the manifest: the half-run --------------------------
    # Requires a trustworthy git reading: in a shallow clone `data_age` is
    # the age of any commit, so this would accuse a healthy pipeline (or,
    # worse, absolve a dead one).
    manifest_fresh = (
        manifest_age is not None
        and -SKEW_TOLERANCE_HOURS <= manifest_age < window_hours
    )
    data_absent_or_stale = data_age is None or data_age >= window_hours
    if git_freshness_readable and manifest_fresh and data_absent_or_stale and not young:
        claim = obs.manifest_status or "unknown"
        verdict.add(
            ERROR,
            "MANIFEST_DISAGREES",
            f"HALF-RUN: {MANIFEST_PATH} reports a '{claim}' run "
            f"{fmt_age(manifest_age)} ago, but no price data has been "
            f"committed for {fmt_age(data_age)} (window {window_hours:g}h). "
            "The runner ran and published nothing. A check that trusted the "
            "manifest -- or that only asked whether SOME commit was recent "
            "-- would call this healthy.",
        )

    # pipeline_status is READ and REPORTED, but degradation is not a
    # heartbeat error. scrape.yml's own alarm step already fails the run
    # that wrote a degraded manifest, and this heartbeat re-reports that as
    # SCRAPE_RUN_FAILED. Raising it a second time here would add a red light
    # this job cannot clear -- gaia's R14: an alarm that cannot be cleared
    # by any scheduled action converts the owner's only monitoring signal
    # into noise.
    if obs.manifest_status is not None and obs.manifest_status != "healthy":
        degraded = ", ".join(obs.manifest_degraded) or "unnamed"
        verdict.add(
            WARNING,
            "MANIFEST_DEGRADED",
            f"{MANIFEST_PATH} reports pipeline_status='{obs.manifest_status}' "
            f"(degraded: {degraded}). Not raised as a heartbeat error on "
            "purpose: the scrape run that wrote it already failed its own "
            "job, and this heartbeat reports that separately.",
        )

    # -- 4. the Scrape workflow's most recent completed run ---------------
    if obs.scrape_workflow_missing:
        # A 404 is not a blip. Either the workflow was renamed or deleted --
        # in which case nothing is scraping -- or this check has been
        # pointed at a file that does not exist and has been reporting on
        # nothing. Both are alarms; only transient errors get the warning.
        verdict.add(
            ERROR,
            "SCRAPE_WORKFLOW_MISSING",
            f"The GitHub API has no workflow named {SCRAPE_WORKFLOW_FILE} in "
            "this repository (HTTP 404). Either the scrape workflow was "
            "renamed or deleted -- so nothing is scraping -- or this check "
            "has been silently measuring a file that does not exist.",
        )
    elif obs.scrape_runs is None:
        verdict.add(
            WARNING,
            "SCRAPE_RUNS_UNKNOWN",
            "Could not read the Scrape workflow's run history "
            f"({obs.scrape_runs_error or 'no reason given'}). Deliberately a "
            "warning, not an error: checks 1-3 answer the primary question "
            "without the API, and a transient api.github.com failure that "
            "turns this job red teaches the owner to ignore it.",
        )
    else:
        run = latest_completed_run(obs.scrape_runs)
        if run is None:
            # HTTP 200 with an empty list is GitHub stating, definitively,
            # that this workflow has never completed a run. That is a fact
            # about the pipeline, not a failure to reach the API, and it
            # belongs with SCRAPE_WORKFLOW_MISSING rather than with a 5xx.
            #
            # Found live on this repository: the real API returns
            # total_count=0 -- CI has never run the scrape, and every data
            # commit in the history was made from a laptop. The scheduled
            # pipeline the whole monitor is built around has never actually
            # fired, and the previous NOTICE let the tool print "the
            # pipeline is publishing" over the top of that.
            #
            # Still graced while the repo is young: a repository created an
            # hour ago legitimately has no runs yet. Past the window, "no
            # scrape has ever completed" is the pipeline being down.
            detail = (
                "it has never run"
                if not obs.scrape_runs
                else "every run in the queried page is still in progress"
            )
            verdict.add(
                NOTICE if young else ERROR,
                "NO_COMPLETED_SCRAPE_RUN",
                f"The Scrape workflow ({SCRAPE_WORKFLOW_FILE}) has no "
                f"completed runs: {detail}. The API answered, so this is not "
                "an outage -- it is GitHub reporting that the scheduled "
                "pipeline has produced nothing. Any price data in the "
                "repository came from somewhere else.",
            )
        else:
            conclusion = str(run.get("conclusion") or "unknown").lower()
            if conclusion in FAILING_CONCLUSIONS:
                verdict.add(
                    ERROR,
                    "SCRAPE_RUN_FAILED",
                    "The most recent COMPLETED Scrape run concluded "
                    f"'{conclusion}': {describe_run(run)} "
                    f"{run.get('html_url') or ''}".strip()
                    + ". The pipeline is crash-looping or has already "
                    "stopped; the price data may still look fresh for "
                    f"another {window_hours:g}h.",
                )
            elif conclusion in SOFT_CONCLUSIONS:
                verdict.add(
                    WARNING,
                    "SCRAPE_RUN_SOFT",
                    f"The most recent completed Scrape run was "
                    f"'{conclusion}': {describe_run(run)}. Not a failure, "
                    "but it published nothing.",
                )
            elif conclusion != "success":
                verdict.add(
                    WARNING,
                    "SCRAPE_RUN_UNRECOGNISED",
                    f"Unrecognised run conclusion '{conclusion}' "
                    f"({describe_run(run)}). Treated as neither success nor "
                    "failure -- update FAILING_CONCLUSIONS if it is one.",
                )

    # -- the deliberate self-test -----------------------------------------
    # gaia's CORRECTNESS_ACTIONS B-2: an alert channel is not proven until a
    # deliberately failed run has been observed arriving. This is the switch
    # that produces one on demand.
    if force_alert:
        verdict.add(
            ERROR,
            "FORCED_ALERT",
            "force_alert was requested: the alert path is being exercised on "
            "purpose. Nothing is wrong with the pipeline because of this "
            "line alone -- read the other findings.",
        )

    return verdict


# --------------------------------------------------------------------------
# Collection (the impure edge)
# --------------------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_last_commit(repo_root: Path, path: str | None = None) -> datetime | None:
    args = ["log", "-1", "--format=%cI"]
    if path is not None:
        args += ["--", path]
    return parse_iso(_git(repo_root, *args))


def git_first_commit(repo_root: Path) -> datetime | None:
    """Date of the earliest root commit.

    `--max-parents=0` also matches the grafted boundary of a shallow clone,
    which is why history_complete() is consulted before the answer is
    trusted for the young-repo grace.
    """
    out = _git(repo_root, "log", "--max-parents=0", "--format=%cI")
    if not out:
        return None
    moments = [m for m in (parse_iso(line) for line in out.splitlines()) if m]
    return min(moments) if moments else None


def git_history_complete(repo_root: Path) -> bool:
    return _git(repo_root, "rev-parse", "--is-shallow-repository") == "false"


def read_manifest(path: Path) -> tuple[datetime | None, str | None, tuple, str | None]:
    """-> (timestamp, pipeline_status, degraded_retailers, error)."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None, None, (), "file not found"
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return None, None, (), f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, None, (), "manifest is not a JSON object"
    status = payload.get("pipeline_status")
    degraded = payload.get("degraded_retailers") or []
    if not isinstance(degraded, list):
        degraded = [str(degraded)]
    return (
        parse_iso(payload.get("timestamp")),
        str(status) if status is not None else None,
        tuple(str(d) for d in degraded),
        None,
    )


def fetch_scrape_runs(
    repo: str,
    token: str,
    workflow: str = SCRAPE_WORKFLOW_FILE,
    per_page: int = 10,
    timeout: int = 20,
) -> list[dict]:
    """The Scrape workflow's recent runs, newest first, from the REST API.

    Needs `permissions: actions: read` in the workflow. The token is the
    run's own GITHUB_TOKEN -- no secret to configure. This is GitHub's
    documented API accessed with an issued credential, not a crawl, so the
    repo's polite/robots machinery (which governs retailer sites) does not
    apply and is deliberately not imported.
    """
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}"
        f"/runs?per_page={per_page}&exclude_pull_requests=true"
    )
    request = urllib.request.Request(  # noqa: S310 -- fixed https URL
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "helios-heartbeat",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise WorkflowNotFound(workflow) from exc
        raise
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ValueError("API payload has no workflow_runs list")
    return runs


def collect_scrape_runs(args, env: dict) -> tuple[list[dict] | None, str | None, bool]:
    """-> (runs, error, workflow_missing).

    runs is None when the question could not be asked -- which is never the
    same as an empty list, and never the same as success.
    """
    if args.runs_json:
        try:
            with open(args.runs_json, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, f"--runs-json unreadable: {type(exc).__name__}: {exc}", False
        if isinstance(payload, dict):
            payload = payload.get("workflow_runs")
        if not isinstance(payload, list):
            return None, "--runs-json has no workflow_runs list", False
        return payload, None, False
    if args.no_api:
        return None, "--no-api requested", False
    repo = env.get("GITHUB_REPOSITORY")
    token = env.get("GITHUB_TOKEN")
    if not repo or not token:
        return None, "GITHUB_REPOSITORY / GITHUB_TOKEN not set (not in Actions?)", False
    try:
        return fetch_scrape_runs(repo, token), None, False
    except WorkflowNotFound as exc:
        return None, str(exc), True
    except Exception as exc:  # noqa: BLE001 -- any failure is "could not ask"
        return None, f"{type(exc).__name__}: {exc}", False


def observe(args, env: dict, now: datetime) -> Observation:
    repo_root = Path(args.repo_root)
    manifest_path = (
        Path(args.manifest) if args.manifest else repo_root / MANIFEST_PATH
    )
    timestamp, status, degraded, manifest_error = read_manifest(manifest_path)
    runs, runs_error, workflow_missing = collect_scrape_runs(args, env)
    return Observation(
        now=now,
        last_data_commit=git_last_commit(repo_root, DATA_PATH),
        last_any_commit=git_last_commit(repo_root),
        first_commit=git_first_commit(repo_root),
        history_complete=git_history_complete(repo_root),
        manifest_timestamp=timestamp,
        manifest_status=status,
        manifest_degraded=degraded,
        manifest_error=manifest_error,
        scrape_runs=runs,
        scrape_runs_error=runs_error,
        scrape_workflow_missing=workflow_missing,
    )


# --------------------------------------------------------------------------
# Reporting. ASCII only (repo convention); every writer emits LF.
# --------------------------------------------------------------------------


def report_lines(obs: Observation, verdict: Verdict) -> list[str]:
    run = latest_completed_run(obs.scrape_runs)
    if obs.scrape_workflow_missing:
        run_text = f"WORKFLOW NOT FOUND ({SCRAPE_WORKFLOW_FILE})"
    elif obs.scrape_runs is None:
        run_text = f"unavailable ({obs.scrape_runs_error or 'unknown'})"
    else:
        run_text = describe_run(run)
    if verdict.repo_age_hours is None:
        age_text = "unknown"
    else:
        grace = "grace ON" if verdict.young else "grace off"
        age_text = f"{fmt_age(verdict.repo_age_hours)} ({grace})"
    rows = [
        ("now", fmt_time(obs.now)),
        ("alert window", f"{verdict.window_hours:g}h"),
        ("repository age", age_text),
        ("history", "complete" if obs.history_complete else "SHALLOW"),
        (
            f"last commit {DATA_PATH}",
            # In a shallow clone this query answers with the graft boundary
            # whatever it touched, so printing it as the data-commit age
            # would put a false number on screen under a true-sounding
            # label -- the defect class this project exists to prevent.
            "NOT MEASURABLE (shallow clone)"
            if not obs.history_complete
            else (
                f"{fmt_time(obs.last_data_commit)} "
                f"({fmt_age(hours_since(obs.last_data_commit, obs.now))} ago)"
            ),
        ),
        ("last commit (any path)", fmt_time(obs.last_any_commit)),
        (
            "manifest timestamp",
            f"{fmt_time(obs.manifest_timestamp)} "
            f"({fmt_age(hours_since(obs.manifest_timestamp, obs.now))} ago)",
        ),
        ("manifest pipeline_status", obs.manifest_status or "unknown"),
        ("manifest degraded", ", ".join(obs.manifest_degraded) or "none"),
        ("latest completed scrape", run_text),
        (
            "verdict",
            (
                f"{'HEALTHY' if verdict.ok else 'ALARM'} "
                f"({len(verdict.errors)} error(s), "
                f"{len(verdict.warnings)} warning(s))"
            ),
        ),
    ]
    width = max(len(label) for label, _ in rows)
    lines = ["=== Helios pipeline heartbeat ==="]
    lines += [f"{label.ljust(width)} : {value}" for label, value in rows]
    return lines


def summary_markdown(obs: Observation, verdict: Verdict) -> str:
    lines = ["### Pipeline heartbeat", "", "| | |", "|---|---|"]
    for line in report_lines(obs, verdict)[1:]:
        label, _, value = line.partition(" : ")
        lines.append(f"| {label.strip()} | {value.strip()} |")
    lines.append("")
    for finding in verdict.findings:
        lines.append(f"- **{finding.level.upper()} {finding.code}** "
                     f"{finding.message}")
    lines.append("")
    return "\n".join(lines)


def emit(obs: Observation, verdict: Verdict, env: dict, stream=None) -> None:
    stream = stream or sys.stdout
    for line in report_lines(obs, verdict):
        print(line, file=stream)
    print("", file=stream)

    # ::error:: is the alert channel. It annotates the job AND fails it, and
    # GitHub mails the repo owner about failed scheduled workflows with no
    # secrets configured anywhere. See heartbeat.yml for how to add Slack or
    # email on top -- on top, never instead.
    for finding in verdict.findings:
        if finding.level == ERROR:
            print(f"::error::HEARTBEAT {finding.code}: {finding.message}",
                  file=stream)
        elif finding.level == WARNING:
            print(f"::warning::HEARTBEAT {finding.code}: {finding.message}",
                  file=stream)
        else:
            print(f"NOTICE {finding.code}: {finding.message}", file=stream)

    if verdict.ok:
        # Two different green states, and they must not share a sentence.
        # "The pipeline is publishing" is FALSE for a repository that has
        # never published; printing it under the young-repo grace would be
        # exactly the defect class this project exists to prevent -- a true
        # exit code carrying a false claim.
        age_hours = hours_since(obs.last_data_commit, obs.now)
        if obs.last_data_commit is None:
            print(
                "\nHeartbeat OK (nothing judged): no price data has been "
                "published yet, and this repository is too young for that to "
                "mean anything. Not a statement that the pipeline works.",
                file=stream,
            )
        elif age_hours is not None and age_hours < 0:
            # A negative age is not an age. Printing "last price data
            # -48.0h ago" next to the word publishing asserted health from a
            # measurement that had already failed.
            print(
                f"\nHeartbeat OK (age not measurable): the last price data "
                f"commit is dated {fmt_age(-age_hours)} in the FUTURE, within "
                f"the {SKEW_TOLERANCE_HOURS * 60:.0f}-minute clock tolerance. "
                "Treated as fresh; no age is claimed.",
                file=stream,
            )
        else:
            print(
                f"\nHeartbeat OK: the pipeline is publishing (last price "
                f"data {fmt_age(age_hours)} ago, "
                f"window {verdict.window_hours:g}h).",
                file=stream,
            )

    summary_path = env.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(summary_markdown(obs, verdict))
        except OSError as exc:
            print(f"::warning::could not write step summary: {exc}", file=stream)


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Has the Helios pipeline published recently?"
    )
    parser.add_argument("--repo-root", default=".", help="repository to inspect")
    parser.add_argument("--manifest", default=None, help="override manifest path")
    parser.add_argument(
        "--now", default=None, help="ISO instant to evaluate against (testing)"
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=WINDOW_HOURS,
        help=f"alert window (default {WINDOW_HOURS})",
    )
    parser.add_argument(
        "--no-api", action="store_true", help="skip the GitHub API query"
    )
    parser.add_argument(
        "--runs-json", default=None, help="read a saved workflow-runs payload"
    )
    parser.add_argument(
        "--force-alert",
        action="store_true",
        help="add a forced error finding to prove the alert path works",
    )
    return parser


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ if env is None else env)

    if args.now:
        now = parse_iso(args.now)
        if now is None:
            print(f"::error::--now is not an ISO instant: {args.now}")
            return 2
    else:
        now = datetime.now(timezone.utc)

    # `nan <= 0` is False and `inf > 0` is True, so a bare positivity test
    # accepts both -- and every comparison against a nan window is False,
    # which disables the entire check while still exiting 0. A monitor that
    # can be silenced by a typo in its own argument is not a monitor.
    if not math.isfinite(args.window_hours) or args.window_hours <= 0:
        print(
            f"::error::--window-hours must be a positive finite number, not "
            f"{args.window_hours!r}"
        )
        return 2

    obs = observe(args, env, now)
    verdict = evaluate(obs, args.window_hours, force_alert=args.force_alert)
    emit(obs, verdict, env)
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
