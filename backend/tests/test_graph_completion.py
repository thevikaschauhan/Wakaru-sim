from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from zep_cloud.types.episode import Episode
from app.services.graph_builder import GraphBuilderService


def builder_with(get):
    builder = object.__new__(GraphBuilderService)
    builder.client = SimpleNamespace(graph=SimpleNamespace(episode=SimpleNamespace(get=get)))
    return builder


def test_timeout_never_reports_complete():
    progress=[]
    builder=builder_with(Mock(return_value=Episode(processed=False, content="test", created_at="2026-09-15T00:00:00Z", uuid_="episode")))
    with pytest.raises(TimeoutError):
        builder._wait_for_episodes(["episode"],lambda message,ratio:progress.append(ratio),timeout=0)
    assert 1.0 not in progress


def test_query_error_fails_without_exposing_source(monkeypatch):
    monkeypatch.setattr("app.services.graph_builder.time.sleep",lambda _:None)
    builder=builder_with(Mock(side_effect=RuntimeError("private shopper payload")))
    with pytest.raises(RuntimeError, match="episode status unavailable") as error:
        builder._wait_for_episodes(["episode"])
    assert "private shopper" not in str(error.value)


def test_all_episodes_must_be_processed():
    progress=[]
    get=Mock(return_value=Episode(processed=True, content="test", created_at="2026-09-15T00:00:00Z", uuid_="episode"))
    builder_with(get)._wait_for_episodes(["one","two","one"],lambda message,ratio:progress.append(ratio))
    assert get.call_count==2
    assert progress[-1]==1.0
