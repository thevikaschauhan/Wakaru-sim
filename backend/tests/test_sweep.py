"""Issue #72 - one-shot sweep entrypoint (sweep.py).

sweep.py replaces the deleted self-perpetuating RQ scheduler: a dedicated Railway
cron runs one sweep per fire, wrapped in a Sentry Cron Monitor check-in. These
tests pin the check-in behavior (in_progress -> ok on success, error + re-raise
on failure), that it runs exactly one sweep with the live ZEP_SWEEP_* config,
that the monitor's declared schedule matches the Railway cron VERBATIM (a
mismatch would alert on every healthy run), and that the runtime bound is really
enforced (a hang must not leave the process Active, or Railway skips every later
run). ``create_app`` is stubbed so the tests exercise only the entrypoint's
wrapper, not the Flask boot.
"""
import pathlib
import signal
import time
import tomllib

import pytest

import sweep


class CheckinRecorder:
    """Records (status, check_in_id, monitor_config) for each capture_checkin."""

    def __init__(self):
        self.calls = []

    def __call__(self, *, monitor_slug, check_in_id=None, status=None,
                 duration=None, monitor_config=None):
        self.calls.append((status, check_in_id, monitor_config))
        return check_in_id or "test-check-in-id"


@pytest.fixture
def stub_app(monkeypatch):
    """Skip the Flask/config/Sentry boot — the wrapper is what's under test."""
    monkeypatch.setattr(sweep, "create_app", lambda: None)


def test_one_shot_checkin_in_progress_then_ok_on_success(stub_app, monkeypatch):
    rec = CheckinRecorder()
    monkeypatch.setattr(sweep, "capture_checkin", rec)
    swept = []
    monkeypatch.setattr(
        sweep, "sweep_orphan_graphs",
        lambda **kw: (swept.append(kw), None)[1],
    )

    rc = sweep.main()

    assert rc == 0
    assert len(swept) == 1                                  # exactly one sweep
    assert [s for s, _, _ in rec.calls] == [
        sweep.MonitorStatus.IN_PROGRESS, sweep.MonitorStatus.OK,
    ]
    # OK check-in reuses the id returned by the IN_PROGRESS check-in.
    assert rec.calls[1][1] == "test-check-in-id"
    # Each check-in carries a monitor_config so Sentry can detect a missed run.
    assert rec.calls[0][2]["schedule"]["type"] == "crontab"
    assert rec.calls[0][2]["checkin_margin"] >= 1
    # The watchdog is disarmed on the success path: a pending alarm would other-
    # wise fire during interpreter shutdown (e.g. Sentry's atexit flush).
    assert signal.alarm(0) == 0


def test_one_shot_checkin_error_and_reraise_on_sweep_failure(stub_app, monkeypatch):
    rec = CheckinRecorder()
    monkeypatch.setattr(sweep, "capture_checkin", rec)

    def boom(**kw):
        raise RuntimeError("sweep boom")

    monkeypatch.setattr(sweep, "sweep_orphan_graphs", boom)

    # A sweep-level failure re-raises so the process exits non-zero and Railway
    # marks the cron run failed (the ERROR check-in already fired).
    with pytest.raises(RuntimeError):
        sweep.main()

    assert [s for s, _, _ in rec.calls] == [
        sweep.MonitorStatus.IN_PROGRESS, sweep.MonitorStatus.ERROR,
    ]
    assert rec.calls[1][1] == "test-check-in-id"
    assert signal.alarm(0) == 0            # watchdog disarmed on the error path too


def test_monitor_config_mirrors_the_cron_schedule():
    cfg = sweep._monitor_config()
    assert cfg["schedule"] == {"type": "crontab", "value": sweep.SWEEP_CRON_SCHEDULE}
    assert cfg["checkin_margin"] == sweep.SWEEP_CHECKIN_MARGIN_MINUTES
    # Sentry takes max_runtime in whole minutes; the enforced bound is in seconds.
    assert cfg["max_runtime"] == 5 and sweep.SWEEP_MAX_RUNTIME_SECONDS == 300
    assert cfg["timezone"] == "UTC"                        # Railway crons are UTC
    assert sweep.SWEEP_MONITOR_SLUG == "zep-graph-sweep"   # existing monitor carries over


def test_cron_schedule_matches_railway_service_config():
    """The Sentry monitor's expected schedule and the Railway cron that actually
    fires must be the SAME string. If they drift, Sentry alerts on every healthy
    run (or stays silent through a dead one), so pin them against each other."""
    toml_path = pathlib.Path(__file__).resolve().parents[1] / "railway.sweep.toml"
    deploy = tomllib.loads(toml_path.read_text())["deploy"]

    assert deploy["cronSchedule"] == sweep.SWEEP_CRON_SCHEDULE
    assert deploy["startCommand"] == "python sweep.py"
    # A completed cron run must not be restarted, and a healthcheck on a service
    # that serves no HTTP would restart-loop it (see railway.worker.toml).
    assert deploy["restartPolicyType"] == "NEVER"
    assert "healthcheckPath" not in deploy


def test_runtime_bound_is_enforced_not_just_advertised(stub_app, monkeypatch):
    """A hung sweep must be cut short: Railway does not terminate deployments and
    SKIPS a scheduled run while the previous one is still Active, so an unbounded
    hang would silently end every later sweep."""
    rec = CheckinRecorder()
    monkeypatch.setattr(sweep, "capture_checkin", rec)
    monkeypatch.setattr(sweep, "SWEEP_MAX_RUNTIME_SECONDS", 1)

    def hang(**kw):
        time.sleep(30)

    monkeypatch.setattr(sweep, "sweep_orphan_graphs", hang)

    started = time.monotonic()
    with pytest.raises(sweep.SweepTimeout):
        sweep.main()

    assert time.monotonic() - started < 10          # cut short, not run to completion
    assert [s for s, _, _ in rec.calls] == [
        sweep.MonitorStatus.IN_PROGRESS, sweep.MonitorStatus.ERROR,
    ]
    assert signal.alarm(0) == 0
