# -*- coding: utf-8 -*-
"""dbhms_telemetry - usage tracking for the dbHMS Revit Extensions.

Records one JSON-Lines event per tool invocation to a shared firm
folder (and a local fallback cache) so usage can be analyzed later:
who ran what tool, when it started, when it ended, how long it took,
which Revit document was open, and whether it errored.

Why this is a shared lib (and not duplicated per pushbutton):
    The repo's CLAUDE.md says tools generally don't share helpers, on
    purpose - each pyRevit pushbutton ships standalone. This module is
    the third documented exception (alongside `clash_*` for clash
    detection and `dbhms_ui` for popups): EVERY tool needs the same
    telemetry shape, and duplicating the recorder + path logic across
    a dozen pushbuttons would make a "change the storage path" tweak
    a 12-file edit. Lives in `lib/` (auto-added to `sys.path` by
    pyRevit) and is scoped under the `dbhms_telemetry` namespace so
    other tools' scripts can ignore it. See CLAUDE.md for the rule.

Public API:
    dbhms_telemetry.session(tool_name, script_path=None)
        - Context manager. Use this for tools whose entry point is a
          modal `ShowDialog()` or a `main()` call - the session ends
          when the `with` block exits.

            with dbhms_telemetry.session(__title__):
                main()

    dbhms_telemetry.start(tool_name, script_path=None) -> Session
    dbhms_telemetry.end(session, status='completed', error=None)
        - Lower-level pair for tools whose entry point is a modeless
          `Show()` (e.g. Clash Detection's Walkthrough). Call
          `start()` near the top, then hook `Window.Closed` to call
          `end()` with the real shutdown time.

Storage:
    Events append to `<NETWORK_ROOT>/<YYYY>/<MM>/<YYYY-MM-DD>_<USER>.jsonl`
    AND to `<LOCAL_ROOT>/<YYYY>/<MM>/<YYYY-MM-DD>_<USER>.jsonl`. One
    file per user per day so concurrent writers never contend. The
    local copy is a fallback for when H: is unmapped or offline.

    Override either path with environment variables for testing:
        DBHMS_TELEMETRY_NETWORK_ROOT  - default: H:\\TOOLS\\REVIT\\dbHMS Custom Extensions\\Data Traceback
        DBHMS_TELEMETRY_LOCAL_ROOT    - default: %LOCALAPPDATA%\\dbhms_telemetry

Failure mode:
    Telemetry NEVER affects the tool. Every I/O path is wrapped in a
    blanket try/except so a missing network drive, locked file, or
    permission error is silently swallowed.

Imports are pure-Python at module top so the CPython 3 test suite
parses this module without needing pyRevit / Revit / .NET.
"""

from .telemetry import (  # noqa: F401
    session,
    start,
    end,
    network_root,
    local_root,
    SCHEMA_VERSION,
)
