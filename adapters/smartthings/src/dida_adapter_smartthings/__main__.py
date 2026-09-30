from __future__ import annotations

from dida_core import run_adapter, run_service, setup_logging

from dida_adapter_smartthings.adapter import SmartThingsAdapter

setup_logging()


if __name__ == "__main__":
    run_service(run_adapter(SmartThingsAdapter()))
