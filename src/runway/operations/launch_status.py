"""What the Windows launcher needs to know before it touches anything.

`python -m runway.operations.launch_status` prints one JSON object:

- `database`, `current`, `head`, `up_to_date`, `known_revision`: the configured
  database's revision against this checkout's migration head (read-only;
  a missing database is reported, never created);
- `revision`: this checkout's commit, the value the API reports on `/health`
  and the worker writes to `worker.pid`;
- `worker_lock`, `worker_pid`: where the worker holds its single-instance lock
  and writes its PID record for the configured data directory.

The launcher compares these with the running API's `/health` to decide whether
the API is older code that must be stopped before `runway db upgrade`.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from runway.config import Settings, get_settings
from runway.db.bootstrap import migration_status
from runway.operations.revision import checkout_revision
from runway.operations.runtime_files import worker_lock_path, worker_pid_path


def launch_status(settings: Settings) -> dict[str, Any]:
    status = migration_status(settings)
    return {
        "database": str(settings.database_path),
        **status.as_dict(),
        "revision": checkout_revision(),
        "worker_lock": str(worker_lock_path(settings)),
        "worker_pid": str(worker_pid_path(settings)),
    }


def main() -> int:
    print(json.dumps(launch_status(get_settings())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
