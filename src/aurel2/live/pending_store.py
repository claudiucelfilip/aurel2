"""Serialize dashboard and daemon updates to the shared pending file."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile


@contextmanager
def pending_transaction(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {"decisions": {}}
        yield data
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(data, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
