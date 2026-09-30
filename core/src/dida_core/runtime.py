"""Shared service entrypoint: uvloop + graceful shutdown in one place.

Replaces the per-service `uvloop.install(); asyncio.run(main())` boilerplate.
`uvloop.run()` avoids Python 3.14's deprecated event-loop-policy API.

uvloop 0.22.1 (the latest release) still calls the deprecated
`asyncio.iscoroutinefunction` inside its compiled `add_signal_handler` on
Python 3.14 — there is no newer uvloop and we can't patch its Cython, so that
one upstream DeprecationWarning is filtered here. Remove the filter once uvloop
ships a 3.14 fix.
"""

from __future__ import annotations

import warnings
from collections.abc import Coroutine
from typing import Any

import uvloop


def run_service(main: Coroutine[Any, Any, Any]) -> None:
    """Run a DIDA service coroutine on uvloop until it returns."""
    warnings.filterwarnings(
        "ignore",
        message=r"'asyncio\.iscoroutinefunction' is deprecated",
        category=DeprecationWarning,
    )
    uvloop.run(main)
