"""
Counting image-model calls (gpt-image-2) made while one piece of work runs, e.g. one ad.

    with counting() as calls:
        ...                      # every image call inside, across awaited tasks, is counted
    calls.n                      # how many were made

Calls made with no counter running (organic posts, video) are not counted and cost nothing here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional


class _Calls:
    def __init__(self) -> None:
        self.n = 0


_current: ContextVar[Optional[_Calls]] = ContextVar("image_calls", default=None)


def bump(n: int = 1) -> None:
    calls = _current.get()
    if calls is not None:
        calls.n += n


@contextmanager
def counting() -> Iterator[_Calls]:
    calls = _Calls()
    token = _current.set(calls)
    try:
        yield calls
    finally:
        _current.reset(token)
