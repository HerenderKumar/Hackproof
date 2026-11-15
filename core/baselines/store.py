"""Baseline files on disk (M9, the plan's A4).

    ARTIFACT_DIR/baselines/<baseline_id>/parts/<n>.bin   nonce + AES-256-GCM(part)
    ARTIFACT_DIR/baselines/<baseline_id>/index.sqlite    hashes and paths, no code

Folder and file names come from the baseline's ID and the part's number, never from anything
a device sent. A part is written under a temporary name and renamed, so a reader sees it whole
or not at all.
"""

import os
import shutil
import uuid
from pathlib import Path

from core.baselines.crypto import decrypt, encrypt


class BaselineStore:
    def __init__(self, root: Path, key: bytes) -> None:
        self._root = root / "baselines"
        self._key = key

    def dir(self, baseline_id: uuid.UUID) -> Path:
        return self._root / str(uuid.UUID(str(baseline_id)))

    def _part(self, baseline_id: uuid.UUID, n: int) -> Path:
        if n < 1:
            raise ValueError("parts are numbered from 1")
        return self.dir(baseline_id) / "parts" / f"{n}.bin"

    @staticmethod
    def _label(baseline_id: uuid.UUID, n: int) -> bytes:
        return f"{baseline_id}:part:{n}".encode()

    def write_part(self, baseline_id: uuid.UUID, n: int, data: bytes) -> None:
        path = self._part(baseline_id, n)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            temporary.write_bytes(encrypt(self._key, data, self._label(baseline_id, n)))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def read_part(self, baseline_id: uuid.UUID, n: int) -> bytes:
        sealed = self._part(baseline_id, n).read_bytes()
        return decrypt(self._key, sealed, self._label(baseline_id, n))

    def index_path(self, baseline_id: uuid.UUID) -> Path:
        return self.dir(baseline_id) / "index.sqlite"

    def delete(self, baseline_id: uuid.UUID) -> None:
        shutil.rmtree(self.dir(baseline_id), ignore_errors=True)
