"""Exercise the exact redis-py re-entrant resubscribe deadlock with real Redis."""
import os
import subprocess
import sys
import pytest


def test_pubsub_resubscribe_after_disconnect():
    url=os.getenv("WAKARU_TEST_REDIS_URL")
    if not url:
        pytest.skip("WAKARU_TEST_REDIS_URL unset")
    # Isolate the potentially deadlocking operation so a regression is bounded.
    script='''
import os,redis
r=redis.Redis.from_url(os.environ["WAKARU_TEST_REDIS_URL"])
p=r.pubsub()
p.subscribe("vakaru-reconnect-test")
p.get_message(timeout=1)
p.connection.disconnect()
p.subscribe("vakaru-reconnect-test-two")
p.close()
print("resubscribe completed",redis.__version__)
'''
    result=subprocess.run([sys.executable,"-c",script],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr
    assert "resubscribe completed" in result.stdout
