"""The same `DIDA_LOG_LEVEL` contract as every other service, spelled out locally.

planvision is the one service that cannot `from dida_core import setup_logging`: it
builds on its own base and shares no code with the rest of DIDA. That isolation is deliberate, so the switch
is reimplemented here rather than dragging core across the boundary — thirteen lines
against making a CV service depend on the bus contract.

Unrecognised levels fall back to INFO and say so, matching core: someone who set
`DIDA_LOG_LEVEL=verbose` and got silence would conclude the switch does not work.
"""

from __future__ import annotations

import logging
import os

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def setup_logging(*, default: str = "INFO", env: str = "DIDA_LOG_LEVEL") -> int:
    raw = (os.environ.get(env) or default).strip().upper()
    if raw not in LEVELS:
        logging.basicConfig(level=logging.INFO, format=FORMAT)
        logging.getLogger("dida.planvision").error(
            "%s=%r is not one of %s — falling back to INFO", env, raw, ", ".join(LEVELS))
        return logging.INFO
    level = getattr(logging, raw)
    logging.basicConfig(level=level, format=FORMAT)
    if level != logging.INFO:
        logging.getLogger("dida.planvision").info("log level %s (from %s)", raw, env)
    return level
