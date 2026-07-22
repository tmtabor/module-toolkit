"""
Concurrency tests for agents/researcher.py's module-level Brave Search rate
limiter (_brave_get / _brave_lock / _brave_last_call).

temporal/PHASE5.md Workstream E ("concurrent multi-run validation") calls out
this process-wide state as something to confirm rather than just assume is
safe when multiple workflows run concurrently against one worker process.
_BRAVE_MIN_INTERVAL is monkeypatched down so the test runs in well under a
second instead of len(threads) * 1.1s.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from agents import researcher


def test_concurrent_calls_are_serialized_not_dropped_or_duplicated(monkeypatch):
    monkeypatch.setattr(researcher, "_BRAVE_MIN_INTERVAL", 0.05)
    monkeypatch.setattr(researcher, "_brave_last_call", 0.0)

    call_times: list[float] = []
    resp = MagicMock(status_code=200)

    def fake_get(url, params, headers, timeout):
        call_times.append(time.monotonic())
        return resp

    monkeypatch.setattr(researcher.requests, "get", fake_get)

    n = 6
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda _: researcher._brave_get("http://x", {}, {}), range(n)))

    # Every concurrent caller got a response, and every response corresponds
    # to a distinct, real call -- the lock doesn't drop or coalesce calls.
    assert len(results) == n
    assert len(call_times) == n
    assert all(r is resp for r in results)

    # And they were actually serialized with the minimum gap enforced, not
    # fired simultaneously the instant the lock allowed threads through.
    call_times.sort()
    gaps = [b - a for a, b in zip(call_times, call_times[1:])]
    assert all(gap >= 0.05 - 0.01 for gap in gaps)
