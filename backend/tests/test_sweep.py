"""Issue #72 - one-shot sweep entrypoint (sweep.py).

sweep.py replaces the deleted self-perpetuating RQ scheduler: a dedicated Railway
cron runs one sweep per fire, wrapped in a Sentry Cron Monitor check-in. These
tests pin the check-in behavior (in_progress -> ok on success, error + re-raise
on failure) and that it runs exactly one sweep with the live ZEP_SWEEP_* config.
``create_app`` is stubbed so the test exercises only the entrypoint's wrapper,
not the Flask boot.
"""
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
    assert rec.calls[0][2]["schedule"]["type"] == "interval"
    assert rec.calls[0][2]["checkin_margin"] >= 1


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


def test_monitor_config_uses_slug_and_interval(monkeypatch):
    monkeypatch.setenv("ZEP_SWEEP_INTERVAL_MINUTES", "60")
    cfg = sweep._monitor_config()
    assert cfg["schedule"] == {"type": "interval", "value": 60, "unit": "minute"}
    assert cfg["checkin_margin"] == 60
    assert cfg["max_runtime"] == sweep.SWEEP_MAX_RUNTIME_MINUTES
    assert sweep.SWEEP_MONITOR_SLUG == "zep-graph-sweep"   # existing monitor carries over
