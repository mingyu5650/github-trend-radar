"""Small helpers for secure locks and atomic local-file installation."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple, Union

from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook


class StorageError(OSError):
    """Raised when locked or atomic local persistence fails."""


_TITLE_DATE_PATTERN = re.compile(
    r"\A# [^\n]*?(\d{4}-\d{2}-\d{2})[^\n]*(?:\n|\Z)"
)


def _plain_business_path(path: Union[str, Path]) -> Path:
    try:
        raw_path = os.fspath(path)
    except TypeError as exc:
        raise ValueError("business target must be a filesystem path") from exc
    if type(raw_path) is not str:
        raise ValueError("business target must be a plain text path")
    target = Path(raw_path)
    if target.name.casefold().endswith(".lock"):
        raise ValueError("business target must not use the lock-file suffix")
    return target


def _preflight_business_targets(
    report_path: Union[str, Path], data_path: Union[str, Path]
) -> None:
    report = _plain_business_path(report_path)
    data = _plain_business_path(data_path)

    def lexical_key(target: Path) -> Tuple[str, str]:
        absolute = Path(os.path.abspath(str(target)))
        parent = unicodedata.normalize("NFC", str(absolute.parent)).casefold()
        name = unicodedata.normalize("NFC", absolute.name).casefold()
        return parent, name

    report_parent, report_name = lexical_key(report)
    data_parent, data_name = lexical_key(data)
    if (
        (report_parent, report_name + ".lock") == (data_parent, data_name)
        or (data_parent, data_name + ".lock") == (report_parent, report_name)
    ):
        raise ValueError("derived lock path must not overlap a business target")


def secure_target_path(path: Union[str, Path]) -> Path:
    """Return a canonical-parent path and reject target symlinks/non-files."""

    supplied = Path(os.path.abspath(str(_plain_business_path(path))))
    try:
        target_stat = os.lstat(str(supplied))
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise StorageError("unable to inspect workbook target") from exc
    else:
        if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(
            target_stat.st_mode
        ):
            raise StorageError("workbook target must be a regular file")
    return Path(os.path.realpath(str(supplied.parent))) / supplied.name


def _ensure_regular_lock(lock_path: Path) -> None:
    try:
        lock_stat = os.lstat(str(lock_path))
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StorageError("unable to inspect workbook lock") from exc
    if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode):
        raise StorageError("workbook lock must be a regular file")


def lock_path_for(path: Union[str, Path]) -> Path:
    """Return a stable lock path outside user-facing business directories."""

    supplied = Path(os.path.abspath(str(_plain_business_path(path))))
    target = Path(os.path.realpath(str(supplied.parent))) / supplied.name
    markers = ("配置", "最新报告", "历史归档", "运行状态")
    marker_index = next(
        (index for index, part in enumerate(target.parts) if part in markers),
        None,
    )
    if marker_index is None:
        lock_root = target.parent
    else:
        workspace = Path(*target.parts[:marker_index])
        lock_root = workspace / "运行状态" / "锁"
    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:16]
    return lock_root / (target.name + "." + digest + ".lock")


@contextmanager
def exclusive_file_lock(path: Union[str, Path]) -> Iterator[None]:
    """Lock one canonical target; closing the fd releases the lock."""

    target = secure_target_path(path)
    lock_path = lock_path_for(target)
    descriptor = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_regular_lock(lock_path)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(lock_path), flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise StorageError("workbook lock must be a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except (OSError, StorageError) as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, StorageError):
            raise
        raise StorageError("unable to acquire workbook lock") from exc
    try:
        yield
    finally:
        try:
            os.close(descriptor)
        except OSError:
            # Do not turn a committed transaction into a reported failure.
            pass


def _new_temp_path(target: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".{}.".format(target.name),
        suffix=".tmp.xlsx",
        dir=str(target.parent),
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return Path(name)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _validate_workbook(path: Path, validator: Callable[[Workbook], None]) -> None:
    workbook = load_workbook(path, data_only=False)
    try:
        validator(workbook)
    finally:
        workbook.close()


def install_new_workbook(
    workbook: Workbook,
    path: Union[str, Path],
    validator: Callable[[Workbook], None],
) -> None:
    """Validate and hard-link-install a new workbook without clobbering."""

    target = secure_target_path(path)
    temp_path = None
    committed = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = _new_temp_path(target)
        workbook.save(temp_path)
        os.chmod(str(temp_path), 0o600, follow_symlinks=False)
        _fsync_file(temp_path)
        _validate_workbook(temp_path, validator)
        secure_target_path(target)
        os.link(str(temp_path), str(target))
        committed = True
        _fsync_directory(target.parent)
    except Exception as exc:
        if committed:
            return
        if isinstance(exc, StorageError):
            raise
        raise StorageError("unable to install workbook without clobbering") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except (FileNotFoundError, OSError):
                pass


def atomic_replace_file(
    path: Union[str, Path],
    writer: Callable[[Path], None],
    validator: Callable[[Path], None],
) -> None:
    """Write, validate, preserve mode, and atomically replace a regular file."""

    target = secure_target_path(path)
    try:
        original_stat = os.lstat(str(target))
    except OSError as exc:
        raise StorageError("unable to inspect workbook before update") from exc
    if not stat.S_ISREG(original_stat.st_mode):
        raise StorageError("workbook target must be a regular file")
    original_mode = stat.S_IMODE(original_stat.st_mode)
    temp_path = None
    try:
        temp_path = _new_temp_path(target)
        writer(temp_path)
        os.chmod(str(temp_path), original_mode, follow_symlinks=False)
        _fsync_file(temp_path)
        validator(temp_path)
        if secure_target_path(target) != target:
            raise StorageError("workbook target changed during update")
        os.replace(str(temp_path), str(target))
        temp_path = None
        _fsync_directory(target.parent)
    except Exception as exc:
        if isinstance(exc, StorageError):
            raise
        raise StorageError("unable to replace workbook atomically") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except (FileNotFoundError, OSError):
                pass


def _validated_report_payload(markdown: Any, model: Any) -> str:
    if type(markdown) is not str or not markdown.strip():
        raise ValueError("markdown must be non-empty text")
    if type(model) is not dict or not model:
        raise ValueError("model must be a non-empty mapping")
    # Lazy import keeps Task 8 workbook users independent of report modules,
    # while complete-report persistence shares the exact build-time schema.
    from report import build_report

    canonical_markdown = build_report(model)
    if markdown != canonical_markdown:
        raise ValueError("markdown must exactly match build_report(model)")
    report_date = model["metadata"]["date"]
    title_match = _TITLE_DATE_PATTERN.search(markdown)
    if title_match is None or title_match.group(1) != report_date:
        raise ValueError("metadata.date must match the Markdown title date")
    try:
        payload = _encode_json_value(model, 0)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("model must be JSON serializable") from exc
    return payload + "\n"


def _encode_json_value(value: Any, level: int) -> str:
    """Encode the already-validated plain model, preserving Decimal exactly."""

    value_type = type(value)
    if value_type is str:
        return json.dumps(value, ensure_ascii=False)
    if value_type in {int, float, bool} or value is None:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if value_type is Decimal:
        if not value.is_finite():
            raise ValueError("Decimal must be finite")
        return str(value)
    if value_type is list:
        if not value:
            return "[]"
        indent = "  " * (level + 1)
        closing = "  " * level
        items = [
            indent + _encode_json_value(item, level + 1)
            for item in value
        ]
        return "[\n{}\n{}]".format(",\n".join(items), closing)
    if value_type is dict:
        if not value:
            return "{}"
        indent = "  " * (level + 1)
        closing = "  " * level
        items = []
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be plain text")
            encoded_key = json.dumps(key, ensure_ascii=False)
            items.append(
                "{}{}: {}".format(
                    indent,
                    encoded_key,
                    _encode_json_value(item, level + 1),
                )
            )
        return "{\n" + ",\n".join(items) + "\n" + closing + "}"
    raise ValueError("model must contain only plain JSON values")


def _decode_json_payload(payload: str) -> Any:
    return json.loads(
        payload,
        parse_float=Decimal,
        parse_int=int,
        parse_constant=lambda _: (_ for _ in ()).throw(
            ValueError("non-finite JSON constant")
        ),
    )


def _report_target(path: Union[str, Path]) -> Path:
    """Resolve a target and reject existing symlinks/non-regular files."""

    target = secure_target_path(path)
    try:
        target_stat = os.lstat(str(target))
    except FileNotFoundError:
        return target
    except OSError as exc:
        raise StorageError("unable to inspect report target") from exc
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
        raise StorageError("report target must be a regular file")
    return target


def _target_state(target: Path) -> Optional[os.stat_result]:
    try:
        target_stat = os.lstat(str(target))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageError("unable to inspect report target") from exc
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
        raise StorageError("report target must be a regular file")
    return target_stat


def _same_file_state(before: Optional[os.stat_result], after: Optional[os.stat_result]) -> bool:
    if before is None or after is None:
        return before is after
    return before.st_dev == after.st_dev and before.st_ino == after.st_ino


def _conservative_alias_key(target: Path) -> Tuple[str, str]:
    parent = os.path.realpath(str(target.parent))
    name = unicodedata.normalize("NFC", target.name).casefold()
    return parent, name


def _reject_same_target(report_target: Path, data_target: Path) -> None:
    report_state = _target_state(report_target)
    data_state = _target_state(data_target)
    if report_state is not None and data_state is not None:
        if (
            report_state.st_dev == data_state.st_dev
            and report_state.st_ino == data_state.st_ino
        ):
            raise ValueError("report and data targets must be distinct files")
        try:
            if os.path.samefile(str(report_target), str(data_target)):
                raise ValueError("report and data targets must be distinct files")
        except FileNotFoundError:
            pass
    if _conservative_alias_key(report_target) == _conservative_alias_key(data_target):
        raise ValueError("report and data targets must not be aliases")


def _stage_text(target: Path, text: str, mode: int) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".{}.".format(target.name),
        suffix=".tmp",
        dir=str(target.parent),
    )
    stage = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        return stage
    except Exception:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            stage.unlink()
        except (FileNotFoundError, OSError):
            pass
        raise


def _backup_file(target: Path, mode: int) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".{}.".format(target.name),
        suffix=".bak",
        dir=str(target.parent),
    )
    backup = Path(name)
    os.close(descriptor)
    try:
        target_stat = os.lstat(str(target))
        if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
            raise StorageError("backup source must be a regular file")
        backup.unlink()
        os.link(str(target), str(backup), follow_symlinks=False)
        backup_stat = os.lstat(str(backup))
        if not stat.S_ISREG(backup_stat.st_mode) or (
            backup_stat.st_dev != target_stat.st_dev
            or backup_stat.st_ino != target_stat.st_ino
        ):
            raise StorageError("hardlink backup verification failed")
        return backup
    except Exception as exc:
        try:
            backup.unlink()
        except (FileNotFoundError, OSError):
            pass
        if isinstance(exc, StorageError):
            raise
        raise StorageError("unable to create hardlink backup") from exc


def _materialize_recovery_backup(backup: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".{}.".format(backup.name),
        suffix=".recovery.bak",
        dir=str(backup.parent),
    )
    recovery = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as destination, backup.open("rb") as source:
            descriptor = -1
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        backup.unlink()
        return recovery
    except Exception:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            recovery.unlink()
        except (FileNotFoundError, OSError):
            pass
        raise


def _install_stage(stage: Path, target: Path, existed: bool) -> None:
    if existed:
        os.replace(str(stage), str(target))
        return
    # A hard-link install has O_EXCL-like semantics: a non-cooperating writer
    # that creates the path during staging wins and is never overwritten.
    os.link(str(stage), str(target))
    stage.unlink()


def _restore_target(target: Path, backup: Optional[Path]) -> None:
    if backup is None:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return
    os.replace(str(backup), str(target))


def save_complete_report(
    report_path: Union[str, Path],
    data_path: Union[str, Path],
    markdown: str,
    model: Mapping[str, Any],
) -> None:
    """Persist Markdown and its JSON model as one process-failure transaction.

    The transaction is serialized across staging, validation, and commit.  If
    the second installation fails, the first target is restored to its exact
    pre-call existence state.  This provides process-level failure atomicity;
    no claim is made of absolute power-loss atomicity across two files.
    """

    _preflight_business_targets(report_path, data_path)
    json_payload = _validated_report_payload(markdown, model)
    report_target = _report_target(report_path)
    data_target = _report_target(data_path)
    _reject_same_target(report_target, data_target)

    report_target.parent.mkdir(parents=True, exist_ok=True)
    data_target.parent.mkdir(parents=True, exist_ok=True)
    lock_targets = sorted(
        (report_target, data_target),
        key=lambda target: os.fsencode(str(target)),
    )

    with ExitStack() as lock_stack:
        for target in lock_targets:
            lock_stack.enter_context(exclusive_file_lock(target))
        _reject_same_target(report_target, data_target)
        report_state = _target_state(report_target)
        data_state = _target_state(data_target)
        stages: Dict[Path, Optional[Path]] = {report_target: None, data_target: None}
        backups: Dict[Path, Optional[Path]] = {report_target: None, data_target: None}
        committed = []
        pair_committed = False
        preserved_backups = set()
        try:
            report_mode = stat.S_IMODE(report_state.st_mode) if report_state else 0o600
            data_mode = stat.S_IMODE(data_state.st_mode) if data_state else 0o600
            stages[report_target] = _stage_text(report_target, markdown, report_mode)
            stages[data_target] = _stage_text(data_target, json_payload, data_mode)

            if stages[report_target].read_text(encoding="utf-8") != markdown:
                raise StorageError("Markdown staging verification failed")
            if stages[data_target].read_text(encoding="utf-8") != json_payload:
                raise StorageError("JSON staging verification failed")
            try:
                staged_model = _decode_json_payload(json_payload)
                reread_model = _decode_json_payload(
                    stages[data_target].read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise StorageError("JSON staging verification failed") from exc
            if reread_model != staged_model:
                raise StorageError("JSON staging verification failed")

            current_report = _target_state(report_target)
            current_data = _target_state(data_target)
            if not _same_file_state(report_state, current_report) or not _same_file_state(
                data_state, current_data
            ):
                raise StorageError("report target changed during staging")

            if report_state is not None:
                backups[report_target] = _backup_file(report_target, report_mode)
            if data_state is not None:
                backups[data_target] = _backup_file(data_target, data_mode)

            _install_stage(stages[report_target], report_target, report_state is not None)
            stages[report_target] = None
            committed.append(report_target)
            _install_stage(stages[data_target], data_target, data_state is not None)
            stages[data_target] = None
            committed.append(data_target)
            # Read the installed pair while the lock is still held.
            if report_target.read_text(encoding="utf-8") != markdown:
                raise StorageError("installed Markdown verification failed")
            installed_model = _decode_json_payload(
                data_target.read_text(encoding="utf-8")
            )
            if installed_model != staged_model:
                raise StorageError("installed JSON verification failed")
            pair_committed = True
        except Exception as exc:
            if committed and not pair_committed:
                rollback_failed = False
                recovery_paths = []
                for target in reversed(committed):
                    try:
                        _restore_target(target, backups[target])
                        backups[target] = None
                    except Exception:
                        rollback_failed = True
                        backup = backups[target]
                        if backup is not None:
                            try:
                                recovery = _materialize_recovery_backup(backup)
                                backups[target] = recovery
                                preserved_backups.add(recovery)
                                recovery_paths.append(recovery)
                            except Exception:
                                try:
                                    os.chmod(str(backup), 0o600, follow_symlinks=False)
                                except OSError:
                                    pass
                                preserved_backups.add(backup)
                                recovery_paths.append(backup)
                if rollback_failed:
                    paths = ", ".join(str(path) for path in recovery_paths)
                    raise StorageError(
                        "report transaction failed and rollback was incomplete; "
                        "recovery backup: {}".format(paths or "unavailable")
                    ) from exc
            if isinstance(exc, (ValueError, TypeError)):
                raise
            if isinstance(exc, StorageError):
                raise
            raise StorageError("unable to save complete report") from exc
        finally:
            for path in list(stages.values()) + list(backups.values()):
                if path is not None and path not in preserved_backups:
                    try:
                        path.unlink()
                    except (FileNotFoundError, OSError):
                        pass

        # Durability enhancement and cleanup happen after the logical commit;
        # their failure must not turn a committed pair into a reported failure.
        try:
            _fsync_directory(report_target.parent)
            if data_target.parent != report_target.parent:
                _fsync_directory(data_target.parent)
        except OSError:
            pass
