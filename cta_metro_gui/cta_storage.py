"""Safe file handling: strict JSON reads, crash-safe writes, backups and restores.

The rule this module enforces: a damaged or unreadable data file is NEVER treated as an empty, new installation.

* File does not exist          -> first run: the caller's default is returned.
* File exists, cannot be read  -> ``DataUnreadable``   (permissions, locked file, disk error)
* File exists, not valid JSON  -> ``DataCorrupted``    (includes empty or truncated files)
* Valid JSON, wrong structure  -> ``DataInvalid``      (wrong type, missing fields, duplicate ids, bad status)

All of these are ``StorageError``. ``StorageError`` subclasses ``OSError`` on purpose: every existing
``except OSError`` in the GUI already treats it as "could not save / could not load" and refuses to continue,
so no caller can accidentally carry on with an empty list.

``str(error)`` is the operator-facing message. It names the file but never the full path (which can contain the
Windows user name), and never includes file contents.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

BACKUP_DIR_NAME = 'backups'
KEEP_ROUTINE_BACKUPS = 30                       # per file; pre-migration / pre-restore copies are never pruned


class StorageError(OSError):
    """Base class. ``str(exc)`` is safe to show to an operator."""
    kind = 'storage'

    def __init__(self, message: str, path: Optional[Path] = None):
        super().__init__(message)
        self.message = message
        self.path = Path(path) if path else None

    def __str__(self) -> str:
        return self.message


class DataUnreadable(StorageError):
    kind = 'unreadable'


class DataCorrupted(StorageError):
    kind = 'corrupted'


class DataInvalid(StorageError):
    kind = 'invalid'


class DataMissing(StorageError):
    """The data file is gone but backups of it exist: this is not a first run."""
    kind = 'missing'


class DataWriteError(StorageError):
    kind = 'write'


class ReadOnlyStorage(StorageError):
    kind = 'read-only'


class ConcurrentUpdate(StorageError):
    """Someone else changed the same record after we read it. Nothing was written."""
    kind = 'conflict'


RECOVERY_HINT = 'Nothing has been changed or deleted. Run "python cta_recover.py status" or ask your administrator.'


def _name(path: Path) -> str:
    return Path(path).name


# ------------------------------------------------------------------ reading
def read_json(path: Path, default: Callable[[], Any], validate: Optional[Callable[[Any], None]] = None,
              what: str = 'data') -> Any:
    """Load ``path`` strictly. Missing -> ``default()``; anything wrong with an existing file -> StorageError."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default()
    except IsADirectoryError:
        raise DataUnreadable(f'The {what} file ({_name(path)}) is a folder, not a file. {RECOVERY_HINT}', path)
    except PermissionError:
        raise DataUnreadable(f'The {what} file ({_name(path)}) cannot be read: permission denied. {RECOVERY_HINT}',
                             path)
    except OSError:
        raise DataUnreadable(f'The {what} file ({_name(path)}) cannot be read because of a disk or system error. '
                             f'{RECOVERY_HINT}', path)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise DataCorrupted(f'The {what} file ({_name(path)}) is damaged (not readable text). {RECOVERY_HINT}', path)
    if not text.strip():
        raise DataCorrupted(f'The {what} file ({_name(path)}) is empty, which usually means a save was interrupted. '
                            f'{RECOVERY_HINT}', path)
    try:
        data = json.loads(text)
    except ValueError:
        raise DataCorrupted(f'The {what} file ({_name(path)}) is damaged (not valid JSON). {RECOVERY_HINT}', path)
    if validate is not None:
        try:
            validate(data)
        except DataInvalid as exc:
            raise DataInvalid(f'The {what} file ({_name(path)}) contains invalid records: {exc.message} '
                              f'{RECOVERY_HINT}', path) from None
    return data


# ------------------------------------------------------------------ writing
def _fsync_dir(folder: Path) -> None:
    """Make the rename itself durable where the OS supports it (not on Windows)."""
    if os.name == 'nt':
        return
    try:
        fd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write to a temporary file in the same folder, flush + fsync, then replace. The original survives any failure."""
    path = Path(path)
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
        with os.fdopen(fd, 'wb') as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
        tmp_name = None
        _fsync_dir(path.parent)
    except StorageError:
        raise
    except PermissionError:
        raise DataWriteError(f'Could not save {_name(path)}: permission denied. The previous version was kept.', path)
    except OSError:
        raise DataWriteError(f'Could not save {_name(path)} (disk full or not writable). '
                             'The previous version was kept.', path)
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def atomic_write_json(path: Path, data: Any, validate: Optional[Callable[[Any], None]] = None,
                      what: str = 'data', keep_previous: bool = True) -> None:
    """Validate ``data``, refuse to overwrite a damaged existing file, keep ``<name>.bak``, then write safely."""
    path = Path(path)
    if validate is not None:
        try:
            validate(data)
        except DataInvalid as exc:
            raise DataInvalid(f'Not saved: the {what} to be written is invalid ({exc.message}).', path) from None
    if path.exists():
        # Raises if the current file is damaged: it may still be recoverable, so it must not be overwritten.
        read_json(path, list, validate, what)
        if keep_previous:
            atomic_write_bytes(path.with_name(path.name + '.bak'), path.read_bytes())
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    atomic_write_bytes(path, payload)


# ------------------------------------------------------------------ backups
def backup_dir(folder: Path) -> Path:
    return Path(folder) / BACKUP_DIR_NAME


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime('%Y%m%d-%H%M%S-%f')


def backup_file(path: Path, dest_dir: Path, label: str, now: Optional[datetime] = None) -> Path:
    """Copy ``path`` into ``dest_dir`` and check the copy is byte-identical. Raises DataWriteError if it is not."""
    path, dest_dir = Path(path), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f'{path.stem}-{stamp(now)}-{label}{path.suffix}'
    try:
        shutil.copy2(str(path), str(target))
        if sha256(path) != sha256(target):
            raise OSError('copy differs')
    except OSError:
        raise DataWriteError(f'Could not make a backup of {_name(path)}. Nothing was changed.', path)
    return target


def list_backups(dest_dir: Path, stem: str, suffix: str) -> List[Path]:
    """Newest first."""
    dest_dir = Path(dest_dir)
    if not dest_dir.is_dir():
        return []
    found = [p for p in dest_dir.rglob(f'{stem}-*{suffix}') if p.is_file()]
    return sorted(found, key=lambda p: p.name, reverse=True)


def prune_backups(dest_dir: Path, stem: str, suffix: str, label: str, keep: int = KEEP_ROUTINE_BACKUPS) -> None:
    """Delete old routine backups with this label only (never migration or pre-restore copies)."""
    routine = [p for p in list_backups(dest_dir, stem, suffix) if p.parent == Path(dest_dir)
               and p.name.endswith(f'-{label}{suffix}')]
    for old in routine[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def restore_json_backup(backup: Path, target: Path, validate: Optional[Callable[[Any], None]] = None,
                        what: str = 'data', now: Optional[datetime] = None) -> Optional[Path]:
    """Put a checked backup back in place. The file being replaced (even if damaged) is kept in backups first.
    Returns where the replaced file was kept, or None if there was nothing to replace."""
    backup, target = Path(backup), Path(target)
    if not backup.is_file():
        raise DataMissing(f'The backup {_name(backup)} does not exist.', backup)
    read_json(backup, list, validate, what)                        # refuses a damaged or invalid backup
    kept = None
    if target.exists():
        kept = backup_file(target, backup_dir(target.parent) / 'replaced', 'before-restore', now)
    atomic_write_bytes(target, backup.read_bytes())
    return kept


def load_config(path: Path) -> dict:
    """cta_config.json: missing -> {}; damaged -> DataCorrupted (settings are never silently reset)."""
    def check(data):
        if not isinstance(data, dict):
            raise DataInvalid('the settings must be a JSON object.')
    return read_json(path, dict, check, 'settings')
