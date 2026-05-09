# -*- coding: utf-8 -*-
"""Chatbot - dbHMS engineering assistant powered by the Anthropic Claude API.

v1.5 scope (this iteration):
  * Plain text chat with Claude (Sonnet 4.6 default, Opus 4.7 toggleable).
  * Streaming responses via Server-Sent Events.
  * Per-Revit-project chat *history*: a sidebar of past conversations,
    grouped by recency. New chat creates a fresh conversation without
    deleting prior ones.
  * Auto-titles each conversation from the first user message.
  * API key + system prompt configured through a Settings sub-dialog;
    key encrypted at rest via DPAPI (CurrentUser scope).
  * No Revit-side actions yet - those land in v2+ via Anthropic tool use.

Storage layout (v1.5):
  %APPDATA%\\dbHMS\\chatbot\\
      config.json                     - api key (encrypted), model, system prompt
      history\\<project_key>\\
          chat_<unix_ts>_<rand4>.json - one conversation per file
      history\\<project_key>.json.migrated
                                      - leftover from v1 single-file layout

The v1 single-file layout (history\\<project_key>.json) is migrated
lazily on first open of that project: its messages become a single
conversation in the new folder, and the old file is renamed `.migrated`
so it isn't re-imported.
"""

__title__  = 'Chat-\nbot'
__author__ = 'Nathaniel'
__doc__    = ('Ask Claude engineering questions (ASHRAE 90.1 / 62.1 / 62.2 / 55, '
              'HVAC design, controls, etc.). Conversations are saved per project '
              'and shown in the sidebar. Future: live Revit actions.')

import os
import re
import json
import time
import random
import hashlib
import traceback
from datetime import datetime

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System")
clr.AddReference("System.Security")
clr.AddReference("System.Net")

import System
from System import Convert
from System.IO import StreamReader
from System.Text import Encoding
from System.Net import (
    HttpWebRequest, WebException, WebExceptionStatus,
    ServicePointManager, SecurityProtocolType,
)
from System.Security.Cryptography import ProtectedData, DataProtectionScope
from System.Threading import Thread, ThreadStart
from System.Diagnostics import Process

from System.Windows import (
    Visibility, HorizontalAlignment, VerticalAlignment,
    Thickness, TextWrapping, TextTrimming, FontWeights,
    GridLength, GridUnitType,
)
from System.Windows.Controls import (
    Border, TextBlock, StackPanel, Grid, ColumnDefinition, RowDefinition,
    Button, Orientation,
)
from System.Windows.Media import SolidColorBrush, Color, Colors
from System.Windows.Input import Key, ModifierKeys, Keyboard, Cursors

from pyrevit import revit, forms, script

import dbhms_ui


# ---------------------------------------------------------------------------
# TLS - the Anthropic API only accepts TLS 1.2+. .NET 4.x defaults to TLS 1.0
# in some configs; force 1.2 explicitly to avoid silent connection failures.
# ---------------------------------------------------------------------------
ServicePointManager.SecurityProtocol = (
    SecurityProtocolType.Tls12 | SecurityProtocolType.Tls13
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR        = os.path.dirname(__file__)
CHATBOT_FORM_XAML = os.path.join(SCRIPT_DIR, 'ChatbotForm.xaml')
SETTINGS_XAML     = os.path.join(SCRIPT_DIR, 'SettingsForm.xaml')

API_ENDPOINT      = "https://api.anthropic.com/v1/messages"
API_VERSION       = "2023-06-01"
MAX_TOKENS        = 4096

MODELS = {
    "sonnet": {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
    "opus":   {"id": "claude-opus-4-7",   "label": "Opus 4.7"},
}
DEFAULT_MODEL_KEY = "sonnet"

HISTORY_WINDOW = 60  # cap on prior turns shipped to the API per request

DEFAULT_SYSTEM_PROMPT = (
    "You are an MEP engineering assistant for dbHMS, a mechanical / "
    "electrical / plumbing consulting firm. You help dbHMS engineers "
    "(many of whom are working in Autodesk Revit) with questions about "
    "HVAC design, plumbing, electrical, building controls, ASHRAE "
    "standards (especially 90.1, 62.1, 62.2, 55), and the ASHRAE "
    "Handbooks (Fundamentals, HVAC Applications, HVAC Systems and "
    "Equipment, Refrigeration).\n\n"
    "Style:\n"
    "  - Direct and concise. Engineers value precision over filler.\n"
    "  - Use IP units by default (the firm is US-based). Show SI in "
    "parentheses when the value matters.\n"
    "  - When citing ASHRAE values, name the standard and edition. "
    "If you are not certain of an exact table value, say so rather "
    "than guess - your training data may not match the latest edition.\n"
    "  - Show your work for calculations.\n"
    "  - If a question is ambiguous (climate zone, building type, "
    "occupancy category), ask one short clarifying question rather "
    "than assume.\n\n"
    "You are running inside Autodesk Revit via the dbHMS Extensions "
    "toolkit. In v1 you can only chat - you cannot read or modify the "
    "active Revit model. If asked to take a Revit action (create a "
    "sheet, build a schedule, export to Excel, etc.), explain the "
    "limitation and offer to help the engineer plan the action so they "
    "can do it themselves. Live Revit actions are planned for future "
    "iterations of this tool."
)


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

def _appdata_root():
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    root = os.path.join(appdata, "dbHMS", "chatbot")
    if not os.path.isdir(root):
        os.makedirs(root)
    return root


def _history_root():
    d = os.path.join(_appdata_root(), "history")
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def _project_history_dir(project_key):
    """%APPDATA%\\dbHMS\\chatbot\\history\\<project_key>\\"""
    d = os.path.join(_history_root(), project_key)
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def _config_path():
    return os.path.join(_appdata_root(), "config.json")


# ---------------------------------------------------------------------------
# DPAPI key encryption (CurrentUser scope)
# ---------------------------------------------------------------------------

def _encrypt_key(plaintext):
    if not plaintext:
        return ""
    raw = Encoding.UTF8.GetBytes(plaintext)
    enc = ProtectedData.Protect(raw, None, DataProtectionScope.CurrentUser)
    return Convert.ToBase64String(enc)


def _decrypt_key(b64):
    if not b64:
        return ""
    try:
        enc = Convert.FromBase64String(b64)
        raw = ProtectedData.Unprotect(enc, None, DataProtectionScope.CurrentUser)
        return Encoding.UTF8.GetString(raw)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Config load / save
# ---------------------------------------------------------------------------

def load_config():
    path = _config_path()
    if not os.path.isfile(path):
        return {
            "api_key": "",
            "model_key": DEFAULT_MODEL_KEY,
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
        }
    try:
        with open(path, "r") as f:
            raw = json.load(f)
    except Exception:
        return {
            "api_key": "",
            "model_key": DEFAULT_MODEL_KEY,
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
        }
    enc = raw.get("api_key_enc", "")
    api_key = _decrypt_key(enc) if enc else ""
    model_key = raw.get("model_key", DEFAULT_MODEL_KEY)
    if model_key not in MODELS:
        model_key = DEFAULT_MODEL_KEY
    system_prompt = raw.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    return {"api_key": api_key, "model_key": model_key, "system_prompt": system_prompt}


def save_config(api_key, model_key, system_prompt):
    payload = {
        "api_key_enc": _encrypt_key(api_key) if api_key else "",
        "model_key": model_key if model_key in MODELS else DEFAULT_MODEL_KEY,
        "system_prompt": system_prompt or DEFAULT_SYSTEM_PROMPT,
    }
    with open(_config_path(), "w") as f:
        json.dump(payload, f, indent=2)


# ---------------------------------------------------------------------------
# Project keying  - same logic as v1 so old single-file layouts can be
# migrated under the right project bucket.
# ---------------------------------------------------------------------------

_BASENAME_RE = re.compile(r'[^a-z0-9_-]+')


def _project_key(doc):
    if doc is None:
        return "_no_project"
    path = ""
    try:
        path = doc.PathName or ""
    except Exception:
        path = ""
    if not path:
        return "_unsaved"
    base = os.path.basename(path)
    if base.lower().endswith(".rvt"):
        base = base[:-4]
    safe = _BASENAME_RE.sub('_', base.lower()).strip('_') or "project"
    h = hashlib.md5(path.encode('utf-8')).hexdigest()[:8]
    return safe + "_" + h


def _project_label(doc):
    if doc is None:
        return "(no project open)"
    try:
        path = doc.PathName or ""
    except Exception:
        path = ""
    if not path:
        try:
            return (doc.Title or "(unsaved)") + "  - unsaved"
        except Exception:
            return "(unsaved)"
    base = os.path.basename(path)
    if base.lower().endswith(".rvt"):
        base = base[:-4]
    return base


# ---------------------------------------------------------------------------
# Conversation management
#
# A conversation is a JSON file inside the project's history folder:
#   {
#     "id":            "chat_<unix>_<rand4>",
#     "project_key":   "...",
#     "project_label": "...",
#     "title":         "auto-derived from first user message",
#     "created_at":    "YYYY-MM-DDTHH:MM:SS",
#     "updated_at":    "YYYY-MM-DDTHH:MM:SS",
#     "messages":      [{"role": "user"|"assistant", "content": "...", "ts": "..."}]
#   }
# ---------------------------------------------------------------------------

def _short_id(n=4):
    return ''.join(random.choice('0123456789abcdef') for _ in range(n))


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _new_conversation(project_key, project_label):
    return {
        "id":            "chat_{}_{}".format(int(time.time()), _short_id()),
        "project_key":   project_key,
        "project_label": project_label,
        "title":         "New chat",
        "created_at":    _now_iso(),
        "updated_at":    _now_iso(),
        "messages":      [],
    }


def _derive_title(messages):
    """Produce a sidebar-friendly title from the first user turn.
    Truncates around 50 chars at a word boundary."""
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            text = " ".join(m["content"].split())  # collapse whitespace
            if len(text) <= 50:
                return text
            cut = text[:50]
            sp = cut.rfind(" ")
            if sp > 30:
                cut = cut[:sp]
            return cut + "..."
    return "New chat"


def save_conversation(conv):
    path = os.path.join(_project_history_dir(conv["project_key"]),
                        conv["id"] + ".json")
    with open(path, "w") as f:
        json.dump(conv, f, indent=2)


def load_conversation(project_key, conv_id):
    path = os.path.join(_project_history_dir(project_key), conv_id + ".json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def delete_conversation(project_key, conv_id):
    path = os.path.join(_project_history_dir(project_key), conv_id + ".json")
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass


def list_conversations(project_key):
    """Returns conversation summaries for the project, sorted updated_at desc.

    Triggers a one-time migration from the v1 single-file layout if needed.
    """
    _migrate_old_history(project_key)
    d = _project_history_dir(project_key)
    out = []
    try:
        entries = os.listdir(d)
    except Exception:
        return out
    for fn in entries:
        if not fn.startswith("chat_") or not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), "r") as f:
                data = json.load(f)
        except Exception:
            continue
        out.append({
            "id":            data.get("id", fn[:-5]),
            "title":         data.get("title", "(untitled)"),
            "updated_at":    data.get("updated_at", ""),
            "created_at":    data.get("created_at", ""),
            "message_count": len(data.get("messages", [])),
        })
    out.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return out


def _migrate_old_history(project_key):
    """v1 stored a single file per project at history\\<project_key>.json.
    This imports it into the v1.5 per-folder layout as one conversation,
    then renames the old file to ``.migrated`` so it's never imported again.

    Idempotent and safe under partial-failure: if the new folder already
    has any chat files, skip importing (assume migration already happened)
    and just rename the old file aside.
    """
    old = os.path.join(_history_root(), project_key + ".json")
    if not os.path.isfile(old):
        return
    new_dir = _project_history_dir(project_key)
    try:
        already_migrated = any(
            fn.startswith("chat_") and fn.endswith(".json")
            for fn in os.listdir(new_dir)
        )
    except Exception:
        already_migrated = False

    if not already_migrated:
        try:
            with open(old, "r") as f:
                data = json.load(f)
            msgs = data.get("messages", [])
            if msgs:
                ts = data.get("saved_at") or _now_iso()
                conv = {
                    "id":            "chat_{}_{}".format(int(time.time()), _short_id()),
                    "project_key":   project_key,
                    "project_label": data.get("project_label", ""),
                    "title":         _derive_title(msgs),
                    "created_at":    ts,
                    "updated_at":    ts,
                    "messages":      msgs,
                }
                save_conversation(conv)
        except Exception:
            # Don't block app startup on a migration error.
            pass

    try:
        os.rename(old, old + ".migrated")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Date humanization for the sidebar
# ---------------------------------------------------------------------------

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAY_ABBR   = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def _humanize_date(iso_str):
    dt = _parse_iso(iso_str)
    if dt is None:
        return iso_str or ""
    now = datetime.now()
    days = (now.date() - dt.date()).days
    if days == 0:
        h12 = dt.hour % 12 or 12
        am  = "AM" if dt.hour < 12 else "PM"
        return "Today {}:{:02d} {}".format(h12, dt.minute, am)
    if days == 1:
        return "Yesterday"
    if days < 7:
        return _DAY_ABBR[dt.weekday()]
    if dt.year == now.year:
        return "{} {}".format(_MONTH_ABBR[dt.month - 1], dt.day)
    return "{} {}, {}".format(_MONTH_ABBR[dt.month - 1], dt.day, dt.year)


def _bucket_for(iso_str):
    """Sidebar group bucket. Returns one of the BUCKETS_ORDER strings."""
    dt = _parse_iso(iso_str)
    if dt is None:
        return "Older"
    days = (datetime.now().date() - dt.date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return "Previous 7 days"
    if days < 30:
        return "Previous 30 days"
    return "Older"


BUCKETS_ORDER = ["Today", "Yesterday", "Previous 7 days",
                 "Previous 30 days", "Older"]


# ---------------------------------------------------------------------------
# API call - streaming Server-Sent Events
# ---------------------------------------------------------------------------

class StreamCancelled(Exception):
    pass


def _build_messages(history, user_text):
    trimmed = history[-HISTORY_WINDOW:]
    out = [{"role": m["role"], "content": m["content"]} for m in trimmed]
    out.append({"role": "user", "content": user_text})
    return out


def _post_stream(api_key, model_id, system_prompt, messages,
                 on_text_delta, on_done, on_error, is_cancelled):
    try:
        body = json.dumps({
            "model":      model_id,
            "max_tokens": MAX_TOKENS,
            "stream":     True,
            "system":     system_prompt,
            "messages":   messages,
        })
        body_bytes = Encoding.UTF8.GetBytes(body)

        req = HttpWebRequest.Create(API_ENDPOINT)
        req.Method = "POST"
        req.ContentType = "application/json"
        req.Headers.Add("x-api-key", api_key)
        req.Headers.Add("anthropic-version", API_VERSION)
        req.Timeout          = 60 * 1000
        req.ReadWriteTimeout = 10 * 60 * 1000
        req.ContentLength    = body_bytes.Length

        rs = req.GetRequestStream()
        try:
            rs.Write(body_bytes, 0, body_bytes.Length)
        finally:
            rs.Close()

        try:
            resp = req.GetResponse()
        except WebException as we:
            _handle_web_exception(we, on_error)
            return

        usage = {"input_tokens": 0, "output_tokens": 0}
        try:
            stream = resp.GetResponseStream()
            reader = StreamReader(stream, Encoding.UTF8)
            try:
                while True:
                    if is_cancelled():
                        raise StreamCancelled()
                    line = reader.ReadLine()
                    if line is None:
                        break
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        evt = json.loads(data_str)
                    except Exception:
                        continue

                    etype = evt.get("type")
                    if etype == "content_block_delta":
                        delta = evt.get("delta", {})
                        if delta.get("type") == "text_delta":
                            txt = delta.get("text", "")
                            if txt:
                                on_text_delta(txt)
                    elif etype == "message_start":
                        u = evt.get("message", {}).get("usage", {})
                        usage["input_tokens"]  = u.get("input_tokens", 0)
                        usage["output_tokens"] = u.get("output_tokens", 0)
                    elif etype == "message_delta":
                        u = evt.get("usage", {})
                        if "output_tokens" in u:
                            usage["output_tokens"] = u["output_tokens"]
                    elif etype == "error":
                        err = evt.get("error", {})
                        on_error("api", err.get("message", "Unknown error"))
                        return
            finally:
                reader.Close()
                stream.Close()
        finally:
            resp.Close()

        on_done(usage)

    except StreamCancelled:
        pass
    except WebException as we:
        _handle_web_exception(we, on_error)
    except Exception as e:
        on_error("internal", "{}: {}".format(type(e).__name__, str(e)))


def _handle_web_exception(we, on_error):
    if we.Response is None:
        if we.Status == WebExceptionStatus.RequestCanceled:
            return
        on_error("network", we.Message or "Network error")
        return
    status = 0
    try:
        status = int(we.Response.StatusCode)
    except Exception:
        pass
    try:
        rs = we.Response.GetResponseStream()
        body = StreamReader(rs).ReadToEnd()
    except Exception:
        body = ""
    msg = ""
    try:
        parsed = json.loads(body)
        msg = parsed.get("error", {}).get("message", "") or body
    except Exception:
        msg = body

    if status == 401:
        on_error("auth", "API key was rejected. Check Settings -> API Key.")
    elif status == 429:
        on_error("rate_limit",
                 "Rate-limited by the Anthropic API. " + (msg or ""))
    elif status == 529:
        on_error("overloaded",
                 "Anthropic is overloaded. Wait a moment and try again.")
    else:
        on_error("api",
                 "HTTP {}: {}".format(status, msg or "API request failed"))


# ---------------------------------------------------------------------------
# Settings sub-dialog (unchanged from v1)
# ---------------------------------------------------------------------------

class SettingsForm(forms.WPFWindow):
    def __init__(self, current_config):
        forms.WPFWindow.__init__(self, SETTINGS_XAML)
        self._loaded_key   = current_config.get("api_key", "")
        self._key_revealed = False
        self._result       = None

        self.pwd_api_key.Password         = self._loaded_key
        self.txt_api_key_visible.Text     = self._loaded_key
        self.txt_key_hint.Text = _key_hint(self._loaded_key)

        if current_config.get("model_key") == "opus":
            self.rad_model_opus.IsChecked = True
        else:
            self.rad_model_sonnet.IsChecked = True

        self.txt_system_prompt.Text = current_config.get(
            "system_prompt", DEFAULT_SYSTEM_PROMPT)

        self.run_config_path.Text  = _config_path()
        self.run_history_path.Text = _history_root()

        self.btn_save.Click          += self._on_save
        self.btn_cancel.Click        += self._on_cancel
        self.btn_show_key.Click      += self._on_toggle_show_key
        self.btn_reset_prompt.Click  += self._on_reset_prompt
        self.btn_open_folder.Click   += self._on_open_folder

    def _on_save(self, sender, args):
        if self._key_revealed:
            api_key = (self.txt_api_key_visible.Text or "").strip()
        else:
            api_key = (self.pwd_api_key.Password or "").strip()

        if api_key and not _looks_like_anthropic_key(api_key):
            self.bnr_key_error.Visibility = Visibility.Visible
            self.txt_key_error.Text = (
                "That doesn't look like an Anthropic API key (expected "
                "something starting with 'sk-ant-'). Save anyway?  "
                "Click Save again to confirm.")
            if not getattr(self, "_save_confirmed", False):
                self._save_confirmed = True
                return
        self._save_confirmed = False

        model_key = "opus" if self.rad_model_opus.IsChecked else "sonnet"
        system_prompt = (self.txt_system_prompt.Text or "").strip() or DEFAULT_SYSTEM_PROMPT

        try:
            save_config(api_key, model_key, system_prompt)
        except Exception as e:
            self.bnr_key_error.Visibility = Visibility.Visible
            self.txt_key_error.Text = "Could not save config: {}".format(e)
            return

        self._result = {
            "api_key":       api_key,
            "model_key":     model_key,
            "system_prompt": system_prompt,
        }
        self.Close()

    def _on_cancel(self, sender, args):
        self._result = None
        self.Close()

    def _on_toggle_show_key(self, sender, args):
        if self._key_revealed:
            self.pwd_api_key.Password         = self.txt_api_key_visible.Text or ""
            self.txt_api_key_visible.Visibility = Visibility.Collapsed
            self.pwd_api_key.Visibility         = Visibility.Visible
            self.btn_show_key.Content = "Show"
            self._key_revealed = False
        else:
            self.txt_api_key_visible.Text     = self.pwd_api_key.Password or ""
            self.pwd_api_key.Visibility         = Visibility.Collapsed
            self.txt_api_key_visible.Visibility = Visibility.Visible
            self.btn_show_key.Content = "Hide"
            self._key_revealed = True

    def _on_reset_prompt(self, sender, args):
        self.txt_system_prompt.Text = DEFAULT_SYSTEM_PROMPT

    def _on_open_folder(self, sender, args):
        try:
            Process.Start("explorer.exe", _appdata_root())
        except Exception:
            pass

    def show_modal(self):
        self.ShowDialog()
        return self._result


def _looks_like_anthropic_key(s):
    return s.startswith("sk-ant-") and len(s) > 20


def _key_hint(api_key):
    if not api_key:
        return "No key configured."
    if len(api_key) <= 12:
        return "Key configured (short)."
    return "Key configured: {} ... {}  ({} chars)".format(
        api_key[:8], api_key[-4:], len(api_key))


# ---------------------------------------------------------------------------
# Brushes built once and reused.
# ---------------------------------------------------------------------------

_BRUSH_USER_BG    = SolidColorBrush(Color.FromRgb( 43, 108, 176))  # #2B6CB0
_BRUSH_USER_FG    = SolidColorBrush(Colors.White)
_BRUSH_BOT_BG     = SolidColorBrush(Colors.White)
_BRUSH_BOT_BORDER = SolidColorBrush(Color.FromRgb(226, 232, 240))  # #E2E8F0
_BRUSH_BOT_FG     = SolidColorBrush(Color.FromRgb( 45,  55,  72))  # #2D3748
_BRUSH_AI_ACCENT  = SolidColorBrush(Color.FromRgb(107,  70, 193))  # #6B46C1
_BRUSH_BOT_LABEL  = SolidColorBrush(Color.FromRgb(113, 128, 150))  # #718096
_BRUSH_ERROR_BG   = SolidColorBrush(Color.FromRgb(255, 245, 245))  # #FFF5F5
_BRUSH_ERROR_BORD = SolidColorBrush(Color.FromRgb(252, 129, 129))  # #FC8181
_BRUSH_ERROR_FG   = SolidColorBrush(Color.FromRgb(116,  42,  42))  # #742A2A

_BRUSH_TRANSPARENT  = SolidColorBrush(Colors.Transparent)
_BRUSH_ROW_HOVER    = SolidColorBrush(Color.FromRgb(247, 250, 252))  # #F7FAFC
_BRUSH_ROW_ACTIVE   = SolidColorBrush(Color.FromRgb(235, 248, 255))  # #EBF8FF
_BRUSH_ROW_ACCENT   = SolidColorBrush(Color.FromRgb( 49, 130, 206))  # #3182CE
_BRUSH_ROW_TITLE    = SolidColorBrush(Color.FromRgb( 45,  55,  72))  # #2D3748
_BRUSH_ROW_DATE     = SolidColorBrush(Color.FromRgb(160, 174, 192))  # #A0AEC0
_BRUSH_ROW_DEL      = SolidColorBrush(Color.FromRgb(160, 174, 192))  # #A0AEC0
_BRUSH_ROW_DEL_HOT  = SolidColorBrush(Color.FromRgb(229,  62,  62))  # #E53E3E
_BRUSH_BUCKET_HEAD  = SolidColorBrush(Color.FromRgb(160, 174, 192))  # #A0AEC0


def _is_descendant_of_button(elem):
    """Walk up the logical tree; True if `elem` is a Button or inside one."""
    while elem is not None:
        if isinstance(elem, Button):
            return True
        try:
            parent = elem.Parent
        except Exception:
            return False
        if parent is elem:
            return False
        elem = parent
    return False


# ---------------------------------------------------------------------------
# Main chat form
# ---------------------------------------------------------------------------

class ChatbotForm(forms.WPFWindow):

    def __init__(self):
        forms.WPFWindow.__init__(self, CHATBOT_FORM_XAML)

        # Resolve Revit doc -> per-project keying.
        try:
            self._doc = revit.doc
        except Exception:
            self._doc = None
        self._project_key   = _project_key(self._doc)
        self._project_label = _project_label(self._doc)

        # Config + conversation list.
        self._config        = load_config()
        self._conversations = list_conversations(self._project_key)
        self._current_conv  = None

        # Streaming state.
        self._cancelled            = False
        self._streaming_buffer     = ""
        self._streaming_text_block = None
        self._streaming_bubble     = None

        # Sidebar collapse state. Remembers the user's resize so toggling
        # back to expanded restores the prior width rather than snapping
        # to the default.
        self._sidebar_collapsed   = False
        self._sidebar_last_width  = 260.0

        # Static UI.
        self.Title = "Chatbot - " + self._project_label
        self.txt_project_name.Text = self._project_label
        self._refresh_model_label()
        self._refresh_key_banner()

        # Pick initial conversation: most recent if any, else fresh.
        if self._conversations:
            initial_id = self._conversations[0]["id"]
            conv = load_conversation(self._project_key, initial_id)
            if conv is not None:
                self._current_conv = conv
        if self._current_conv is None:
            self._current_conv = _new_conversation(
                self._project_key, self._project_label)
            save_conversation(self._current_conv)
            self._conversations.insert(0, _summarize_conv(self._current_conv))

        self._render_chat_pane()
        self._render_sidebar()
        self._refresh_chat_title()
        self._refresh_msg_count()

        # Wire events.
        self.btn_send.Click                          += self._on_send
        self.btn_stop.Click                          += self._on_stop
        self.btn_settings.Click                      += self._on_open_settings
        self.btn_open_settings_from_banner.Click     += self._on_open_settings
        self.btn_new_chat.Click                      += self._on_new_chat
        self.btn_toggle_sidebar.Click                += self._on_toggle_sidebar
        self.txt_input.PreviewKeyDown                += self._on_input_keydown

        self.Loaded += self._on_loaded

    # -----------------------------------------------------------------
    # Init / focus
    # -----------------------------------------------------------------

    def _on_loaded(self, sender, args):
        self.txt_input.Focus()
        self._scroll_to_bottom()

    # -----------------------------------------------------------------
    # UI helpers
    # -----------------------------------------------------------------

    def _refresh_msg_count(self):
        n = len(self._conversations)
        self.txt_msg_count.Text = "{} chat{}".format(n, "" if n == 1 else "s")

    def _refresh_model_label(self):
        info = MODELS.get(self._config["model_key"], MODELS[DEFAULT_MODEL_KEY])
        self.txt_model_label.Text = info["label"]

    def _refresh_key_banner(self):
        if self._config.get("api_key"):
            self.bnr_no_key.Visibility = Visibility.Collapsed
        else:
            self.bnr_no_key.Visibility = Visibility.Visible

    def _refresh_chat_title(self):
        if not self._current_conv:
            self.txt_chat_title.Text = "New chat"
            self.txt_chat_subtitle.Text = ""
            return
        title = self._current_conv.get("title") or "New chat"
        self.txt_chat_title.Text = title
        msgs = self._current_conv.get("messages", [])
        if not msgs:
            self.txt_chat_subtitle.Text = "No messages yet."
        else:
            n = len(msgs)
            self.txt_chat_subtitle.Text = "{} message{}  *  {}".format(
                n, "" if n == 1 else "s",
                _humanize_date(self._current_conv.get("updated_at")))

    def _hide_empty_state(self):
        try:
            self.bnr_empty_state.Visibility = Visibility.Collapsed
        except Exception:
            pass

    def _show_empty_state(self):
        try:
            self.bnr_empty_state.Visibility = Visibility.Visible
        except Exception:
            pass

    def _scroll_to_bottom(self):
        try:
            self.scroll_chat.ScrollToBottom()
        except Exception:
            pass

    def _set_status(self, text):
        self.txt_status.Text = text or ""

    def _set_streaming_ui(self, streaming):
        if streaming:
            self.btn_send.IsEnabled  = False
            self.txt_input.IsEnabled = False
            self.btn_stop.Visibility = Visibility.Visible
            self.btn_new_chat.IsEnabled = False
        else:
            self.btn_send.IsEnabled  = True
            self.txt_input.IsEnabled = True
            self.btn_stop.Visibility = Visibility.Collapsed
            self.btn_new_chat.IsEnabled = True

    def _on_toggle_sidebar(self, sender, args):
        if self._sidebar_collapsed:
            # Expand: restore the remembered width.
            self.col_sidebar.MinWidth = 200
            self.col_sidebar.Width = GridLength(self._sidebar_last_width or 260.0)
            self.btn_toggle_sidebar.Content = u"❮"   # left chevron
            self.btn_toggle_sidebar.ToolTip = "Hide sidebar"
            self._sidebar_collapsed = False
        else:
            # Collapse: snapshot the current width so we can restore it later.
            try:
                cur = float(self.col_sidebar.ActualWidth)
                if cur > 0:
                    self._sidebar_last_width = cur
            except Exception:
                pass
            # MinWidth has to drop to 0 too, or the column won't shrink past it.
            self.col_sidebar.MinWidth = 0
            self.col_sidebar.Width = GridLength(0)
            self.btn_toggle_sidebar.Content = u"❯"   # right chevron
            self.btn_toggle_sidebar.ToolTip = "Show sidebar"
            self._sidebar_collapsed = True

    # -----------------------------------------------------------------
    # Sidebar render
    # -----------------------------------------------------------------

    def _render_sidebar(self):
        self.pnl_conversations.Children.Clear()

        if not self._conversations:
            tb = TextBlock()
            tb.Text = "No saved chats yet for this project."
            tb.Foreground = _BRUSH_BOT_LABEL
            tb.FontSize = 11
            tb.TextWrapping = TextWrapping.Wrap
            tb.TextAlignment = System.Windows.TextAlignment.Center
            tb.Margin = Thickness(8, 16, 8, 0)
            self.pnl_conversations.Children.Add(tb)
            return

        active_id = self._current_conv["id"] if self._current_conv else None

        # Group by recency bucket. self._conversations is already
        # sorted updated_at desc.
        last_bucket = None
        for summary in self._conversations:
            bucket = _bucket_for(summary.get("updated_at", ""))
            if bucket != last_bucket:
                self.pnl_conversations.Children.Add(
                    self._make_bucket_header(bucket))
                last_bucket = bucket
            self.pnl_conversations.Children.Add(
                self._make_conv_row(summary, summary["id"] == active_id))

    def _make_bucket_header(self, label):
        tb = TextBlock()
        tb.Text       = label.upper()
        tb.FontSize   = 10
        tb.FontWeight = FontWeights.SemiBold
        tb.Foreground = _BRUSH_BUCKET_HEAD
        tb.Margin     = Thickness(8, 12, 0, 4)
        return tb

    def _make_conv_row(self, summary, is_active):
        border = Border()
        border.Padding      = Thickness(10, 6, 6, 6)
        border.Margin       = Thickness(0, 0, 0, 2)
        border.CornerRadius = System.Windows.CornerRadius(4)
        border.Cursor       = Cursors.Hand

        if is_active:
            border.Background      = _BRUSH_ROW_ACTIVE
            border.BorderBrush     = _BRUSH_ROW_ACCENT
            border.BorderThickness = Thickness(2, 0, 0, 0)
        else:
            border.Background = _BRUSH_TRANSPARENT

        # 2-col Grid: text stack | delete button
        grid = Grid()
        c1 = ColumnDefinition(); c1.Width = GridLength(1, GridUnitType.Star)
        c2 = ColumnDefinition(); c2.Width = GridLength.Auto
        grid.ColumnDefinitions.Add(c1)
        grid.ColumnDefinitions.Add(c2)

        sp = StackPanel()
        Grid.SetColumn(sp, 0)

        title_tb = TextBlock()
        title_tb.Text         = summary.get("title", "(untitled)")
        title_tb.FontSize     = 12
        title_tb.Foreground   = _BRUSH_ROW_TITLE
        title_tb.TextTrimming = TextTrimming.CharacterEllipsis
        title_tb.FontWeight   = FontWeights.SemiBold if is_active else FontWeights.Normal
        sp.Children.Add(title_tb)

        date_tb = TextBlock()
        date_tb.Text         = _humanize_date(summary.get("updated_at", ""))
        date_tb.FontSize     = 10
        date_tb.Foreground   = _BRUSH_ROW_DATE
        date_tb.TextTrimming = TextTrimming.CharacterEllipsis
        date_tb.Margin       = Thickness(0, 1, 0, 0)
        sp.Children.Add(date_tb)

        grid.Children.Add(sp)

        del_btn = Button()
        Grid.SetColumn(del_btn, 1)
        del_btn.Content         = "x"
        del_btn.Width           = 22
        del_btn.Height          = 22
        del_btn.Padding         = Thickness(0)
        del_btn.FontSize        = 12
        del_btn.FontWeight      = FontWeights.Bold
        del_btn.Background      = _BRUSH_TRANSPARENT
        del_btn.BorderThickness = Thickness(0)
        del_btn.Foreground      = _BRUSH_ROW_DEL
        del_btn.Cursor          = Cursors.Hand
        del_btn.ToolTip         = "Delete this chat"
        cid_for_del = summary["id"]
        del_btn.Click += (lambda s, a, c=cid_for_del: self._on_delete_conv(c))
        del_btn.MouseEnter += (lambda s, a, b=del_btn:
                               setattr(b, "Foreground", _BRUSH_ROW_DEL_HOT))
        del_btn.MouseLeave += (lambda s, a, b=del_btn:
                               setattr(b, "Foreground", _BRUSH_ROW_DEL))
        grid.Children.Add(del_btn)

        border.Child = grid

        cid_for_click = summary["id"]
        border.MouseLeftButtonDown += (
            lambda s, a, c=cid_for_click: self._on_row_click(s, a, c))
        if not is_active:
            border.MouseEnter += (lambda s, a, b=border:
                                  setattr(b, "Background", _BRUSH_ROW_HOVER))
            border.MouseLeave += (lambda s, a, b=border:
                                  setattr(b, "Background", _BRUSH_TRANSPARENT))
        return border

    # -----------------------------------------------------------------
    # Conversation switching
    # -----------------------------------------------------------------

    def _on_row_click(self, sender, args, conv_id):
        # Don't switch if the click landed on the delete button.
        if _is_descendant_of_button(args.OriginalSource):
            return
        self._switch_to_conversation(conv_id)

    def _switch_to_conversation(self, conv_id):
        if self._current_conv and self._current_conv["id"] == conv_id:
            return

        # Auto-prune the current chat if it's empty - clutter prevention.
        self._prune_if_empty(self._current_conv)

        conv = load_conversation(self._project_key, conv_id)
        if conv is None:
            self._conversations = [
                c for c in self._conversations if c["id"] != conv_id]
            self._render_sidebar()
            self._refresh_msg_count()
            return

        self._current_conv = conv
        self._render_chat_pane()
        self._render_sidebar()
        self._refresh_chat_title()
        self._refresh_msg_count()

    def _prune_if_empty(self, conv):
        """Delete a conversation from disk + the summary list if it has
        no messages. Returns True if pruning happened."""
        if conv is None:
            return False
        if conv.get("messages"):
            return False
        cid = conv.get("id")
        if not cid:
            return False
        delete_conversation(self._project_key, cid)
        self._conversations = [c for c in self._conversations if c["id"] != cid]
        return True

    def _on_new_chat(self, sender, args):
        # If we're already in an empty chat, just signal that.
        if (self._current_conv
                and not self._current_conv.get("messages")):
            self._set_status("You're already in a new chat.")
            self.txt_input.Focus()
            return

        # Prune any empty current and create a fresh conversation.
        self._prune_if_empty(self._current_conv)

        new = _new_conversation(self._project_key, self._project_label)
        save_conversation(new)
        self._current_conv = new
        self._conversations.insert(0, _summarize_conv(new))

        self._render_chat_pane()
        self._render_sidebar()
        self._refresh_chat_title()
        self._refresh_msg_count()
        self._set_status("New chat started.")
        self.txt_input.Focus()

    def _on_delete_conv(self, conv_id):
        summary = next(
            (c for c in self._conversations if c["id"] == conv_id), None)
        title = summary["title"] if summary else conv_id
        ans = forms.alert(
            "Delete chat \"{}\"?\n\nThis cannot be undone.".format(title),
            title="Delete chat", yes=True, no=True)
        if not ans:
            return

        delete_conversation(self._project_key, conv_id)
        self._conversations = [
            c for c in self._conversations if c["id"] != conv_id]

        # If we deleted the active chat, pick another or create a new one.
        if self._current_conv and self._current_conv["id"] == conv_id:
            if self._conversations:
                next_id = self._conversations[0]["id"]
                conv = load_conversation(self._project_key, next_id)
                if conv is not None:
                    self._current_conv = conv
                else:
                    self._current_conv = None
            else:
                self._current_conv = None

            if self._current_conv is None:
                # No other chats - start a fresh empty one.
                self._current_conv = _new_conversation(
                    self._project_key, self._project_label)
                save_conversation(self._current_conv)
                self._conversations.insert(0, _summarize_conv(self._current_conv))
            self._render_chat_pane()

        self._render_sidebar()
        self._refresh_chat_title()
        self._refresh_msg_count()

    # -----------------------------------------------------------------
    # Chat pane render
    # -----------------------------------------------------------------

    def _render_chat_pane(self):
        # Clear all message bubbles, keep the empty-state border.
        kept = [c for c in list(self.pnl_messages.Children)
                if c == self.bnr_empty_state]
        self.pnl_messages.Children.Clear()
        for c in kept:
            self.pnl_messages.Children.Add(c)

        msgs = self._current_conv.get("messages", []) if self._current_conv else []
        if msgs:
            self._hide_empty_state()
            for m in msgs:
                self._add_message_bubble(
                    m["role"], m.get("content", ""), persist=False)
        else:
            self._show_empty_state()

        self._scroll_to_bottom()

    # -----------------------------------------------------------------
    # Bubble construction
    # -----------------------------------------------------------------

    def _add_message_bubble(self, role, text, persist=True):
        self._hide_empty_state()

        bubble = Border()
        bubble.CornerRadius = System.Windows.CornerRadius(8)
        bubble.Padding      = Thickness(12, 8, 12, 8)
        bubble.Margin       = Thickness(0, 0, 0, 10)
        bubble.MaxWidth     = 760

        tb = TextBlock()
        tb.Text         = text or ""
        tb.TextWrapping = TextWrapping.Wrap
        tb.FontSize     = 13

        if role == "user":
            bubble.Background          = _BRUSH_USER_BG
            tb.Foreground              = _BRUSH_USER_FG
            bubble.HorizontalAlignment = HorizontalAlignment.Right
            bubble.Child = tb
        else:
            bubble.Background          = _BRUSH_BOT_BG
            bubble.BorderBrush         = _BRUSH_BOT_BORDER
            bubble.BorderThickness     = Thickness(1)
            bubble.HorizontalAlignment = HorizontalAlignment.Left
            tb.Foreground              = _BRUSH_BOT_FG

            inner = StackPanel()

            label_row = StackPanel()
            label_row.Orientation = Orientation.Horizontal
            label_row.Margin      = Thickness(0, 0, 0, 4)

            sparkle = TextBlock()
            sparkle.Text       = u"✨"
            sparkle.Foreground = _BRUSH_AI_ACCENT
            sparkle.FontSize   = 12
            sparkle.Margin     = Thickness(0, 0, 4, 0)
            label_row.Children.Add(sparkle)

            label = TextBlock()
            label.Text       = "Assistant"
            label.Foreground = _BRUSH_BOT_LABEL
            label.FontSize   = 10
            label.FontWeight = FontWeights.SemiBold
            label_row.Children.Add(label)

            inner.Children.Add(label_row)
            inner.Children.Add(tb)
            bubble.Child = inner

        self.pnl_messages.Children.Add(bubble)

        if persist:
            self._current_conv["messages"].append({
                "role":    role,
                "content": text,
                "ts":      _now_iso(),
            })
            self._save_current_conv_safe()

        self._scroll_to_bottom()
        return bubble, tb

    def _add_error_bubble(self, label, detail):
        self._hide_empty_state()

        bubble = Border()
        bubble.Background          = _BRUSH_ERROR_BG
        bubble.BorderBrush         = _BRUSH_ERROR_BORD
        bubble.BorderThickness     = Thickness(1)
        bubble.CornerRadius        = System.Windows.CornerRadius(4)
        bubble.Padding             = Thickness(12, 8, 12, 8)
        bubble.Margin              = Thickness(0, 0, 0, 10)
        bubble.MaxWidth            = 760
        bubble.HorizontalAlignment = HorizontalAlignment.Left

        sp = StackPanel()
        head = TextBlock()
        head.Text       = label
        head.Foreground = _BRUSH_ERROR_FG
        head.FontWeight = FontWeights.SemiBold
        head.FontSize   = 12
        sp.Children.Add(head)

        body = TextBlock()
        body.Text         = detail
        body.Foreground   = _BRUSH_ERROR_FG
        body.FontSize     = 11
        body.TextWrapping = TextWrapping.Wrap
        body.Margin       = Thickness(0, 4, 0, 0)
        sp.Children.Add(body)

        bubble.Child = sp
        self.pnl_messages.Children.Add(bubble)
        self._scroll_to_bottom()

    # -----------------------------------------------------------------
    # Input keyboard handling
    # -----------------------------------------------------------------

    def _on_input_keydown(self, sender, args):
        if args.Key == Key.Enter or args.Key == Key.Return:
            shift_held = (Keyboard.Modifiers & ModifierKeys.Shift) == ModifierKeys.Shift
            if not shift_held:
                args.Handled = True
                self._do_send()

    def _on_send(self, sender, args):
        self._do_send()

    def _do_send(self):
        if not self._config.get("api_key"):
            dbhms_ui.info(
                "No API key configured. Click Settings to paste one in.",
                title="Chatbot")
            return

        user_text = (self.txt_input.Text or "").strip()
        if not user_text:
            return

        self.txt_input.Text = ""

        was_empty = not self._current_conv.get("messages")
        self._add_message_bubble("user", user_text, persist=True)

        # First user message? Auto-derive title and re-render the sidebar.
        if was_empty:
            self._current_conv["title"] = _derive_title(self._current_conv["messages"])
            self._save_current_conv_safe()
            for c in self._conversations:
                if c["id"] == self._current_conv["id"]:
                    c["title"]         = self._current_conv["title"]
                    c["updated_at"]    = self._current_conv["updated_at"]
                    c["message_count"] = len(self._current_conv["messages"])
                    break
            self._refresh_chat_title()
            self._render_sidebar()

        # Empty assistant bubble to stream into.
        bubble, tb = self._add_message_bubble("assistant", "", persist=False)
        self._streaming_bubble     = bubble
        self._streaming_text_block = tb
        self._streaming_buffer     = ""

        self._cancelled = False
        self._set_streaming_ui(True)
        self._set_status("Asking " + MODELS[self._config["model_key"]]["label"] + "...")

        api_key       = self._config["api_key"]
        model_id      = MODELS[self._config["model_key"]]["id"]
        system_prompt = self._config["system_prompt"] or DEFAULT_SYSTEM_PROMPT
        # Build messages array from prior turns + the new user_text.
        prior = self._current_conv["messages"][:-1]  # exclude the user msg we just appended
        messages = _build_messages(prior, user_text)

        def worker():
            _post_stream(
                api_key, model_id, system_prompt, messages,
                on_text_delta = self._dispatch_text_delta,
                on_done       = self._dispatch_done,
                on_error      = self._dispatch_error,
                is_cancelled  = lambda: self._cancelled,
            )

        t = Thread(ThreadStart(worker))
        t.IsBackground = True
        t.Start()

    def _on_stop(self, sender, args):
        self._cancelled = True
        self._set_status("Stopping...")

    # -----------------------------------------------------------------
    # Worker-thread -> UI marshalling
    # -----------------------------------------------------------------

    def _dispatch_text_delta(self, text):
        self.Dispatcher.BeginInvoke(
            System.Action(lambda t=text: self._on_text_delta_ui(t)))

    def _on_text_delta_ui(self, text):
        if self._cancelled:
            return
        self._streaming_buffer += text
        if self._streaming_text_block is not None:
            self._streaming_text_block.Text = self._streaming_buffer
        self._scroll_to_bottom()

    def _dispatch_done(self, usage):
        self.Dispatcher.BeginInvoke(
            System.Action(lambda u=usage: self._on_done_ui(u)))

    def _on_done_ui(self, usage):
        full_text = self._streaming_buffer or ""
        self._current_conv["messages"].append({
            "role":    "assistant",
            "content": full_text,
            "ts":      _now_iso(),
            "usage":   usage,
        })
        self._save_current_conv_safe()
        # Update summary in conversations list and re-sort/render so the
        # active chat moves to the top of "Today".
        for c in self._conversations:
            if c["id"] == self._current_conv["id"]:
                c["updated_at"]    = self._current_conv["updated_at"]
                c["message_count"] = len(self._current_conv["messages"])
                break
        self._conversations.sort(
            key=lambda c: c.get("updated_at", ""), reverse=True)
        self._render_sidebar()
        self._refresh_chat_title()

        self._streaming_bubble     = None
        self._streaming_text_block = None
        self._streaming_buffer     = ""

        self._set_streaming_ui(False)
        self._set_status("")
        self.txt_input.Focus()

    def _dispatch_error(self, label, detail):
        self.Dispatcher.BeginInvoke(
            System.Action(lambda l=label, d=detail: self._on_error_ui(l, d)))

    def _on_error_ui(self, label, detail):
        # Drop the empty assistant bubble (if any).
        if self._streaming_bubble is not None and not self._streaming_buffer:
            try:
                self.pnl_messages.Children.Remove(self._streaming_bubble)
            except Exception:
                pass
        self._streaming_bubble     = None
        self._streaming_text_block = None
        self._streaming_buffer     = ""

        nice = {
            "auth":       "Authentication failed",
            "rate_limit": "Rate-limited",
            "overloaded": "Anthropic overloaded",
            "network":    "Network error",
            "api":        "API error",
            "internal":   "Internal error",
        }.get(label, "Error")
        self._add_error_bubble(nice, detail or "")

        self._set_streaming_ui(False)
        self._set_status("")
        self.txt_input.Focus()

    # -----------------------------------------------------------------
    # Settings + Save with error swallow
    # -----------------------------------------------------------------

    def _on_open_settings(self, sender, args):
        dlg = SettingsForm(self._config)
        dlg.Owner = self
        result = dlg.show_modal()
        if result is None:
            return
        self._config.update(result)
        self._refresh_model_label()
        self._refresh_key_banner()
        self._set_status("Settings saved.")

    def _save_current_conv_safe(self):
        if self._current_conv is None:
            return
        self._current_conv["updated_at"] = _now_iso()
        try:
            save_conversation(self._current_conv)
        except Exception:
            try:
                script.get_logger().error(
                    "Failed to save conversation:\n" + traceback.format_exc())
            except Exception:
                pass


def _summarize_conv(conv):
    return {
        "id":            conv["id"],
        "title":         conv.get("title", "(untitled)"),
        "updated_at":    conv.get("updated_at", ""),
        "created_at":    conv.get("created_at", ""),
        "message_count": len(conv.get("messages", [])),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ChatbotForm().ShowDialog()


main()
