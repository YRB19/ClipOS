"""Atomic small artifact writes; incomplete JSON is never a checkpoint."""
import json
import os
import tempfile
from pathlib import Path


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)
