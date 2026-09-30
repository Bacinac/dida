"""One place that decides how loud a service is.

Every service used to open with the same hardcoded line:

    logging.basicConfig(level=logging.INFO, format="…")

Thirty-nine copies of it. That is not just duplication — it means raising ONE
adapter to DEBUG in the middle of an incident requires editing code, rebuilding
its image and redeploying, which is exactly when you can least afford to. Since
every adapter is already its own container, a plain env var gives per-adapter
granularity for free.

An unrecognised level is a LOUD failure, not a silent fall back to INFO: someone
who set `DIDA_LOG_LEVEL=verbose` and got nothing would conclude the switch does
not work, and stop reaching for it.
"""

from __future__ import annotations

import logging
import os

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def setup_logging(*, default: str = "INFO", env: str = "DIDA_LOG_LEVEL") -> int:
    """Configure the root logger and return the level actually applied."""
    raw = (os.environ.get(env) or default).strip().upper()
    if raw not in LEVELS:
        logging.basicConfig(level=logging.INFO, format=FORMAT)
        logging.getLogger("dida.logsetup").error(
            "%s=%r is not one of %s — falling back to INFO", env, raw, ", ".join(LEVELS))
        return logging.INFO
    level = getattr(logging, raw)
    logging.basicConfig(level=level, format=FORMAT)
    if level != logging.INFO:
        # Say so once at boot. A service running at DEBUG for a forgotten reason is
        # a cost (disk, and now ClickHouse rows) that should never be invisible.
        logging.getLogger("dida.logsetup").info("log level %s (from %s)", raw, env)
    return level
