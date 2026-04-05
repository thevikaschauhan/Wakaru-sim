import time
from typing import Callable, TypeVar

from .exceptions import TimeoutError

T = TypeVar("T")


def poll_until_done(
    fetch_fn: Callable[[], T],
    is_done_fn: Callable[[T], bool],
    operation: str,
    interval_seconds: float = 5.0,
    timeout_seconds: int = 300,
    on_progress: Callable[[T], None] | None = None,
) -> T:
    """
    Repeatedly calls `fetch_fn` until `is_done_fn` returns True or timeout.

    Args:
        fetch_fn: Callable that returns the current state (no args).
        is_done_fn: Callable that receives the state and returns True when done.
        operation: Human-readable name used in TimeoutError messages.
        interval_seconds: Sleep time between polls.
        timeout_seconds: Max total wait time before raising TimeoutError.
        on_progress: Optional callback called with each intermediate state.

    Returns:
        The final state when is_done_fn returns True.

    Raises:
        TimeoutError: If the operation doesn't complete within timeout_seconds.
    """
    deadline = time.monotonic() + timeout_seconds

    while True:
        state = fetch_fn()

        if on_progress:
            on_progress(state)

        if is_done_fn(state):
            return state

        if time.monotonic() >= deadline:
            raise TimeoutError(operation, timeout_seconds)

        time.sleep(interval_seconds)
