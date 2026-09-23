import json
from pathlib import Path
from .test_recovery import cart, Judge
from .recovery import RecoveryWorkflow
from ..buyer_state import direct_insight


def activate(tmp_path, monkeypatch):
    profile = json.loads(Path("ops/typesafe/development.json").read_text())
    profile["workflows"]["recovery_intelligence"]["merchants"] = ["merchant"]
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(profile))
    monkeypatch.setenv("TYPESAFE_PRELAUNCH", "1")
    monkeypatch.setenv("TYPESAFE_DEPLOYMENT_FILE", str(path))
    monkeypatch.setenv("TYPESAFE_ENVIRONMENT", "development")
    monkeypatch.setenv("TYPESAFE_RECOVERY_MODE", "enforce")
    monkeypatch.setenv("TYPESAFE_RECOVERY_MERCHANTS", "merchant")
    return path, profile


def test_rollback_blocks_persisted_plan_and_preserves_history(tmp_path, monkeypatch):
    path, profile = activate(tmp_path, monkeypatch)
    judge = Judge()
    judge.path = str(tmp_path / "journal.sqlite")
    workflow = RecoveryWorkflow(judge)
    c = cart()
    first = workflow.run(c, direct_insight(c), "merchant", "enforce")
    assert first.recovery_plan["action"] == "checkout_assistance"
    calls = len(judge.calls)
    profile["workflows"]["recovery_intelligence"]["mode"] = "off"
    path.write_text(json.dumps(profile))
    held = workflow.run(c, direct_insight(c), "merchant", "enforce")
    assert held.recovery_plan["action"] == "review"
    assert held.recovery_plan["status"] == "needs_review"
    assert len(judge.calls) == calls
    profile["workflows"]["recovery_intelligence"]["mode"] = "enforce"
    path.write_text(json.dumps(profile))
    replay = workflow.run(c, direct_insight(c), "merchant", "enforce")
    assert replay.recovery_plan == first.recovery_plan
    assert len(judge.calls) == calls


def test_inflight_rollback_cannot_select_action(tmp_path, monkeypatch):
    path, profile = activate(tmp_path, monkeypatch)

    class RevokingJudge(Judge):
        def judge(self, identity, request):
            result = super().judge(identity, request)
            profile["workflows"]["recovery_intelligence"]["mode"] = "off"
            path.write_text(json.dumps(profile))
            return result

    c = cart()
    result = RecoveryWorkflow(RevokingJudge()).run(
        c, direct_insight(c), "merchant", "enforce"
    )
    assert result.recovery_plan["action"] == "review"
    assert result.buyer_intelligence["status"] == "unavailable"


def test_invalid_profile_and_flag_override_fail_closed(tmp_path, monkeypatch):
    from .operations import control
    import pytest

    path, profile = activate(tmp_path, monkeypatch)
    monkeypatch.setenv("TYPESAFE_PRELAUNCH", "0")
    assert control("recovery_intelligence", "merchant")[0]
    for change in [
        lambda d: d.update(no_publish=False),
        lambda d: d.update(unknown_field=True),
        lambda d: d.update(daily_merchant_usd=True),
        lambda d: d.update(environment="production"),
        lambda d: d.update(workflows=list(d["workflows"])),
    ]:
        original = json.loads(json.dumps(profile))
        change(original)
        path.write_text(json.dumps(original))
        with pytest.raises(ValueError, match="prelaunch_workflow_held"):
            control("recovery_intelligence", "merchant")
    path.unlink()
    with pytest.raises(ValueError, match="prelaunch_workflow_held"):
        control("recovery_intelligence", "merchant")
