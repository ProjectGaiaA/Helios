"""Tests for the dead-man's switch (heartbeat.py + heartbeat.yml).

Two halves, and the split is the point.

1. THE DECISION is a pure function -- `evaluate(Observation) -> Verdict` --
   so every failure mode is reachable offline in microseconds, including the
   ones that take a dead pipeline days to produce in reality. No git, no
   network, no clock: `now` and every timestamp are injected.

2. THE WORKFLOW is not executable here, so it is TRACED instead: the yml is
   read as text and the couplings that would silently break the monitor are
   asserted. Those couplings are real, not decorative --
     * the crons must stay offset from the scrape crons (a heartbeat that
       reads the repo mid-commit measures a race, not a pipeline),
     * the checkout must be unshallow (the young-repo grace needs the true
       first commit),
     * the job must not install dependencies AND heartbeat.py must not need
       any (a broken requirements.txt is one of the failures this survives),
     * nothing may be continue-on-error (the failed job IS the alert),
     * the window stated in the yml prose must equal the constant that
       decides. Gaia's heartbeat shipped a calibration paragraph measured on
       a different metric than the check used: the false sentence was the
       defect, not the code. Prose that can drift from behaviour is pinned.
"""

import ast
import io
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import heartbeat
from heartbeat import (
    Observation,
    describe_run,
    evaluate,
    hours_since,
    latest_completed_run,
    parse_iso,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "heartbeat.yml"
SCRAPE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "scrape.yml"
SCRIPT = REPO_ROOT / "heartbeat.py"

NOW = datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)
WINDOW = heartbeat.WINDOW_HOURS


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def obs(**kwargs) -> Observation:
    """A healthy, mature repository unless a keyword says otherwise.

    Defaults matter: every test below states only the ONE thing it is about,
    so a test that fails names its own cause.
    """
    base = {
        "now": NOW,
        "last_data_commit": ago(2),
        "last_any_commit": ago(2),
        "first_commit": ago(24 * 90),
        "history_complete": True,
        "manifest_timestamp": ago(2),
        "manifest_status": "healthy",
        "manifest_degraded": (),
        "manifest_error": None,
        "scrape_runs": [
            {
                "run_number": 41,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-01T11:00:03Z",
            }
        ],
        "scrape_runs_error": None,
        "scrape_workflow_missing": False,
    }
    base.update(kwargs)
    return Observation(**base)


def run(**kwargs) -> dict:
    base = {
        "run_number": 1,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-09-01T11:00:00Z",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# The healthy baseline. If this ever fails, every other test is meaningless.
# ---------------------------------------------------------------------------


def test_a_publishing_pipeline_is_silent():
    verdict = evaluate(obs())
    assert verdict.ok
    assert verdict.exit_code == 0
    assert verdict.errors == []


# ---------------------------------------------------------------------------
# Window math. The boundary is >=, so the window is inclusive of its own
# value: "30h" means an age of exactly 30.0h alarms.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age,expect_stale",
    [
        (0.0, False),
        (13.5, False),  # healthy spacing, 21:30 -> 11:00
        (24.0, False),  # one entirely missed cycle: tolerated in silence
        (WINDOW - 0.1, False),
        (WINDOW, True),  # the boundary alarms
        (WINDOW + 0.1, True),
        (72.0, True),
    ],
)
def test_data_staleness_is_decided_at_the_window(age, expect_stale):
    verdict = evaluate(obs(last_data_commit=ago(age), manifest_timestamp=ago(age)))
    assert ("DATA_STALE" in verdict.error_codes()) is expect_stale


def test_the_window_is_a_parameter_not_a_hardcode():
    """A 40h-old commit is stale at the default window and fine at 48h."""
    observation = obs(last_data_commit=ago(40), manifest_timestamp=ago(40))
    assert "DATA_STALE" in evaluate(observation).error_codes()
    assert "DATA_STALE" not in evaluate(observation, window_hours=48).error_codes()


def test_stale_data_names_the_age_and_the_window():
    """The annotation is the entire alert. It has to carry the numbers."""
    verdict = evaluate(obs(last_data_commit=ago(41.5), manifest_timestamp=ago(41.5)))
    (finding,) = [f for f in verdict.errors if f.code == "DATA_STALE"]
    assert "41.5h" in finding.message
    assert f"{WINDOW:g}h" in finding.message


def test_a_documentation_push_does_not_reset_the_clock():
    """The failure this exists to prevent: the repo looks alive (a commit an
    hour ago) while the scraper has been dead for two days."""
    verdict = evaluate(
        obs(
            last_data_commit=ago(50),
            last_any_commit=ago(1),
            manifest_timestamp=ago(50),
        )
    )
    assert "DATA_STALE" in verdict.error_codes()


# --- F2 regression: forward clock skew ------------------------------------
# The defect: CLOCK_SKEW was an `elif` in FRONT of the staleness test, so a
# future-dated commit removed the staleness question from the run entirely,
# and the tool printed "the pipeline is publishing (last price data -48.0h
# ago)". A +48h skew bought a dead pipeline 78h of asserted health.


def test_a_future_dated_commit_beyond_tolerance_is_an_alarm():
    verdict = evaluate(obs(last_data_commit=NOW + timedelta(hours=48)))
    assert not verdict.ok, "forward skew must never read as health"
    assert "CLOCK_SKEW" in verdict.error_codes()
    (finding,) = [f for f in verdict.errors if f.code == "CLOCK_SKEW"]
    assert "UNVERIFIABLE" in finding.message
    assert "48.0h" in finding.message


def test_small_forward_skew_is_tolerated_as_clock_jitter():
    verdict = evaluate(obs(last_data_commit=NOW + timedelta(minutes=2)))
    assert verdict.ok
    assert "CLOCK_SKEW" in {f.code for f in verdict.warnings}


def test_skew_does_not_suppress_the_staleness_evaluation():
    """Skew is an ADDITIONAL condition, not an alternative branch: a
    future-dated manifest must not remove the data-staleness question."""
    verdict = evaluate(
        obs(last_data_commit=ago(40), manifest_timestamp=NOW + timedelta(hours=48))
    )
    codes = verdict.error_codes()
    assert "DATA_STALE" in codes, "the staleness verdict was suppressed by skew"
    assert "CLOCK_SKEW" in codes


def test_a_future_dated_manifest_cannot_hide_a_stale_manifest():
    """A `manifest_age` of -48 reads as 'well inside the window' to any naive
    comparison, which would suppress MANIFEST_STALE forever."""
    verdict = evaluate(
        obs(last_data_commit=ago(1), manifest_timestamp=NOW + timedelta(hours=48))
    )
    assert "CLOCK_SKEW" in verdict.error_codes()
    assert not verdict.ok


def test_the_healthy_sentence_never_prints_a_negative_age():
    """Beyond tolerance there must be no healthy sentence at all; within
    tolerance there must be no negative number inside it."""
    big = obs(last_data_commit=NOW + timedelta(hours=48))
    stream = io.StringIO()
    heartbeat.emit(big, evaluate(big), {}, stream=stream)
    assert "Heartbeat OK" not in stream.getvalue()

    small = obs(last_data_commit=NOW + timedelta(minutes=2))
    verdict = evaluate(small)
    assert verdict.ok
    stream = io.StringIO()
    heartbeat.emit(small, verdict, {}, stream=stream)
    closing = stream.getvalue().rsplit("Heartbeat OK", 1)[1]
    assert "the pipeline is publishing" not in closing
    assert "age not measurable" in closing
    assert not re.search(r"-\d", closing), f"negative age printed: {closing}"


# ---------------------------------------------------------------------------
# The young repository (requirement 4): no false alarm on the first days of
# life, and no blanket amnesty either.
# ---------------------------------------------------------------------------


def test_a_young_repo_with_nothing_published_yet_does_not_alarm():
    verdict = evaluate(
        obs(
            first_commit=ago(6),
            last_data_commit=None,
            last_any_commit=ago(6),
            manifest_timestamp=None,
            manifest_status=None,
            manifest_error="file not found",
            scrape_runs=[],
        )
    )
    assert verdict.ok, verdict.errors
    assert verdict.young is True
    assert "YOUNG_NO_DATA_YET" in verdict.codes()
    assert "YOUNG_NO_MANIFEST" in verdict.codes()


def test_the_same_repo_one_hour_past_the_window_does_alarm():
    """Grace is bounded by the window, not open-ended. Same observation as
    above, aged past it."""
    verdict = evaluate(
        obs(
            first_commit=ago(WINDOW + 1),
            last_data_commit=None,
            last_any_commit=ago(WINDOW + 1),
            manifest_timestamp=None,
            manifest_status=None,
            manifest_error="file not found",
            scrape_runs=[],
        )
    )
    assert verdict.young is False
    assert verdict.error_codes() == {
        "NO_DATA_COMMIT",
        "MANIFEST_UNREADABLE",
        # past grace, "the scrape has never completed a run" is no longer a
        # notice -- see the F3 block below
        "NO_COMPLETED_SCRAPE_RUN",
    }


def test_youth_never_excuses_a_failed_scrape_run():
    """A scrape that failed on day one failed. No window makes that fine, and
    a young repo is exactly when a broken workflow most needs reporting."""
    verdict = evaluate(
        obs(
            first_commit=ago(3),
            last_data_commit=None,
            manifest_error="file not found",
            manifest_timestamp=None,
            manifest_status=None,
            scrape_runs=[run(conclusion="failure")],
        )
    )
    assert verdict.young is True
    assert verdict.error_codes() == {"SCRAPE_RUN_FAILED"}


# --- F1 regression: the shallow clone -------------------------------------
# Shallowness does not merely hide the first commit, it CORRUPTS the
# freshness metric in the direction of looking healthy: at the graft boundary
# git reports that commit as touching every path, so
# `git log -1 -- data/prices/` degrades into "age of the last commit of any
# kind". Measured on a real --depth 1 clone of this repo: 9.1h of false
# freshness. So it is red, and the git-derived verdict is suppressed rather
# than reported from a broken measurement.


def test_a_shallow_clone_cannot_claim_youth():
    verdict = evaluate(obs(history_complete=False, first_commit=ago(4)))
    assert verdict.young is False
    assert "HISTORY_INCOMPLETE" in verdict.error_codes()


def test_a_shallow_clone_suppresses_the_freshness_verdict_entirely():
    """The dangerous case: a shallow clone whose graft commit is recent. The
    old code read that as 'data 1h old' and exited 0. Neither a fresh nor a
    stale verdict may be issued from a measurement known to be wrong."""
    verdict = evaluate(
        obs(history_complete=False, last_data_commit=ago(1), last_any_commit=ago(1))
    )
    assert not verdict.ok
    assert "HISTORY_INCOMPLETE" in verdict.error_codes()
    assert "DATA_STALE" not in verdict.codes()
    assert "NO_DATA_COMMIT" not in verdict.codes()
    assert "MANIFEST_DISAGREES" not in verdict.codes()
    (finding,) = [f for f in verdict.errors if f.code == "HISTORY_INCOMPLETE"]
    assert "NOT evaluated" in finding.message
    assert "fetch-depth: 0" in finding.message


def test_a_shallow_clone_does_not_print_an_age_it_cannot_measure():
    """The verdict was suppressed but the report still printed '(0.8h ago)'
    under the data-commit label -- a false number beside a true-sounding
    label, which is the same defect one layer down."""
    observation = obs(history_complete=False, last_data_commit=ago(0.8))
    row = [
        line
        for line in heartbeat.report_lines(observation, evaluate(observation))
        if line.startswith(f"last commit {heartbeat.DATA_PATH}")
    ]
    assert row and "NOT MEASURABLE" in row[0]
    assert "0.8h" not in row[0]


def test_a_shallow_clone_still_evaluates_what_does_not_depend_on_git():
    """The manifest is a file, not history: it stays readable and its own
    staleness is still judged."""
    verdict = evaluate(
        obs(history_complete=False, last_data_commit=ago(1),
            manifest_timestamp=ago(40))
    )
    assert "MANIFEST_STALE" in verdict.error_codes()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_shallowness_is_actually_detected_on_a_real_shallow_clone(tmp_path):
    """The graft-detection path against real git, because the whole
    suppression above hangs on `rev-parse --is-shallow-repository`."""
    origin = tmp_path / "origin"
    origin.mkdir()
    base = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args, cwd, when=None):
        env = {**os.environ, **base}
        if when:  # distinct dates, or both commits land in the same second
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
        return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                              capture_output=True, env=env)

    prices_at = "2026-08-01T00:00:00Z"
    docs_at = "2026-08-02T00:00:00Z"   # 24h later, and touches NO price data
    git("init", "-q", ".", cwd=origin)
    (origin / "data" / "prices").mkdir(parents=True)
    (origin / "data" / "prices" / "p.jsonl").write_text("{}\n", encoding="utf-8")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "prices", cwd=origin, when=prices_at)
    (origin / "README.md").write_text("docs only\n", encoding="utf-8")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "docs", cwd=origin, when=docs_at)

    shallow = tmp_path / "shallow"
    try:
        git("clone", "-q", "--depth", "1", origin.as_uri(), str(shallow),
            cwd=tmp_path)
    except subprocess.CalledProcessError as exc:  # pragma: no cover
        pytest.skip(f"local clone unavailable: {exc}")

    assert heartbeat.git_history_complete(origin) is True
    assert heartbeat.git_history_complete(shallow) is False, (
        "shallowness went undetected -- the freshness suppression is dead code"
    )
    # In the full clone the pathspec works: the data commit is the older one.
    assert heartbeat.git_last_commit(origin, heartbeat.DATA_PATH) == parse_iso(
        prices_at
    )
    # In the shallow clone the graft boundary answers the same query with the
    # DOCS commit -- 24h of freshness this repository never had. That is the
    # corruption HISTORY_INCOMPLETE exists to refuse to report.
    assert heartbeat.git_last_commit(shallow, heartbeat.DATA_PATH) == parse_iso(
        docs_at
    ), "premise changed: the graft no longer defeats the pathspec"

    # And end to end: the same moment in time, judged from both clones.
    now = parse_iso("2026-08-02T06:00:00Z")  # 30h after the real data commit
    full = evaluate(
        Observation(
            now=now,
            last_data_commit=heartbeat.git_last_commit(origin, heartbeat.DATA_PATH),
            first_commit=heartbeat.git_first_commit(origin),
            history_complete=True,
            manifest_timestamp=now,
            manifest_status="healthy",
        )
    )
    assert "DATA_STALE" in full.error_codes(), "the truthful clone must alarm"
    shallow_verdict = evaluate(
        Observation(
            now=now,
            last_data_commit=heartbeat.git_last_commit(shallow, heartbeat.DATA_PATH),
            first_commit=heartbeat.git_first_commit(shallow),
            history_complete=heartbeat.git_history_complete(shallow),
            manifest_timestamp=now,
            manifest_status="healthy",
        )
    )
    assert shallow_verdict.exit_code == 1, (
        "the shallow clone reported a healthy pipeline from a corrupt metric"
    )
    assert "HISTORY_INCOMPLETE" in shallow_verdict.error_codes()
    assert "DATA_STALE" not in shallow_verdict.codes()


def test_unknown_repository_age_disables_grace_with_a_warning():
    verdict = evaluate(obs(first_commit=None, last_data_commit=None))
    assert verdict.young is False
    assert "REPO_AGE_UNKNOWN" in verdict.codes()
    assert "NO_DATA_COMMIT" in verdict.error_codes()


# ---------------------------------------------------------------------------
# git vs the manifest. The manifest is a claim, and it can lie both ways.
# ---------------------------------------------------------------------------


def test_half_run_fresh_manifest_but_no_new_price_data():
    """The reachable defect: scrape.yml git-adds the manifest alongside
    data/prices/, so a run that collects zero rows still commits, still
    writes a 'healthy' manifest, and leaves the price store untouched.
    Checking the manifest alone -- or asking only 'was there a commit?' --
    reads that as success."""
    verdict = evaluate(
        obs(
            last_data_commit=ago(36),
            last_any_commit=ago(1),
            manifest_timestamp=ago(1),
            manifest_status="healthy",
        )
    )
    codes = verdict.error_codes()
    assert "MANIFEST_DISAGREES" in codes
    assert "DATA_STALE" in codes
    (finding,) = [f for f in verdict.errors if f.code == "MANIFEST_DISAGREES"]
    assert "healthy" in finding.message
    assert "36.0h" in finding.message


def test_half_run_is_caught_even_with_no_price_data_at_all():
    verdict = evaluate(obs(last_data_commit=None, manifest_timestamp=ago(1)))
    assert "MANIFEST_DISAGREES" in verdict.error_codes()


def test_the_reverse_disagreement_stale_manifest_fresh_prices():
    """Prices moved without the runner: a hand commit or a backfill. The
    pipeline of record still has not run inside the window."""
    verdict = evaluate(obs(last_data_commit=ago(1), manifest_timestamp=ago(40)))
    assert "MANIFEST_STALE" in verdict.error_codes()
    assert "DATA_STALE" not in verdict.error_codes()
    assert "MANIFEST_DISAGREES" not in verdict.error_codes()


def test_a_young_repo_is_not_accused_of_a_half_run():
    verdict = evaluate(
        obs(first_commit=ago(5), last_data_commit=None, manifest_timestamp=ago(1))
    )
    assert "MANIFEST_DISAGREES" not in verdict.error_codes()
    assert verdict.ok


def test_a_corrupt_manifest_on_a_mature_repo_is_an_error():
    verdict = evaluate(
        obs(manifest_error="JSONDecodeError: line 1", manifest_timestamp=None,
            manifest_status=None)
    )
    assert "MANIFEST_UNREADABLE" in verdict.error_codes()


def test_a_manifest_without_a_timestamp_is_an_error():
    verdict = evaluate(obs(manifest_timestamp=None))
    assert "MANIFEST_NO_TIMESTAMP" in verdict.error_codes()


def test_degraded_pipeline_status_warns_but_does_not_alarm():
    """Deliberate (gaia R14). The scrape run that wrote a degraded manifest
    already failed its own job, and that arrives here as SCRAPE_RUN_FAILED.
    A second red light nothing scheduled can clear is noise, and noise is how
    a monitoring channel dies."""
    verdict = evaluate(
        obs(manifest_status="degraded", manifest_degraded=("rich-solar",))
    )
    assert verdict.ok
    codes = {f.code for f in verdict.warnings}
    assert "MANIFEST_DEGRADED" in codes
    (finding,) = [f for f in verdict.warnings if f.code == "MANIFEST_DEGRADED"]
    assert "rich-solar" in finding.message


def test_pipeline_status_is_read_and_reported_even_when_healthy():
    """It is an input to the half-run finding, so it must never be ignored."""
    verdict = evaluate(
        obs(last_data_commit=ago(40), manifest_timestamp=ago(1),
            manifest_status="degraded")
    )
    (finding,) = [f for f in verdict.errors if f.code == "MANIFEST_DISAGREES"]
    assert "degraded" in finding.message


# ---------------------------------------------------------------------------
# Requirement 3: the crash-loop. Scrape fails twice a day, publishes nothing,
# and the failure mail lands in a folder nobody reads.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "timed_out", "startup_failure", "action_required", "stale"],
)
def test_a_failed_scrape_run_alarms_even_while_data_is_still_fresh(conclusion):
    verdict = evaluate(obs(scrape_runs=[run(conclusion=conclusion)]))
    assert "SCRAPE_RUN_FAILED" in verdict.error_codes()
    assert "DATA_STALE" not in verdict.error_codes()


def test_a_cancelled_run_warns_rather_than_alarms():
    verdict = evaluate(obs(scrape_runs=[run(conclusion="cancelled")]))
    assert verdict.ok
    assert "SCRAPE_RUN_SOFT" in verdict.codes()


def test_an_unrecognised_conclusion_is_surfaced_not_assumed_healthy():
    verdict = evaluate(obs(scrape_runs=[run(conclusion="banana")]))
    assert "SCRAPE_RUN_UNRECOGNISED" in verdict.codes()


def test_a_newer_success_clears_an_older_failure():
    verdict = evaluate(
        obs(
            scrape_runs=[
                run(run_number=9, conclusion="success",
                    created_at="2026-09-01T11:00:00Z"),
                run(run_number=8, conclusion="failure",
                    created_at="2026-08-31T21:30:00Z"),
            ]
        )
    )
    assert verdict.ok


def test_api_order_is_not_trusted():
    """Same two runs, listed oldest-first. The newest still wins."""
    verdict = evaluate(
        obs(
            scrape_runs=[
                run(run_number=8, conclusion="success",
                    created_at="2026-08-31T21:30:00Z"),
                run(run_number=9, conclusion="failure",
                    created_at="2026-09-01T11:00:00Z"),
            ]
        )
    )
    assert "SCRAPE_RUN_FAILED" in verdict.error_codes()


def test_an_in_flight_run_cannot_mask_the_previous_failure():
    """A scrape running right now has no conclusion. Reading it as 'not a
    failure' would hide the failure underneath it."""
    verdict = evaluate(
        obs(
            scrape_runs=[
                {"run_number": 10, "status": "in_progress", "conclusion": None,
                 "created_at": "2026-09-01T12:59:00Z"},
                run(run_number=9, conclusion="failure",
                    created_at="2026-09-01T11:00:00Z"),
            ]
        )
    )
    assert "SCRAPE_RUN_FAILED" in verdict.error_codes()


def test_latest_completed_run_selection():
    assert latest_completed_run(None) is None
    assert latest_completed_run([]) is None
    assert latest_completed_run([{"status": "queued", "conclusion": None}]) is None
    # a "completed" run with a null conclusion is not a usable answer
    assert latest_completed_run([{"status": "completed", "conclusion": None}]) is None
    # no timestamp anywhere: must not raise
    assert latest_completed_run([run(created_at=None)])["run_number"] == 1
    assert latest_completed_run(["not a dict", run(run_number=7)])["run_number"] == 7


def test_an_unreachable_api_warns_rather_than_alarms():
    """The git checks answer the primary question without the API. A
    transient api.github.com error that pages the owner trains the owner to
    ignore the page."""
    verdict = evaluate(
        obs(scrape_runs=None, scrape_runs_error="HTTPError: 503")
    )
    assert verdict.ok
    assert "SCRAPE_RUNS_UNKNOWN" in verdict.codes()
    (finding,) = [f for f in verdict.warnings if f.code == "SCRAPE_RUNS_UNKNOWN"]
    assert "503" in finding.message


def test_a_missing_scrape_workflow_alarms_rather_than_warns():
    """A 404 on the workflow is structural, not transient: either the scrape
    was renamed or deleted (nothing is scraping) or this check has been
    measuring a file that does not exist. Youth is no excuse for either."""
    verdict = evaluate(
        obs(
            scrape_runs=None,
            scrape_runs_error="workflow scrape.yml not found (HTTP 404)",
            scrape_workflow_missing=True,
        )
    )
    assert "SCRAPE_WORKFLOW_MISSING" in verdict.error_codes()
    assert "SCRAPE_RUNS_UNKNOWN" not in verdict.codes(), "reported twice"
    young = evaluate(
        obs(
            first_commit=ago(2), last_data_commit=None, manifest_timestamp=None,
            manifest_status=None, manifest_error="file not found",
            scrape_runs=None, scrape_workflow_missing=True,
        )
    )
    assert "SCRAPE_WORKFLOW_MISSING" in young.error_codes()


def test_a_404_is_distinguished_from_a_transient_api_failure(monkeypatch):
    """The two arrive on the same code path and must not collapse into one
    finding: one is a warning by design, the other is red."""
    import urllib.error

    def not_found(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.github.com/x", 404, "Not Found", {}, None
        )

    def unavailable(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.github.com/x", 503, "Server Error", {}, None
        )

    args = heartbeat.build_parser().parse_args([])
    env = {"GITHUB_REPOSITORY": "x/y", "GITHUB_TOKEN": "t"}

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", not_found)
    runs, error, missing = heartbeat.collect_scrape_runs(args, env)
    assert runs is None and missing is True
    assert "404" in error

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", unavailable)
    runs, error, missing = heartbeat.collect_scrape_runs(args, env)
    assert runs is None and missing is False
    assert "503" in error


# --- F3 regression: HTTP 200 with an empty run list ------------------------
# Found live on this repository: the real API returns total_count=0 for
# scrape.yml. CI has never run the scrape; every data commit was made from a
# laptop. The old code called that a NOTICE and printed "the pipeline is
# publishing" over the top of it. An answered query reporting zero runs is
# GitHub stating a fact, not an outage.

LIVE_EMPTY_PAYLOAD = {"total_count": 0, "workflow_runs": []}


def test_zero_completed_runs_is_an_alarm_once_past_grace():
    verdict = evaluate(obs(scrape_runs=LIVE_EMPTY_PAYLOAD["workflow_runs"]))
    assert not verdict.ok, "an answered 'zero runs' must not read as health"
    assert "NO_COMPLETED_SCRAPE_RUN" in verdict.error_codes()
    (finding,) = [f for f in verdict.errors if f.code == "NO_COMPLETED_SCRAPE_RUN"]
    assert "never run" in finding.message
    assert "not an outage" in finding.message.lower()


def test_zero_completed_runs_is_still_graced_while_the_repo_is_young():
    """A repository created an hour ago legitimately has no runs yet."""
    young = evaluate(
        obs(first_commit=ago(4), last_data_commit=None, scrape_runs=[],
            manifest_timestamp=None, manifest_status=None,
            manifest_error="file not found")
    )
    assert young.ok
    assert "NO_COMPLETED_SCRAPE_RUN" in {f.code for f in young.notices}


def test_runs_present_but_all_in_flight_is_distinguished_from_never_ran():
    verdict = evaluate(
        obs(scrape_runs=[{"status": "in_progress", "conclusion": None,
                          "created_at": "2026-09-01T12:59:00Z"}])
    )
    assert "NO_COMPLETED_SCRAPE_RUN" in verdict.error_codes()
    (finding,) = [f for f in verdict.errors if f.code == "NO_COMPLETED_SCRAPE_RUN"]
    assert "still in progress" in finding.message


def test_the_live_empty_payload_survives_the_whole_collection_path(tmp_path):
    """End to end from the exact bytes the API returned for this repo."""
    payload = tmp_path / "runs.json"
    payload.write_text(json.dumps(LIVE_EMPTY_PAYLOAD), encoding="utf-8")
    args = heartbeat.build_parser().parse_args(["--runs-json", str(payload)])
    runs, error, missing = heartbeat.collect_scrape_runs(args, {})
    assert runs == [] and error is None and missing is False
    assert latest_completed_run(runs) is None
    verdict = evaluate(obs(scrape_runs=runs))
    assert verdict.exit_code == 1


# ---------------------------------------------------------------------------
# The drill, and pure-function hygiene.
# ---------------------------------------------------------------------------


def test_force_alert_fails_a_healthy_pipeline_on_purpose():
    verdict = evaluate(obs(), force_alert=True)
    assert not verdict.ok
    assert verdict.error_codes() == {"FORCED_ALERT"}
    assert verdict.exit_code == 1


def test_evaluate_survives_an_observation_that_knows_nothing():
    """Adversarial: every input None. It must produce an alarm, not a
    traceback -- though a traceback would also fail the job, which is the
    safe direction."""
    verdict = evaluate(
        Observation(now=NOW, history_complete=False, manifest_error="x")
    )
    assert not verdict.ok
    assert "HISTORY_INCOMPLETE" in verdict.error_codes()


# --- F4 regression: a window that disables every comparison ----------------


@pytest.mark.parametrize(
    "bad",
    # `=` form for the negatives: argparse reads a bare "-inf" as an option.
    ["nan", "inf", "0", "=-inf", "=-1"],
)
def test_a_non_finite_or_non_positive_window_is_rejected(bad, capsys):
    """`nan <= 0` is False and `inf > 0` is True, so the old positivity
    guard accepted both. Every comparison against a nan window is False, so
    the check silently passed at any staleness -- exit 0 on a repository 72
    years dead."""
    argv = ["--no-api"]
    argv += [f"--window-hours{bad}"] if bad.startswith("=") else [
        "--window-hours", bad
    ]
    assert heartbeat.main(argv, env={}) == 2
    assert "must be a positive finite number" in capsys.readouterr().out


def test_a_nan_window_cannot_reach_the_evaluation():
    """Belt and braces: if it ever did, this is what it would do."""
    nan = float("nan")
    verdict = evaluate(obs(last_data_commit=ago(24 * 365 * 72)), window_hours=nan)
    assert verdict.ok, (
        "premise check -- a nan window really does disable every comparison, "
        "which is why main() must reject it before evaluate() is reached"
    )


def test_parse_iso_handles_every_shape_this_program_meets():
    assert parse_iso("2026-09-01T11:00:03Z") == datetime(
        2026, 9, 1, 11, 0, 3, tzinfo=timezone.utc
    )
    # git %cI, non-UTC offset: normalised, not truncated
    assert parse_iso("2026-08-13T23:57:21-04:00") == datetime(
        2026, 8, 14, 3, 57, 21, tzinfo=timezone.utc
    )
    assert parse_iso("2026-08-14T03:35:30.950806+00:00").microsecond == 950806
    # naive is read as UTC, never as runner-local
    assert parse_iso("2026-09-01T11:00:00").tzinfo is timezone.utc
    for junk in [None, "", "   ", "not a date", 17, {"a": 1}]:
        assert parse_iso(junk) is None


def test_hours_since_and_describe_run_tolerate_missing_input():
    assert hours_since(None, NOW) is None
    assert hours_since(NOW, None) is None
    assert hours_since(ago(3), NOW) == pytest.approx(3.0)
    assert describe_run(None) == "none"
    assert "#1 success" in describe_run(run())


# ---------------------------------------------------------------------------
# The alert channel itself: ::error:: annotations and the step summary.
# ---------------------------------------------------------------------------


def test_errors_are_emitted_as_github_error_annotations(tmp_path):
    summary = tmp_path / "summary.md"
    observation = obs(last_data_commit=ago(40), manifest_timestamp=ago(40))
    verdict = evaluate(observation)
    stream = io.StringIO()
    heartbeat.emit(
        observation, verdict, {"GITHUB_STEP_SUMMARY": str(summary)}, stream=stream
    )
    text = stream.getvalue()
    assert "::error::HEARTBEAT DATA_STALE" in text
    assert "ALARM" in text
    assert "Heartbeat OK" not in text
    written = summary.read_bytes()
    assert b"DATA_STALE" in written
    assert b"\r\n" not in written  # LF-only, like every other writer here


def test_a_healthy_run_emits_no_error_annotation():
    stream = io.StringIO()
    observation = obs()
    heartbeat.emit(observation, evaluate(observation), {}, stream=stream)
    text = stream.getvalue()
    assert "::error::" not in text
    assert "the pipeline is publishing" in text


def test_a_green_newborn_is_not_told_the_pipeline_is_publishing():
    """A true exit code must not carry a false sentence. Under the young-repo
    grace nothing has been published, so the closing line says so."""
    stream = io.StringIO()
    observation = obs(
        first_commit=ago(2), last_data_commit=None, last_any_commit=ago(2),
        manifest_timestamp=None, manifest_status=None,
        manifest_error="file not found", scrape_runs=[],
    )
    verdict = evaluate(observation)
    assert verdict.ok
    heartbeat.emit(observation, verdict, {}, stream=stream)
    text = stream.getvalue()
    assert "the pipeline is publishing" not in text
    assert "nothing judged" in text


def test_warnings_do_not_use_the_error_channel():
    stream = io.StringIO()
    observation = obs(scrape_runs=None, scrape_runs_error="HTTPError: 503")
    heartbeat.emit(observation, evaluate(observation), {}, stream=stream)
    text = stream.getvalue()
    assert "::warning::HEARTBEAT SCRAPE_RUNS_UNKNOWN" in text
    assert "::error::" not in text


def test_the_report_is_ascii_only():
    """Repo convention: console output survives a Windows code page."""
    observation = obs()
    body = "\n".join(heartbeat.report_lines(observation, evaluate(observation)))
    body.encode("ascii")


# ---------------------------------------------------------------------------
# The impure edge, exercised without a network.
# ---------------------------------------------------------------------------


def test_no_token_means_no_http_request_is_attempted(monkeypatch):
    """Running locally must not reach out, and must not read silence as
    health: the answer is None ('could not ask'), which evaluates to a
    warning, never to a success."""
    def explode(*args, **kwargs):
        raise AssertionError("heartbeat.py opened a socket")

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", explode)
    args = heartbeat.build_parser().parse_args([])
    runs, error, missing = heartbeat.collect_scrape_runs(args, {})
    assert runs is None
    assert missing is False, "absent credentials are not a missing workflow"
    assert "GITHUB_REPOSITORY" in error


def test_no_api_flag_short_circuits_before_the_token_is_read(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("heartbeat.py opened a socket")

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", explode)
    args = heartbeat.build_parser().parse_args(["--no-api"])
    runs, error, missing = heartbeat.collect_scrape_runs(
        args, {"GITHUB_REPOSITORY": "x/y", "GITHUB_TOKEN": "t"}
    )
    assert runs is None
    assert missing is False
    assert error == "--no-api requested"


def test_a_saved_api_payload_can_be_replayed_offline(tmp_path):
    payload = tmp_path / "runs.json"
    payload.write_text(
        '{"workflow_runs": [{"status": "completed", "conclusion": "failure",'
        ' "run_number": 3, "created_at": "2026-09-01T11:00:00Z"}]}',
        encoding="utf-8",
    )
    args = heartbeat.build_parser().parse_args(["--runs-json", str(payload)])
    runs, error, missing = heartbeat.collect_scrape_runs(args, {})
    assert error is None
    assert missing is False
    assert latest_completed_run(runs)["run_number"] == 3


def test_read_manifest_on_the_real_repo_manifest():
    timestamp, status, degraded, error = heartbeat.read_manifest(
        REPO_ROOT / heartbeat.MANIFEST_PATH
    )
    assert error is None, error
    assert timestamp is not None, "the committed manifest has no parseable timestamp"
    assert status is not None
    assert isinstance(degraded, tuple)


def test_read_manifest_reports_rather_than_raises(tmp_path):
    _, _, _, missing = heartbeat.read_manifest(tmp_path / "nope.json")
    assert missing == "file not found"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert "JSONDecodeError" in heartbeat.read_manifest(bad)[3]
    notdict = tmp_path / "list.json"
    notdict.write_text("[1, 2]", encoding="utf-8")
    assert heartbeat.read_manifest(notdict)[3] == "manifest is not a JSON object"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_end_to_end_on_an_empty_repository_alarms_offline(tmp_path):
    """The whole program, no network, no history: an empty repo publishes
    nothing, so the answer is 1. Also proves the git helpers degrade to None
    instead of raising when there is nothing to read."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    code = heartbeat.main(
        ["--no-api", "--repo-root", str(tmp_path)], env={}
    )
    assert code == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_the_git_helpers_read_this_repository():
    """The one test that touches real git: the commands must actually match
    this repo's layout. A typo in the pathspec would otherwise report 'no
    data commit' forever, or -- worse -- never report at all.

    This test runs in two very different checkouts: complete local clones
    and CI's fetch-depth-1 shallow checkout (scrape.yml). Asserting
    `git_history_complete(...) is True` here asserted the SHAPE OF THE
    CHECKOUT, not the correctness of the helper, and turned every CI run
    red from the moment this file landed. Instead, verify the helper
    against git's own ground truth -- the shallow marker in the real git
    dir -- so CI exercises the shallow branch no local run ever reaches,
    and local clones keep exercising the complete branch."""
    if heartbeat.git_first_commit(REPO_ROOT) is None:
        pytest.skip("not a git checkout")
    git_dir = subprocess.run(
        # --git-common-dir, not --absolute-git-dir: in a linked worktree the
        # shallow marker lives in the COMMON dir, and reading the per-worktree
        # dir would fail a correct helper -- the same assert-the-topology
        # defect this rewrite removes. Verified in all four topologies
        # (complete/shallow x main/worktree) before commit.
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--path-format=absolute",
         "--git-common-dir"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    actually_shallow = (Path(git_dir) / "shallow").exists()
    assert heartbeat.git_history_complete(REPO_ROOT) is (not actually_shallow)
    if actually_shallow:
        # The pathspec questions are unanswerable on a shallow clone -- at
        # the graft boundary git reports that commit as touching every
        # path (see heartbeat.yml's checkout comment), which is exactly
        # why heartbeat.py suppresses freshness verdicts as
        # HISTORY_INCOMPLETE rather than trusting them. The test declines
        # for the same reason; the pathspec is still proven by every
        # complete-clone run.
        assert heartbeat.git_last_commit(REPO_ROOT) is not None
        return
    data_commit = heartbeat.git_last_commit(REPO_ROOT, heartbeat.DATA_PATH)
    assert data_commit is not None, (
        f"no commit touches {heartbeat.DATA_PATH} -- the pathspec is wrong "
        "or the price store moved"
    )
    assert data_commit <= heartbeat.git_last_commit(REPO_ROOT)
    assert heartbeat.git_first_commit(REPO_ROOT) <= data_commit


# ---------------------------------------------------------------------------
# The workflow, traced. It cannot be executed here, so its couplings are
# asserted as text.
# ---------------------------------------------------------------------------


def yml() -> str:
    """The whole file, comments included -- what a human reads."""
    return WORKFLOW.read_text(encoding="utf-8")


def yml_code() -> str:
    """Only the lines GitHub executes.

    The comment block explains what this workflow deliberately does NOT do
    (no pip install, no SMTP, no continue-on-error), so a naive substring
    search over the whole file finds the very strings the file exists to
    disclaim. Absence has to be asserted against the executable half.
    """
    return "\n".join(
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


def script_error_codes() -> set[str]:
    """Codes heartbeat.py can raise at ERROR level, read out of its AST.

    Extracted rather than listed so that a new red condition cannot be added
    to the script without also being documented in the workflow.
    """
    codes = set()
    for node in ast.walk(ast.parse(SCRIPT.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "add"):
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
            continue
        level = node.args[0]
        names = []
        if isinstance(level, ast.Name):
            names = [level.id]
        elif isinstance(level, ast.IfExp):
            names = [
                part.id
                for part in (level.body, level.orelse)
                if isinstance(part, ast.Name)
            ]
        if "ERROR" in names:
            codes.add(node.args[1].value)
    return codes


def crons(path: Path) -> list[str]:
    """Real `- cron:` entries only -- commented-out examples are skipped."""
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- cron:"):
            found.append(stripped.split(":", 1)[1].strip().strip("'\""))
    return found


def minute_of_day(cron: str) -> int:
    minute, hour = cron.split()[0], cron.split()[1]
    return int(hour) * 60 + int(minute)


def test_the_heartbeat_runs_on_its_own_schedule():
    entries = crons(WORKFLOW)
    assert len(entries) >= 2, "a once-a-day heartbeat cannot see both scrapes"
    assert sorted(entries) == sorted({"0 13 * * *", "30 23 * * *"})


def test_every_heartbeat_cron_is_offset_after_a_scrape_cron():
    """Derived from scrape.yml, so changing the scrape schedule without
    re-offsetting the heartbeat fails here rather than in production. Two
    hours is the requirement: it clears scrape.yml's own 60-minute timeout
    cap with an hour to spare, so the heartbeat never measures a repository
    that a healthy scrape is still committing to."""
    scrape = [minute_of_day(c) for c in crons(SCRAPE_WORKFLOW)]
    assert scrape, "scrape.yml has no cron -- this test's premise is gone"
    for entry in crons(WORKFLOW):
        beat = minute_of_day(entry)
        assert beat not in scrape, f"heartbeat cron {entry} collides with a scrape"
        lag = min((beat - s) % (24 * 60) for s in scrape)
        assert lag >= 120, f"heartbeat cron {entry} is only {lag} min after a scrape"


# Every place a human can read the window, keyed to the one place that
# decides it. Two adjacency patterns ("window ... 30h" and "30h ... window"),
# with digit-free gaps so an unrelated hour figure in the same sentence
# cannot be mistaken for the window.
WINDOW_PATTERNS = (
    # `h\b` matters: without it, "window? Exit 0\n healthy" reads as a 0-hour
    # window, because `\s*h` happily crosses a newline into "healthy".
    r"window[^0-9]{0,12}(\d+(?:\.\d+)?)\s*h\b",
    r"(\d+(?:\.\d+)?)h\b[^0-9.|]{0,40}?window",
)


def stated_windows(text: str) -> list[str]:
    found = []
    for pattern in WINDOW_PATTERNS:
        found += re.findall(pattern, text, re.I)
    return found


def test_the_window_stated_in_the_workflow_is_the_window_that_decides():
    """Gaia's defect: prose describing a threshold the code no longer used.
    Change WINDOW_HOURS alone and this fails."""
    stated = stated_windows(yml())
    assert stated, "the workflow no longer states its window in prose"
    for value in stated:
        assert value == f"{WINDOW:g}", f"workflow states a {value}h window"


@pytest.mark.parametrize(
    "doc", ["CLAUDE.md", "docs/HEARTBEAT_LOG.md"]
)
def test_the_window_stated_in_the_docs_is_the_window_that_decides(doc):
    """The pin used to cover the yml only, so the operator log's four 30h
    cells and CLAUDE.md's '30h window' could drift silently."""
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    stated = stated_windows(text)
    assert stated, f"{doc} no longer states the window"
    for value in stated:
        assert value == f"{WINDOW:g}", f"{doc} states a {value}h window"


def test_every_window_cell_in_the_log_table_matches():
    """The §2 table's 'Window / source' column, parsed structurally rather
    than by prose adjacency -- those cells read '30h, git' with the word
    'window' only in the header."""
    text = (REPO_ROOT / "docs" / "HEARTBEAT_LOG.md").read_text(encoding="utf-8")
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.startswith("| `")
    ]
    assert rows, "the failure-condition table is gone or reshaped"
    hours = []
    for row in rows:
        hours += re.findall(r"(\d+(?:\.\d+)?)h", row[2])
    assert hours, "no window figures found in the table's window column"
    for value in hours:
        assert value == f"{WINDOW:g}", f"the table states a {value}h window"


def test_the_workflow_names_what_it_measures():
    text = yml()
    assert heartbeat.DATA_PATH in text
    assert heartbeat.MANIFEST_PATH in text


def test_every_red_condition_is_documented_in_the_workflow():
    """A new alarm the operator cannot look up is an alarm they cannot act
    on. Add a red condition to heartbeat.py without documenting it here and
    this fails."""
    text = yml()
    codes = script_error_codes()
    assert len(codes) >= 7, f"AST extraction found suspiciously few codes: {codes}"
    for code in sorted(codes):
        assert code in text, f"error code {code} is not documented in the yml"


def test_the_checkout_is_unshallow():
    assert "fetch-depth: 0" in yml(), (
        "the young-repo grace needs the true first commit; a shallow clone "
        "reports the wrong repository age"
    )


def test_the_job_can_query_the_scrape_workflow():
    code = yml_code()
    assert "actions: read" in code, "no permission to read the Scrape run history"
    assert "contents: read" in code
    assert "contents: write" not in code, "the heartbeat must never push"


def test_the_workflow_runs_the_script_and_nothing_else_decides():
    code = yml_code()
    assert "python -X utf8 heartbeat.py" in code
    assert "continue-on-error" not in code, (
        "a continue-on-error step would swallow the alarm; the script's exit "
        "code is the whole alert"
    )
    assert "if:" not in code, "no step may be conditional on anything"


def test_the_job_installs_no_dependencies_and_the_script_needs_none():
    """The coupling that keeps the monitor independent of the pipeline: the
    workflow has no pip install, so heartbeat.py must import only the
    standard library -- and must not import this repo's own modules, since a
    monitor sharing code with the thing it watches inherits its defects."""
    code = yml_code()
    assert "pip install" not in code
    assert "requirements.txt" not in code
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    allowed = {
        "argparse", "dataclasses", "datetime", "json", "math", "os",
        "pathlib", "subprocess", "sys", "urllib",
    }
    assert roots <= allowed, f"heartbeat.py imports beyond the stdlib set: {roots}"


def test_the_workflow_carries_no_secret_based_alert_path():
    """The plant tracker's SMTP step is deliberately not ported: its own
    notes record that the secrets were never set and the path has plausibly
    never delivered a message. A mail step that silently sends nothing is
    worse than none, because it gets counted as coverage."""
    code = yml_code()
    assert "action-send-mail" not in code
    assert "SMTP" not in code.upper()
    assert "secrets." not in code, "the alert path must need no secrets at all"
    # ...while the upgrade path stays written down in the comments.
    text = yml()
    assert "SLACK_WEBHOOK_URL" in text
    assert "healthchecks.io" in text


def test_the_workflow_documents_that_it_shares_fate_with_what_it_watches():
    text = yml()
    assert "60 days" in text, "the disabled-scheduled-workflow trap must be stated"
    assert "force_alert" in text, "there must be a way to prove the alert path"
