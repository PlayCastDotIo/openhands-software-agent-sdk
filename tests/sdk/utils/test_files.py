"""Regression test for #30: `atomic_write_text` failing on Windows with
`[WinError 5] Access is denied` when the target file is still held open.

The atomic temp-file + `os.replace` scheme fails on Windows when the target is
open (e.g. a concurrent reader / the same process holding a handle). The fix
must tolerate a locked target via a retry-on-locked approach.
"""

import os
from pathlib import Path

import pytest

from openhands.sdk.utils.files import atomic_write_text


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific file-locking behavior")
def test_atomic_write_text_tolerates_locked_target_on_windows(tmp_path: Path) -> None:
    """atomic_write_text should succeed even when the target is held open."""
    target = tmp_path / "base_state.json"
    target.write_text("old", encoding="utf-8")

    # Hold the target open in a read-write mode that, on Windows, blocks
    # os.replace with WinError 5. Then attempt the atomic write.
    with target.open("r+"):
        # Must not raise — the write should retry/tolerate the lock.
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
