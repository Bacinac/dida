from __future__ import annotations

import os
from pathlib import Path

from dida_core import run_service, setup_logging

from dida_runner.runner import Runner

setup_logging()

# The project directory is mounted at the SAME absolute path it has on the host:
# compose hands the daemon host paths derived from it, so a different path inside
# would turn every relative bind mount into a directory that does not exist there.
PROJECT_DIR = Path(os.environ.get("DIDA_REPO_HOST", "/mnt/docker/dida"))

run_service(Runner(PROJECT_DIR).run())
