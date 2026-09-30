from __future__ import annotations

import os

import uvicorn
from dida_core import setup_logging

if __name__ == "__main__":
    # uvicorn's log_level configures ONLY uvicorn's own loggers; application
    # loggers (dida.api.*) propagate to the root logger, which without a
    # handler drops everything below WARNING — every log.info() in the app was
    # invisible. Same setup as every adapter's __main__.
    setup_logging()
    uvicorn.run(
        "dida_api.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("DIDA_API_PORT", "8090")),
        log_level="info",
    )
