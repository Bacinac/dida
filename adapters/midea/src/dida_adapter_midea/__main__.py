from __future__ import annotations

import logging

from dida_core import run_adapter, run_service, setup_logging

from dida_adapter_midea.adapter import MideaAdapter

setup_logging()
# msmart logs the device's local key at INFO on connect — keep that out of our logs.
logging.getLogger("msmart").setLevel(logging.WARNING)


if __name__ == "__main__":
    run_service(run_adapter(MideaAdapter()))
