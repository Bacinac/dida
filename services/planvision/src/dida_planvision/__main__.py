"""Run the planvision HTTP service."""

from __future__ import annotations

import os

import uvicorn

from dida_planvision.logsetup import setup_logging

if __name__ == "__main__":
    setup_logging()
    uvicorn.run(
        "dida_planvision.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("DIDA_PLANVISION_PORT", "8094")),
        # Without this uvicorn installs its own dictConfig over the root logger and
        # DIDA_LOG_LEVEL stops governing anything.
        log_config=None,
    )
