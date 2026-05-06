# -*- coding: utf-8 -*-
"""Per-machine configuration for the clash detection tools.

Stored at %APPDATA%/dbHMS_clash/config.json (Windows). One file per machine,
shared by every Clash Detection pushbutton on that machine. Engineers point
this once at the firm's shared clash-data folder; everything else is
automatic from then on.

Schema (as of v1):
    {
        "schema_version":     1,
        "shared_root":        "T:\\\\_clash_data",   # path to the shared folder; None = first run
        "user_display_name":  "Nathan R.",           # optional override of Application.Username
        "warn_threshold":     2000                   # warn before loading clash sets bigger than this
    }
"""

import codecs
import json
import os


CONFIG_DIR_NAME  = "dbHMS_clash"
CONFIG_FILE_NAME = "config.json"
SCHEMA_VERSION   = 1

# Default values used when a key is missing from the on-disk config.
DEFAULTS = {
    "schema_version":    SCHEMA_VERSION,
    "shared_root":       None,
    "user_display_name": None,
    "warn_threshold":    2000,
}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _appdata_root():
    """Return %APPDATA% on Windows; ~/.config as a sane fallback elsewhere."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return appdata
    return os.path.expanduser("~/.config")


def config_dir():
    """Return %APPDATA%/dbHMS_clash, creating it if needed."""
    d = os.path.join(_appdata_root(), CONFIG_DIR_NAME)
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def config_path():
    """Return the full path to the per-machine config.json."""
    return os.path.join(config_dir(), CONFIG_FILE_NAME)


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load():
    """Read the per-machine config dict; merge over DEFAULTS; return.

    Missing or unreadable file returns the defaults (caller can detect this
    with `is_first_run()`). Keys present in the file but not in DEFAULTS are
    preserved, so adding new fields later doesn't drop existing user data.
    """
    cfg = dict(DEFAULTS)
    path = config_path()
    if os.path.isfile(path):
        try:
            with codecs.open(path, "r", "utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
        except (IOError, ValueError):
            # corrupted or unreadable file - fall through with defaults
            pass
    return cfg


def save(cfg):
    """Atomically write the per-machine config dict to disk.

    Writes to <path>.tmp then os.rename onto the real path so a Revit crash
    mid-write can't leave truncated JSON behind.
    """
    cfg = dict(cfg)  # don't mutate caller
    cfg["schema_version"] = SCHEMA_VERSION
    path = config_path()
    tmp = path + ".tmp"
    text = json.dumps(cfg, indent=2, sort_keys=True)
    with codecs.open(tmp, "w", "utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------

def shared_root():
    """Return the configured shared-folder path, or None if first-run setup hasn't happened."""
    return load().get("shared_root")


def is_first_run():
    """True if no shared_root is configured yet (first-run wizard should fire)."""
    return shared_root() is None
