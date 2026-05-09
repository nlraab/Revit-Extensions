# -*- coding: utf-8 -*-
"""Telemetry recorder. See `dbhms_telemetry/__init__.py` for the rule.

Implementation notes:
    - All I/O is wrapped in blanket try/except. A telemetry write must
      never fail a tool. If H: is not mapped, the local-cache copy
      still captures the event.
    - Records are JSON Lines (one JSON object per line, UTF-8). Append
      mode means many concurrent writers for the same file would race;
      we sidestep that by partitioning the path by user (one file per
      user per day).
    - Revit / pyRevit imports are deferred inside `_safe_get_revit_info`
      so this module parses cleanly under CPython 3 for the test suite.
"""

import datetime
import getpass
import json
import os
import socket
import traceback
import uuid


SCHEMA_VERSION = 1

_DEFAULT_NETWORK_ROOT = r"H:\TOOLS\REVIT\dbHMS Custom Extensions\Data Traceback"
_DEFAULT_LOCAL_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "dbhms_telemetry",
)


def network_root():
    """Resolve the shared network folder for telemetry events.

    Override with the DBHMS_TELEMETRY_NETWORK_ROOT env var (used by
    tests). Defaults to the firm's H:-drive Data Traceback folder.
    """
    return os.environ.get("DBHMS_TELEMETRY_NETWORK_ROOT") or _DEFAULT_NETWORK_ROOT


def local_root():
    """Resolve the local-fallback cache folder.

    Override with DBHMS_TELEMETRY_LOCAL_ROOT. Defaults to
    `%LOCALAPPDATA%\\dbhms_telemetry`.
    """
    return os.environ.get("DBHMS_TELEMETRY_LOCAL_ROOT") or _DEFAULT_LOCAL_ROOT


def _utc_now():
    return datetime.datetime.utcnow()


def _iso_z(dt):
    """Render a datetime as a UTC ISO-8601 string with millisecond precision."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(dt.microsecond // 1000)


def _safe_username():
    try:
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def _safe_hostname():
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def _safe_get_revit_info():
    """Best-effort capture of the Revit / pyRevit context.

    Returns a dict; absent fields just stay missing. Designed to never
    raise: if pyRevit isn't loaded (e.g. running under tests), this
    returns {}.
    """
    info = {}
    try:
        from pyrevit import revit, HOST_APP  # type: ignore
    except Exception:
        return info
    try:
        if HOST_APP is not None:
            info["revit_version"] = str(getattr(HOST_APP, "version", "") or "") or None
            info["revit_build"] = str(getattr(HOST_APP, "build", "") or "") or None
            info["revit_username"] = str(getattr(HOST_APP, "username", "") or "") or None
    except Exception:
        pass
    try:
        doc = getattr(revit, "doc", None)
        if doc is not None:
            try:
                info["doc_title"] = str(doc.Title)
            except Exception:
                pass
            try:
                info["doc_path"] = str(doc.PathName)
            except Exception:
                pass
            try:
                info["doc_is_workshared"] = bool(doc.IsWorkshared)
            except Exception:
                pass
    except Exception:
        pass
    # Drop any None values for a cleaner record.
    return dict((k, v) for k, v in info.items() if v is not None)


def _relative_path_for(now_local, username):
    """Year/Month/Day file path. One file per user per day - prevents
    concurrent-writer contention across the firm."""
    safe_user = "".join(c for c in username if c.isalnum() or c in ("-", "_", "."))
    if not safe_user:
        safe_user = "unknown"
    return os.path.join(
        now_local.strftime("%Y"),
        now_local.strftime("%m"),
        "{0}_{1}.jsonl".format(now_local.strftime("%Y-%m-%d"), safe_user),
    )


def _append_jsonl(root, rel, line):
    """Append one JSON-line to <root>/<rel>. Swallow all errors -
    telemetry must never break a tool."""
    if not root:
        return
    try:
        path = os.path.join(root, rel)
        dir_ = os.path.dirname(path)
        if not os.path.isdir(dir_):
            os.makedirs(dir_)
        # 'a' is best-effort atomic-ish for short JSON lines on Windows;
        # combined with one-file-per-user-per-day partitioning, this is
        # safe enough for telemetry.
        f = open(path, "a")
        try:
            f.write(line)
        finally:
            f.close()
    except Exception:
        pass


def _write_record(record):
    """Append one JSON record to the network root AND the local fallback."""
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
    except Exception:
        # Rare: object that doesn't JSON-serialize. Coerce values to str.
        try:
            safe = dict((k, str(v)) for k, v in record.items())
            line = json.dumps(safe, ensure_ascii=False) + "\n"
        except Exception:
            return
    user = record.get("user") or _safe_username()
    rel = _relative_path_for(datetime.datetime.now(), user)
    _append_jsonl(network_root(), rel, line)
    _append_jsonl(local_root(), rel, line)


def _normalize_tool(name):
    """Strip the toolbar `\\n` line-break and any surrounding whitespace."""
    if not name:
        return "Unknown"
    return str(name).replace("\n", " ").replace("\r", " ").strip() or "Unknown"


class Session(object):
    """Handle for one in-flight tool invocation. Use via `session()` or
    `start()` / `end()`."""

    def __init__(self, tool, script_path=None):
        self.tool = _normalize_tool(tool)
        self.script_path = str(script_path) if script_path else None
        self.session_id = uuid.uuid4().hex
        self.user = _safe_username()
        self.host = _safe_hostname()
        self.start_time = _utc_now()
        self.start_iso = _iso_z(self.start_time)
        self.revit = _safe_get_revit_info()
        self._ended = False

    def _common(self):
        rec = {
            "schema": SCHEMA_VERSION,
            "session_id": self.session_id,
            "tool": self.tool,
            "user": self.user,
            "host": self.host,
        }
        if self.script_path:
            rec["script_path"] = self.script_path
        rec.update(self.revit)
        return rec

    def write_started(self):
        rec = self._common()
        rec["event"] = "session_started"
        rec["started_at"] = self.start_iso
        _write_record(rec)

    def write_ended(self, status="completed", error=None):
        if self._ended:
            return
        self._ended = True
        end_time = _utc_now()
        end_iso = _iso_z(end_time)
        try:
            duration = (end_time - self.start_time).total_seconds()
        except Exception:
            duration = None
        rec = self._common()
        rec["event"] = "session_ended"
        rec["status"] = status
        rec["started_at"] = self.start_iso
        rec["ended_at"] = end_iso
        if duration is not None:
            rec["duration_seconds"] = round(duration, 3)
        if error is not None:
            err_type, err_msg, tb = error
            rec["error_type"] = str(err_type) if err_type else None
            rec["error_message"] = str(err_msg) if err_msg else None
            rec["traceback"] = str(tb) if tb else None
        _write_record(rec)


def start(tool, script_path=None):
    """Begin a telemetry session. Writes a `session_started` record
    immediately so we have evidence the tool fired even if Revit
    hard-crashes before `end()` runs.

    Returns a Session you must pass to `end()`. Errors in telemetry
    itself are swallowed.
    """
    s = Session(tool, script_path=script_path)
    try:
        s.write_started()
    except Exception:
        pass
    return s


def end(session, status="completed", error=None):
    """Close a telemetry session. `error` is an optional (type, message,
    traceback) tuple. Safe to call with `session=None`."""
    if session is None:
        return
    try:
        session.write_ended(status=status, error=error)
    except Exception:
        pass


class _SessionCM(object):
    """Context manager wrapper around start()/end()."""

    def __init__(self, tool, script_path=None):
        self._tool = tool
        self._script_path = script_path
        self.session = None

    def __enter__(self):
        self.session = start(self._tool, script_path=self._script_path)
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.session is None:
            return False
        if exc_type is None:
            end(self.session, status="completed")
        else:
            try:
                tb_str = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
            except Exception:
                tb_str = ""
            err = (
                getattr(exc_type, "__name__", str(exc_type)) if exc_type else None,
                str(exc_val) if exc_val is not None else "",
                tb_str,
            )
            end(self.session, status="failed", error=err)
        return False  # never swallow the tool's exception


def session(tool, script_path=None):
    """Context manager idiom for tools with a synchronous entry point.

    Usage::

        with dbhms_telemetry.session(__title__, script_path=__file__):
            main()

    On clean exit, writes a `session_ended` record with status
    `completed`. On unhandled exception, writes status `failed` plus
    the error type, message, and traceback - then re-raises.
    """
    return _SessionCM(tool, script_path=script_path)
