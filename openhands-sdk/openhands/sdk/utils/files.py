import os
import tempfile
import time
from pathlib import Path


def atomic_write_text(path: Path, value: str, mode: int = 0o600) -> None:
    """Atomically write text with owner-only permissions."""
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is None:
            os.chmod(temporary_path, mode)
        else:
            fchmod(fd, mode)
        file = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        _replace_tolerating_lock(temporary_path, path)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _replace_tolerating_lock(source: Path, target: Path) -> None:
    """os.replace, tolerating a locked target on Windows.

    Windows raises ``PermissionError`` ([WinError 5] Access is denied) when the
    target file is held open by a concurrent reader or by the same process via
    a lingering handle. The lock is usually transient, so a short bounded
    retry lets it close. If the target stays locked (e.g. a long-lived reader),
    fall back to a direct non-atomic write of the contents — overwriting in
    place is far better than failing the whole save (the locked reader simply
    sees the prior content until it closes).
    """
    attempts = 5
    delay = 0.05
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
    # Still locked: write directly to the target (best-effort, non-atomic).
    contents = source.read_bytes()
    try:
        with target.open("wb") as fh:
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        # Give the caller the original failure if even the fallback can't write.
        os.replace(source, target)
