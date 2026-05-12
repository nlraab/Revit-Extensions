# -*- coding: utf-8 -*-
"""Chatbot - dbHMS engineering assistant powered by Anthropic Claude.

v2.0 scope (this iteration): "Eyes on the model"
  * Adds Anthropic *tool use* so Claude becomes an agent that can read the
    active Revit document. Seven read-only tools to start:
        get_document_info, list_views, list_sheets, list_schedules,
        get_selection, count_by_category, get_element_details
  * Each tool runs on Revit's UI thread (where the API context lives)
    via Dispatcher.Invoke from the streaming worker.
  * Agent loop: stream -> if stop_reason="tool_use", execute tools,
    POST again with tool_result blocks, repeat until end_turn.
  * UI shows a small "tool call" bubble per tool invocation, updated
    with a brief result summary once the tool completes.

Lineage:
  v1   - chat-only (Sonnet/Opus), per-Revit-project history file.
  v1.5 - sidebar of past conversations + auto-titling.
  v2.0 - read-only Revit tool use (this).
  v2.5 - modeless WPF + ExternalEvent (lets the user click around in
         Revit while chatting; also a hard prereq for write tools).
  v3.0 - write tools (create_sheet, place_view_on_sheet, etc.) with
         confirmation/preview dialogs for batches and deletes.
  v3.5 - schedule + view creation, view template apply.
  v4.0 - exports: schedule->Excel, sheets->PDF, views->DWG.

Storage layout (unchanged from v1.5):
  %APPDATA%\\dbHMS\\chatbot\\
      config.json                     - api key (DPAPI), model, system prompt
      history\\<project_key>\\
          chat_<unix_ts>_<rand4>.json - one conversation per file

Conversation message format (extended in v2.0):
    "content" can be a plain string (legacy) OR a list of content blocks:
        text:        {"type": "text", "text": "..."}
        tool_use:    {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
        tool_result: {"type": "tool_result", "tool_use_id": "...", "content": "...string..."}
    The renderer handles both.
"""

__title__  = 'Chat-\nbot'
__author__ = 'Nathaniel'
__doc__    = ('Engineering assistant that can read your Revit model and '
              'answer questions about it (sheets, views, schedules, '
              'selection, etc.). ASHRAE / HVAC reasoning included.')

# CRITICAL for modeless v2.5: keeps this script's IronPython engine alive
# after script.py exits. Without it, Revit fatal-crashes when invoking
# our IExternalEventHandler.Execute() because the class definition got
# torn down. Same convention Walkthrough.pushbutton uses.
__persistentengine__ = True

import os
import re
import json
import time
import codecs
import random
import hashlib
import math
import traceback
from datetime import datetime

import clr  # noqa: F401
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System")
clr.AddReference("System.Security")
clr.AddReference("System.Net")
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

import System
from System import Convert, Int64
from System.IO import StreamReader, MemoryStream, File as NetFile
from System.Text import Encoding
from System.Net import (
    HttpWebRequest, WebException, WebExceptionStatus,
    ServicePointManager, SecurityProtocolType,
)
from System.Security.Cryptography import ProtectedData, DataProtectionScope
from System.Threading import Thread, ThreadStart, Monitor, ManualResetEventSlim
from System.Windows.Threading import DispatcherPriority
from System.Diagnostics import Process

from System.Windows import (
    Visibility, HorizontalAlignment, VerticalAlignment,
    Thickness, TextWrapping, TextTrimming, FontWeights, FontStyles,
    GridLength, GridUnitType, WindowState, DataFormats, DragDropEffects,
)
from System.Windows.Controls import (
    Border, TextBlock, StackPanel, Grid, ColumnDefinition, RowDefinition,
    Button, Orientation, WrapPanel, TextBox, RichTextBox,
    ContextMenu, MenuItem, ScrollBarVisibility,
)
from System.Windows.Documents import Run, Bold, Italic, FlowDocument, Paragraph
from System.Windows.Media import SolidColorBrush, Color, Colors, FontFamily
from System.Windows.Media.Imaging import (
    BitmapImage, BitmapCacheOption, BitmapCreateOptions,
    PngBitmapEncoder, BitmapFrame,
)
from System.Windows.Controls import Image as WpfImage
from System.Windows.Input import Key, ModifierKeys, Keyboard, Cursors

from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter,
    FilteredElementCollector, ElementId, Element,
    ViewSheet, ViewSchedule, View, Viewport, Category, StorageType,
    Transaction, FamilySymbol, ViewDuplicateOption,
    ViewPlan, View3D, ViewFamilyType, ViewFamily, Level, XYZ,
    ScheduleFilter, ScheduleFilterType,
    ImageExportOptions, ImageFileType, ImageResolution,
    ZoomFitType, ExportRange,
    RevitLinkInstance, RevitLinkType, OverrideGraphicSettings,
    IndependentTag, TagMode, TagOrientation, Reference,
    TextNote, TextNoteType,
    ParameterFilterElement, ParameterFilterRuleFactory,
    ElementParameterFilter, FilterRule,
    Color as RevitColor,
    ElementTransformUtils, Plane, Line, Group,
)
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

# For doc.Delete(ICollection<ElementId>) — Revit wants a typed collection
from System.Collections.Generic import List as NetList

from pyrevit import revit, forms, script

import dbhms_ui
import dbhms_telemetry


# ---------------------------------------------------------------------------
# TLS - Anthropic API requires 1.2+. .NET 4.x default may be 1.0.
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
CONFIRMATION_XAML = os.path.join(SCRIPT_DIR, 'ConfirmationDialog.xaml')
PICK_PROMPT_XAML  = os.path.join(SCRIPT_DIR, 'PickPromptWindow.xaml')

API_ENDPOINT      = "https://api.anthropic.com/v1/messages"
API_VERSION       = "2023-06-01"
MAX_TOKENS        = 4096

MODELS = {
    "sonnet": {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6",
               "price_in_per_m": 3.0,  "price_out_per_m": 15.0},
    "opus":   {"id": "claude-opus-4-7",   "label": "Opus 4.7",
               "price_in_per_m": 15.0, "price_out_per_m": 75.0},
}
DEFAULT_MODEL_KEY = "sonnet"

# Default soft-cap for the pre-send "this might be expensive" warning,
# in USD. Configurable per-user in Settings.
DEFAULT_SPEND_THRESHOLD = 0.10

# When more than this many attachments are pinned, the chip strip
# collapses to the first N + a "+M more" chip. User can expand by
# clicking the "more" chip. Reset on each send.
_ATTACHMENT_VISIBLE_LIMIT = 5

# Text-like extensions: drop targets read content directly into the
# attachment so Claude sees the text inline. Other types attach as
# metadata-only chips (filename + path).
_TEXT_FILE_EXTS = frozenset([
    ".txt", ".md", ".rst", ".log",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".pyx", ".pyw",
    ".js", ".ts", ".tsx", ".jsx",
    ".html", ".htm", ".xml", ".xaml", ".svg", ".css",
    ".csv", ".tsv",
    ".sh", ".ps1", ".bat", ".cmd",
    ".c", ".h", ".cpp", ".hpp", ".cs",
])
_MAX_INLINE_TEXT_BYTES = 256 * 1024   # 256 KB - cap on inline file content

HISTORY_WINDOW = 40

# How many tool-use rounds we'll let the agent take in a single user turn
# before bailing out. Stops a runaway loop from racking up API costs.
MAX_AGENT_ROUNDS = 8

DEFAULT_SYSTEM_PROMPT = (
    "You are an MEP engineering assistant for dbHMS, embedded inside "
    "Autodesk Revit. You help dbHMS engineers with HVAC design, "
    "plumbing, electrical, controls, ASHRAE 90.1 / 62.1 / 62.2 / 55, "
    "and the ASHRAE Handbooks.\n"
    "\n"
    "You ALSO have read-only access to the active Revit document via "
    "tools. Use them aggressively when the user asks about the model "
    "(\"what sheets do I have?\", \"how many ducts?\", \"tell me about "
    "this selected element\", \"summarize the M-1xx sheets\"). Don't "
    "guess what's in the model when a tool can tell you.\n"
    "\n"
    "Tool-use guidelines:\n"
    "  - For 'what's selected?' use get_selection. Don't ask the user "
    "for the element id when they likely just want to know about what "
    "they've already clicked.\n"
    "  - When listing many things, prefer summary -> details on demand. "
    "Don't dump 200 sheet rows if a user just asked 'how many sheets'; "
    "answer the count, then offer a sample.\n"
    "  - Element ids are integers. After get_selection or list_*, you "
    "can use get_element_details to dig into specific items.\n"
    "  - Tools may return {\"error\": \"...\"} if something went wrong "
    "(e.g. element doesn't exist). Surface that to the user instead of "
    "pretending the tool succeeded.\n"
    "\n"
    "Write tools (v3.0+) - you CAN modify the active Revit model:\n"
    "  - create_sheets : create one or more sheets in one transaction\n"
    "  - set_element_parameters : set parameters on one or more elements\n"
    "  - duplicate_view : duplicate a view (Duplicate / WithDetailing / AsDependent)\n"
    "  - delete_elements : delete elements by id\n"
    "  - rename_element : rename a sheet, view, schedule, type, etc. by id\n"
    "  - place_view_on_sheet : place an existing view onto a sheet as a viewport\n"
    "  - apply_view_template : apply a template to one or more views\n"
    "  - create_view_plan : create a floor / ceiling / area plan from a level\n"
    "  - create_view_3d : create an isometric or perspective 3D view\n"
    "  - create_schedule : create a new schedule for a category, optionally with fields\n"
    "  - add_schedule_filter : add a filter rule to a schedule\n"
    "  - apply_scope_box_to_view : apply (or clear) a scope box on a view\n"
    "  - set_view_scale : change a view's scale (e.g. 96 = 1/8\"=1'-0\")\n"
    "  - hide_categories_in_view : hide or show categories in a view\n"
    "  - set_link_visibility_in_view : hide/halftone/show a link across one or more views in one transaction\n"
    "  - tag_elements : place tags for a list of elements in a view (one transaction)\n"
    "  - place_text_note : drop a text note at world coords OR centered on an element\n"
    "  - create_filter : build a view filter with parameter rules\n"
    "  - apply_filter_to_view : apply (or remove) a filter on one or more views with color / halftone / transparency overrides\n"
    "  - place_family_instance / place_family_instance_on_host : drop a new instance of a family in the model (free, level-hosted, or hosted on another element)\n"
    "  - copy_elements / move_elements : translate one or more elements\n"
    "  - mirror_elements : mirror across a plane (origin + normal)\n"
    "  - rotate_elements : rotate around an axis line by a degree angle\n"
    "  - set_elements_pinned : pin or unpin elements\n"
    "  - group_elements / ungroup_elements : bundle / release element groups\n"
    "  - array_elements_linear : N copies in a line, each offset by translation\n"
    "  - array_elements_radial : N copies spaced around an axis\n"
    "\n"
    "Linked models (v4.1 - arch + structural awareness):\n"
    "  dbHMS works MEP in a host .rvt with a linked architectural "
    "model + sometimes linked structural. These tools cross the link "
    "boundary so you can answer questions like 'what rooms are in the "
    "arch link', 'what level is this wall on', etc.\n"
    "  - list_links : ALWAYS the first call when the user asks about "
    "linked content. Returns each link instance's id + name + load "
    "status. The returned `id` is the link_id for every other linked-"
    "model tool.\n"
    "  - get_rooms_in_link : pulls rooms / spaces from a linked doc. "
    "Omit link_id to aggregate across every loaded link. This is the "
    "MEP go-to for room number, name, area, level - far better than "
    "guessing.\n"
    "  - get_elements_from_link : query a link's elements by category "
    "('OST_Walls', 'OST_Doors', 'OST_Floors', 'OST_Levels', 'OST_Grids', "
    "'OST_StructuralColumns', etc.).\n"
    "  - get_link_visibility_in_view / set_link_visibility_in_view : "
    "check / change hidden + halftone state for a link in a view. The "
    "set variant batches across many views in one transaction - use it "
    "for 'halftone the arch link on every M-1xx sheet view'.\n"
    "  IMPORTANT: ids returned by get_rooms_in_link / "
    "get_elements_from_link are LINK-document ids, not host-doc ids. "
    "They're useful in answers and tooltips, but host-doc write tools "
    "(set_element_parameters, delete_elements, rename_element, etc.) "
    "won't accept them. The host doc and the linked doc each have "
    "their own id space.\n"
    "\n"
    "Annotation + filters (v4.2):\n"
    "  - Finding element ids to operate on: list_elements_by_category "
    "is the bridge between count_by_category (counts only) and the "
    "write tools that need explicit ids. scope='active_view' returns "
    "every element of the category visible in the active view; "
    "scope='view' + view_id targets a specific view; scope='document' "
    "is the whole doc. ALWAYS use this when you need ids for "
    "tag_elements / delete_elements / set_element_parameters and "
    "the user said something like 'in this view' or 'on M-201'. "
    "Do NOT tell the user you can't get ids - this tool exists.\n"
    "  - Tagging workflow: 'tag all ducts in this view' becomes "
    "(a) list_elements_by_category(category='OST_DuctCurves', "
    "scope='active_view') -> ids, (b) list_tag_types(family_category"
    "_contains='Duct') -> tag_type_id, (c) tag_elements(view_id=..., "
    "element_ids=[...], tag_type_id=...). For 'tag all VAVs on "
    "M-201': list_sheets to find the sheet -> get_element_details on "
    "the sheet to find placed views -> list_elements_by_category("
    "scope='view', view_id=...) for each. The tag's FamilySymbol "
    "family must match the element's category - 'Pipe Tags' won't "
    "tag a VAV. If list_tag_types returns nothing for the wanted "
    "category, tell the user they need to load a tag family first "
    "(Revit's family browser).\n"
    "  - Text notes: text_note_type_id is optional - omit it and "
    "Revit picks the default type. Position is either an element_id "
    "(places text at the element's bbox center) or x_ft + y_ft "
    "(world coords).\n"
    "  - Filters: create_filter takes a list of categories and a "
    "list of {parameter, operator, value} rules. Parameter must be "
    "a BuiltInParameter enum name (e.g. 'RBS_DUCT_WIDTH_PARAM' for "
    "Duct Width, 'RBS_PIPE_DIAMETER_PARAM' for Pipe Diameter, "
    "'ALL_MODEL_TYPE_NAME' for Type Name). Operators: '=', '!=', "
    "'>', '>=', '<', '<=' for numeric; 'contains', 'begins_with', "
    "'ends_with' also valid for strings. Numeric values for length "
    "parameters are in INTERNAL Revit units (feet); 12 inches = 1.0 "
    "feet. apply_filter_to_view then puts the filter on view(s) "
    "with optional color override. Color accepts hex ('#FF0000' or "
    "'FF0000') or basic names ('red', 'blue', etc.).\n"
    "  - Color overrides set BOTH projection AND cut line color, "
    "which is what \"highlight in red\" usually means.\n"
    "\n"
    "Element placement + geometry editing (v4.3):\n"
    "  - Placement: place_family_instance needs a family_type_id "
    "(FamilySymbol). To find one, call get_element_details on an "
    "existing instance of the same family and read its type_id (until "
    "v4.8 lands list_family_types). For most MEP gear, pass level_id "
    "+ x_ft + y_ft and let z_ft default to 0 (places at the level's "
    "elevation). For ceiling-hosted diffusers / wall-hosted lights / "
    "etc., use place_family_instance_on_host with the host's id.\n"
    "  - All coordinates are FEET in Revit world space. Inches must "
    "be divided by 12 (10 ft 6 in = 10.5).\n"
    "  - Cardinal directions (default Revit project north convention):\n"
    "      +Y = north,  -Y = south\n"
    "      +X = east,   -X = west\n"
    "      +Z = up,     -Z = down\n"
    "    \"5 ft north\" -> translation={x:0, y:5, z:0}; \"5 ft south\" "
    "-> {x:0, y:-5, z:0}; \"going north\" -> positive Y; \"going "
    "south\" -> negative Y. Translation signs MUST follow this "
    "convention - sloppy interpretation here puts equipment in the "
    "wrong location.\n"
    "    CAVEAT: this assumes the project's project-north is "
    "aligned with the model's +Y axis (the Revit default). If the "
    "user's project has project-north rotated, the model-axis "
    "directions won't match compass directions. When the user's "
    "intent is ambiguous (e.g. an existing layout looks rotated, "
    "or they ask about a specific real-world bearing), ASK once: "
    "\"Is your project-north aligned with the model's +Y axis, "
    "or rotated? If rotated, give me the offset XYZ instead.\"\n"
    "  - CRITICAL distinction for LINEAR elements (ducts, pipes, "
    "conduit, walls, lines): they have a natural length axis. The "
    "phrase \"ducts going north-south\" / \"pipes running E-W\" / "
    "\"a wall running north\" means the ELEMENT'S OWN AXIS is "
    "oriented along that direction - NOT that copies are arranged "
    "along that direction.\n"
    "      \"Make 5 ducts going north-south\" -> the ducts THEMSELVES "
    "run N-S (each duct's curve is along the Y axis). If the "
    "original duct runs E-W, you must ROTATE it 90 deg about Z "
    "FIRST. Or rotate every copy after the array.\n"
    "      \"Make 5 ducts spaced 4 ft apart going north\" -> the "
    "ARRAY ITSELF marches north (each copy is +Y from the previous), "
    "duct orientation unchanged.\n"
    "      When the phrasing is ambiguous between orientation and "
    "spacing, ASK before doing the wrong thing.\n"
    "  - Rotating a linear element around its OWN center: call "
    "get_element_location to get its location point/curve, then "
    "call rotate_elements with axis_point=that point + "
    "axis_direction={x:0,y:0,z:1} for a horizontal spin. To rotate "
    "MANY elements each around their own center, loop: one "
    "rotate_elements call per element with a different axis_point.\n"
    "  - DON'T refuse a task by claiming a 'routing engine' or "
    "specialized tool is needed when copy_elements + rotate_elements "
    "would accomplish it. copy_elements works on ducts, pipes, "
    "conduit, cable trays, walls, anything - it duplicates the "
    "existing geometry as-is. Rotate the copy if you need a "
    "different orientation. The new instance carries the same "
    "system, size, insulation, etc. as the original. This is the "
    "right workflow for \"give me another duct running the other "
    "direction\" - copy + rotate the copy 90 deg. The MEP routing "
    "engine (Document.NewDuct + connectors) is for NEW system "
    "branches; for COPIES of existing elements, plain "
    "copy_elements is correct.\n"
    "  - For midpoint of a duct: get_element_location returns "
    "curve_start + curve_end. Midpoint = average of those two XYZ "
    "points. Use that as axis_point when rotating about the duct's "
    "center.\n"
    "  - copy_elements vs array_elements_linear: copy makes ONE "
    "translated copy; array makes N evenly spaced copies. Linear "
    "array count=5 = original + 4 copies.\n"
    "  - move_elements MUTATES in place - same ids, new position. "
    "Use copy if the user wants both. \n"
    "  - Mirror plane: define by origin + normal. normal={x:1,y:0,z:0} "
    "mirrors left/right across YZ; {x:0,y:1,z:0} mirrors front/back "
    "across XZ. keep_original defaults to true (most workflows want "
    "the mirrored COPY).\n"
    "  - Rotation: axis_direction is the SPIN axis (right-hand rule). "
    "For plan-view rotation of equipment, axis_direction={x:0,y:0,z:1} "
    "spins around vertical. angle is in degrees, positive is CCW "
    "looking down the axis.\n"
    "  - Before transforming, consider calling get_element_location "
    "or get_element_geometry to know where you're starting from. "
    "Many placement tasks need a reference point.\n"
    "  - Pin equipment after laying it out so the user can't drag "
    "it by accident: set_elements_pinned with pinned=true.\n"
    "\n"
    "Schedules + scope box workflow tips:\n"
    "  - Before create_schedule, call list_schedulable_fields with the "
    "category to see what parameters can be added as columns. Don't "
    "guess field names - if the agent passes a wrong name, the field "
    "is silently skipped (it shows up in `missing_fields` in the "
    "result).\n"
    "  - add_schedule_filter requires the field to already be a column "
    "on the schedule. Add it via create_schedule's field_names first.\n"
    "  - Scope boxes can only be CREATED in the Revit UI (the API "
    "doesn't expose creation). list_scope_boxes finds existing ones; "
    "apply_scope_box_to_view assigns them to views.\n"
    "  - hide_categories_in_view affects only the specified view (not "
    "view templates or other views).\n"
    "\n"
    "View / sheet workflow tips:\n"
    "  - To find a view template's id, call list_views with "
    "only_templates=true (regular list_views skips templates).\n"
    "  - To create a plan view, you need a level_id - call list_levels "
    "first.\n"
    "  - place_view_on_sheet defaults to the title block center; use "
    "x_ft / y_ft only when the user asks for specific coordinates.\n"
    "  - A view can only be placed on ONE sheet at a time (Revit rule). "
    "If place_view_on_sheet refuses, the view is probably already on "
    "another sheet - use duplicate_view first to make a copy.\n"
    "  - Sheet NUMBER (e.g. 'M-101') is a parameter, not the Name. To "
    "renumber a sheet, use set_element_parameters with "
    "parameter_name='Sheet Number'. rename_element changes the Name "
    "(e.g. 'MECHANICAL FIRST FLOOR PLAN').\n"
    "\n"
    "Write-tool rules:\n"
    "  - Each tool call is ONE Revit transaction = one Ctrl+Z step. If "
    "you set 12 parameters in one call, the user can undo all 12 at "
    "once.\n"
    "  - Before calling create_sheets / set_element_parameters (>5 "
    "changes) / delete_elements, DESCRIBE THE PLAN in your reply as a "
    "markdown table so the user can sanity-check it (sheet number + "
    "name; element id + category + name + reason for delete; etc.). "
    "Some tools also show their own confirmation dialog - both layers "
    "are intentional.\n"
    "  - delete_elements ALWAYS shows a confirmation dialog. ALWAYS "
    "describe what you're about to delete and why before calling it.\n"
    "  - If a tool returns {\"cancelled\": true}, the user clicked "
    "Cancel on the confirmation dialog. Respect that decision - do "
    "NOT immediately propose the same action again. Ask what they'd "
    "like to do differently instead.\n"
    "  - If you make a mistake (wrong sheet number, deleted wrong "
    "thing), tell the user to press Ctrl+Z. Don't try to 'fix it' by "
    "calling more write tools - that creates more undo steps to "
    "untangle.\n"
    "  - For element_id values, use ids you got from a list_* or "
    "get_selection tool, or from an attached context preamble. Don't "
    "invent ids.\n"
    "  - You still CANNOT export to Excel/PDF, create new views from "
    "scratch (other than duplicate_view), apply view templates, or "
    "move/rotate elements in space. Those land in later iterations.\n"
    "\n"
    "Efficiency / rate limits:\n"
    "  - BATCH tool calls when the tool supports it. Prefer ONE call "
    "to set_element_parameters with 10 changes over 10 separate "
    "single-change calls. Prefer ONE apply_view_template across many "
    "views over many individual calls. Each round trip ships the full "
    "conversation history, so fewer rounds = less work.\n"
    "  - If the user asks for something that needs MANY rounds (e.g. "
    "'duplicate 30 sheets, each with views and templates'), tell them "
    "the plan and ask before starting - long multi-round operations "
    "burn input tokens and can hit the 30k tokens/min rate limit.\n"
    "\n"
    "Attachments / context preamble:\n"
    "  The user can pin Revit items to a message via the '+ Add' menu "
    "(elements, sheets, current selection). Attachments arrive inline "
    "at the top of the user's message as a block like:\n"
    "      [Attached context: 1 item]\n"
    "      - element  id=12345  category=\"Mechanical Equipment\"  family=\"VAV - Single Duct\"  type=\"VAV-100-CFM\"  name=\"VAV-12\"\n"
    "  When this preamble is present, treat the attachments AS the "
    "subject of the question. \"this\" / \"these\" / \"it\" refer to the "
    "attachments, not to anything else. Do NOT call get_selection - the "
    "preamble already identifies what the user is asking about. You CAN "
    "call get_element_details(element_id=...) to fetch full parameter "
    "values when basic info from the preamble isn't enough.\n"
    "\n"
    "Image attachments (v4 vision):\n"
    "  The user can also attach IMAGES to a message - typically a "
    "screenshot of the active Revit view (Capture active view), a "
    "clipboard paste (Ctrl+V of a screenshot), or a dragged PNG/JPG "
    "file. Images arrive as separate vision content blocks BEFORE the "
    "text part of the message. You can see them directly - describe "
    "what's in them, comment on layout/routing/clashes/text legibility/"
    "title-block content/etc., or read text and numbers off a captured "
    "view or schedule.\n"
    "  - Treat captured views as ground truth about what's currently "
    "visible at that view's settings (scale, range, filters). Don't "
    "second-guess the image with a tool call unless you genuinely need "
    "data the image can't show (e.g. parameter values, hidden "
    "elements).\n"
    "  - You CAN combine vision with tools - e.g. \"this view shows two "
    "VAVs labeled VAV-12 and VAV-13\" -> follow up with get_element_details "
    "for those elements if the user asks about their parameters.\n"
    "  - Don't pretend you can't see images; if one is attached you "
    "have it.\n"
    "\n"
    "Engineering style:\n"
    "  - Direct and concise. Engineers value precision over filler.\n"
    "  - Default to IP units (US firm). Show SI in parens when it "
    "matters.\n"
    "  - When citing ASHRAE values, name the standard and edition. "
    "Flag uncertainty rather than guess - your training data may not "
    "match the latest edition.\n"
    "  - Show your work for calculations.\n"
    "  - If a question is ambiguous (climate zone, occupancy "
    "category), ask one short clarifying question rather than assume.\n"
    "  - DO NOT end replies with closing pleasantries OR follow-up "
    "offers. The UI flags any reply containing '?' as a Question "
    "(purple highlight) so the user can quickly spot when you're "
    "actually waiting on them; filler questions defeat that signal. "
    "Just stop after the answer. Forbidden trailing patterns include:\n"
    "      \"Anything else I can help you with?\"\n"
    "      \"Let me know if you need anything else.\"\n"
    "      \"Happy to help.\" / \"Hope that helps.\"\n"
    "      \"Would you like to tag them?\" / \"Want me to do X next?\"\n"
    "      \"Should I do Y now?\" / \"Ready to do Z?\"\n"
    "    If you genuinely need the user to decide something to PROCEED "
    "with the current task, that question IS the point of the reply "
    "and should be the only one in it (e.g. \"Use 8\\\" or 10\\\" "
    "ducts?\" is fine if asked instead of running an arbitrary "
    "default). But \"Would you like me to tag the new ducts?\" after "
    "you've finished placing them is just filler - the user will say "
    "so in their next message if they want that.\n"
    "\n"
    "Referring to Revit things by name, not by id:\n"
    "  Engineers think in names: \"M-101\", \"Level 2 - Mech\", \"VAV-12\", "
    "\"VAV - Single Duct: VAV-100-CFM\". They DON'T think in element ids "
    "(those are arbitrary integers Revit assigns internally). When you "
    "reply to the user, ALWAYS lead with the human name:\n"
    "    - Sheets:    use the sheet NUMBER (\"M-101\") - drop \"id 12345\" "
    "unless the user explicitly asked for the id, or there are two "
    "sheets with the same number and you need to disambiguate.\n"
    "    - Views:     use the view NAME (\"Level 2 - Mech\", \"3D - "
    "Overall\").\n"
    "    - Elements:  use family + type + mark/instance-name when "
    "available (\"VAV - Single Duct: VAV-100-CFM (Mark VAV-12)\"). Fall "
    "back to category + type if the instance has no mark or name. Only "
    "show the id when you genuinely need it for a follow-up tool call "
    "and want the user to see what you'll be acting on.\n"
    "    - Schedules: use the schedule name (\"Mechanical Equipment "
    "Schedule\").\n"
    "  Ids belong INSIDE tool calls (because the API demands them), "
    "not in prose answers. If you're listing several items in a table, "
    "you can include an Id column as the LAST column for traceability, "
    "but lead with the human-readable columns (number, name, category, "
    "type). NEVER write a reply like \"I updated element 12345 to be on "
    "level 7890\" - write \"I moved VAV-12 to Level 3\" instead.\n"
    "\n"
    "Formatting (the chat UI renders real markdown):\n"
    "  - When you list multiple items with multiple attributes (sheets "
    "with their numbers and names; views with their type and scale; "
    "elements with their parameters), use a markdown table. The UI "
    "renders | pipe | tables | as proper tables with header rows and "
    "gridlines. DO NOT use a series of '###' headings followed by "
    "lists when a table would do - tables are far more scannable.\n"
    "  - Use **bold** for the most important value(s) in an answer "
    "(the count, the spec value, the recommendation).\n"
    "  - Use `inline code` for element ids (when you do need to show "
    "one), parameter names, file paths, and other code-like tokens.\n"
    "  - Avoid emoji - they don't add information for engineers."
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
    d = os.path.join(_history_root(), project_key)
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def _config_path():
    return os.path.join(_appdata_root(), "config.json")


# ---------------------------------------------------------------------------
# DPAPI key encryption
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

def _default_config():
    return {"api_key": "",
            "model_key": DEFAULT_MODEL_KEY,
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
            "spend_threshold": DEFAULT_SPEND_THRESHOLD}


def load_config():
    path = _config_path()
    if not os.path.isfile(path):
        return _default_config()
    try:
        with codecs.open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return _default_config()
    enc = raw.get("api_key_enc", "")
    api_key = _decrypt_key(enc) if enc else ""
    model_key = raw.get("model_key", DEFAULT_MODEL_KEY)
    if model_key not in MODELS:
        model_key = DEFAULT_MODEL_KEY
    system_prompt = raw.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    # spend_threshold: USD. <= 0 disables the soft-cap prompt entirely.
    try:
        spend_threshold = float(raw.get("spend_threshold", DEFAULT_SPEND_THRESHOLD))
    except (TypeError, ValueError):
        spend_threshold = DEFAULT_SPEND_THRESHOLD
    return {"api_key":         api_key,
            "model_key":       model_key,
            "system_prompt":   system_prompt,
            "spend_threshold": spend_threshold}


def save_config(api_key, model_key, system_prompt, spend_threshold=None):
    if spend_threshold is None:
        spend_threshold = DEFAULT_SPEND_THRESHOLD
    payload = {
        "api_key_enc":     _encrypt_key(api_key) if api_key else "",
        "model_key":       model_key if model_key in MODELS else DEFAULT_MODEL_KEY,
        "system_prompt":   system_prompt or DEFAULT_SYSTEM_PROMPT,
        "spend_threshold": float(spend_threshold),
    }
    # Use codecs.open + ensure_ascii=False; see save_conversation() for why.
    with codecs.open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Project keying
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
# Question-detection: filter trailing pleasantries
# ---------------------------------------------------------------------------
#
# The chat UI flags any assistant turn whose text contains '?' as a
# "Question" (purple highlight + violet left-border) so the user can
# spot at a glance when Claude is genuinely waiting on them. Closing
# pleasantries like "Anything else I can help you with?" trip that
# detection without being real questions. We strip them from the END of
# the reply before scanning for '?'. The system prompt also tells the
# model not to emit them in the first place; this is the second line of
# defense.
_PLEASANTRY_PREFIXES = (
    "anything else",
    "is there anything else",
    "is there anything i can",
    "let me know",
    "let me know if",
    "feel free to ask",
    "feel free to let me know",
    "hope that helps",
    "hope this helps",
    "happy to help",
    "any other questions",
    "any other question",
    "if you need anything",
    "need anything else",
    "do you have any other",
    # Closing follow-up offers that read as "checking in for more work"
    # rather than real choice prompts. If the user wanted Claude to
    # actually do one of these, they'd ask in the next turn - the
    # offer here is filler.
    "would you like to",
    "would you like me to",
    "want me to",
    "do you want me to",
    "do you want to",
    "should i ",
    "shall i ",
    "ready to ",
    "next, would",
    "next, want",
)


def _strip_trailing_pleasantries(text):
    """Drop trailing courtesy sentences ("anything else?", "let me know
    if...", "hope that helps", etc.) from the END of `text`. Walks the
    sentence list from the back and trims while the last sentence
    starts with a known pleasantry prefix. Substantive questions
    earlier in the body, or as the LAST sentence, are preserved."""
    if not text:
        return text
    out = text
    # Cap the loop so a pathological input can't spin forever.
    for _ in range(20):
        stripped = out.rstrip(" \t\r\n")
        if not stripped:
            break
        # Find the start of the last sentence by looking for the
        # nearest sentence boundary before the end.
        boundary_marks = (". ", "! ", "? ", "\n")
        last_split = -1
        for mark in boundary_marks:
            idx = stripped.rfind(mark)
            if idx > last_split:
                last_split = idx
        if last_split < 0:
            candidate_start = 0
        else:
            # Skip past the boundary delimiter itself.
            # For ". " / "! " / "? " that's 2 chars; "\n" is 1.
            mark_len = 1 if stripped[last_split] == "\n" else 2
            candidate_start = last_split + mark_len
        candidate = stripped[candidate_start:].lstrip()
        # Strip every leading non-letter character (markdown wrappers
        # like "**", quotes, parens, AND emoji like "😄" / "✨") so
        # that "**Anything else?**" and "😄 Anything else?" both match
        # the prefix list cleanly. We just want to land on the first
        # actual word.
        while candidate and not candidate[0].isalpha():
            candidate = candidate[1:]
        candidate_lower = candidate.lower()
        is_pleasantry = False
        for prefix in _PLEASANTRY_PREFIXES:
            if candidate_lower.startswith(prefix):
                is_pleasantry = True
                break
        if not is_pleasantry:
            break
        # Drop the pleasantry sentence and keep walking back.
        out = stripped[:candidate_start].rstrip(" \t\r\n")
    return out


# ---------------------------------------------------------------------------
# Conversation management
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
    """First user-typed text turn, truncated. Skips tool-result messages."""
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        text = " ".join(content.split())
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
    # IronPython 2.7's json encoder has a bug in py_encode_basestring_ascii
    # where it raises UnicodeEncodeError on emoji / non-ASCII characters
    # under the default ensure_ascii=True (instead of escaping them as
    # it does in CPython). Force UTF-8 on disk and skip the ascii pass.
    # Affects any chat where Claude emits an emoji like ⚡.
    with codecs.open(path, "w", encoding="utf-8") as f:
        json.dump(conv, f, indent=2, ensure_ascii=False)


def load_conversation(project_key, conv_id):
    path = os.path.join(_project_history_dir(project_key), conv_id + ".json")
    if not os.path.isfile(path):
        return None
    try:
        with codecs.open(path, "r", encoding="utf-8") as f:
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
            with codecs.open(os.path.join(d, fn), "r", encoding="utf-8") as f:
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
    old = os.path.join(_history_root(), project_key + ".json")
    if not os.path.isfile(old):
        return
    new_dir = _project_history_dir(project_key)
    try:
        already_migrated = any(
            fn.startswith("chat_") and fn.endswith(".json")
            for fn in os.listdir(new_dir))
    except Exception:
        already_migrated = False
    if not already_migrated:
        try:
            with codecs.open(old, "r", encoding="utf-8") as f:
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
            pass
    try:
        os.rename(old, old + ".migrated")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Date humanization
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


# ===========================================================================
# REVIT API HELPERS + TOOL IMPLEMENTATIONS
# ===========================================================================
#
# Every tool function takes (doc, input_dict) and returns a JSON-serializable
# result. Tool functions MUST run on Revit's UI thread (the DB API is not
# thread-safe). The agent loop marshals calls via Dispatcher.Invoke before
# invoking these.
#
# Convention:
#   - On error or invalid input, return {"error": "<short message>"}.
#   - For lists, return [] (not None).
#   - Truncate large lists to `max_results` if specified, and include
#     {"truncated": true} signaling somewhere the agent can find it.
# ---------------------------------------------------------------------------

def _eid_int(eid):
    """Revit 2024+ uses Int64 (.Value); older uses Int32 (.IntegerValue)."""
    if eid is None:
        return None
    try:
        return int(eid.Value)
    except (AttributeError, OverflowError):
        try:
            return int(eid.IntegerValue)
        except AttributeError:
            return None


def _make_eid(int_val):
    """Build an ElementId from an int. 2024+ uses Int64."""
    try:
        return ElementId(Int64(int(int_val)))
    except Exception:
        return ElementId(int(int_val))


def _param_value(param):
    """Return a JSON-friendly value for a Revit Parameter."""
    if param is None or not param.HasValue:
        return None
    try:
        st = param.StorageType
        if st == StorageType.Double:
            # Prefer the human-readable display value (with units).
            vs = param.AsValueString()
            if vs:
                return vs
            return param.AsDouble()
        if st == StorageType.Integer:
            # YesNo parameters are stored as int 0/1; AsValueString gives the label.
            vs = param.AsValueString()
            if vs:
                return vs
            return param.AsInteger()
        if st == StorageType.String:
            return param.AsString() or ""
        if st == StorageType.ElementId:
            eid = param.AsElementId()
            i = _eid_int(eid)
            if i is None or i == -1:
                return None
            return i
    except Exception:
        try:
            return param.AsValueString()
        except Exception:
            return None
    return None


def _category_name(el):
    try:
        return el.Category.Name if el.Category else None
    except Exception:
        return None


def _type_and_family(doc, el):
    """Returns (type_name, family_name) for an instance, or ('', '')."""
    try:
        type_id = el.GetTypeId()
        if type_id is None or _eid_int(type_id) == -1:
            return "", ""
        sym = doc.GetElement(type_id)
        if sym is None:
            return "", ""
        type_name = ""
        try:
            type_name = sym.Name or ""
        except Exception:
            pass
        fam_name = ""
        try:
            if hasattr(sym, "Family") and sym.Family is not None:
                fam_name = sym.Family.Name or ""
        except Exception:
            pass
        return type_name, fam_name
    except Exception:
        return "", ""


# ---- Individual tools ------------------------------------------------------

def _tool_get_document_info(doc, input_dict):
    if doc is None:
        return {"error": "No active Revit document."}
    try:
        app = doc.Application
        try:
            active_view = doc.ActiveView
        except Exception:
            active_view = None
        return {
            "title":            doc.Title or "",
            "path":             doc.PathName or "",
            "is_workshared":    bool(doc.IsWorkshared),
            "active_view":      active_view.Name if active_view else None,
            "active_view_type": str(active_view.ViewType) if active_view else None,
            "active_view_id":   _eid_int(active_view.Id) if active_view else None,
            "revit_version":    str(app.VersionNumber),
            "revit_build":      str(app.VersionBuild),
        }
    except Exception as e:
        return {"error": "get_document_info failed: {}".format(e)}


def _tool_list_views(doc, input_dict):
    if doc is None:
        return {"error": "No active Revit document."}
    view_type   = (input_dict or {}).get("view_type")
    name_sub    = ((input_dict or {}).get("name_contains") or "").lower()
    max_results = int((input_dict or {}).get("max_results") or 200)
    # include_templates: when True, view templates are included AND
    # marked with is_template=True. Useful when the agent is about to
    # call apply_view_template and needs the template's id.
    include_templates = bool((input_dict or {}).get("include_templates", False))
    # only_templates: short-cut for "I just want the templates."
    only_templates    = bool((input_dict or {}).get("only_templates", False))
    try:
        col = FilteredElementCollector(doc).OfClass(View)
        out = []
        truncated = False
        for v in col:
            is_template = bool(v.IsTemplate)
            if only_templates and not is_template:
                continue
            if (not include_templates) and (not only_templates) and is_template:
                continue
            try:
                vt = str(v.ViewType)
            except Exception:
                vt = ""
            if view_type and vt.lower() != view_type.lower():
                continue
            try:
                vname = v.Name
            except Exception:
                vname = ""
            if name_sub and name_sub not in vname.lower():
                continue
            row = {
                "id":          _eid_int(v.Id),
                "name":        vname,
                "type":        vt,
                "is_template": is_template,
            }
            try:
                row["scale"] = int(v.Scale) if hasattr(v, "Scale") else None
            except Exception:
                row["scale"] = None
            out.append(row)
            if len(out) >= max_results:
                truncated = True
                break
        out.sort(key=lambda r: (r.get("type") or "", r.get("name") or ""))
        result = {"count": len(out), "views": out}
        if truncated:
            result["truncated"] = True
            result["note"] = ("Truncated at max_results={}. Refine "
                              "name_contains or view_type to see more.").format(max_results)
        return result
    except Exception as e:
        return {"error": "list_views failed: {}".format(e)}


def _tool_list_levels(doc, input_dict):
    """Returns all Level elements in the active document. Used as a
    pre-step to create_view_plan, which needs a level_id."""
    if doc is None:
        return {"error": "No active Revit document."}
    try:
        col = FilteredElementCollector(doc).OfClass(Level)
        out = []
        for lv in col:
            try:
                out.append({
                    "id":        _eid_int(lv.Id),
                    "name":      _safe_name(lv),
                    "elevation": float(lv.Elevation),  # in internal units (feet)
                })
            except Exception:
                continue
        out.sort(key=lambda r: r.get("elevation", 0.0))
        return {"count": len(out), "levels": out}
    except Exception as e:
        return {"error": "list_levels failed: {}".format(e)}


def _tool_list_sheets(doc, input_dict):
    if doc is None:
        return {"error": "No active Revit document."}
    num_sub  = ((input_dict or {}).get("number_contains") or "").lower()
    name_sub = ((input_dict or {}).get("name_contains") or "").lower()
    max_results = int((input_dict or {}).get("max_results") or 200)
    try:
        col = FilteredElementCollector(doc).OfClass(ViewSheet)
        out = []
        truncated = False
        for s in col:
            num = s.SheetNumber or ""
            nm  = s.Name or ""
            if num_sub  and num_sub  not in num.lower(): continue
            if name_sub and name_sub not in nm.lower():  continue

            # Title block family (first found).
            tb_fam = ""
            try:
                tbs = (FilteredElementCollector(doc, s.Id)
                       .OfCategory(BuiltInCategory.OST_TitleBlocks)
                       .WhereElementIsNotElementType())
                for tb in tbs:
                    try:
                        tb_fam = tb.Symbol.Family.Name
                    except Exception:
                        pass
                    break
            except Exception:
                pass

            try:
                placed = s.GetAllPlacedViews()
                view_count = placed.Count if placed is not None else 0
            except Exception:
                view_count = 0

            out.append({
                "id":                 _eid_int(s.Id),
                "number":             num,
                "name":                nm,
                "title_block_family": tb_fam,
                "view_count":         view_count,
            })
            if len(out) >= max_results:
                truncated = True
                break
        out.sort(key=lambda r: r.get("number") or "")
        result = {"count": len(out), "sheets": out}
        if truncated:
            result["truncated"] = True
        return result
    except Exception as e:
        return {"error": "list_sheets failed: {}".format(e)}


def _tool_list_schedules(doc, input_dict):
    if doc is None:
        return {"error": "No active Revit document."}
    name_sub = ((input_dict or {}).get("name_contains") or "").lower()
    max_results = int((input_dict or {}).get("max_results") or 200)
    try:
        col = FilteredElementCollector(doc).OfClass(ViewSchedule)
        out = []
        truncated = False
        for s in col:
            try:
                nm = s.Name or ""
            except Exception:
                nm = ""
            if name_sub and name_sub not in nm.lower():
                continue
            cat_name = ""
            try:
                cat_id = s.Definition.CategoryId
                cat = Category.GetCategory(doc, cat_id)
                if cat is not None:
                    cat_name = cat.Name
            except Exception:
                pass
            out.append({
                "id":                     _eid_int(s.Id),
                "name":                    nm,
                "category":                cat_name,
                "is_titleblock_revision": bool(s.IsTitleblockRevisionSchedule),
            })
            if len(out) >= max_results:
                truncated = True
                break
        out.sort(key=lambda r: r.get("name") or "")
        result = {"count": len(out), "schedules": out}
        if truncated:
            result["truncated"] = True
        return result
    except Exception as e:
        return {"error": "list_schedules failed: {}".format(e)}


def _tool_get_selection(doc, input_dict):
    if doc is None:
        return {"error": "No active Revit document."}
    try:
        uidoc = revit.uidoc
        if uidoc is None:
            return {"error": "No active UI document."}
        sel_ids = uidoc.Selection.GetElementIds()
        out = []
        for eid in sel_ids:
            el = doc.GetElement(eid)
            if el is None:
                continue
            type_name, fam_name = _type_and_family(doc, el)
            row = {
                "id":          _eid_int(el.Id),
                "category":    _category_name(el),
                "family_name": fam_name,
                "type_name":   type_name,
            }
            try:
                row["name"] = el.Name
            except Exception:
                row["name"] = ""
            out.append(row)
        return {"count": len(out), "elements": out}
    except Exception as e:
        return {"error": "get_selection failed: {}".format(e)}


def _tool_count_by_category(doc, input_dict):
    if doc is None:
        return {"error": "No active Revit document."}
    scope = ((input_dict or {}).get("scope") or "document").lower()
    cat_sub = ((input_dict or {}).get("category_filter") or "").lower()
    try:
        if scope == "active_view":
            view = doc.ActiveView
            if view is None:
                return {"error": "No active view."}
            col = FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
        else:
            col = FilteredElementCollector(doc).WhereElementIsNotElementType()
        counts = {}
        for el in col:
            try:
                cat = el.Category
                if cat is None:
                    continue
                cn = cat.Name
                if not cn:
                    continue
            except Exception:
                continue
            if cat_sub and cat_sub not in cn.lower():
                continue
            counts[cn] = counts.get(cn, 0) + 1
        rows = [{"category": n, "count": c} for n, c in counts.items()]
        rows.sort(key=lambda r: -r["count"])
        return {"scope": scope, "total_categories": len(rows), "rows": rows}
    except Exception as e:
        return {"error": "count_by_category failed: {}".format(e)}


def _tool_list_elements_by_category(doc, input_dict):
    """List actual element INSTANCES of a specific category, scoped
    either to the whole document, the active view, or a specific view
    id. Returns each element's id, name, category, type, family, and
    mark - the ids are what you feed to tag_elements / delete_elements /
    set_element_parameters.

    This is the missing link between count_by_category (counts only,
    no ids) and the write tools that need explicit element_ids."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    category    = inp.get("category")
    scope       = (inp.get("scope") or "document").lower()
    view_id     = inp.get("view_id")
    name_sub    = (inp.get("name_contains") or "").lower()
    mark_sub    = (inp.get("mark_contains") or "").lower()
    max_results = int(inp.get("max_results") or 200)

    if not category:
        return {"error": ("category is required (e.g. 'OST_DuctCurves', "
                          "'OST_DuctTerminal', 'Ducts', 'Mechanical Equipment').")}

    # Resolve category to BuiltInCategory or Category id.
    bic, cat_obj = _bic_or_category_from_name(doc, category)
    if bic is None and cat_obj is None:
        return {"error": ("Unknown category: {}. Use a BuiltInCategory "
                          "name (preferred) or display name.").format(category)}

    # Resolve scope -> target view (None means whole document).
    target_view = None
    if scope == "active_view":
        try:
            target_view = doc.ActiveView
        except Exception:
            target_view = None
        if target_view is None:
            return {"error": "No active view."}
    elif scope == "view":
        if view_id is None:
            return {"error": "view_id is required when scope='view'."}
        try:
            target_view = doc.GetElement(_make_eid(int(view_id)))
        except Exception:
            return {"error": "Invalid view_id."}
        if target_view is None or not isinstance(target_view, View):
            return {"error": "view_id is not a View."}
    elif scope != "document":
        return {"error": ("scope must be 'document', 'active_view', or "
                          "'view'. Got: {}").format(scope)}

    try:
        if target_view is not None:
            col = FilteredElementCollector(doc, target_view.Id)
        else:
            col = FilteredElementCollector(doc)

        if bic is not None:
            col = col.OfCategory(bic)
        else:
            col = col.OfCategoryId(cat_obj.Id)
        col = col.WhereElementIsNotElementType()

        out = []
        truncated = False
        for el in col:
            nm = _safe_name(el) or ""
            if name_sub and name_sub not in nm.lower():
                continue
            row = {
                "id":   _eid_int(el.Id),
                "name": nm,
            }
            try:
                row["category"] = el.Category.Name if el.Category else ""
            except Exception:
                row["category"] = ""
            # Family + type when available
            try:
                tid = el.GetTypeId()
                if tid is not None and _eid_int(tid) != -1:
                    type_el = doc.GetElement(tid)
                    if type_el is not None:
                        row["type"] = _safe_name(type_el)
                        try:
                            row["family"] = type_el.FamilyName
                        except Exception:
                            pass
            except Exception:
                pass
            # Mark parameter (instance identifier engineers actually use)
            mark = ""
            try:
                p = el.LookupParameter("Mark")
                if p is not None:
                    mark = p.AsString() or ""
            except Exception:
                pass
            if mark_sub and mark_sub not in mark.lower():
                continue
            if mark:
                row["mark"] = mark
            # Best-effort level
            try:
                lp = el.LookupParameter("Level") or el.LookupParameter("Reference Level")
                if lp is not None and lp.StorageType == StorageType.ElementId:
                    lvl = doc.GetElement(lp.AsElementId())
                    if lvl is not None:
                        row["level"] = _safe_name(lvl)
            except Exception:
                pass
            out.append(row)
            if len(out) >= max_results:
                truncated = True
                break
    except Exception as e:
        return {"error": "list_elements_by_category failed: {}".format(e)}

    out.sort(key=lambda r: (r.get("category") or "",
                            r.get("type") or "",
                            r.get("mark") or "",
                            r.get("name") or ""))
    result = {
        "scope":    scope,
        "category": category,
        "count":    len(out),
        "elements": out,
    }
    if target_view is not None:
        result["view_id"]   = _eid_int(target_view.Id)
        result["view_name"] = _safe_name(target_view)
    if truncated:
        result["truncated"] = True
        result["note"] = ("Truncated at max_results={}. Refine "
                          "name_contains / mark_contains to narrow.").format(max_results)
    return result


def _tool_get_element_details(doc, input_dict):
    if doc is None:
        return {"error": "No active Revit document."}
    raw_id = (input_dict or {}).get("element_id")
    if raw_id is None:
        return {"error": "element_id is required."}
    try:
        eid_int_val = int(raw_id)
    except Exception:
        return {"error": "element_id must be an integer."}
    try:
        el = doc.GetElement(_make_eid(eid_int_val))
        if el is None:
            return {"error": "Element id {} not found.".format(eid_int_val)}

        type_name, fam_name = _type_and_family(doc, el)

        instance_params = {}
        try:
            for p in el.Parameters:
                try:
                    name = p.Definition.Name
                except Exception:
                    continue
                instance_params[name] = _param_value(p)
        except Exception:
            pass

        type_params = {}
        try:
            type_id = el.GetTypeId()
            if type_id is not None and _eid_int(type_id) != -1:
                type_el = doc.GetElement(type_id)
                if type_el is not None:
                    for p in type_el.Parameters:
                        try:
                            name = p.Definition.Name
                        except Exception:
                            continue
                        type_params[name] = _param_value(p)
        except Exception:
            pass

        try:
            elem_name = el.Name
        except Exception:
            elem_name = ""

        result = {
            "id":              eid_int_val,
            "name":            elem_name,
            "category":        _category_name(el),
            "family_name":     fam_name,
            "type_name":       type_name,
            "instance_params": instance_params,
            "type_params":     type_params,
        }

        # For sheets, also surface what's actually placed on them and
        # which title block they use. Enables a "duplicate this sheet
        # with its views" workflow without the agent guessing.
        if isinstance(el, ViewSheet):
            # Placed views (covers both viewports and schedule placements)
            placed = []
            try:
                placed_ids = el.GetAllPlacedViews()
                for view_id in (placed_ids or []):
                    view_el = doc.GetElement(view_id)
                    if view_el is None:
                        continue
                    try:
                        vtype = str(view_el.ViewType)
                    except Exception:
                        vtype = ""
                    placed.append({
                        "view_id":   _eid_int(view_el.Id),
                        "view_name": _safe_name(view_el),
                        "view_type": vtype,
                    })
            except Exception:
                pass
            result["placed_views"] = placed

            # First title block on the sheet (covers the typical "one
            # title block per sheet" case). Exposes both the instance
            # id and the type id - create_sheets accepts either.
            try:
                tb_col = (FilteredElementCollector(doc, el.Id)
                          .OfCategory(BuiltInCategory.OST_TitleBlocks)
                          .WhereElementIsNotElementType())
                for tb in tb_col:
                    tb_info = {"instance_id": _eid_int(tb.Id)}
                    try:
                        tb_type_id = tb.GetTypeId()
                        if tb_type_id is not None and _eid_int(tb_type_id) != -1:
                            tb_info["type_id"] = _eid_int(tb_type_id)
                            sym = doc.GetElement(tb_type_id)
                            if sym is not None:
                                try:
                                    tb_info["family"] = sym.Family.Name if (
                                        hasattr(sym, "Family") and sym.Family is not None) else ""
                                except Exception:
                                    tb_info["family"] = ""
                                try:
                                    tb_info["type_name"] = _safe_name(sym)
                                except Exception:
                                    tb_info["type_name"] = ""
                    except Exception:
                        pass
                    result["title_block"] = tb_info
                    break
            except Exception:
                pass

        return result
    except Exception as e:
        return {"error": "get_element_details failed: {}".format(e)}


# ===========================================================================
# CONFIRMATION DIALOG (used by write tools before batch / destructive ops)
# ===========================================================================
#
# A modal preview dialog. Tools call _show_confirmation(...) before
# committing any batch or destructive operation. The dialog shows a
# table of what's about to happen and a Cancel / Confirm pair.
#
# Runs on Revit's UI thread inside the action handler's Execute() call,
# so the write tool's transaction doesn't start until the user agrees.
# ---------------------------------------------------------------------------

class ConfirmationDialog(forms.WPFWindow):
    def __init__(self, title, subtitle, intro_text, columns, rows,
                 confirm_label, destructive, footnote):
        forms.WPFWindow.__init__(self, CONFIRMATION_XAML)
        self.txt_title.Text    = title or "Confirm action"
        self.txt_subtitle.Text = subtitle or ""

        if intro_text:
            self.txt_intro.Text       = intro_text
            self.txt_intro.Visibility = Visibility.Visible

        # Reuse the markdown renderer's _build_table for visual
        # consistency with chat-rendered tables.
        if columns and rows:
            header = [c.get("header", c.get("key", "")) for c in columns]
            data_rows = [
                [_render_cell(row.get(c.get("key", ""), "")) for c in columns]
                for row in rows
            ]
            grid = _build_table(header, data_rows)
            if grid is not None:
                self.pnl_grid.Children.Add(grid)

        if footnote:
            self.txt_footnote.Text     = footnote
            self.bnr_footnote.Visibility = Visibility.Visible

        self.btn_confirm.Content = confirm_label or "Confirm"
        if destructive:
            # Red destructive button. Same red the tool-call error
            # bubbles use, so the visual cue is consistent.
            self.btn_confirm.Background = SolidColorBrush(Color.FromRgb(229, 62, 62))

        self._confirmed = False
        self.btn_cancel.Click  += self._on_cancel
        self.btn_confirm.Click += self._on_confirm

    def _on_cancel(self, sender, args):
        self._confirmed = False
        self.Close()

    def _on_confirm(self, sender, args):
        self._confirmed = True
        self.Close()

    def show_modal(self, owner=None):
        if owner is not None:
            try:
                self.Owner = owner
            except Exception:
                pass
        self.ShowDialog()
        return bool(self._confirmed)


def _render_cell(value):
    """Coerce a cell value to a string for the preview table.
    None -> empty; bool/int/float -> str(); else str()."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _show_confirmation(title, subtitle, columns, rows,
                       intro_text="", confirm_label="Confirm",
                       destructive=False, footnote=""):
    """Open a modal confirmation dialog over the active chatbot window.
    Returns True if the user clicked Confirm, False otherwise.

    Must be called on Revit's UI thread - the write tools satisfy this
    because they run inside _RevitActionHandler.Execute()."""
    dlg = ConfirmationDialog(title, subtitle, intro_text, columns, rows,
                             confirm_label, destructive, footnote)
    return dlg.show_modal(owner=__window__)


# ===========================================================================
# WRITE TOOLS (v3.0)
# ===========================================================================
#
# Each tool:
#   - Validates inputs first (no partial writes from a bad batch)
#   - Shows a confirmation dialog when the operation is batch or
#     destructive (see per-tool docstrings)
#   - Wraps mutations in a SINGLE Revit Transaction so Ctrl+Z undoes
#     the whole call as one unit
#   - On exception, rolls back the transaction and returns {"error": ...}
#   - On user cancellation, returns {"cancelled": True}
# ---------------------------------------------------------------------------

def _safe_name(el):
    try:
        return el.Name or ""
    except Exception:
        return ""


def _is_titleblock_symbol(el):
    """True if `el` is a FamilySymbol of category OST_TitleBlocks."""
    if not isinstance(el, FamilySymbol):
        return False
    try:
        cat = el.Category
        if cat is None:
            return False
        return _eid_int(cat.Id) == int(BuiltInCategory.OST_TitleBlocks)
    except Exception:
        return False


def _tool_create_sheets(doc, input_dict):
    if doc is None:
        return {"error": "No active document."}

    sheets_spec = (input_dict or {}).get("sheets")
    if not isinstance(sheets_spec, list) or not sheets_spec:
        return {"error": "sheets must be a non-empty list of {number, name} objects."}

    # Validate spec shape
    for idx, s in enumerate(sheets_spec):
        if not isinstance(s, dict):
            return {"error": "sheets[{}] must be an object with number + name.".format(idx)}
        if not s.get("number"):
            return {"error": "sheets[{}].number is required.".format(idx)}
        if not s.get("name"):
            return {"error": "sheets[{}].name is required.".format(idx)}

    # Resolve title block FamilySymbol. Accept either:
    #   - The FamilySymbol (type) id directly, or
    #   - An instance id of a placed title block (auto-resolves to its type).
    # The latter case is common because get_element_details on a sheet
    # surfaces the placed title block's instance id, and the agent
    # naturally tries to reuse that.
    tb_id = (input_dict or {}).get("title_block_id")
    tb_symbol = None
    if tb_id is not None:
        try:
            el = doc.GetElement(_make_eid(int(tb_id)))
            if el is None:
                return {"error": "title_block_id {} not found.".format(tb_id)}
            if _is_titleblock_symbol(el):
                tb_symbol = el
            else:
                # Probably an instance - try resolving via its type.
                try:
                    type_id = el.GetTypeId()
                    if type_id is not None and _eid_int(type_id) != -1:
                        type_el = doc.GetElement(type_id)
                        if _is_titleblock_symbol(type_el):
                            tb_symbol = type_el
                except Exception:
                    pass
            if tb_symbol is None:
                return {"error": ("title_block_id {} doesn't resolve to a title block "
                                  "type. Pass either a title block TYPE id (FamilySymbol) "
                                  "or an INSTANCE id of a placed title block.").format(tb_id)}
        except Exception as e:
            return {"error": "Could not resolve title_block_id: {}".format(e)}
    else:
        # First available title block
        col = (FilteredElementCollector(doc)
               .OfCategory(BuiltInCategory.OST_TitleBlocks)
               .WhereElementIsElementType())
        for s in col:
            if isinstance(s, FamilySymbol):
                tb_symbol = s
                break
    if tb_symbol is None:
        return {"error": "No title block family symbols available in this project."}

    # Detect existing sheet-number conflicts
    existing_numbers = set()
    for s in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            num = (s.SheetNumber or "").lower()
            if num:
                existing_numbers.add(num)
        except Exception:
            pass
    conflicts = [s["number"] for s in sheets_spec
                 if s["number"].lower() in existing_numbers]

    tb_label = "{} : {}".format(
        tb_symbol.Family.Name if tb_symbol.Family else "?",
        _safe_name(tb_symbol))

    # Confirmation preview
    columns = [
        {"key": "number", "header": "Number"},
        {"key": "name",   "header": "Name"},
        {"key": "tb",     "header": "Title Block"},
    ]
    rows = [{"number": s["number"], "name": s["name"], "tb": tb_label}
            for s in sheets_spec]

    footnote = ""
    if conflicts:
        footnote = ("These sheet numbers ALREADY EXIST in the project and will "
                    "fail to create as-is: {}").format(", ".join(conflicts))

    confirmed = _show_confirmation(
        title="Create {} sheet{}?".format(len(sheets_spec),
                                          "" if len(sheets_spec) == 1 else "s"),
        subtitle="The following sheets will be added to the active document.",
        columns=columns,
        rows=rows,
        confirm_label="Create sheets",
        footnote=footnote,
    )
    if not confirmed:
        return {"cancelled": True, "message": "User cancelled sheet creation."}

    # Single transaction = single Ctrl+Z step
    t = Transaction(doc, "Create {} sheet{}".format(
        len(sheets_spec), "" if len(sheets_spec) == 1 else "s"))
    created = []
    try:
        t.Start()
        # Activate the family symbol inside the same transaction so the
        # activation itself becomes part of the undo unit.
        if not tb_symbol.IsActive:
            tb_symbol.Activate()
            doc.Regenerate()
        for spec in sheets_spec:
            sheet = ViewSheet.Create(doc, tb_symbol.Id)
            sheet.SheetNumber = spec["number"]
            sheet.Name        = spec["name"]
            created.append({
                "id":     _eid_int(sheet.Id),
                "number": spec["number"],
                "name":   spec["name"],
            })
        t.Commit()
        return {"created_count": len(created), "sheets": created}
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Sheet creation failed (rolled back): {}".format(e)}


def _tool_set_element_parameters(doc, input_dict):
    if doc is None:
        return {"error": "No active document."}
    changes = (input_dict or {}).get("changes")
    if not isinstance(changes, list) or not changes:
        return {"error": "changes must be a non-empty list of {element_id, parameter_name, value}."}

    # Validate + resolve everything before opening a transaction
    resolved = []
    errors = []
    for idx, ch in enumerate(changes):
        if not isinstance(ch, dict):
            errors.append("changes[{}] must be an object.".format(idx))
            continue
        try:
            eid_val = int(ch.get("element_id"))
        except (TypeError, ValueError):
            errors.append("changes[{}].element_id must be an integer.".format(idx))
            continue
        pname = ch.get("parameter_name")
        if not pname:
            errors.append("changes[{}].parameter_name is required.".format(idx))
            continue
        if "value" not in ch:
            errors.append("changes[{}].value is required.".format(idx))
            continue
        el = doc.GetElement(_make_eid(eid_val))
        if el is None:
            errors.append("changes[{}]: element id {} not found.".format(idx, eid_val))
            continue
        param = el.LookupParameter(pname)
        if param is None:
            errors.append("changes[{}]: parameter '{}' not found on element {}.".format(
                idx, pname, eid_val))
            continue
        if param.IsReadOnly:
            errors.append("changes[{}]: parameter '{}' on element {} is read-only.".format(
                idx, pname, eid_val))
            continue
        resolved.append({
            "element_id":     eid_val,
            "element":        el,
            "parameter":      param,
            "parameter_name": pname,
            "new_value":      ch["value"],
            "old_value":      _param_value(param),
            "storage_type":   str(param.StorageType),
            "element_label":  "{}: {}".format(
                _category_name(el) or "Element",
                _safe_name(el) or "(unnamed)"),
        })

    if errors:
        return {"error": "Validation failed:\n  - " + "\n  - ".join(errors)}

    # Confirm for batch (>10 changes). Single / small batches skip the dialog.
    if len(resolved) > 10:
        columns = [
            {"key": "element_label",  "header": "Element"},
            {"key": "element_id",     "header": "ID"},
            {"key": "parameter_name", "header": "Parameter"},
            {"key": "old_value",      "header": "From"},
            {"key": "new_value",      "header": "To"},
        ]
        rows = [
            {
                "element_label":  r["element_label"],
                "element_id":     r["element_id"],
                "parameter_name": r["parameter_name"],
                "old_value":      r["old_value"] if r["old_value"] is not None else "(empty)",
                "new_value":      r["new_value"],
            }
            for r in resolved
        ]
        confirmed = _show_confirmation(
            title="Apply {} parameter changes?".format(len(resolved)),
            subtitle="All changes happen in one transaction (one Ctrl+Z step).",
            columns=columns,
            rows=rows,
            confirm_label="Set parameters",
        )
        if not confirmed:
            return {"cancelled": True}

    # Apply
    t = Transaction(doc, "Set {} parameter{}".format(
        len(resolved), "" if len(resolved) == 1 else "s"))
    results = []
    try:
        t.Start()
        for r in resolved:
            try:
                _set_param_value(r["parameter"], r["new_value"])
                results.append({
                    "element_id": r["element_id"],
                    "parameter":  r["parameter_name"],
                    "status":     "ok",
                })
            except Exception as e:
                results.append({
                    "element_id": r["element_id"],
                    "parameter":  r["parameter_name"],
                    "status":     "error",
                    "error":      "{}: {}".format(type(e).__name__, e),
                })
        t.Commit()
        ok = sum(1 for x in results if x["status"] == "ok")
        return {
            "applied_count": ok,
            "failed_count":  len(results) - ok,
            "results":       results,
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Parameter set failed (rolled back): {}".format(e)}


def _set_param_value(param, val):
    """Set a parameter to `val`, coercing as needed for its storage type."""
    st = str(param.StorageType)
    if st == "Double":
        if isinstance(val, (int, float)):
            param.Set(float(val))
        else:
            # Try the unit-aware string parser first (handles "12 ft 6 in" etc.)
            sval = str(val)
            try:
                if not param.SetValueString(sval):
                    param.Set(float(sval))
            except Exception:
                param.Set(float(sval))
    elif st == "Integer":
        param.Set(int(val))
    elif st == "String":
        param.Set(str(val) if val is not None else "")
    elif st == "ElementId":
        param.Set(_make_eid(int(val)))
    else:
        # Catch-all: try the raw value
        param.Set(val)


def _tool_duplicate_view(doc, input_dict):
    if doc is None:
        return {"error": "No active document."}

    raw = input_dict or {}
    src_id = raw.get("source_view_id")
    if src_id is None:
        return {"error": "source_view_id is required."}
    try:
        src_id = int(src_id)
    except (TypeError, ValueError):
        return {"error": "source_view_id must be an integer."}

    dup_type_str = raw.get("duplicate_type", "Duplicate") or "Duplicate"
    dup_map = {
        "Duplicate":     ViewDuplicateOption.Duplicate,
        "WithDetailing": ViewDuplicateOption.WithDetailing,
        "AsDependent":   ViewDuplicateOption.AsDependent,
    }
    if dup_type_str not in dup_map:
        return {"error": "duplicate_type must be one of: " + ", ".join(sorted(dup_map.keys()))}
    dup_opt = dup_map[dup_type_str]

    src_view = doc.GetElement(_make_eid(src_id))
    if src_view is None:
        return {"error": "source_view_id {} not found.".format(src_id)}
    if not isinstance(src_view, View):
        return {"error": "Element {} is not a view.".format(src_id)}
    try:
        if not src_view.CanViewBeDuplicated(dup_opt):
            return {"error": "View '{}' cannot be duplicated with type '{}'.".format(
                _safe_name(src_view), dup_type_str)}
    except Exception:
        pass  # Some view types may not implement CanViewBeDuplicated; try anyway

    new_name = raw.get("new_name")

    t = Transaction(doc, "Duplicate view: {}".format(_safe_name(src_view)))
    try:
        t.Start()
        new_eid = src_view.Duplicate(dup_opt)
        new_view = doc.GetElement(new_eid)
        if new_name and new_view is not None:
            try:
                new_view.Name = new_name
            except Exception:
                # Rename failure isn't fatal - Revit picks a default name.
                pass
        t.Commit()
        return {
            "new_view_id":   _eid_int(new_eid),
            "new_view_name": _safe_name(new_view) if new_view is not None else "",
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Duplicate failed (rolled back): {}".format(e)}


def _tool_delete_elements(doc, input_dict):
    if doc is None:
        return {"error": "No active document."}

    raw_ids = (input_dict or {}).get("element_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return {"error": "element_ids must be a non-empty list of integers."}

    rows = []
    valid_eids = []
    not_found = []
    for raw_id in raw_ids:
        try:
            i = int(raw_id)
        except (TypeError, ValueError):
            not_found.append(raw_id)
            continue
        el = doc.GetElement(_make_eid(i))
        if el is None:
            not_found.append(i)
            continue
        type_name, _ = _type_and_family(doc, el)
        rows.append({
            "id":        i,
            "category":  _category_name(el) or "(no category)",
            "type_name": type_name or "",
            "name":      _safe_name(el),
        })
        valid_eids.append(_make_eid(i))

    if not valid_eids:
        return {"error": "No valid elements to delete. Missing ids: {}".format(not_found)}

    # MANDATORY confirmation - delete is irreversible after committing
    # (only recoverable via immediate Ctrl+Z).
    footnote = ""
    if not_found:
        sample = ", ".join(str(x) for x in not_found[:10])
        if len(not_found) > 10:
            sample += " ... ({} total)".format(len(not_found))
        footnote = "Skipping {} id(s) that couldn't be resolved: {}".format(
            len(not_found), sample)

    columns = [
        {"key": "id",        "header": "ID"},
        {"key": "category",  "header": "Category"},
        {"key": "type_name", "header": "Type"},
        {"key": "name",      "header": "Name"},
    ]
    confirmed = _show_confirmation(
        title="Delete {} element{}?".format(
            len(valid_eids), "" if len(valid_eids) == 1 else "s"),
        subtitle=("This is destructive. Ctrl+Z immediately after the delete "
                  "is your only undo path."),
        columns=columns,
        rows=rows,
        confirm_label="Delete",
        destructive=True,
        footnote=footnote,
    )
    if not confirmed:
        return {"cancelled": True}

    # doc.Delete wants ICollection<ElementId>
    eid_collection = NetList[ElementId]()
    for eid in valid_eids:
        eid_collection.Add(eid)

    t = Transaction(doc, "Delete {} element{}".format(
        len(valid_eids), "" if len(valid_eids) == 1 else "s"))
    try:
        t.Start()
        deleted_set = doc.Delete(eid_collection)
        deleted_count = deleted_set.Count if deleted_set is not None else 0
        t.Commit()
        return {
            "deleted_count": deleted_count,
            "not_found":     not_found,
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Delete failed (rolled back): {}".format(e)}


# ===========================================================================
# WRITE TOOLS (v3.2 - Sheets & views)
# ===========================================================================

def _sheet_center_xyz(doc, sheet):
    """Returns an XYZ at the title-block centroid on `sheet`, or a
    reasonable fallback if no title block is on it yet. Used as the
    default placement origin for place_view_on_sheet."""
    try:
        col = (FilteredElementCollector(doc, sheet.Id)
               .OfCategory(BuiltInCategory.OST_TitleBlocks)
               .WhereElementIsNotElementType())
        for tb in col:
            bbox = tb.get_BoundingBox(sheet)
            if bbox is None:
                continue
            cx = (bbox.Min.X + bbox.Max.X) / 2.0
            cy = (bbox.Min.Y + bbox.Max.Y) / 2.0
            return XYZ(cx, cy, 0.0)
    except Exception:
        pass
    # Fallback - middle of a typical ARCH D sheet (~36"x24").
    return XYZ(1.5, 1.0, 0.0)


def _find_view_family_type(doc, view_family):
    """Return the first ViewFamilyType matching the given ViewFamily
    enum value, or None if the project has none."""
    try:
        for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
            try:
                if vft.ViewFamily == view_family:
                    return vft
            except Exception:
                continue
    except Exception:
        pass
    return None


def _tool_rename_element(doc, input_dict):
    """Rename an element by id. Works on anything with a writable
    Name property (sheets, views, schedules, types, etc.). Sheet
    *numbers* aren't Name - use set_element_parameters with
    parameter_name='Sheet Number' for that."""
    if doc is None:
        return {"error": "No active document."}

    eid_raw  = (input_dict or {}).get("element_id")
    new_name = (input_dict or {}).get("new_name")
    if eid_raw is None:
        return {"error": "element_id is required."}
    if not new_name or not str(new_name).strip():
        return {"error": "new_name is required (non-empty)."}
    try:
        eid_int_val = int(eid_raw)
    except (TypeError, ValueError):
        return {"error": "element_id must be an integer."}

    el = doc.GetElement(_make_eid(eid_int_val))
    if el is None:
        return {"error": "Element {} not found.".format(eid_int_val)}

    try:
        old_name = el.Name
    except Exception:
        return {"error": "Element {} has no Name property.".format(eid_int_val)}

    new_name_str = str(new_name).strip()
    if new_name_str == old_name:
        return {"id": eid_int_val, "old_name": old_name, "new_name": new_name_str,
                "note": "Name was already that value; no change made."}

    t = Transaction(doc, "Rename '{}' to '{}'".format(old_name, new_name_str))
    try:
        t.Start()
        el.Name = new_name_str
        t.Commit()
        return {
            "id":       eid_int_val,
            "category": _category_name(el),
            "old_name": old_name,
            "new_name": new_name_str,
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Rename failed (rolled back): {}".format(e)}


def _tool_place_view_on_sheet(doc, input_dict):
    """Place a view on a sheet as a viewport. The position defaults to
    the title block centroid on the sheet; the agent can override with
    explicit x_ft / y_ft coordinates (sheet-local feet)."""
    if doc is None:
        return {"error": "No active document."}

    view_raw  = (input_dict or {}).get("view_id")
    sheet_raw = (input_dict or {}).get("sheet_id")
    if view_raw is None:
        return {"error": "view_id is required."}
    if sheet_raw is None:
        return {"error": "sheet_id is required."}
    try:
        view_id_int  = int(view_raw)
        sheet_id_int = int(sheet_raw)
    except (TypeError, ValueError):
        return {"error": "view_id and sheet_id must be integers."}

    view  = doc.GetElement(_make_eid(view_id_int))
    sheet = doc.GetElement(_make_eid(sheet_id_int))
    if view is None:
        return {"error": "view_id {} not found.".format(view_id_int)}
    if not isinstance(view, View):
        return {"error": "Element {} is not a view.".format(view_id_int)}
    if view.IsTemplate:
        return {"error": "View {} is a template; templates can't be placed on sheets.".format(view_id_int)}
    if sheet is None or not isinstance(sheet, ViewSheet):
        return {"error": "sheet_id {} is not a ViewSheet.".format(sheet_id_int)}

    try:
        if not Viewport.CanAddViewToSheet(doc, sheet.Id, view.Id):
            return {"error": ("Revit refused to place view '{}' on sheet '{} - {}'. "
                              "It may already be on another sheet, or this view type "
                              "(legend / schedule) needs a different API.").format(
                _safe_name(view), sheet.SheetNumber or "", sheet.Name or "")}
    except Exception:
        pass  # CanAddViewToSheet may throw on certain view types; let Create surface the real error

    # Position: default to title-block center; overrides via x_ft / y_ft.
    x_override = (input_dict or {}).get("x_ft")
    y_override = (input_dict or {}).get("y_ft")
    if x_override is not None and y_override is not None:
        try:
            origin = XYZ(float(x_override), float(y_override), 0.0)
        except (TypeError, ValueError):
            return {"error": "x_ft and y_ft must be numbers (feet)."}
    else:
        origin = _sheet_center_xyz(doc, sheet)

    t = Transaction(doc, "Place '{}' on '{}'".format(
        _safe_name(view), sheet.SheetNumber or _safe_name(sheet)))
    try:
        t.Start()
        vp = Viewport.Create(doc, sheet.Id, view.Id, origin)
        t.Commit()
        return {
            "viewport_id": _eid_int(vp.Id),
            "sheet_id":    sheet_id_int,
            "view_id":     view_id_int,
            "placed_at":   {"x_ft": origin.X, "y_ft": origin.Y},
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Place failed (rolled back): {}".format(e)}


def _tool_apply_view_template(doc, input_dict):
    """Apply a view template (the one whose id you pass as template_id)
    to one or more views. The template itself must be a View where
    IsTemplate=True (use list_views with include_templates=true to
    find it). Confirms with a preview dialog when applying to >5 views."""
    if doc is None:
        return {"error": "No active document."}

    view_ids_raw    = (input_dict or {}).get("view_ids")
    template_id_raw = (input_dict or {}).get("template_id")
    if not isinstance(view_ids_raw, list) or not view_ids_raw:
        return {"error": "view_ids must be a non-empty list of integers."}
    if template_id_raw is None:
        return {"error": "template_id is required."}
    try:
        template_id_int = int(template_id_raw)
    except (TypeError, ValueError):
        return {"error": "template_id must be an integer."}

    template_el = doc.GetElement(_make_eid(template_id_int))
    if template_el is None or not isinstance(template_el, View):
        return {"error": "template_id {} is not a view.".format(template_id_int)}
    if not template_el.IsTemplate:
        return {"error": "View {} ('{}') is not a view template.".format(
            template_id_int, _safe_name(template_el))}

    # Validate target views first - no partial application from a bad batch.
    resolved = []
    errors = []
    for idx, raw in enumerate(view_ids_raw):
        try:
            i = int(raw)
        except (TypeError, ValueError):
            errors.append("view_ids[{}] must be an integer.".format(idx))
            continue
        v = doc.GetElement(_make_eid(i))
        if v is None:
            errors.append("view_ids[{}]: view {} not found.".format(idx, i))
            continue
        if not isinstance(v, View):
            errors.append("view_ids[{}]: element {} is not a view.".format(idx, i))
            continue
        if v.IsTemplate:
            errors.append("view_ids[{}]: view {} is itself a template.".format(idx, i))
            continue
        try:
            if not v.IsValidViewTemplate(template_el.Id):
                errors.append("view_ids[{}]: template '{}' isn't valid for view '{}'.".format(
                    idx, _safe_name(template_el), _safe_name(v)))
                continue
        except Exception:
            pass
        resolved.append({"id": i, "view": v, "name": _safe_name(v)})

    if errors:
        return {"error": "Validation failed:\n  - " + "\n  - ".join(errors)}

    # Confirm for batch > 5.
    if len(resolved) > 5:
        columns = [
            {"key": "id",   "header": "ID"},
            {"key": "name", "header": "View"},
        ]
        rows = [{"id": r["id"], "name": r["name"]} for r in resolved]
        confirmed = _show_confirmation(
            title="Apply view template to {} views?".format(len(resolved)),
            subtitle="Template: {}".format(_safe_name(template_el)),
            columns=columns,
            rows=rows,
            confirm_label="Apply template",
        )
        if not confirmed:
            return {"cancelled": True}

    t = Transaction(doc, "Apply view template '{}' to {} view{}".format(
        _safe_name(template_el), len(resolved), "" if len(resolved) == 1 else "s"))
    applied = []
    try:
        t.Start()
        for r in resolved:
            try:
                r["view"].ViewTemplateId = template_el.Id
                applied.append({"id": r["id"], "name": r["name"], "status": "ok"})
            except Exception as e:
                applied.append({
                    "id": r["id"], "name": r["name"], "status": "error",
                    "error": "{}: {}".format(type(e).__name__, e),
                })
        t.Commit()
        ok = sum(1 for x in applied if x["status"] == "ok")
        return {
            "applied_count": ok,
            "failed_count":  len(applied) - ok,
            "results":       applied,
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Apply template failed (rolled back): {}".format(e)}


_PLAN_TYPE_TO_VIEW_FAMILY = {
    "floor":   ViewFamily.FloorPlan,
    "ceiling": ViewFamily.CeilingPlan,
    "area":    ViewFamily.AreaPlan,
}


def _tool_create_view_plan(doc, input_dict):
    """Create a new plan view (floor, ceiling, or area) based on a
    level. Returns the new view's id."""
    if doc is None:
        return {"error": "No active document."}

    level_raw = (input_dict or {}).get("level_id")
    if level_raw is None:
        return {"error": "level_id is required."}
    try:
        level_id_int = int(level_raw)
    except (TypeError, ValueError):
        return {"error": "level_id must be an integer."}

    level_el = doc.GetElement(_make_eid(level_id_int))
    if level_el is None or not isinstance(level_el, Level):
        return {"error": "level_id {} is not a Level.".format(level_id_int)}

    plan_type = ((input_dict or {}).get("plan_type") or "floor").lower()
    view_family = _PLAN_TYPE_TO_VIEW_FAMILY.get(plan_type)
    if view_family is None:
        return {"error": "plan_type must be one of: {}".format(
            ", ".join(sorted(_PLAN_TYPE_TO_VIEW_FAMILY.keys())))}

    # Resolve ViewFamilyType.
    vft_raw = (input_dict or {}).get("view_family_type_id")
    vft = None
    if vft_raw is not None:
        try:
            cand = doc.GetElement(_make_eid(int(vft_raw)))
            if cand is not None and isinstance(cand, ViewFamilyType) and cand.ViewFamily == view_family:
                vft = cand
            else:
                return {"error": ("view_family_type_id {} is not a {} ViewFamilyType. "
                                  "Omit it to use the project default.").format(vft_raw, plan_type)}
        except (TypeError, ValueError):
            return {"error": "view_family_type_id must be an integer."}
    if vft is None:
        vft = _find_view_family_type(doc, view_family)
    if vft is None:
        return {"error": "No {} ViewFamilyType available in this project.".format(plan_type)}

    name  = (input_dict or {}).get("name")
    scale = (input_dict or {}).get("scale")

    t = Transaction(doc, "Create {} plan view".format(plan_type))
    try:
        t.Start()
        new_view = ViewPlan.Create(doc, vft.Id, level_el.Id)
        if name:
            try:
                new_view.Name = str(name).strip()
            except Exception:
                pass
        if scale is not None:
            try:
                new_view.Scale = int(scale)
            except Exception:
                pass
        t.Commit()
        return {
            "id":         _eid_int(new_view.Id),
            "name":       _safe_name(new_view),
            "plan_type":  plan_type,
            "level_id":   level_id_int,
            "level_name": _safe_name(level_el),
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Create plan failed (rolled back): {}".format(e)}


def _tool_create_view_3d(doc, input_dict):
    """Create a new 3D view. Defaults to an isometric view; pass
    perspective=true for a perspective view."""
    if doc is None:
        return {"error": "No active document."}

    vft = _find_view_family_type(doc, ViewFamily.ThreeDimensional)
    if vft is None:
        return {"error": "No 3D ViewFamilyType available in this project."}

    perspective = bool((input_dict or {}).get("perspective", False))
    name        = (input_dict or {}).get("name")

    t = Transaction(doc, "Create 3D view ({})".format("perspective" if perspective else "isometric"))
    try:
        t.Start()
        if perspective:
            new_view = View3D.CreatePerspective(doc, vft.Id)
        else:
            new_view = View3D.CreateIsometric(doc, vft.Id)
        if name:
            try:
                new_view.Name = str(name).strip()
            except Exception:
                pass
        t.Commit()
        return {
            "id":          _eid_int(new_view.Id),
            "name":        _safe_name(new_view),
            "perspective": perspective,
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Create 3D view failed (rolled back): {}".format(e)}


# ===========================================================================
# WRITE / READ TOOLS (v3.3 - Schedules + scope boxes + view basics)
# ===========================================================================

def _resolve_category_id(doc, category_str):
    """Look up an ElementId for a category, accepting:
       - a BuiltInCategory enum name like "OST_DuctTerminal"
       - a display name like "Air Terminals" (case-insensitive)
       - an integer id
       Returns None if no match."""
    if category_str is None or (isinstance(category_str, str) and not category_str.strip()):
        return None

    # int / int-like: treat as element id directly
    try:
        eid_val = int(category_str)
        return _make_eid(eid_val)
    except (TypeError, ValueError):
        pass

    s = str(category_str).strip()

    # Try BuiltInCategory enum name
    try:
        bic = getattr(BuiltInCategory, s, None)
        if bic is not None:
            try:
                cat = Category.GetCategory(doc, bic)
                if cat is not None:
                    return cat.Id
            except Exception:
                pass
            # Fallback: ElementId of the enum int value
            try:
                return ElementId(bic)
            except Exception:
                pass
    except Exception:
        pass

    # Try by display name (case-insensitive, top-level categories)
    target = s.lower()
    try:
        for cat in doc.Settings.Categories:
            try:
                if cat.Name.lower() == target:
                    return cat.Id
            except Exception:
                continue
    except Exception:
        pass

    return None


def _tool_list_schedulable_fields(doc, input_dict):
    """List the parameter names available as fields on a schedule for a
    given category. Used as a pre-step to create_schedule so the agent
    knows what to put in `field_names`.

    Implementation: creates a temporary ViewSchedule inside a transaction
    that we immediately RollBack. Nothing persists in the model. This is
    the documented way to query GetSchedulableFields()."""
    if doc is None:
        return {"error": "No active document."}

    category_str = (input_dict or {}).get("category")
    if not category_str:
        return {"error": "category is required."}

    cat_id = _resolve_category_id(doc, category_str)
    if cat_id is None:
        return {"error": "Category '{}' not found. Try a BuiltInCategory name "
                         "(e.g. 'OST_DuctTerminal') or the display name shown "
                         "in Revit (e.g. 'Air Terminals').".format(category_str)}

    t = Transaction(doc, "_query_schedulable_fields_TEMP")
    try:
        t.Start()
        try:
            temp = ViewSchedule.CreateSchedule(doc, cat_id)
        except Exception as e:
            t.RollBack()
            return {"error": "Could not create a query schedule for category '{}': {}".format(
                category_str, e)}

        out = []
        seen_names = set()
        try:
            for sf in temp.Definition.GetSchedulableFields():
                try:
                    nm = sf.GetName(doc) or ""
                except Exception:
                    nm = ""
                if not nm or nm in seen_names:
                    continue
                seen_names.add(nm)
                # Return JUST the name string. Schedules can have 100+
                # schedulable fields; including a structured object per
                # field would balloon this result and chew through the
                # per-minute input-token rate limit on the next round.
                out.append(nm)
        finally:
            # Always discard the temp schedule - we just wanted the query.
            t.RollBack()
        out.sort()
        return {"category": category_str, "count": len(out), "fields": out}
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "list_schedulable_fields failed: {}".format(e)}


def _tool_create_schedule(doc, input_dict):
    """Create a new ViewSchedule for a category, optionally pre-populated
    with the listed fields. Field names match what list_schedulable_fields
    returns. Missing fields are reported in the result rather than failing
    the whole call."""
    if doc is None:
        return {"error": "No active document."}

    category_str = (input_dict or {}).get("category")
    if not category_str:
        return {"error": "category is required."}

    cat_id = _resolve_category_id(doc, category_str)
    if cat_id is None:
        return {"error": "Category '{}' not found.".format(category_str)}

    field_names = (input_dict or {}).get("field_names") or []
    if not isinstance(field_names, list):
        return {"error": "field_names must be a list of strings."}

    schedule_name = (input_dict or {}).get("name")

    t = Transaction(doc, "Create schedule for {}".format(category_str))
    try:
        t.Start()
        schedule = ViewSchedule.CreateSchedule(doc, cat_id)

        if schedule_name:
            try:
                schedule.Name = str(schedule_name).strip()
            except Exception:
                pass

        added = []
        missing = []
        if field_names:
            # Build name -> SchedulableField map
            name_map = {}
            for sf in schedule.Definition.GetSchedulableFields():
                try:
                    nm = sf.GetName(doc) or ""
                except Exception:
                    nm = ""
                if nm:
                    name_map[nm.lower()] = sf

            for raw in field_names:
                nm_str = str(raw).strip()
                sf = name_map.get(nm_str.lower())
                if sf is None:
                    missing.append(nm_str)
                    continue
                try:
                    schedule.Definition.AddField(sf)
                    added.append(nm_str)
                except Exception as e:
                    missing.append("{} ({})".format(nm_str, e))

        t.Commit()
        return {
            "id":             _eid_int(schedule.Id),
            "name":           _safe_name(schedule),
            "category":       category_str,
            "added_fields":   added,
            "missing_fields": missing,
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Create schedule failed (rolled back): {}".format(e)}


_FILTER_OP_MAP = {
    "equals":           ScheduleFilterType.Equal,
    "not_equals":       ScheduleFilterType.NotEqual,
    "greater":          ScheduleFilterType.GreaterThan,
    "greater_or_equal": ScheduleFilterType.GreaterThanOrEqual,
    "less":             ScheduleFilterType.LessThan,
    "less_or_equal":    ScheduleFilterType.LessThanOrEqual,
    "contains":         ScheduleFilterType.Contains,
    "begins_with":      ScheduleFilterType.BeginsWith,
    "ends_with":        ScheduleFilterType.EndsWith,
}


def _tool_add_schedule_filter(doc, input_dict):
    """Add a filter rule to an existing schedule. The field must already
    be one of the schedule's columns (use create_schedule's field_names
    or modify the schedule in the UI first)."""
    if doc is None:
        return {"error": "No active document."}

    sched_raw = (input_dict or {}).get("schedule_id")
    field_name = (input_dict or {}).get("field_name")
    op_str = (input_dict or {}).get("operator")
    value = (input_dict or {}).get("value")

    if sched_raw is None:
        return {"error": "schedule_id is required."}
    if not field_name:
        return {"error": "field_name is required."}
    if not op_str:
        return {"error": "operator is required. Valid: " + ", ".join(sorted(_FILTER_OP_MAP.keys()))}
    if value is None:
        return {"error": "value is required."}

    try:
        sched_id_int = int(sched_raw)
    except (TypeError, ValueError):
        return {"error": "schedule_id must be an integer."}

    schedule = doc.GetElement(_make_eid(sched_id_int))
    if schedule is None or not isinstance(schedule, ViewSchedule):
        return {"error": "schedule_id {} is not a ViewSchedule.".format(sched_id_int)}

    op = _FILTER_OP_MAP.get(op_str.lower())
    if op is None:
        return {"error": "Unknown operator '{}'. Valid: {}".format(
            op_str, ", ".join(sorted(_FILTER_OP_MAP.keys())))}

    # Find the schedule field by name
    target_field = None
    try:
        definition = schedule.Definition
        for field_id in definition.GetFieldOrder():
            f = definition.GetField(field_id)
            try:
                fn = f.GetName() or ""
            except Exception:
                fn = ""
            if fn.lower() == str(field_name).lower():
                target_field = f
                break
    except Exception as e:
        return {"error": "Couldn't read schedule fields: {}".format(e)}

    if target_field is None:
        return {"error": ("Field '{}' isn't a column on this schedule. Add it first "
                          "(create_schedule with field_names, or modify the schedule "
                          "in the UI).").format(field_name)}

    t = Transaction(doc, "Add filter to {}".format(_safe_name(schedule)))
    try:
        t.Start()
        # Pick the right ScheduleFilter overload based on the value's type.
        # Falls back to string if nothing else fits.
        sf = None
        if isinstance(value, bool):
            sf = ScheduleFilter(target_field.FieldId, op, 1 if value else 0)
        elif isinstance(value, int):
            sf = ScheduleFilter(target_field.FieldId, op, int(value))
        elif isinstance(value, float):
            sf = ScheduleFilter(target_field.FieldId, op, float(value))
        else:
            sf = ScheduleFilter(target_field.FieldId, op, str(value))
        schedule.Definition.AddFilter(sf)
        t.Commit()
        return {
            "schedule_id": sched_id_int,
            "field":       field_name,
            "operator":    op_str,
            "value":       value,
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Add filter failed (rolled back): {}".format(e)}


def _tool_list_scope_boxes(doc, input_dict):
    """List all scope boxes (OST_VolumeOfInterest) in the active document."""
    if doc is None:
        return {"error": "No active document."}
    try:
        col = (FilteredElementCollector(doc)
               .OfCategory(BuiltInCategory.OST_VolumeOfInterest)
               .WhereElementIsNotElementType())
        out = []
        for sb in col:
            try:
                out.append({"id": _eid_int(sb.Id), "name": _safe_name(sb)})
            except Exception:
                continue
        out.sort(key=lambda r: r.get("name") or "")
        return {"count": len(out), "scope_boxes": out}
    except Exception as e:
        return {"error": "list_scope_boxes failed: {}".format(e)}


def _tool_apply_scope_box_to_view(doc, input_dict):
    """Apply (or clear) a scope box on a view. Pass scope_box_id to
    apply; omit it or pass null to clear the view's current scope box."""
    if doc is None:
        return {"error": "No active document."}

    view_raw = (input_dict or {}).get("view_id")
    if view_raw is None:
        return {"error": "view_id is required."}
    try:
        view_id_int = int(view_raw)
    except (TypeError, ValueError):
        return {"error": "view_id must be an integer."}

    view = doc.GetElement(_make_eid(view_id_int))
    if view is None or not isinstance(view, View):
        return {"error": "view_id {} is not a view.".format(view_id_int)}

    sb_raw = (input_dict or {}).get("scope_box_id")
    target_sb_id = ElementId.InvalidElementId
    if sb_raw is not None and sb_raw != "":
        try:
            sb_int = int(sb_raw)
        except (TypeError, ValueError):
            return {"error": "scope_box_id must be an integer (or null to clear)."}
        sb_el = doc.GetElement(_make_eid(sb_int))
        if sb_el is None:
            return {"error": "Scope box {} not found.".format(sb_int)}
        target_sb_id = sb_el.Id

    # The view's scope box assignment is exposed as the
    # VIEWER_VOLUME_OF_INTEREST_CROP parameter (BuiltInParameter).
    p = view.get_Parameter(BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
    if p is None:
        return {"error": "View {} doesn't support a scope box assignment.".format(view_id_int)}
    if p.IsReadOnly:
        return {"error": "View {}'s scope box assignment is read-only here.".format(view_id_int)}

    t = Transaction(doc, "Apply scope box to view")
    try:
        t.Start()
        p.Set(target_sb_id)
        t.Commit()
        return {
            "view_id":       view_id_int,
            "scope_box_id":  _eid_int(target_sb_id) if _eid_int(target_sb_id) != -1 else None,
            "cleared":       (_eid_int(target_sb_id) == -1),
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Apply scope box failed (rolled back): {}".format(e)}


def _tool_set_view_scale(doc, input_dict):
    """Set a view's scale (the integer denominator: 96 = 1/8\" = 1'-0\",
    48 = 1/4\" = 1'-0\", etc.)."""
    if doc is None:
        return {"error": "No active document."}
    view_raw  = (input_dict or {}).get("view_id")
    scale_raw = (input_dict or {}).get("scale")
    if view_raw is None or scale_raw is None:
        return {"error": "view_id and scale are required."}
    try:
        view_id_int = int(view_raw)
        scale_int   = int(scale_raw)
    except (TypeError, ValueError):
        return {"error": "view_id and scale must be integers."}
    if scale_int <= 0:
        return {"error": "scale must be a positive integer."}

    view = doc.GetElement(_make_eid(view_id_int))
    if view is None or not isinstance(view, View):
        return {"error": "view_id {} is not a view.".format(view_id_int)}

    t = Transaction(doc, "Set view scale to 1:{}".format(scale_int))
    try:
        t.Start()
        view.Scale = scale_int
        t.Commit()
        return {
            "view_id": view_id_int,
            "scale":   scale_int,
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Set scale failed (rolled back): {}".format(e)}


def _tool_hide_categories_in_view(doc, input_dict):
    """Hide or show categories in a view by category name. Accepts
    BuiltInCategory enum names ('OST_Walls') or display names ('Walls').
    Use action='show' to un-hide previously hidden categories."""
    if doc is None:
        return {"error": "No active document."}

    view_raw = (input_dict or {}).get("view_id")
    if view_raw is None:
        return {"error": "view_id is required."}
    try:
        view_id_int = int(view_raw)
    except (TypeError, ValueError):
        return {"error": "view_id must be an integer."}

    view = doc.GetElement(_make_eid(view_id_int))
    if view is None or not isinstance(view, View):
        return {"error": "view_id {} is not a view.".format(view_id_int)}

    cats_raw = (input_dict or {}).get("categories")
    if not isinstance(cats_raw, list) or not cats_raw:
        return {"error": "categories must be a non-empty list of category names."}

    action = ((input_dict or {}).get("action") or "hide").lower()
    if action not in ("hide", "show"):
        return {"error": "action must be 'hide' or 'show'."}

    resolved = []
    not_found = []
    for raw in cats_raw:
        cat_id = _resolve_category_id(doc, raw)
        if cat_id is None or _eid_int(cat_id) == -1:
            not_found.append(str(raw))
            continue
        resolved.append({"input": str(raw), "id": cat_id})

    if not resolved:
        return {"error": "No valid categories. Not found: {}".format(not_found)}

    hidden_flag = (action == "hide")
    t = Transaction(doc, "{} {} categor{} in view".format(
        "Hide" if hidden_flag else "Show",
        len(resolved),
        "y" if len(resolved) == 1 else "ies"))
    applied = []
    errors = []
    try:
        t.Start()
        for r in resolved:
            try:
                # Some categories aren't hideable in some view types
                # (e.g. you can't hide Lines in a schedule view).
                # CanCategoryBeHidden returns false for those.
                if hasattr(view, "CanCategoryBeHidden"):
                    if not view.CanCategoryBeHidden(r["id"]):
                        errors.append("{}: not hideable in this view".format(r["input"]))
                        continue
                view.SetCategoryHidden(r["id"], hidden_flag)
                applied.append(r["input"])
            except Exception as e:
                errors.append("{}: {}".format(r["input"], e))
        t.Commit()
        return {
            "view_id":         view_id_int,
            "action":          action,
            "applied":         applied,
            "errors":          errors,
            "not_found":       not_found,
            "applied_count":   len(applied),
        }
    except Exception as e:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        return {"error": "Visibility change failed (rolled back): {}".format(e)}


# ---------------------------------------------------------------------------
# Linked-model tools (v4.1)
# ---------------------------------------------------------------------------
#
# dbHMS works MEP in a host .rvt with linked architecture + sometimes
# linked structural. These tools let the chatbot reach across that link
# boundary to answer questions like "what rooms are in the arch link"
# or "what level is this wall on" - and toggle a link's visibility/
# halftone across many views in one transaction.
#
# Important: ids returned from a LINKED document refer to that link's
# internal element ids, NOT host-document ids. They're useful for
# describing what's in the link but most write tools in this script
# operate on the HOST doc and won't accept linked-doc ids.


def _bic_or_category_from_name(link_doc, category_str):
    """Resolve a category descriptor (BuiltInCategory name, display name,
    or integer enum value) against a linked document. Returns a tuple
    (built_in_category_or_None, category_or_None). Pass whichever is
    non-None to OfCategory / OfCategoryId."""
    if category_str is None:
        return (None, None)
    # Integer literal: convert to BuiltInCategory by enum value.
    try:
        ival = int(category_str)
        try:
            bic = BuiltInCategory(ival)
            return (bic, None)
        except Exception:
            return (None, None)
    except (TypeError, ValueError):
        pass
    s = str(category_str).strip()
    # BuiltInCategory enum name (the recommended form: e.g. OST_Walls).
    bic = getattr(BuiltInCategory, s, None)
    if bic is not None:
        return (bic, None)
    # Display name fallback (case-insensitive, top-level categories in link).
    try:
        target = s.lower()
        for cat in link_doc.Settings.Categories:
            try:
                if (cat.Name or "").lower() == target:
                    return (None, cat)
            except Exception:
                continue
    except Exception:
        pass
    return (None, None)


def _link_inst_or_error(doc, link_id):
    """Resolve `link_id` to a RevitLinkInstance or return an error dict."""
    if link_id is None:
        return None, {"error": "link_id is required. Call list_links to find it."}
    try:
        inst = doc.GetElement(_make_eid(int(link_id)))
    except Exception:
        return None, {"error": "Invalid link_id: {}".format(link_id)}
    if inst is None:
        return None, {"error": "No element with id {}.".format(link_id)}
    if not isinstance(inst, RevitLinkInstance):
        return None, {"error": ("Element {} is not a RevitLinkInstance "
                                "(it's a {}).").format(link_id, type(inst).__name__)}
    return inst, None


def _tool_list_links(doc, input_dict):
    """List every Revit link in the active document with load status,
    attachment type, and the link doc's title/path when loaded."""
    if doc is None:
        return {"error": "No active Revit document."}
    try:
        col = FilteredElementCollector(doc).OfClass(RevitLinkInstance)
        out = []
        for inst in col:
            link_doc = None
            try:
                link_doc = inst.GetLinkDocument()
            except Exception:
                link_doc = None

            link_type = None
            try:
                tid = inst.GetTypeId()
                if tid is not None and _eid_int(tid) != -1:
                    link_type = doc.GetElement(tid)
            except Exception:
                pass

            attachment_type = None
            try:
                if link_type is not None:
                    attachment_type = str(link_type.AttachmentType)
            except Exception:
                pass

            row = {
                "id":              _eid_int(inst.Id),
                "name":            _safe_name(inst),
                "type_id":         _eid_int(link_type.Id) if link_type else None,
                "type_name":       _safe_name(link_type) if link_type else None,
                "is_loaded":       link_doc is not None,
                "attachment_type": attachment_type,
            }
            if link_doc is not None:
                try:
                    row["link_doc_title"] = link_doc.Title or ""
                except Exception:
                    pass
                try:
                    row["link_doc_path"] = link_doc.PathName or ""
                except Exception:
                    pass
            out.append(row)
        out.sort(key=lambda r: (r.get("name") or ""))
        return {"count": len(out), "links": out}
    except Exception as e:
        return {"error": "list_links failed: {}".format(e)}


def _tool_get_rooms_in_link(doc, input_dict):
    """Read rooms / spaces from a linked document. If link_id is omitted,
    rooms from every loaded link are aggregated; in that case each row
    is tagged with the source link's id+name."""
    if doc is None:
        return {"error": "No active Revit document."}

    inp = input_dict or {}
    link_id       = inp.get("link_id")
    max_results   = int(inp.get("max_results") or 500)
    name_sub      = (inp.get("name_contains") or "").lower()
    min_area      = inp.get("min_area_sqft")
    if min_area is not None:
        try:
            min_area = float(min_area)
        except Exception:
            min_area = None

    # Build the target list (one or many links).
    targets = []
    try:
        if link_id is None:
            for inst in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
                try:
                    ld = inst.GetLinkDocument()
                except Exception:
                    ld = None
                if ld is not None:
                    targets.append((inst, ld))
        else:
            inst, err = _link_inst_or_error(doc, link_id)
            if err:
                return err
            try:
                ld = inst.GetLinkDocument()
            except Exception:
                ld = None
            if ld is None:
                return {"error": "Link is not loaded; no document available."}
            targets.append((inst, ld))
    except Exception as e:
        return {"error": "get_rooms_in_link link lookup failed: {}".format(e)}

    if not targets:
        return {"count": 0, "rooms": [],
                "note": "No loaded links in this document."}

    out = []
    truncated = False
    try:
        for inst, link_doc in targets:
            col = FilteredElementCollector(link_doc).OfCategory(
                BuiltInCategory.OST_Rooms)
            for r in col:
                nm = _safe_name(r) or ""
                if name_sub and name_sub not in nm.lower():
                    continue
                try:
                    area = float(r.Area)
                except Exception:
                    area = 0.0
                if min_area is not None and area < min_area:
                    continue
                level_name = None
                try:
                    lvl = link_doc.GetElement(r.LevelId)
                    if lvl is not None:
                        level_name = _safe_name(lvl)
                except Exception:
                    pass
                number = ""
                try:
                    p = r.LookupParameter("Number")
                    if p is not None:
                        number = p.AsString() or ""
                except Exception:
                    pass
                out.append({
                    "id":         _eid_int(r.Id),
                    "link_id":    _eid_int(inst.Id),
                    "link_name":  _safe_name(inst),
                    "number":     number,
                    "name":       nm,
                    "area_sqft":  round(area, 2),
                    "level":      level_name,
                    "unbounded":  area <= 0.0,
                })
                if len(out) >= max_results:
                    truncated = True
                    break
            if truncated:
                break
    except Exception as e:
        return {"error": "get_rooms_in_link enumerate failed: {}".format(e)}

    out.sort(key=lambda r: (r.get("level") or "",
                            r.get("number") or "",
                            r.get("name") or ""))
    result = {"count": len(out), "rooms": out}
    if truncated:
        result["truncated"] = True
        result["note"] = "Truncated at max_results={}.".format(max_results)
    return result


def _tool_get_elements_from_link(doc, input_dict):
    """Query elements in a linked document by category. Returns
    descriptive rows but the ids are LINK-doc ids - useful for prose
    answers, NOT for host-doc write tools."""
    if doc is None:
        return {"error": "No active Revit document."}

    inp = input_dict or {}
    link_id  = inp.get("link_id")
    category = inp.get("category")
    name_sub = (inp.get("name_contains") or "").lower()
    max_results = int(inp.get("max_results") or 200)

    if not category:
        return {"error": ("category is required (e.g. 'OST_Walls', "
                          "'OST_Doors', 'OST_Floors', 'OST_Levels').")}

    inst, err = _link_inst_or_error(doc, link_id)
    if err:
        return err
    try:
        link_doc = inst.GetLinkDocument()
    except Exception:
        link_doc = None
    if link_doc is None:
        return {"error": "Link is not loaded."}

    bic, cat_obj = _bic_or_category_from_name(link_doc, category)
    if bic is None and cat_obj is None:
        return {"error": ("Unknown category: {}. Use a BuiltInCategory "
                          "name like 'OST_Walls', 'OST_Doors', "
                          "'OST_Floors', 'OST_Levels'.").format(category)}

    out = []
    truncated = False
    try:
        if bic is not None:
            col = FilteredElementCollector(link_doc).OfCategory(bic) \
                                                   .WhereElementIsNotElementType()
        else:
            col = FilteredElementCollector(link_doc).OfCategoryId(cat_obj.Id) \
                                                    .WhereElementIsNotElementType()
        for el in col:
            nm = _safe_name(el) or ""
            if name_sub and name_sub not in nm.lower():
                continue
            row = {
                "id":       _eid_int(el.Id),
                "name":     nm,
            }
            try:
                row["category"] = el.Category.Name if el.Category else ""
            except Exception:
                row["category"] = ""
            # Family + type when available
            try:
                tid = el.GetTypeId()
                if tid is not None and _eid_int(tid) != -1:
                    type_el = link_doc.GetElement(tid)
                    if type_el is not None:
                        row["type"] = _safe_name(type_el)
                        try:
                            row["family"] = type_el.FamilyName
                        except Exception:
                            pass
            except Exception:
                pass
            # Best-effort level lookup
            try:
                lp = el.LookupParameter("Level") or el.LookupParameter("Reference Level")
                if lp is not None and lp.StorageType == StorageType.ElementId:
                    lvl = link_doc.GetElement(lp.AsElementId())
                    if lvl is not None:
                        row["level"] = _safe_name(lvl)
            except Exception:
                pass
            out.append(row)
            if len(out) >= max_results:
                truncated = True
                break
    except Exception as e:
        return {"error": "get_elements_from_link enumerate failed: {}".format(e)}

    out.sort(key=lambda r: (r.get("category") or "", r.get("name") or ""))
    result = {
        "link_id":   _eid_int(inst.Id),
        "link_name": _safe_name(inst),
        "category":  category,
        "count":     len(out),
        "elements":  out,
    }
    if truncated:
        result["truncated"] = True
        result["note"] = ("Truncated at max_results={}. Refine "
                          "name_contains to narrow.").format(max_results)
    return result


def _tool_get_link_visibility_in_view(doc, input_dict):
    """Return whether a link is hidden / halftoned in a specific view."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    view_id = inp.get("view_id")
    link_id = inp.get("link_id")
    if view_id is None or link_id is None:
        return {"error": "Both view_id and link_id are required."}

    try:
        view = doc.GetElement(_make_eid(int(view_id)))
    except Exception:
        return {"error": "Invalid view_id."}
    if view is None or not isinstance(view, View):
        return {"error": "Element {} is not a View.".format(view_id)}

    inst, err = _link_inst_or_error(doc, link_id)
    if err:
        return err

    hidden = None
    try:
        hidden = bool(view.IsElementHiddenInView(inst.Id))
    except Exception:
        pass
    halftone = None
    try:
        ogs = view.GetElementOverrides(inst.Id)
        if ogs is not None:
            halftone = bool(ogs.Halftone)
    except Exception:
        pass

    return {
        "view_id":   _eid_int(view.Id),
        "view_name": _safe_name(view),
        "link_id":   _eid_int(inst.Id),
        "link_name": _safe_name(inst),
        "hidden":    hidden,
        "halftone":  halftone,
    }


def _tool_set_link_visibility_in_view(doc, input_dict):
    """Set hidden / halftone / normal for a link across one or more
    views. All updates wrap in a single Revit transaction so Ctrl+Z
    undoes the whole batch."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    view_ids = inp.get("view_ids")
    if not view_ids and inp.get("view_id") is not None:
        view_ids = [inp.get("view_id")]
    link_id = inp.get("link_id")
    mode    = (inp.get("mode") or "normal").lower()

    if not view_ids:
        return {"error": "Either view_ids (list) or view_id is required."}
    if link_id is None:
        return {"error": "link_id is required."}
    if mode not in ("hidden", "halftone", "normal"):
        return {"error": "mode must be 'hidden', 'halftone', or 'normal'."}

    inst, err = _link_inst_or_error(doc, link_id)
    if err:
        return err

    # Resolve views, splitting bad ones into an errors bucket.
    views = []
    errors = []
    for vid in view_ids:
        try:
            v = doc.GetElement(_make_eid(int(vid)))
        except Exception:
            errors.append({"view_id": vid, "error": "invalid id"})
            continue
        if v is None or not isinstance(v, View):
            errors.append({"view_id": vid, "error": "not a View"})
            continue
        if v.IsTemplate:
            errors.append({
                "view_id":   _eid_int(v.Id),
                "view_name": _safe_name(v),
                "error":     "view is a template - cannot set link visibility there",
            })
            continue
        views.append(v)

    if not views:
        return {"error": "No valid views to update.", "errors": errors}

    link_eid_list = NetList[ElementId]()
    link_eid_list.Add(inst.Id)

    updated = []
    try:
        t = Transaction(doc, "Set link visibility ({}) in {} view(s)".format(
            mode, len(views)))
        t.Start()
        try:
            for v in views:
                if mode == "hidden":
                    # Hide it; also reset any halftone override so the
                    # visual state on re-show is clean.
                    try:
                        if not v.IsElementHiddenInView(inst.Id):
                            v.HideElements(link_eid_list)
                    except Exception:
                        pass
                    try:
                        v.SetElementOverrides(inst.Id, OverrideGraphicSettings())
                    except Exception:
                        pass
                elif mode == "halftone":
                    # Unhide if hidden, then apply halftone override.
                    try:
                        if v.IsElementHiddenInView(inst.Id):
                            v.UnhideElements(link_eid_list)
                    except Exception:
                        pass
                    try:
                        ogs = OverrideGraphicSettings()
                        ogs.SetHalftone(True)
                        v.SetElementOverrides(inst.Id, ogs)
                    except Exception:
                        pass
                else:  # normal: clear hide + clear override
                    try:
                        if v.IsElementHiddenInView(inst.Id):
                            v.UnhideElements(link_eid_list)
                    except Exception:
                        pass
                    try:
                        v.SetElementOverrides(inst.Id, OverrideGraphicSettings())
                    except Exception:
                        pass
                updated.append({
                    "view_id":   _eid_int(v.Id),
                    "view_name": _safe_name(v),
                })
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "Transaction failed (rolled back): {}".format(e),
                    "errors": errors}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    result = {
        "link_id":       _eid_int(inst.Id),
        "link_name":     _safe_name(inst),
        "mode":          mode,
        "updated":       updated,
        "updated_count": len(updated),
    }
    if errors:
        result["errors"] = errors
    return result


# ---------------------------------------------------------------------------
# Annotation + filter tools (v4.2)
# ---------------------------------------------------------------------------
#
# Day-to-day modeling chores: tag a bunch of equipment on a view, drop a
# text note, build view filters with color overrides. These cover the
# things Nathan would otherwise click through dozens of times manually.


def _color_from_hex(hex_str):
    """Parse '#RRGGBB' / 'RRGGBB' / 'red' / etc. -> Autodesk.Revit.DB.Color.
    Returns None for unrecognized input."""
    if not hex_str:
        return None
    s = str(hex_str).strip()
    if s.startswith("#"):
        s = s[1:]
    # Named-color shortcuts engineers use in conversation.
    named = {
        "red":     "FF0000",
        "green":   "00AA00",
        "blue":    "0000FF",
        "yellow":  "FFFF00",
        "orange":  "FFA500",
        "purple":  "800080",
        "magenta": "FF00FF",
        "cyan":    "00FFFF",
        "black":   "000000",
        "white":   "FFFFFF",
        "gray":    "808080",
        "grey":    "808080",
    }
    if s.lower() in named:
        s = named[s.lower()]
    if len(s) != 6:
        return None
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return RevitColor(r, g, b)
    except Exception:
        return None


def _resolve_builtin_parameter_id(name):
    """Resolve a BuiltInParameter enum name (e.g. 'RBS_PIPE_DIAMETER_PARAM')
    to an ElementId. Returns None if not a recognized enum name."""
    if not name:
        return None
    s = str(name).strip()
    # Allow "BuiltInParameter.X" prefix
    if s.startswith("BuiltInParameter."):
        s = s[len("BuiltInParameter."):]
    bip = getattr(BuiltInParameter, s, None)
    if bip is None:
        return None
    try:
        return ElementId(bip)
    except Exception:
        return None


def _bbox_center_in_view(view, el):
    """Return an XYZ at the center of an element's bounding box as seen
    in the given view. Falls back to the element's location point /
    origin when no BB is available. None if nothing usable."""
    try:
        bb = el.get_BoundingBox(view)
    except Exception:
        bb = None
    if bb is None:
        try:
            bb = el.get_BoundingBox(None)
        except Exception:
            bb = None
    if bb is not None:
        try:
            cx = (bb.Min.X + bb.Max.X) / 2.0
            cy = (bb.Min.Y + bb.Max.Y) / 2.0
            cz = (bb.Min.Z + bb.Max.Z) / 2.0
            return XYZ(cx, cy, cz)
        except Exception:
            pass
    # Fall back to LocationPoint if the element has one
    try:
        loc = el.Location
        if loc is not None and hasattr(loc, "Point") and loc.Point is not None:
            return loc.Point
    except Exception:
        pass
    return None


def _tool_list_tag_types(doc, input_dict):
    """List tag types available in the document. Each tag belongs to a
    Tags family category (e.g. 'Mechanical Equipment Tags') - the
    family_category_name tells you what category the tag is for. Filter
    by family_category_contains to narrow down (e.g. 'Duct' finds Duct
    Tags, Duct Fitting Tags, Duct Accessory Tags, etc.)."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    cat_sub  = (inp.get("family_category_contains") or "").lower()
    name_sub = (inp.get("name_contains") or "").lower()
    max_results = int(inp.get("max_results") or 200)
    try:
        col = FilteredElementCollector(doc).OfClass(FamilySymbol)
        out = []
        for sym in col:
            try:
                fam_cat = sym.Family.FamilyCategory
            except Exception:
                fam_cat = None
            if fam_cat is None:
                continue
            try:
                fam_cat_name = fam_cat.Name or ""
            except Exception:
                fam_cat_name = ""
            # Only categories whose name contains "Tag" (or "Tags").
            if "tag" not in fam_cat_name.lower():
                continue
            if cat_sub and cat_sub not in fam_cat_name.lower():
                continue
            try:
                fam_name = sym.Family.Name or ""
            except Exception:
                fam_name = ""
            try:
                type_name = sym.Name or ""
            except Exception:
                type_name = ""
            full = "{} : {}".format(fam_name, type_name).lower()
            if name_sub and name_sub not in full:
                continue
            out.append({
                "id":                   _eid_int(sym.Id),
                "family":               fam_name,
                "type":                 type_name,
                "family_category_name": fam_cat_name,
            })
            if len(out) >= max_results:
                break
        out.sort(key=lambda r: (r.get("family_category_name") or "",
                                r.get("family") or "",
                                r.get("type") or ""))
        return {"count": len(out), "tag_types": out}
    except Exception as e:
        return {"error": "list_tag_types failed: {}".format(e)}


def _tool_list_filters(doc, input_dict):
    """List existing ParameterFilterElements (view filters) in the doc.
    These are the filters that get added to a view via Visibility
    Graphics > Filters tab."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    name_sub = (inp.get("name_contains") or "").lower()
    try:
        col = FilteredElementCollector(doc).OfClass(ParameterFilterElement)
        out = []
        for fe in col:
            nm = _safe_name(fe) or ""
            if name_sub and name_sub not in nm.lower():
                continue
            cats = []
            try:
                for cat_id in fe.GetCategories():
                    try:
                        cat = Category.GetCategory(doc, cat_id)
                        if cat is not None:
                            cats.append(cat.Name or "")
                    except Exception:
                        pass
            except Exception:
                pass
            out.append({
                "id":         _eid_int(fe.Id),
                "name":       nm,
                "categories": cats,
            })
        out.sort(key=lambda r: r.get("name") or "")
        return {"count": len(out), "filters": out}
    except Exception as e:
        return {"error": "list_filters failed: {}".format(e)}


def _tool_tag_elements(doc, input_dict):
    """Place tags for a list of elements in a view. The tag's TYPE
    (FamilySymbol) determines what's tagged - it must be a Tag family
    for the category of the elements being tagged. Use list_tag_types
    to find a tag_type_id."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    view_id      = inp.get("view_id")
    element_ids  = inp.get("element_ids") or []
    tag_type_id  = inp.get("tag_type_id")
    orientation  = (inp.get("orientation") or "horizontal").lower()
    leader       = bool(inp.get("leader", False))

    if view_id is None:
        return {"error": "view_id is required."}
    if not element_ids:
        return {"error": "element_ids is required (list of integer ids)."}
    if tag_type_id is None:
        return {"error": ("tag_type_id is required. Call list_tag_types "
                          "first to find a tag type for the element's "
                          "category.")}

    try:
        view = doc.GetElement(_make_eid(int(view_id)))
    except Exception:
        return {"error": "Invalid view_id."}
    if view is None or not isinstance(view, View):
        return {"error": "Element {} is not a View.".format(view_id)}
    if view.IsTemplate:
        return {"error": "Cannot tag in a view template."}

    try:
        tag_sym = doc.GetElement(_make_eid(int(tag_type_id)))
    except Exception:
        return {"error": "Invalid tag_type_id."}
    if tag_sym is None or not isinstance(tag_sym, FamilySymbol):
        return {"error": "tag_type_id is not a FamilySymbol."}

    # Activate the tag symbol so Revit will let us place instances.
    try:
        if not tag_sym.IsActive:
            # Activation has to happen INSIDE a transaction; we'll start
            # one below.
            pass
    except Exception:
        pass

    orient = (TagOrientation.Vertical
              if orientation.startswith("v") else
              TagOrientation.Horizontal)

    tagged = []
    failed = []
    try:
        t = Transaction(doc, "Tag {} element(s) in '{}'".format(
            len(element_ids), _safe_name(view) or "view"))
        t.Start()
        try:
            # Activate the symbol (must be inside a transaction).
            try:
                if not tag_sym.IsActive:
                    tag_sym.Activate()
                    doc.Regenerate()
            except Exception:
                pass

            for eid in element_ids:
                try:
                    el = doc.GetElement(_make_eid(int(eid)))
                except Exception:
                    failed.append({"element_id": eid, "error": "invalid id"})
                    continue
                if el is None:
                    failed.append({"element_id": eid, "error": "not found"})
                    continue
                # Build a Reference to the element.
                try:
                    ref = Reference(el)
                except Exception as e:
                    failed.append({"element_id": eid,
                                   "error": "could not build Reference: {}".format(e)})
                    continue
                pt = _bbox_center_in_view(view, el) or XYZ(0, 0, 0)
                try:
                    new_tag = IndependentTag.Create(
                        doc, tag_sym.Id, view.Id, ref, leader, orient, pt)
                    tagged.append({
                        "element_id": _eid_int(el.Id),
                        "tag_id":     _eid_int(new_tag.Id),
                    })
                except Exception as e:
                    failed.append({
                        "element_id": _eid_int(el.Id),
                        "name":       _safe_name(el),
                        "error":      str(e),
                    })
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "Transaction failed (rolled back): {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    result = {
        "view_id":      _eid_int(view.Id),
        "view_name":    _safe_name(view),
        "tagged":       tagged,
        "tagged_count": len(tagged),
    }
    if failed:
        result["failed"]       = failed
        result["failed_count"] = len(failed)
    return result


def _tool_place_text_note(doc, input_dict):
    """Drop a text note in a view. Position is either explicit world
    coords (x_ft / y_ft / z_ft) OR the bounding-box center of a target
    element (element_id)."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    view_id     = inp.get("view_id")
    text        = inp.get("text") or ""
    element_id  = inp.get("element_id")
    type_id     = inp.get("text_note_type_id")
    x_ft        = inp.get("x_ft")
    y_ft        = inp.get("y_ft")
    z_ft        = inp.get("z_ft", 0.0)

    if view_id is None:
        return {"error": "view_id is required."}
    if not text:
        return {"error": "text is required."}
    if element_id is None and (x_ft is None or y_ft is None):
        return {"error": ("Either element_id or both x_ft and y_ft are "
                          "required for the note position.")}

    try:
        view = doc.GetElement(_make_eid(int(view_id)))
    except Exception:
        return {"error": "Invalid view_id."}
    if view is None or not isinstance(view, View):
        return {"error": "Element {} is not a View.".format(view_id)}
    if view.IsTemplate:
        return {"error": "Cannot place text in a view template."}

    # Resolve the type id (default to first TextNoteType in doc).
    type_eid = None
    if type_id is not None:
        try:
            type_eid = _make_eid(int(type_id))
        except Exception:
            return {"error": "Invalid text_note_type_id."}
    else:
        try:
            first = (FilteredElementCollector(doc)
                     .OfClass(TextNoteType)
                     .FirstElement())
            if first is None:
                return {"error": ("No TextNoteType in document. Create a "
                                  "text-note type in Revit first.")}
            type_eid = first.Id
        except Exception as e:
            return {"error": "Could not find a TextNoteType: {}".format(e)}

    # Resolve position.
    if element_id is not None:
        try:
            el = doc.GetElement(_make_eid(int(element_id)))
        except Exception:
            return {"error": "Invalid element_id."}
        if el is None:
            return {"error": "element_id not found."}
        pt = _bbox_center_in_view(view, el)
        if pt is None:
            return {"error": "Could not find a placement point for element."}
    else:
        try:
            pt = XYZ(float(x_ft), float(y_ft), float(z_ft))
        except Exception:
            return {"error": "x_ft / y_ft / z_ft must be numeric."}

    try:
        t = Transaction(doc, "Place text note")
        t.Start()
        try:
            note = TextNote.Create(doc, view.Id, pt, text, type_eid)
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "TextNote.Create failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "view_id":     _eid_int(view.Id),
        "view_name":   _safe_name(view),
        "text_note_id":_eid_int(note.Id),
        "text":        text,
    }


def _build_filter_rule(param_id, op, value):
    """Translate a (param_id, operator, value) tuple to a FilterRule via
    ParameterFilterRuleFactory. Returns None if the combination isn't
    supported. Numeric epsilon defaults to 1e-9."""
    if param_id is None:
        return None
    eps = 1e-9
    op = (op or "=").strip()
    # Coerce numeric strings to numbers so '12' works as well as 12.
    if isinstance(value, str):
        try:
            value = float(value)
            # Preserve int if it round-trips
            if value == int(value):
                value = int(value)
        except (TypeError, ValueError):
            pass

    try:
        if isinstance(value, (int, float)):
            v = float(value)
            if op == "=":  return ParameterFilterRuleFactory.CreateEqualsRule(param_id, v, eps)
            if op == "!=": return ParameterFilterRuleFactory.CreateNotEqualsRule(param_id, v, eps)
            if op == ">":  return ParameterFilterRuleFactory.CreateGreaterRule(param_id, v, eps)
            if op == ">=": return ParameterFilterRuleFactory.CreateGreaterOrEqualRule(param_id, v, eps)
            if op == "<":  return ParameterFilterRuleFactory.CreateLessRule(param_id, v, eps)
            if op == "<=": return ParameterFilterRuleFactory.CreateLessOrEqualRule(param_id, v, eps)
        else:
            s = "" if value is None else str(value)
            if op == "=":           return ParameterFilterRuleFactory.CreateEqualsRule(param_id, s, False)
            if op == "!=":          return ParameterFilterRuleFactory.CreateNotEqualsRule(param_id, s, False)
            if op == "contains":    return ParameterFilterRuleFactory.CreateContainsRule(param_id, s, False)
            if op == "begins_with": return ParameterFilterRuleFactory.CreateBeginsWithRule(param_id, s, False)
            if op == "ends_with":   return ParameterFilterRuleFactory.CreateEndsWithRule(param_id, s, False)
    except Exception:
        return None
    return None


def _tool_create_filter(doc, input_dict):
    """Create a ParameterFilterElement (view filter). Built-in
    parameter names only for now (e.g. 'RBS_DUCT_WIDTH_PARAM'); shared
    / instance parameter support can come later if needed."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    name       = (inp.get("name") or "").strip()
    categories = inp.get("categories") or []
    rules      = inp.get("rules") or []

    if not name:
        return {"error": "name is required."}
    if not categories:
        return {"error": "categories is required (list of category names)."}
    if not rules:
        return {"error": ("rules is required (list of "
                          "{parameter, operator, value} objects).")}

    # Resolve categories to ElementIds.
    cat_ids = NetList[ElementId]()
    bad_cats = []
    for c in categories:
        cid = _resolve_category_id(doc, c)
        if cid is None:
            bad_cats.append(c)
        else:
            cat_ids.Add(cid)
    if bad_cats:
        return {"error": "Unknown category names: {}".format(bad_cats)}
    if cat_ids.Count == 0:
        return {"error": "No valid categories after resolution."}

    # Build filter rules.
    rule_list = NetList[FilterRule]()
    bad_rules = []
    for r in rules:
        param_name = r.get("parameter")
        op         = r.get("operator", "=")
        val        = r.get("value")
        param_id   = _resolve_builtin_parameter_id(param_name)
        if param_id is None:
            bad_rules.append({"rule": r,
                              "error": ("Unknown built-in parameter. Use a "
                                        "BuiltInParameter enum name like "
                                        "'RBS_DUCT_WIDTH_PARAM'.")})
            continue
        rule = _build_filter_rule(param_id, op, val)
        if rule is None:
            bad_rules.append({"rule": r,
                              "error": ("Could not build a rule for "
                                        "op='{}', value type {}.").format(
                                            op, type(val).__name__)})
            continue
        rule_list.Add(rule)
    if bad_rules:
        return {"error": "Some rules could not be built.",
                "rule_errors": bad_rules}
    if rule_list.Count == 0:
        return {"error": "No valid rules after parsing."}

    try:
        t = Transaction(doc, "Create filter '{}'".format(name))
        t.Start()
        try:
            element_filter = ElementParameterFilter(rule_list, False)
            fe = ParameterFilterElement.Create(doc, name, cat_ids,
                                               element_filter)
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "ParameterFilterElement.Create failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "filter_id":   _eid_int(fe.Id),
        "filter_name": _safe_name(fe),
        "rule_count":  rule_list.Count,
    }


def _tool_apply_filter_to_view(doc, input_dict):
    """Apply a filter to one or more views with optional graphic
    overrides (color / halftone / transparency). Batches across views
    in a single Revit transaction."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    filter_id   = inp.get("filter_id")
    view_ids    = inp.get("view_ids")
    if not view_ids and inp.get("view_id") is not None:
        view_ids = [inp.get("view_id")]
    color_hex   = inp.get("color")
    halftone    = inp.get("halftone")
    transp_pct  = inp.get("transparency_pct")
    visible     = inp.get("visible")
    remove      = bool(inp.get("remove", False))

    if filter_id is None:
        return {"error": "filter_id is required."}
    if not view_ids:
        return {"error": "view_ids (list) or view_id is required."}

    try:
        fe = doc.GetElement(_make_eid(int(filter_id)))
    except Exception:
        return {"error": "Invalid filter_id."}
    if fe is None or not isinstance(fe, ParameterFilterElement):
        return {"error": "filter_id is not a ParameterFilterElement."}

    # Build the OverrideGraphicSettings up front (shared across all views).
    ogs = None
    if not remove:
        ogs = OverrideGraphicSettings()
        if color_hex is not None:
            col = _color_from_hex(color_hex)
            if col is None:
                return {"error": "Unrecognized color: {}".format(color_hex)}
            try:
                ogs.SetProjectionLineColor(col)
            except Exception:
                pass
            try:
                ogs.SetCutLineColor(col)
            except Exception:
                pass
        if halftone is not None:
            try:
                ogs.SetHalftone(bool(halftone))
            except Exception:
                pass
        if transp_pct is not None:
            try:
                pct = int(transp_pct)
                pct = max(0, min(100, pct))
                ogs.SetSurfaceTransparency(pct)
            except Exception:
                pass

    # Resolve views.
    views = []
    errors = []
    for vid in view_ids:
        try:
            v = doc.GetElement(_make_eid(int(vid)))
        except Exception:
            errors.append({"view_id": vid, "error": "invalid id"})
            continue
        if v is None or not isinstance(v, View):
            errors.append({"view_id": vid, "error": "not a View"})
            continue
        if v.IsTemplate:
            errors.append({"view_id": _eid_int(v.Id),
                           "view_name": _safe_name(v),
                           "error": "view is a template - cannot apply filters here"})
            continue
        views.append(v)
    if not views:
        return {"error": "No valid views to update.", "errors": errors}

    updated = []
    try:
        t = Transaction(doc, "Apply filter '{}' to {} view(s)".format(
            _safe_name(fe), len(views)))
        t.Start()
        try:
            for v in views:
                if remove:
                    try:
                        v.RemoveFilter(fe.Id)
                    except Exception as e:
                        errors.append({
                            "view_id":   _eid_int(v.Id),
                            "view_name": _safe_name(v),
                            "error":     "RemoveFilter failed: {}".format(e),
                        })
                        continue
                else:
                    # Add filter if not already present.
                    try:
                        if not v.IsFilterApplied(fe.Id):
                            v.AddFilter(fe.Id)
                    except Exception as e:
                        errors.append({
                            "view_id":   _eid_int(v.Id),
                            "view_name": _safe_name(v),
                            "error":     "AddFilter failed: {}".format(e),
                        })
                        continue
                    # Apply overrides.
                    try:
                        v.SetFilterOverrides(fe.Id, ogs)
                    except Exception:
                        pass
                    # Visibility toggle.
                    if visible is not None:
                        try:
                            v.SetFilterVisibility(fe.Id, bool(visible))
                        except Exception:
                            pass
                updated.append({
                    "view_id":   _eid_int(v.Id),
                    "view_name": _safe_name(v),
                })
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "Transaction failed (rolled back): {}".format(e),
                    "errors": errors}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    result = {
        "filter_id":   _eid_int(fe.Id),
        "filter_name": _safe_name(fe),
        "mode":        "removed" if remove else "applied",
        "updated":     updated,
        "updated_count": len(updated),
    }
    if errors:
        result["errors"] = errors
    return result


# ---------------------------------------------------------------------------
# Element placement + geometry editing (v4.3)
# ---------------------------------------------------------------------------
#
# This is the iteration where the chatbot stops being "an assistant that
# reads + tweaks" and becomes "an assistant that can model for you" -
# placing equipment, copying / moving / rotating / arraying it, grouping
# it, pinning it.


def _xyz_from_dict(d, default_z=0.0):
    """{'x':..,'y':..,'z':..} -> XYZ. None on bad input."""
    if d is None:
        return None
    try:
        x = float(d.get("x", 0.0))
        y = float(d.get("y", 0.0))
        z = float(d.get("z", default_z))
        return XYZ(x, y, z)
    except Exception:
        return None


def _xyz_to_dict(p):
    """XYZ -> {'x':..,'y':..,'z':..}. Empty dict on bad input."""
    if p is None:
        return {}
    try:
        return {"x": round(p.X, 6), "y": round(p.Y, 6), "z": round(p.Z, 6)}
    except Exception:
        return {}


def _resolve_element(doc, eid_input):
    """eid (int or numeric string) -> (Element, None) or (None, error_str)."""
    if eid_input is None:
        return None, "id is None"
    try:
        eid_val = int(eid_input)
    except Exception:
        return None, "id is not an integer: {}".format(eid_input)
    try:
        el = doc.GetElement(_make_eid(eid_val))
    except Exception as e:
        return None, "doc.GetElement({}) raised: {}".format(eid_val, e)
    if el is None:
        return None, "no element with id {}".format(eid_val)
    return el, None


def _typed_id_list(eids):
    """Build an ICollection[ElementId] from a Python list of integers."""
    out = NetList[ElementId]()
    for eid in eids:
        try:
            out.Add(_make_eid(int(eid)))
        except Exception:
            pass
    return out


def _activate_symbol(sym):
    """FamilySymbols must be activated (inside a transaction) before
    Revit will place instances of them. No-op if already active."""
    try:
        if not sym.IsActive:
            sym.Activate()
    except Exception:
        pass


def _tool_place_family_instance(doc, input_dict):
    """Place a single family instance at a world XYZ position. The
    family symbol (type) is identified by `family_type_id`; get it from
    list_family_types (v4.8) or get_element_details on an existing
    instance and reading its type_id.

    Two placement modes:
      - level-hosted: pass `level_id` + `x_ft`, `y_ft` (and optional
        `z_ft` offset from the level). Used for most MEP equipment,
        air terminals, lighting, etc.
      - free XYZ: pass `x_ft`, `y_ft`, `z_ft` and no level_id. Used for
        absolute placement without a level association.

    Set `structural=true` to place as a structural element (column,
    beam, framing). Default false (most MEP placements)."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    fam_type_id = inp.get("family_type_id")
    x_ft        = inp.get("x_ft")
    y_ft        = inp.get("y_ft")
    z_ft        = inp.get("z_ft", 0.0)
    level_id    = inp.get("level_id")
    structural  = bool(inp.get("structural", False))

    if fam_type_id is None:
        return {"error": "family_type_id is required."}
    if x_ft is None or y_ft is None:
        return {"error": "x_ft and y_ft are required."}

    sym, err = _resolve_element(doc, fam_type_id)
    if err:
        return {"error": "family_type_id: " + err}
    if not isinstance(sym, FamilySymbol):
        return {"error": ("family_type_id is not a FamilySymbol "
                          "(it's a {}).").format(type(sym).__name__)}

    level = None
    if level_id is not None:
        lvl, err = _resolve_element(doc, level_id)
        if err:
            return {"error": "level_id: " + err}
        if not isinstance(lvl, Level):
            return {"error": "level_id is not a Level."}
        level = lvl

    try:
        pt = XYZ(float(x_ft), float(y_ft), float(z_ft))
    except Exception:
        return {"error": "x_ft / y_ft / z_ft must be numeric."}

    st = StructuralType.Beam if structural else StructuralType.NonStructural

    try:
        t = Transaction(doc, "Place {}".format(_safe_name(sym) or "family instance"))
        t.Start()
        try:
            _activate_symbol(sym)
            doc.Regenerate()
            if level is not None:
                inst = doc.Create.NewFamilyInstance(pt, sym, level, st)
            else:
                inst = doc.Create.NewFamilyInstance(pt, sym, st)
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "NewFamilyInstance failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "placed_id":       _eid_int(inst.Id),
        "family_type_id":  _eid_int(sym.Id),
        "family":          getattr(sym, "FamilyName", "") or "",
        "type":            _safe_name(sym),
        "position":        _xyz_to_dict(pt),
        "level_id":        _eid_int(level.Id) if level is not None else None,
    }


def _tool_place_family_instance_on_host(doc, input_dict):
    """Place a family instance hosted on an existing element (e.g. a
    wall-hosted light on a wall, a ceiling-hosted diffuser on a
    ceiling). The host element must be appropriate for the family - a
    light family marked 'wall-hosted' needs a wall host."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    fam_type_id = inp.get("family_type_id")
    host_id     = inp.get("host_id")
    x_ft        = inp.get("x_ft")
    y_ft        = inp.get("y_ft")
    z_ft        = inp.get("z_ft", 0.0)
    structural  = bool(inp.get("structural", False))

    if fam_type_id is None or host_id is None:
        return {"error": "family_type_id and host_id are required."}
    if x_ft is None or y_ft is None:
        return {"error": "x_ft and y_ft are required."}

    sym, err = _resolve_element(doc, fam_type_id)
    if err:
        return {"error": "family_type_id: " + err}
    if not isinstance(sym, FamilySymbol):
        return {"error": "family_type_id is not a FamilySymbol."}

    host, err = _resolve_element(doc, host_id)
    if err:
        return {"error": "host_id: " + err}

    try:
        pt = XYZ(float(x_ft), float(y_ft), float(z_ft))
    except Exception:
        return {"error": "x_ft / y_ft / z_ft must be numeric."}

    st = StructuralType.Beam if structural else StructuralType.NonStructural

    try:
        t = Transaction(doc, "Place {} on host".format(
            _safe_name(sym) or "family instance"))
        t.Start()
        try:
            _activate_symbol(sym)
            doc.Regenerate()
            inst = doc.Create.NewFamilyInstance(pt, sym, host, st)
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "NewFamilyInstance (hosted) failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "placed_id":      _eid_int(inst.Id),
        "family_type_id": _eid_int(sym.Id),
        "host_id":        _eid_int(host.Id),
        "host_name":      _safe_name(host),
        "type":           _safe_name(sym),
        "position":       _xyz_to_dict(pt),
    }


def _tool_copy_elements(doc, input_dict):
    """Copy one or more elements by an XYZ translation. Returns the
    new element ids. translation is in feet."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    element_ids = inp.get("element_ids") or []
    translation = _xyz_from_dict(inp.get("translation"))

    if not element_ids:
        return {"error": "element_ids is required."}
    if translation is None:
        return {"error": "translation {x,y,z} (feet) is required."}

    eids = _typed_id_list(element_ids)
    if eids.Count == 0:
        return {"error": "No valid element ids."}

    try:
        t = Transaction(doc, "Copy {} element(s)".format(eids.Count))
        t.Start()
        try:
            new_ids = ElementTransformUtils.CopyElements(doc, eids, translation)
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "CopyElements failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "copied_count": new_ids.Count if hasattr(new_ids, "Count") else len(list(new_ids)),
        "new_ids":      [_eid_int(eid) for eid in new_ids],
        "translation":  _xyz_to_dict(translation),
    }


def _tool_move_elements(doc, input_dict):
    """Move one or more elements in place by an XYZ translation
    (feet). Element ids are unchanged - the same instances are just
    relocated."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    element_ids = inp.get("element_ids") or []
    translation = _xyz_from_dict(inp.get("translation"))

    if not element_ids:
        return {"error": "element_ids is required."}
    if translation is None:
        return {"error": "translation {x,y,z} (feet) is required."}

    eids = _typed_id_list(element_ids)
    if eids.Count == 0:
        return {"error": "No valid element ids."}

    try:
        t = Transaction(doc, "Move {} element(s)".format(eids.Count))
        t.Start()
        try:
            ElementTransformUtils.MoveElements(doc, eids, translation)
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "MoveElements failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "moved_count": eids.Count,
        "moved_ids":   list(element_ids),
        "translation": _xyz_to_dict(translation),
    }


def _tool_mirror_elements(doc, input_dict):
    """Mirror elements across a plane. The plane is defined by an
    origin point + a normal direction. keep_original=true mirrors AND
    keeps the originals (most common); false moves them to the mirror
    position."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    element_ids = inp.get("element_ids") or []
    plane_spec  = inp.get("plane") or {}
    keep_orig   = bool(inp.get("keep_original", True))

    origin = _xyz_from_dict(plane_spec.get("origin"))
    normal = _xyz_from_dict(plane_spec.get("normal"))

    if not element_ids:
        return {"error": "element_ids is required."}
    if origin is None or normal is None:
        return {"error": "plane.origin and plane.normal {x,y,z} are required."}
    try:
        if normal.GetLength() < 1e-9:
            return {"error": "plane.normal must not be a zero vector."}
        normal = normal.Normalize()
    except Exception:
        return {"error": "plane.normal must not be a zero vector."}

    eids = _typed_id_list(element_ids)
    if eids.Count == 0:
        return {"error": "No valid element ids."}

    try:
        plane = Plane.CreateByNormalAndOrigin(normal, origin)
    except Exception as e:
        return {"error": "Could not build mirror plane: {}".format(e)}

    try:
        t = Transaction(doc, "Mirror {} element(s)".format(eids.Count))
        t.Start()
        try:
            if keep_orig:
                # MirrorElements returns the new (mirrored copy) ids.
                new_ids = ElementTransformUtils.MirrorElements(
                    doc, eids, plane, True)
            else:
                ElementTransformUtils.MirrorElements(doc, eids, plane, False)
                new_ids = None
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "MirrorElements failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    result = {
        "mirrored_count": eids.Count,
        "keep_original":  keep_orig,
    }
    if new_ids is not None:
        result["new_ids"] = [_eid_int(eid) for eid in new_ids]
    return result


def _tool_rotate_elements(doc, input_dict):
    """Rotate elements around an axis line by a signed angle (degrees).
    The axis is defined by a point + a direction vector. Positive angle
    follows the right-hand rule about the axis direction."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    element_ids    = inp.get("element_ids") or []
    axis_point     = _xyz_from_dict(inp.get("axis_point"))
    axis_direction = _xyz_from_dict(inp.get("axis_direction"))
    angle_deg      = inp.get("angle_degrees")

    if not element_ids:
        return {"error": "element_ids is required."}
    if axis_point is None or axis_direction is None:
        return {"error": "axis_point and axis_direction {x,y,z} are required."}
    if angle_deg is None:
        return {"error": "angle_degrees is required."}
    try:
        angle_rad = float(angle_deg) * math.pi / 180.0
    except Exception:
        return {"error": "angle_degrees must be numeric."}
    try:
        if axis_direction.GetLength() < 1e-9:
            return {"error": "axis_direction must not be a zero vector."}
    except Exception:
        return {"error": "axis_direction must not be a zero vector."}

    eids = _typed_id_list(element_ids)
    if eids.Count == 0:
        return {"error": "No valid element ids."}

    try:
        axis = Line.CreateBound(axis_point, axis_point.Add(axis_direction))
    except Exception as e:
        return {"error": "Could not build rotation axis: {}".format(e)}

    try:
        t = Transaction(doc, "Rotate {} element(s)".format(eids.Count))
        t.Start()
        try:
            ElementTransformUtils.RotateElements(doc, eids, axis, angle_rad)
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "RotateElements failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "rotated_count":   eids.Count,
        "rotated_ids":     list(element_ids),
        "angle_degrees":   float(angle_deg),
    }


def _tool_set_elements_pinned(doc, input_dict):
    """Pin or unpin elements. Pinned elements can't be moved / deleted
    accidentally - useful after placing a series of equipment to lock
    them in position."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    element_ids = inp.get("element_ids") or []
    pinned      = inp.get("pinned")
    if pinned is None:
        return {"error": "pinned (true/false) is required."}
    if not element_ids:
        return {"error": "element_ids is required."}

    updated = []
    errors = []
    try:
        t = Transaction(doc, "{} {} element(s)".format(
            "Pin" if pinned else "Unpin", len(element_ids)))
        t.Start()
        try:
            for eid in element_ids:
                el, err = _resolve_element(doc, eid)
                if err:
                    errors.append({"element_id": eid, "error": err})
                    continue
                try:
                    el.Pinned = bool(pinned)
                    updated.append(_eid_int(el.Id))
                except Exception as e:
                    errors.append({"element_id": _eid_int(el.Id),
                                   "error": str(e)})
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "Transaction failed (rolled back): {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    result = {
        "pinned":        bool(pinned),
        "updated_count": len(updated),
        "updated_ids":   updated,
    }
    if errors:
        result["errors"] = errors
    return result


def _tool_group_elements(doc, input_dict):
    """Bundle elements into a single Group element. Optional `name` to
    set the group type name (otherwise Revit auto-generates one)."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    element_ids = inp.get("element_ids") or []
    name        = (inp.get("name") or "").strip()

    if not element_ids:
        return {"error": "element_ids is required."}
    eids = _typed_id_list(element_ids)
    if eids.Count == 0:
        return {"error": "No valid element ids."}

    try:
        t = Transaction(doc, "Group {} element(s)".format(eids.Count))
        t.Start()
        try:
            group = doc.Create.NewGroup(eids)
            if name:
                try:
                    group.GroupType.Name = name
                except Exception:
                    pass
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "NewGroup failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "group_id":     _eid_int(group.Id),
        "group_name":   _safe_name(group),
        "member_count": eids.Count,
    }


def _tool_ungroup_elements(doc, input_dict):
    """Ungroup one or more Group instances. Returns the released
    member ids (so the caller can keep working with them)."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    group_ids = inp.get("group_ids") or []
    if not group_ids:
        return {"error": "group_ids is required."}

    ungrouped     = 0
    released_ids  = []
    errors        = []
    try:
        t = Transaction(doc, "Ungroup {} group(s)".format(len(group_ids)))
        t.Start()
        try:
            for gid in group_ids:
                el, err = _resolve_element(doc, gid)
                if err:
                    errors.append({"group_id": gid, "error": err})
                    continue
                if not isinstance(el, Group):
                    errors.append({"group_id": _eid_int(el.Id),
                                   "error": "not a Group"})
                    continue
                try:
                    member_ids = el.UngroupMembers()
                    ungrouped += 1
                    for mid in member_ids:
                        released_ids.append(_eid_int(mid))
                except Exception as e:
                    errors.append({"group_id": _eid_int(el.Id),
                                   "error": str(e)})
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "Transaction failed (rolled back): {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    result = {
        "ungrouped_count": ungrouped,
        "released_ids":    released_ids,
    }
    if errors:
        result["errors"] = errors
    return result


def _tool_array_elements_linear(doc, input_dict):
    """Linear array: produce `count` total instances of the original
    element(s), each offset by `translation` from the previous. The
    original counts as item 1, so count=5 -> 4 copies. All copies
    happen in one transaction."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    element_ids = inp.get("element_ids") or []
    translation = _xyz_from_dict(inp.get("translation"))
    count       = inp.get("count")

    if not element_ids:
        return {"error": "element_ids is required."}
    if translation is None:
        return {"error": "translation {x,y,z} is required (offset between items)."}
    try:
        count = int(count)
    except Exception:
        return {"error": "count is required (integer >= 2)."}
    if count < 2:
        return {"error": "count must be >= 2."}

    eids = _typed_id_list(element_ids)
    if eids.Count == 0:
        return {"error": "No valid element ids."}

    new_ids = []
    try:
        t = Transaction(doc, "Linear array x{} of {} element(s)".format(
            count, eids.Count))
        t.Start()
        try:
            for i in range(1, count):
                offset = XYZ(translation.X * i,
                             translation.Y * i,
                             translation.Z * i)
                copy_ids = ElementTransformUtils.CopyElements(
                    doc, eids, offset)
                for cid in copy_ids:
                    new_ids.append(_eid_int(cid))
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "Linear array failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "count":         count,
        "copies_made":   count - 1,
        "new_ids":       new_ids,
        "translation":   _xyz_to_dict(translation),
    }


def _tool_array_elements_radial(doc, input_dict):
    """Radial array around an axis. Distributes `count` total
    instances (originals + copies) evenly across `total_angle_degrees`
    around the axis. axis is point + direction (right-hand rule for
    sign)."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    element_ids    = inp.get("element_ids") or []
    axis_point     = _xyz_from_dict(inp.get("axis_point"))
    axis_direction = _xyz_from_dict(inp.get("axis_direction"))
    count          = inp.get("count")
    total_deg      = inp.get("total_angle_degrees", 360.0)

    if not element_ids:
        return {"error": "element_ids is required."}
    if axis_point is None or axis_direction is None:
        return {"error": "axis_point and axis_direction {x,y,z} are required."}
    try:
        count = int(count)
    except Exception:
        return {"error": "count must be an integer >= 2."}
    if count < 2:
        return {"error": "count must be >= 2."}
    try:
        total_deg = float(total_deg)
    except Exception:
        return {"error": "total_angle_degrees must be numeric."}

    eids = _typed_id_list(element_ids)
    if eids.Count == 0:
        return {"error": "No valid element ids."}

    try:
        if axis_direction.GetLength() < 1e-9:
            return {"error": "axis_direction must not be a zero vector."}
        axis = Line.CreateBound(axis_point, axis_point.Add(axis_direction))
    except Exception as e:
        return {"error": "Could not build rotation axis: {}".format(e)}

    # If the total angle is a full circle (or nearly so), the LAST copy
    # would overlap the original. So slot count - 1 copies into the arc
    # excluding the start position - i.e. step = total / count for
    # 360-degree, step = total / (count-1) for partial-arc.
    is_full_circle = abs(abs(total_deg) - 360.0) < 1e-6
    step_deg = (total_deg / count) if is_full_circle else (total_deg / (count - 1))
    step_rad = step_deg * math.pi / 180.0

    new_ids = []
    try:
        t = Transaction(doc, "Radial array x{} of {} element(s)".format(
            count, eids.Count))
        t.Start()
        try:
            for i in range(1, count):
                # Copy first (no transform), then rotate the copies about
                # the axis by i * step_rad. CopyElements returns the new
                # ids; we then rotate those.
                copy_ids = ElementTransformUtils.CopyElements(
                    doc, eids, XYZ(0, 0, 0))
                ElementTransformUtils.RotateElements(
                    doc, copy_ids, axis, step_rad * i)
                for cid in copy_ids:
                    new_ids.append(_eid_int(cid))
            t.Commit()
        except Exception as e:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            return {"error": "Radial array failed: {}".format(e)}
    except Exception as e:
        return {"error": "Could not start transaction: {}".format(e)}

    return {
        "count":               count,
        "copies_made":         count - 1,
        "new_ids":             new_ids,
        "step_degrees":        step_deg,
        "total_angle_degrees": total_deg,
    }


def _tool_get_element_location(doc, input_dict):
    """Read an element's Location: either a point (most equipment,
    fixtures, family instances) or a curve (lines, ducts, pipes, walls
    when they have a path)."""
    if doc is None:
        return {"error": "No active Revit document."}
    raw_id = (input_dict or {}).get("element_id")
    el, err = _resolve_element(doc, raw_id)
    if err:
        return {"error": err}

    out = {
        "element_id":   _eid_int(el.Id),
        "name":         _safe_name(el),
        "kind":         "none",
    }
    try:
        loc = el.Location
    except Exception:
        loc = None
    if loc is None:
        return out
    # LocationPoint
    if hasattr(loc, "Point") and loc.Point is not None:
        out["kind"]  = "point"
        out["point"] = _xyz_to_dict(loc.Point)
        try:
            out["rotation_radians"] = float(loc.Rotation)
            out["rotation_degrees"] = round(
                loc.Rotation * 180.0 / math.pi, 4)
        except Exception:
            pass
        return out
    # LocationCurve
    if hasattr(loc, "Curve") and loc.Curve is not None:
        out["kind"] = "curve"
        try:
            out["curve_start"] = _xyz_to_dict(loc.Curve.GetEndPoint(0))
            out["curve_end"]   = _xyz_to_dict(loc.Curve.GetEndPoint(1))
            out["curve_length"] = round(float(loc.Curve.Length), 4)
        except Exception:
            pass
        try:
            out["curve_type"] = type(loc.Curve).__name__
        except Exception:
            pass
        return out
    return out


def _tool_get_element_geometry(doc, input_dict):
    """Read an element's bounding box + location summary. Useful as
    the lookup step before move / copy / rotate / mirror operations."""
    if doc is None:
        return {"error": "No active Revit document."}
    inp = input_dict or {}
    raw_id  = inp.get("element_id")
    view_id = inp.get("view_id")   # bounding box is view-sensitive

    el, err = _resolve_element(doc, raw_id)
    if err:
        return {"error": err}

    view = None
    if view_id is not None:
        v, err = _resolve_element(doc, view_id)
        if err:
            return {"error": "view_id: " + err}
        if not isinstance(v, View):
            return {"error": "view_id is not a View."}
        view = v

    out = {
        "element_id": _eid_int(el.Id),
        "name":       _safe_name(el),
    }
    try:
        out["category"] = el.Category.Name if el.Category else ""
    except Exception:
        out["category"] = ""

    # Bounding box
    try:
        bb = el.get_BoundingBox(view) if view is not None else el.get_BoundingBox(None)
    except Exception:
        bb = None
    if bb is not None:
        try:
            out["bbox"] = {
                "min": _xyz_to_dict(bb.Min),
                "max": _xyz_to_dict(bb.Max),
                "size": {
                    "x": round(bb.Max.X - bb.Min.X, 6),
                    "y": round(bb.Max.Y - bb.Min.Y, 6),
                    "z": round(bb.Max.Z - bb.Min.Z, 6),
                },
            }
        except Exception:
            pass

    # Location summary (reuse the same logic)
    try:
        loc_result = _tool_get_element_location(doc, {"element_id": raw_id})
        out["location"] = {
            k: v for k, v in loc_result.items()
            if k not in ("element_id", "name") and not k.startswith("_")
        }
    except Exception:
        pass

    return out


# ---- Tool registry ---------------------------------------------------------

# API-side tool definitions sent to Anthropic on every request.
TOOL_DEFS = [
    {
        "name": "get_document_info",
        "description": (
            "Get basic info about the active Revit document: title, file path, "
            "Revit version, whether the file is workshared, and the currently "
            "active view (name + type + id)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_views",
        "description": (
            "List views in the active document. By default skips view templates. "
            "Optionally filter by view type (e.g. FloorPlan, CeilingPlan, ThreeD, "
            "Section, Elevation, Detail, DraftingView, Schedule, AreaPlan, Legend) "
            "and/or by a case-insensitive substring of the view name. Set "
            "include_templates=true to also surface templates (each result has an "
            "is_template field); set only_templates=true to ONLY return templates "
            "(handy before calling apply_view_template - you need the template id)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_type":         {"type": "string", "description": "Optional, e.g. 'FloorPlan'"},
                "name_contains":     {"type": "string", "description": "Optional case-insensitive substring"},
                "include_templates": {"type": "boolean",
                                      "description": "Include view templates in results. Default false."},
                "only_templates":    {"type": "boolean",
                                      "description": "Return ONLY view templates. Default false."},
                "max_results":       {"type": "integer", "description": "Cap on rows. Default 200."},
            },
        },
    },
    {
        "name": "list_levels",
        "description": (
            "List all Level elements in the active document with their elevation "
            "(internal feet). Used as a pre-step to create_view_plan, which needs "
            "a level_id."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_sheets",
        "description": (
            "List sheets in the active document, with optional substring filters "
            "on sheet number and/or sheet name. Use number_contains='M-1' to find "
            "all M-1xx sheets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "number_contains": {"type": "string"},
                "name_contains":   {"type": "string"},
                "max_results":     {"type": "integer", "description": "Default 200."},
            },
        },
    },
    {
        "name": "list_schedules",
        "description": (
            "List all ViewSchedules in the active document with their target "
            "category. The flag is_titleblock_revision marks auto-generated "
            "title block revision schedules - usually noise unless asked about."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
                "max_results":   {"type": "integer", "description": "Default 200."},
            },
        },
    },
    {
        "name": "get_selection",
        "description": (
            "Get the elements the user has currently selected in the Revit UI. "
            "Returns id, category, family_name, type_name, name. Use this when "
            "the user asks about 'this' / 'the selected element' / 'these'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "count_by_category",
        "description": (
            "Count instances by category, either across the whole document or "
            "only what's visible in the active view. category_filter is an "
            "optional case-insensitive substring (e.g. 'Duct' matches 'Ducts', "
            "'Duct Fittings', 'Duct Accessories'). Returns rows sorted by count desc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope":           {"type": "string",
                                    "enum": ["document", "active_view"],
                                    "description": "Default 'document'."},
                "category_filter": {"type": "string"},
            },
        },
    },
    {
        "name": "list_elements_by_category",
        "description": (
            "List actual element INSTANCES of a category - the missing "
            "link between count_by_category (counts only) and write "
            "tools that need explicit element_ids (tag_elements, "
            "delete_elements, set_element_parameters, etc.). Returns "
            "each element's id, name, category, type, family, mark, "
            "and level. scope='document' (whole doc), 'active_view' "
            "(only what's visible in the active view), or 'view' "
            "(visible in a specific view_id). 'view' scope is the "
            "essential workflow for 'tag all X in this view'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category":      {"type": "string",
                                  "description": ("BuiltInCategory name (e.g. "
                                                  "'OST_DuctCurves', 'OST_DuctTerminal') "
                                                  "or display name ('Ducts', 'Air Terminals').")},
                "scope":         {"type": "string",
                                  "enum": ["document", "active_view", "view"],
                                  "description": "Default 'document'."},
                "view_id":       {"type": "integer",
                                  "description": "Required when scope='view'."},
                "name_contains": {"type": "string"},
                "mark_contains": {"type": "string",
                                  "description": "Substring filter on the Mark parameter."},
                "max_results":   {"type": "integer", "description": "Default 200."},
            },
            "required": ["category"],
        },
    },
    {
        "name": "get_element_details",
        "description": (
            "Get full details for one element by its integer id (from any list_* "
            "or get_selection result): category, family, type, all instance "
            "parameters with values, all type parameters with values. "
            "If the element is a ViewSheet, the result ALSO includes:\n"
            "  - placed_views: [{view_id, view_name, view_type}, ...] for every "
            "view currently placed on the sheet (covers both viewports and "
            "schedule placements). Use this for 'duplicate sheet with its "
            "views' workflows.\n"
            "  - title_block: {instance_id, type_id, family, type_name} for "
            "the first title block instance on the sheet. Pass title_block_id "
            "= type_id to create_sheets to make new sheets that match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "integer", "description": "The numeric Revit element id."},
            },
            "required": ["element_id"],
        },
    },

    # ---- Write tools (v3.0) -----------------------------------------------

    {
        "name": "create_sheets",
        "description": (
            "Create one or more sheets in the active document. Each sheet "
            "needs a number and a name. Optionally specify a title block "
            "family symbol (type) by element id - otherwise the first "
            "available title block type is used. Shows a confirmation "
            "dialog with a preview table; the user can cancel. All "
            "sheets are created in ONE Revit transaction so Ctrl+Z "
            "undoes the entire batch as a single step. Returns the new "
            "sheet ids on success, or {\"cancelled\": true} if the user "
            "declined."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sheets": {
                    "type": "array",
                    "description": "List of sheets to create. Each item is {number, name}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "number": {"type": "string",
                                       "description": "Sheet number, e.g. 'M-101'."},
                            "name":   {"type": "string",
                                       "description": "Sheet name, e.g. 'MECHANICAL FIRST FLOOR PLAN'."},
                        },
                        "required": ["number", "name"],
                    },
                },
                "title_block_id": {
                    "type": "integer",
                    "description": ("Optional element id of the title block FAMILY SYMBOL (the "
                                    "type, not an instance). If omitted, picks the first "
                                    "available title block type."),
                },
            },
            "required": ["sheets"],
        },
    },
    {
        "name": "set_element_parameters",
        "description": (
            "Set parameter values on one or more elements. Each change is "
            "{element_id, parameter_name, value}. Multiple changes happen "
            "in a SINGLE transaction (one Ctrl+Z step). The tool shows a "
            "confirmation dialog when there are more than 10 changes; "
            "smaller batches and single sets apply immediately. Returns "
            "per-change status so partial failures surface clearly. "
            "Parameter names match what Revit's Properties palette shows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "element_id":     {"type": "integer"},
                            "parameter_name": {"type": "string",
                                               "description": "Parameter name as in Revit's Properties palette."},
                            "value":          {"description": ("Value to set. Match the parameter's storage type "
                                                               "(string / number / element id). For length/area "
                                                               "parameters you can also pass a unit-aware string "
                                                               "like '12 ft 6 in'.")},
                        },
                        "required": ["element_id", "parameter_name", "value"],
                    },
                },
            },
            "required": ["changes"],
        },
    },
    {
        "name": "duplicate_view",
        "description": (
            "Duplicate a view. Options:\n"
            "  - 'Duplicate'      : geometry only (no view-specific annotations)\n"
            "  - 'WithDetailing'  : geometry + view-specific annotations\n"
            "  - 'AsDependent'    : a dependent view linked to the original\n"
            "Optionally rename the new view. Returns the new view's id and name. "
            "Single op, no confirmation dialog."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_view_id": {"type": "integer",
                                   "description": "Element id of the view to duplicate."},
                "duplicate_type": {"type": "string",
                                   "enum": ["Duplicate", "WithDetailing", "AsDependent"],
                                   "description": "How to duplicate. Default 'Duplicate'."},
                "new_name":       {"type": "string",
                                   "description": "Optional name for the new view."},
            },
            "required": ["source_view_id"],
        },
    },
    {
        "name": "delete_elements",
        "description": (
            "Delete one or more elements by id. ALWAYS shows a confirmation "
            "dialog with the preview table - the user must click Confirm. "
            "All deletes happen in ONE transaction (one Ctrl+Z step). "
            "Returns the count actually deleted (which may exceed the input "
            "count because Revit cascades dependent-element deletions). "
            "Use this carefully and preview the list to the user before "
            "calling - the system prompt requires you to do so."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of element ids to delete.",
                },
            },
            "required": ["element_ids"],
        },
    },

    # ---- Write tools (v3.2 - Sheets & views) ------------------------------

    {
        "name": "rename_element",
        "description": (
            "Rename an element by id. Works on anything with a writable "
            "Name (sheets, views, schedules, types, etc.). NOTE: this "
            "changes the Name, not other identifiers - sheet *numbers* "
            "are a separate parameter (use set_element_parameters with "
            "parameter_name='Sheet Number')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "integer"},
                "new_name":   {"type": "string"},
            },
            "required": ["element_id", "new_name"],
        },
    },
    {
        "name": "place_view_on_sheet",
        "description": (
            "Place a view on a sheet as a viewport. Position defaults to "
            "the title block centroid; override with x_ft / y_ft in "
            "sheet-local feet. Refuses if the view is already on a sheet "
            "(Revit constraint) or is a template. Use this AFTER "
            "create_sheets (or with an existing sheet) to populate sheets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_id":  {"type": "integer", "description": "Element id of the view to place."},
                "sheet_id": {"type": "integer", "description": "Element id of the destination ViewSheet."},
                "x_ft":     {"type": "number",  "description": "Optional. Sheet-local x in feet. Default: title block center."},
                "y_ft":     {"type": "number",  "description": "Optional. Sheet-local y in feet. Default: title block center."},
            },
            "required": ["view_id", "sheet_id"],
        },
    },
    {
        "name": "apply_view_template",
        "description": (
            "Apply a view template to one or more views. The template "
            "itself must be a View where IsTemplate=true; use "
            "list_views with only_templates=true to find one. Shows a "
            "confirmation dialog when applying to more than 5 views. "
            "All applications happen in one Revit transaction (one "
            "Ctrl+Z step)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_ids":    {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of view ids to apply the template to.",
                },
                "template_id": {"type": "integer",
                                "description": "Element id of the view template."},
            },
            "required": ["view_ids", "template_id"],
        },
    },
    {
        "name": "create_view_plan",
        "description": (
            "Create a new plan view (floor plan, ceiling plan, or area "
            "plan) based on a level. Returns the new view's id. The "
            "agent should call list_levels first to find the right "
            "level_id. Optional name + scale; if omitted, Revit picks "
            "defaults."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "level_id":            {"type": "integer",
                                        "description": "Element id of the Level the plan is based on."},
                "plan_type":           {"type": "string",
                                        "enum": ["floor", "ceiling", "area"],
                                        "description": "Default 'floor'."},
                "view_family_type_id": {"type": "integer",
                                        "description": "Optional - which ViewFamilyType to use. Omit for default."},
                "name":                {"type": "string",
                                        "description": "Optional name for the new view."},
                "scale":               {"type": "integer",
                                        "description": "Optional view scale (e.g. 96 for 1/8\"=1'0\")."},
            },
            "required": ["level_id"],
        },
    },
    {
        "name": "create_view_3d",
        "description": (
            "Create a new 3D view. Defaults to isometric; pass "
            "perspective=true for a perspective view. Returns the new "
            "view's id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "perspective": {"type": "boolean",
                                "description": "Default false (isometric)."},
                "name":        {"type": "string",
                                "description": "Optional name for the new view."},
            },
        },
    },

    # ---- Read tools (v3.3) ------------------------------------------------

    {
        "name": "list_schedulable_fields",
        "description": (
            "List the parameter names that can be added as fields on a "
            "schedule for the given category. Call this before "
            "create_schedule so you know what to pass in field_names. "
            "Category accepts a BuiltInCategory enum name (e.g. "
            "'OST_DuctTerminal') or the display name shown in Revit "
            "('Air Terminals')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string",
                             "description": "BuiltInCategory enum name or display name."},
            },
            "required": ["category"],
        },
    },
    {
        "name": "list_scope_boxes",
        "description": "List all scope boxes in the active document.",
        "input_schema": {"type": "object", "properties": {}},
    },

    # ---- Write tools (v3.3 - Schedules + scope boxes + view basics) -------

    {
        "name": "create_schedule",
        "description": (
            "Create a new ViewSchedule for a category. Optionally "
            "pre-populate it with fields by name (use "
            "list_schedulable_fields first to see what's available). "
            "Field names that don't match anything are returned in the "
            "result's `missing_fields` list rather than failing the "
            "whole call. Returns the new schedule's id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category":    {"type": "string",
                                "description": "BuiltInCategory enum name or display name."},
                "field_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of parameter names to add as fields, in order.",
                },
                "name":        {"type": "string",
                                "description": "Optional name for the new schedule."},
            },
            "required": ["category"],
        },
    },
    {
        "name": "add_schedule_filter",
        "description": (
            "Add a filter rule to an existing schedule. The field_name "
            "must already be one of the schedule's columns (add it "
            "with create_schedule's field_names, or in the UI). "
            "Operators: equals, not_equals, greater, greater_or_equal, "
            "less, less_or_equal, contains, begins_with, ends_with."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {"type": "integer"},
                "field_name":  {"type": "string"},
                "operator":    {"type": "string",
                                "enum": ["equals", "not_equals",
                                         "greater", "greater_or_equal",
                                         "less", "less_or_equal",
                                         "contains", "begins_with", "ends_with"]},
                "value":       {"description": "String, integer, or number depending on the field."},
            },
            "required": ["schedule_id", "field_name", "operator", "value"],
        },
    },
    {
        "name": "apply_scope_box_to_view",
        "description": (
            "Apply (or clear) a scope box on a view. Pass scope_box_id "
            "to apply; omit it or pass null to clear the view's "
            "current scope box. NOTE: scope boxes themselves can only "
            "be created in the Revit UI - this tool only applies "
            "existing ones (find them via list_scope_boxes)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_id":       {"type": "integer"},
                "scope_box_id":  {"type": "integer",
                                  "description": "Omit or pass null to clear."},
            },
            "required": ["view_id"],
        },
    },
    {
        "name": "set_view_scale",
        "description": (
            "Set a view's scale. The value is the integer denominator: "
            "96 = 1/8\"=1'-0\", 48 = 1/4\"=1'-0\", 24 = 1/2\"=1'-0\", "
            "12 = 1\"=1'-0\", etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_id": {"type": "integer"},
                "scale":   {"type": "integer", "description": "Positive integer denominator."},
            },
            "required": ["view_id", "scale"],
        },
    },
    {
        "name": "hide_categories_in_view",
        "description": (
            "Hide or show categories in a view. Categories accept "
            "BuiltInCategory enum names ('OST_Walls') or display names "
            "('Walls'). Use action='show' to un-hide previously hidden "
            "categories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_id":    {"type": "integer"},
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of category names to toggle.",
                },
                "action":     {"type": "string",
                               "enum": ["hide", "show"],
                               "description": "Default 'hide'."},
            },
            "required": ["view_id", "categories"],
        },
    },

    # ---- Linked models (v4.1) ---------------------------------------------

    {
        "name": "list_links",
        "description": (
            "List every Revit link in the active document. For each link "
            "instance returns id, name (the file/path), type info, "
            "attachment type (Attached vs Overlay), whether the link is "
            "currently loaded, and the linked doc's title + path when "
            "loaded. ALWAYS call this first when the user asks about "
            "linked content - the result's `id` is the link_id needed "
            "for every other linked-model tool."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_rooms_in_link",
        "description": (
            "Pull rooms / spaces from a linked document (typically the "
            "linked architecture model). For MEP work this is the "
            "go-to source for room number, name, area, and level. "
            "If link_id is omitted, rooms from EVERY loaded link are "
            "aggregated and each row is tagged with link_id+link_name. "
            "Optional filters: name_contains (case-insensitive "
            "substring) and min_area_sqft. Returns up to max_results "
            "(default 500) rooms."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "link_id":        {"type": "integer",
                                   "description": "Optional. Omit to aggregate across all loaded links."},
                "name_contains":  {"type": "string"},
                "min_area_sqft":  {"type": "number"},
                "max_results":    {"type": "integer", "description": "Default 500."},
            },
        },
    },
    {
        "name": "get_elements_from_link",
        "description": (
            "Query elements in a linked document by category. Useful for "
            "'what walls are in the arch link', 'list levels in the "
            "structural link', etc. category accepts a BuiltInCategory "
            "name like 'OST_Walls', 'OST_Doors', 'OST_Floors', "
            "'OST_Levels', 'OST_Grids', 'OST_StructuralColumns', or a "
            "display name. Returns up to max_results rows with id, "
            "name, category, type/family, and best-effort level. "
            "IMPORTANT: the returned ids are LINKED-document ids, not "
            "host-doc ids - they're useful in answers but most write "
            "tools (set_element_parameters, delete_elements, etc.) "
            "won't accept them because they operate on the host doc only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "link_id":       {"type": "integer",
                                  "description": "From list_links."},
                "category":      {"type": "string",
                                  "description": ("BuiltInCategory name "
                                                  "(preferred) or display name.")},
                "name_contains": {"type": "string"},
                "max_results":   {"type": "integer", "description": "Default 200."},
            },
            "required": ["link_id", "category"],
        },
    },
    {
        "name": "get_link_visibility_in_view",
        "description": (
            "Check how a specific link is currently displayed in a view: "
            "whether it's hidden + whether the link instance has a "
            "halftone override. Call this before set_link_visibility_in_view "
            "if you want to confirm the current state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_id": {"type": "integer"},
                "link_id": {"type": "integer", "description": "From list_links."},
            },
            "required": ["view_id", "link_id"],
        },
    },
    {
        "name": "set_link_visibility_in_view",
        "description": (
            "Set a link's display state ('hidden' / 'halftone' / 'normal') "
            "across one or more views in a single Revit transaction. "
            "Pass view_ids (a list) to batch many views; the entire "
            "batch is one undo step. Useful for 'halftone the arch "
            "link on every M-1xx sheet' or 'hide the structural link "
            "on these three views'. 'normal' clears both hidden state "
            "and any halftone override. Views that are templates are "
            "skipped (templates don't carry per-element link "
            "visibility); they appear in the result's `errors` array."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of view ids to update.",
                },
                "view_id":  {"type": "integer",
                             "description": "Alternative single-view shorthand."},
                "link_id":  {"type": "integer", "description": "From list_links."},
                "mode":     {"type": "string",
                             "enum": ["hidden", "halftone", "normal"]},
            },
            "required": ["link_id", "mode"],
        },
    },

    # ---- Annotation + filter tools (v4.2) ---------------------------------

    {
        "name": "list_tag_types",
        "description": (
            "List tag types in the document. Each tag belongs to a Tags "
            "family category (e.g. 'Mechanical Equipment Tags', "
            "'Duct Tags', 'Pipe Tags'). family_category_name tells you "
            "what category the tag is for - that's how you pick a tag "
            "that matches the elements you want to label. ALWAYS call "
            "this before tag_elements to get a tag_type_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "family_category_contains": {
                    "type": "string",
                    "description": "Substring filter on the tag's family category, e.g. 'Mechanical Equipment'.",
                },
                "name_contains": {"type": "string"},
                "max_results":   {"type": "integer", "description": "Default 200."},
            },
        },
    },
    {
        "name": "list_filters",
        "description": (
            "List existing ParameterFilterElements (view filters) in "
            "the document. Returns name + categories each filter "
            "targets. Useful to find an existing filter before "
            "creating a new one with create_filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
            },
        },
    },
    {
        "name": "tag_elements",
        "description": (
            "Place tags on a list of elements in a view. The tag_type_id "
            "must be a tag family that matches the element's category "
            "(e.g. an 'Air Terminal Tag' to tag VAV diffusers). Get "
            "tag_type_id from list_tag_types. The entire batch wraps "
            "in one Revit transaction = one undo step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_id":     {"type": "integer"},
                "element_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Elements to tag.",
                },
                "tag_type_id": {"type": "integer",
                                "description": "From list_tag_types."},
                "orientation": {"type": "string",
                                "enum": ["horizontal", "vertical"],
                                "description": "Default 'horizontal'."},
                "leader":      {"type": "boolean",
                                "description": "Add a leader line. Default false."},
            },
            "required": ["view_id", "element_ids", "tag_type_id"],
        },
    },
    {
        "name": "place_text_note",
        "description": (
            "Drop a TextNote in a view. Position is either explicit world "
            "coordinates (x_ft + y_ft) OR the bounding-box center of a "
            "target element (element_id). text_note_type_id is optional "
            "- defaults to the first TextNoteType in the doc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view_id":           {"type": "integer"},
                "text":              {"type": "string"},
                "element_id":        {"type": "integer",
                                      "description": "Place at this element's bbox center."},
                "x_ft":              {"type": "number"},
                "y_ft":              {"type": "number"},
                "z_ft":              {"type": "number", "description": "Default 0."},
                "text_note_type_id": {"type": "integer"},
            },
            "required": ["view_id", "text"],
        },
    },
    {
        "name": "create_filter",
        "description": (
            "Create a ParameterFilterElement (view filter). Filter "
            "rules use BuiltInParameter enum names ONLY (e.g. "
            "'RBS_DUCT_WIDTH_PARAM', 'RBS_PIPE_DIAMETER_PARAM', "
            "'ALL_MODEL_TYPE_NAME'). Operators: '=', '!=', '>', '>=', "
            "'<', '<=' for numeric; '=', '!=', 'contains', "
            "'begins_with', 'ends_with' for strings. After creating, "
            "use apply_filter_to_view with the returned filter_id to "
            "actually put it on views."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":       {"type": "string",
                               "description": "Unique filter name."},
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("BuiltInCategory names like 'OST_DuctCurves' "
                                    "or display names like 'Ducts'."),
                },
                "rules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "parameter": {"type": "string",
                                          "description": "BuiltInParameter enum name."},
                            "operator":  {"type": "string"},
                            "value":     {"description": "Number or string. Strings used for text params."},
                        },
                        "required": ["parameter", "operator", "value"],
                    },
                },
            },
            "required": ["name", "categories", "rules"],
        },
    },
    {
        "name": "apply_filter_to_view",
        "description": (
            "Apply a filter to one or more views with optional graphic "
            "overrides (color, halftone, transparency). Batches across "
            "views in a single transaction. Set remove=true to take "
            "the filter OFF the listed views instead of applying it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_id": {"type": "integer", "description": "From create_filter or list_filters."},
                "view_ids":  {"type": "array", "items": {"type": "integer"}},
                "view_id":   {"type": "integer", "description": "Single-view shorthand."},
                "color":     {"type": "string",
                              "description": ("Hex like '#FF0000' or a basic "
                                              "name like 'red'. Sets projection "
                                              "and cut line color.")},
                "halftone":  {"type": "boolean"},
                "transparency_pct": {"type": "integer",
                                     "description": "0 - 100. Surface transparency."},
                "visible":   {"type": "boolean",
                              "description": "Toggle filter visibility on the view."},
                "remove":    {"type": "boolean",
                              "description": ("Remove the filter from the listed views "
                                              "instead of applying. Default false.")},
            },
            "required": ["filter_id"],
        },
    },

    # ---- Element placement + geometry editing (v4.3) ----------------------

    {
        "name": "place_family_instance",
        "description": (
            "Place a single family instance at a world XYZ position. "
            "family_type_id is a FamilySymbol id from list_family_types "
            "(or get_element_details on an existing instance, reading "
            "its type_id). Pass level_id + x_ft + y_ft for level-hosted "
            "(z_ft optional - vertical offset above the level). Pass "
            "just x_ft + y_ft + z_ft for absolute world coords. Set "
            "structural=true for structural placement (default false)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "family_type_id": {"type": "integer"},
                "x_ft":           {"type": "number"},
                "y_ft":           {"type": "number"},
                "z_ft":           {"type": "number", "description": "Default 0."},
                "level_id":       {"type": "integer", "description": "Optional - host to a Level."},
                "structural":     {"type": "boolean", "description": "Default false."},
            },
            "required": ["family_type_id", "x_ft", "y_ft"],
        },
    },
    {
        "name": "place_family_instance_on_host",
        "description": (
            "Place a hosted family instance on an existing element "
            "(wall-hosted light on a wall, ceiling-hosted diffuser on "
            "a ceiling, etc.). The family's host requirement must "
            "match host_id's category."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "family_type_id": {"type": "integer"},
                "host_id":        {"type": "integer"},
                "x_ft":           {"type": "number"},
                "y_ft":           {"type": "number"},
                "z_ft":           {"type": "number", "description": "Default 0."},
                "structural":     {"type": "boolean", "description": "Default false."},
            },
            "required": ["family_type_id", "host_id", "x_ft", "y_ft"],
        },
    },
    {
        "name": "copy_elements",
        "description": (
            "Copy one or more elements by an XYZ translation (feet). "
            "Returns the new element ids. All copies happen in one "
            "transaction = one undo step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_ids": {"type": "array", "items": {"type": "integer"}},
                "translation": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                    },
                    "description": "Offset vector in feet.",
                },
            },
            "required": ["element_ids", "translation"],
        },
    },
    {
        "name": "move_elements",
        "description": (
            "Move one or more elements in place by an XYZ translation "
            "(feet). Element ids are unchanged - same instances, new "
            "position."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_ids": {"type": "array", "items": {"type": "integer"}},
                "translation": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                    },
                },
            },
            "required": ["element_ids", "translation"],
        },
    },
    {
        "name": "mirror_elements",
        "description": (
            "Mirror elements across a plane defined by origin + normal. "
            "Set keep_original=true (default) to mirror AND keep the "
            "originals (most common); false moves them to the mirror "
            "position. Common normals: {x:1,y:0,z:0} mirrors across "
            "the YZ plane (left/right); {x:0,y:1,z:0} mirrors across "
            "the XZ plane (front/back); {x:0,y:0,z:1} mirrors across "
            "the XY plane (up/down)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_ids": {"type": "array", "items": {"type": "integer"}},
                "plane": {
                    "type": "object",
                    "properties": {
                        "origin": {"type": "object",
                                   "properties": {"x": {"type": "number"},
                                                  "y": {"type": "number"},
                                                  "z": {"type": "number"}}},
                        "normal": {"type": "object",
                                   "properties": {"x": {"type": "number"},
                                                  "y": {"type": "number"},
                                                  "z": {"type": "number"}}},
                    },
                },
                "keep_original": {"type": "boolean", "description": "Default true."},
            },
            "required": ["element_ids", "plane"],
        },
    },
    {
        "name": "rotate_elements",
        "description": (
            "Rotate elements around an axis line by a signed angle "
            "(degrees). axis_point is the rotation center; "
            "axis_direction is the axis vector (use {x:0,y:0,z:1} for "
            "a horizontal rotation in plan view). Positive angle "
            "follows the right-hand rule about axis_direction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_ids":    {"type": "array", "items": {"type": "integer"}},
                "axis_point":     {"type": "object",
                                   "properties": {"x": {"type": "number"},
                                                  "y": {"type": "number"},
                                                  "z": {"type": "number"}}},
                "axis_direction": {"type": "object",
                                   "properties": {"x": {"type": "number"},
                                                  "y": {"type": "number"},
                                                  "z": {"type": "number"}}},
                "angle_degrees":  {"type": "number"},
            },
            "required": ["element_ids", "axis_point", "axis_direction", "angle_degrees"],
        },
    },
    {
        "name": "set_elements_pinned",
        "description": (
            "Pin or unpin elements. Pinned elements can't be moved or "
            "deleted accidentally - useful after laying out equipment "
            "to lock them in place. Set pinned=true to pin, false to "
            "unpin."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_ids": {"type": "array", "items": {"type": "integer"}},
                "pinned":      {"type": "boolean"},
            },
            "required": ["element_ids", "pinned"],
        },
    },
    {
        "name": "group_elements",
        "description": (
            "Bundle elements into a single Group instance. The user "
            "can later select / move / copy the group as one. Optional "
            "name sets the group TYPE name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_ids": {"type": "array", "items": {"type": "integer"}},
                "name":        {"type": "string"},
            },
            "required": ["element_ids"],
        },
    },
    {
        "name": "ungroup_elements",
        "description": (
            "Ungroup one or more Group instances. The released member "
            "ids are returned so you can keep working with them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "group_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["group_ids"],
        },
    },
    {
        "name": "array_elements_linear",
        "description": (
            "Produce `count` total instances of the original "
            "element(s), each offset by `translation` from the previous. "
            "The original counts as item 1, so count=5 makes 4 copies. "
            "All in one transaction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_ids": {"type": "array", "items": {"type": "integer"}},
                "translation": {"type": "object",
                                "properties": {"x": {"type": "number"},
                                               "y": {"type": "number"},
                                               "z": {"type": "number"}},
                                "description": "Offset between successive items (feet)."},
                "count":       {"type": "integer", "description": ">= 2."},
            },
            "required": ["element_ids", "translation", "count"],
        },
    },
    {
        "name": "array_elements_radial",
        "description": (
            "Radial array: `count` total instances distributed evenly "
            "across an arc of `total_angle_degrees` around axis_point + "
            "axis_direction. total_angle_degrees=360 makes a full "
            "circle with the original counted as item 1 (so 360/count "
            "spacing); other values fit count items across the arc "
            "(count-1 spacing). Use axis_direction={x:0,y:0,z:1} for "
            "the common 'spin around a vertical axis' case."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_ids":    {"type": "array", "items": {"type": "integer"}},
                "axis_point":     {"type": "object",
                                   "properties": {"x": {"type": "number"},
                                                  "y": {"type": "number"},
                                                  "z": {"type": "number"}}},
                "axis_direction": {"type": "object",
                                   "properties": {"x": {"type": "number"},
                                                  "y": {"type": "number"},
                                                  "z": {"type": "number"}}},
                "count":          {"type": "integer", "description": ">= 2."},
                "total_angle_degrees": {"type": "number",
                                        "description": "Default 360."},
            },
            "required": ["element_ids", "axis_point", "axis_direction", "count"],
        },
    },
    {
        "name": "get_element_location",
        "description": (
            "Read the Location of one element: a point (with optional "
            "rotation) for instance-placed equipment, a curve "
            "(start/end/length) for ducts/pipes/lines/walls with a "
            "path. Use this BEFORE move / copy / rotate to know what "
            "you're transforming."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "integer"},
            },
            "required": ["element_id"],
        },
    },
    {
        "name": "get_element_geometry",
        "description": (
            "Read an element's bounding box (min, max, size) and its "
            "Location summary. Bounding box is view-sensitive if "
            "view_id is given, otherwise the model-space bbox is "
            "returned. Use this to figure out coordinate ranges before "
            "laying out other elements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "integer"},
                "view_id":    {"type": "integer", "description": "Optional - view-scoped bbox."},
            },
            "required": ["element_id"],
        },
    },
]


# Dispatch table: tool name -> implementation function.
TOOL_IMPLS = {
    # Read-only (v2.0 + v3.2 + v3.3)
    "get_document_info":         _tool_get_document_info,
    "list_views":                _tool_list_views,
    "list_sheets":               _tool_list_sheets,
    "list_schedules":            _tool_list_schedules,
    "list_levels":               _tool_list_levels,
    "list_schedulable_fields":   _tool_list_schedulable_fields,
    "list_scope_boxes":          _tool_list_scope_boxes,
    "get_selection":             _tool_get_selection,
    "count_by_category":         _tool_count_by_category,
    "list_elements_by_category": _tool_list_elements_by_category,
    "get_element_details":       _tool_get_element_details,
    # Write (v3.0)
    "create_sheets":             _tool_create_sheets,
    "set_element_parameters":    _tool_set_element_parameters,
    "duplicate_view":            _tool_duplicate_view,
    "delete_elements":           _tool_delete_elements,
    # Write (v3.2 - Sheets & views)
    "rename_element":            _tool_rename_element,
    "place_view_on_sheet":       _tool_place_view_on_sheet,
    "apply_view_template":       _tool_apply_view_template,
    "create_view_plan":          _tool_create_view_plan,
    "create_view_3d":            _tool_create_view_3d,
    # Write (v3.3 - Schedules + scope boxes + view basics)
    "create_schedule":           _tool_create_schedule,
    "add_schedule_filter":       _tool_add_schedule_filter,
    "apply_scope_box_to_view":   _tool_apply_scope_box_to_view,
    "set_view_scale":            _tool_set_view_scale,
    "hide_categories_in_view":   _tool_hide_categories_in_view,
    # Linked models (v4.1)
    "list_links":                    _tool_list_links,
    "get_rooms_in_link":             _tool_get_rooms_in_link,
    "get_elements_from_link":        _tool_get_elements_from_link,
    "get_link_visibility_in_view":   _tool_get_link_visibility_in_view,
    "set_link_visibility_in_view":   _tool_set_link_visibility_in_view,
    # Annotation + filters (v4.2)
    "list_tag_types":                _tool_list_tag_types,
    "list_filters":                  _tool_list_filters,
    "tag_elements":                  _tool_tag_elements,
    "place_text_note":               _tool_place_text_note,
    "create_filter":                 _tool_create_filter,
    "apply_filter_to_view":          _tool_apply_filter_to_view,
    # Element placement + geometry editing (v4.3)
    "place_family_instance":         _tool_place_family_instance,
    "place_family_instance_on_host": _tool_place_family_instance_on_host,
    "copy_elements":                 _tool_copy_elements,
    "move_elements":                 _tool_move_elements,
    "mirror_elements":               _tool_mirror_elements,
    "rotate_elements":               _tool_rotate_elements,
    "set_elements_pinned":           _tool_set_elements_pinned,
    "group_elements":                _tool_group_elements,
    "ungroup_elements":              _tool_ungroup_elements,
    "array_elements_linear":         _tool_array_elements_linear,
    "array_elements_radial":         _tool_array_elements_radial,
    "get_element_location":          _tool_get_element_location,
    "get_element_geometry":          _tool_get_element_geometry,
}


def execute_tool(name, input_dict, doc):
    """Run a tool by name. Must be called on Revit's UI thread.
    Returns a JSON-serializable result dict (always; no exceptions escape)."""
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return {"error": "Unknown tool: {}".format(name)}
    try:
        return impl(doc, input_dict or {})
    except Exception as e:
        return {"error": "{} raised {}: {}".format(name, type(e).__name__, e)}


# ---------------------------------------------------------------------------
# Attachment helpers (chips + context preamble)
# ---------------------------------------------------------------------------
#
# An "attachment" is a Revit item the user has pinned to their next message
# via the "+" menu (or future drag-drop). Two kinds today:
#     {"kind": "element", "id": int, "category": str, "name": str,
#      "family_name": str, "type_name": str}
#     {"kind": "sheet",   "id": int, "number": str, "name": str}
# More to come (view, schedule, file path) without changing the protocol.
#
# Wire format: when the user sends, we prepend a context preamble to the
# message content so Claude sees what was attached. Display side: chips
# render as pills both above the input AND inside the user bubble after
# the message is sent.
# ---------------------------------------------------------------------------

def _attachment_from_element(doc, el):
    """Build an attachment dict from a Revit element.

    Special-cases ViewSheet so a sheet selected (in the canvas, the
    Project Browser, or via `+` -> Use current selection) attaches as
    the richer `sheet` kind with its number visible on the chip,
    rather than a generic "Element" chip."""
    # Sheet -> richer chip.
    try:
        if isinstance(el, ViewSheet):
            return {
                "kind":   "sheet",
                "id":     _eid_int(el.Id),
                "number": el.SheetNumber or "",
                "name":   el.Name or "",
            }
    except Exception:
        pass

    type_name, fam_name = _type_and_family(doc, el)
    nm = ""
    try:
        nm = el.Name or ""
    except Exception:
        pass
    return {
        "kind":         "element",
        "id":           _eid_int(el.Id),
        "category":     _category_name(el),
        "name":         nm,
        "family_name":  fam_name,
        "type_name":    type_name,
    }


def _attachment_kind_label(att):
    return {
        "element": u"ELEMENT",
        "sheet":   u"SHEET",
        "file":    u"FILE",
        "folder":  u"FOLDER",
        "image":   u"IMAGE",
    }.get(att.get("kind"), u"ATTACHMENT")


def _attachment_short_label(att):
    """One-line label for chips (truncate-friendly)."""
    kind = att.get("kind")
    if kind == "element":
        cat = att.get("category") or "Element"
        nm  = att.get("name") or ""
        if nm:
            return "{}: {} (id {})".format(cat, nm, att.get("id"))
        return "{} (id {})".format(cat, att.get("id"))
    if kind == "sheet":
        return "{} - {}".format(att.get("number") or "", att.get("name") or "")
    if kind == "file":
        return att.get("name") or att.get("path") or "file"
    if kind == "folder":
        return att.get("name") or att.get("path") or "folder"
    if kind == "image":
        return att.get("name") or "image"
    return "Attachment"


def _attachment_full_label(att):
    """Full descriptor for tooltips."""
    kind = att.get("kind")
    if kind == "element":
        bits = []
        bits.append("id={}".format(att.get("id")))
        if att.get("category"):    bits.append("category={}".format(att["category"]))
        if att.get("family_name"): bits.append("family={}".format(att["family_name"]))
        if att.get("type_name"):   bits.append("type={}".format(att["type_name"]))
        if att.get("name"):        bits.append("name={}".format(att["name"]))
        return "Element: " + ", ".join(bits)
    if kind == "sheet":
        return "Sheet: id={}, number={}, name={}".format(
            att.get("id"), att.get("number") or "", att.get("name") or "")
    if kind == "file":
        bits = ["path={}".format(att.get("path", ""))]
        if att.get("size") is not None:
            bits.append("size={}".format(_human_size(att["size"])))
        if att.get("content"):
            bits.append("content embedded ({} chars)".format(len(att["content"])))
        return "File: " + ", ".join(bits)
    if kind == "folder":
        return "Folder: {}".format(att.get("path", ""))
    if kind == "image":
        bits = ["name={}".format(att.get("name") or "image")]
        if att.get("width") and att.get("height"):
            bits.append("{}x{}".format(att["width"], att["height"]))
        if att.get("byte_size") is not None:
            bits.append("size={}".format(_human_size(att["byte_size"])))
        src = att.get("source")
        if src:
            bits.append("from {}".format(src.replace("_", " ")))
        return "Image: " + ", ".join(bits)
    return repr(att)


def _human_size(n):
    """Format a byte count compactly."""
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n < 1024:
        return "{} B".format(n)
    if n < 1024 * 1024:
        return "{:.1f} KB".format(n / 1024.0)
    return "{:.1f} MB".format(n / (1024.0 * 1024.0))


def _build_attachment_preamble(attachments):
    """Compose the '[Attached context]' block prepended to user_text on
    send. Stable, machine-friendly format Claude can parse. File
    attachments with text content are inlined after the metadata list.

    Image attachments are skipped here - they're sent as separate
    `image` content blocks in the API call (see _build_api_messages),
    so listing them in a text preamble would be redundant."""
    if not attachments:
        return ""
    # Filter out images for preamble purposes - they're sent as their
    # own content blocks alongside this text.
    text_attachments = [a for a in attachments if a.get("kind") != "image"]
    if not text_attachments:
        return ""
    elements = [a for a in text_attachments if a.get("kind") == "element"]
    sheets   = [a for a in text_attachments if a.get("kind") == "sheet"]
    files    = [a for a in text_attachments if a.get("kind") == "file"]
    folders  = [a for a in text_attachments if a.get("kind") == "folder"]

    parts = ["[Attached context: {} item{}]".format(
        len(text_attachments), "" if len(text_attachments) == 1 else "s")]
    for a in elements:
        line = "- element  id={}".format(a.get("id"))
        if a.get("category"):    line += "  category={}".format(_quote(a["category"]))
        if a.get("family_name"): line += "  family={}".format(_quote(a["family_name"]))
        if a.get("type_name"):   line += "  type={}".format(_quote(a["type_name"]))
        if a.get("name"):        line += "  name={}".format(_quote(a["name"]))
        parts.append(line)
    for a in sheets:
        line = "- sheet  id={}".format(a.get("id"))
        if a.get("number"): line += "  number={}".format(_quote(a["number"]))
        if a.get("name"):   line += "  name={}".format(_quote(a["name"]))
        parts.append(line)
    for a in files:
        line = "- file  name={}".format(_quote(a.get("name") or ""))
        if a.get("path"): line += "  path={}".format(_quote(a["path"]))
        if a.get("size") is not None:
            line += "  size={}".format(_human_size(a["size"]))
        if a.get("content"):
            line += "  (content embedded below)"
        parts.append(line)
    for a in folders:
        line = "- folder  name={}".format(_quote(a.get("name") or ""))
        if a.get("path"): line += "  path={}".format(_quote(a["path"]))
        parts.append(line)

    # Inline the text content of any file attachment with content.
    for a in files:
        content = a.get("content")
        if not content:
            continue
        nm = a.get("name") or "(unnamed file)"
        parts.append("")
        parts.append("[Content of {}]".format(nm))
        parts.append(content)
        parts.append("[End {}]".format(nm))
    return "\n".join(parts)


def _quote(s):
    """Wrap a value in double quotes, escaping any inside."""
    return '"' + s.replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Image attachments (vision support)
# ---------------------------------------------------------------------------
#
# An image attachment carries actual pixel data that gets sent to the API as
# a base64-encoded image content block (per Anthropic Messages spec). We
# store the data inline on the attachment dict so the data persists across
# history replay - the API call needs the image bytes EVERY round, not just
# the first send, because each request ships the full conversation.
#
# Schema:
#   {
#     "kind":        "image",
#     "name":        "M-101.png",      # display label
#     "media_type":  "image/png",
#     "data_b64":    "<base64>",       # raw pixel data, no prefix
#     "source":      "active_view" | "clipboard" | "file",
#     "width":       1600,             # decoded pixel width (display)
#     "height":      1200,
#     "byte_size":   1234567,          # raw bytes (pre-base64)
#   }

# Image file extensions Claude can ingest as vision blocks.
_IMAGE_EXTS = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}


def _is_image_ext(ext):
    return (ext or "").lower() in _IMAGE_EXTS


def _attachment_from_image_bytes(raw_bytes, name, source, media_type="image/png"):
    """Build an image attachment dict from raw image bytes + a label.
    `raw_bytes` should be a .NET byte[]. The function base64-encodes the
    bytes for transport. Decodes the bytes once to pull width/height so
    the UI can show actual pixel size on the chip tooltip."""
    if raw_bytes is None:
        return None
    try:
        b64 = Convert.ToBase64String(raw_bytes)
    except Exception:
        return None
    width = None
    height = None
    try:
        # Lightweight decode just for dimensions. Frozen so it's safe
        # to drop the MemoryStream after.
        ms = MemoryStream(raw_bytes)
        bmp = BitmapImage()
        bmp.BeginInit()
        bmp.CacheOption    = BitmapCacheOption.OnLoad
        bmp.CreateOptions  = BitmapCreateOptions.IgnoreColorProfile
        bmp.StreamSource   = ms
        bmp.EndInit()
        try:
            bmp.Freeze()
        except Exception:
            pass
        width  = int(bmp.PixelWidth)  if bmp.PixelWidth  > 0 else None
        height = int(bmp.PixelHeight) if bmp.PixelHeight > 0 else None
    except Exception:
        pass
    return {
        "kind":        "image",
        "name":        name or "image.png",
        "media_type":  media_type,
        "data_b64":    b64,
        "source":      source or "file",
        "width":       width,
        "height":      height,
        "byte_size":   int(raw_bytes.Length) if hasattr(raw_bytes, "Length") else None,
    }


def _attachment_from_image_path(path):
    """Read an image file from disk and return an image attachment.
    Returns None if the path doesn't exist or the extension isn't a
    supported image type."""
    if not path or not os.path.isfile(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if not _is_image_ext(ext):
        return None
    try:
        raw_bytes = NetFile.ReadAllBytes(path)
    except Exception:
        return None
    return _attachment_from_image_bytes(
        raw_bytes,
        name=os.path.basename(path),
        source="file",
        media_type=_IMAGE_EXTS[ext])


def _encode_bitmap_source_to_png_bytes(bitmap_source):
    """Encode a WPF BitmapSource (e.g. from Clipboard.GetImage) to PNG
    bytes (.NET byte[]). Returns None on failure."""
    if bitmap_source is None:
        return None
    try:
        encoder = PngBitmapEncoder()
        encoder.Frames.Add(BitmapFrame.Create(bitmap_source))
        ms = MemoryStream()
        encoder.Save(ms)
        return ms.ToArray()
    except Exception:
        return None


def _bitmap_image_from_b64(b64, decode_pixel_width=128):
    """Decode a base64-encoded image to a frozen BitmapImage for use
    as the Source of a WPF Image control. `decode_pixel_width` caps the
    decoded thumbnail size so a 12-MP screenshot doesn't load 12 MB into
    a 64px chip. Returns None on decode failure."""
    if not b64:
        return None
    try:
        raw_bytes = Convert.FromBase64String(b64)
        ms = MemoryStream(raw_bytes)
        bmp = BitmapImage()
        bmp.BeginInit()
        bmp.CacheOption    = BitmapCacheOption.OnLoad
        bmp.CreateOptions  = BitmapCreateOptions.IgnoreColorProfile
        if decode_pixel_width and decode_pixel_width > 0:
            bmp.DecodePixelWidth = int(decode_pixel_width)
        bmp.StreamSource = ms
        bmp.EndInit()
        try:
            bmp.Freeze()
        except Exception:
            pass
        return bmp
    except Exception:
        return None


# ---------------------------------------------------------------------------
# File-drop attachment building
# ---------------------------------------------------------------------------

def _attachment_from_path(path):
    """Convert a filesystem path to an attachment dict. Reads inline
    content for small text files; everything else attaches as metadata
    only. Returns None if the path doesn't exist or can't be inspected."""
    if not path:
        return None
    try:
        if os.path.isdir(path):
            return {
                "kind": "folder",
                "path": path,
                "name": os.path.basename(os.path.normpath(path)) or path,
            }
        if not os.path.isfile(path):
            return None
        name = os.path.basename(path) or path
        ext  = os.path.splitext(name)[1].lower()
        try:
            size = os.path.getsize(path)
        except Exception:
            size = None

        # Image files become first-class vision attachments (sent to the
        # API as image content blocks) rather than file-metadata chips.
        if _is_image_ext(ext):
            img_att = _attachment_from_image_path(path)
            if img_att is not None:
                return img_att
            # Fall through to file-metadata attachment if decode failed
            # (e.g. corrupt file) - better than dropping it on the floor.

        attachment = {
            "kind": "file",
            "path": path,
            "name": name,
            "size": size,
            "ext":  ext,
        }

        # Inline the content for small text-like files. Anything else
        # attaches as a metadata-only chip (Claude still sees the
        # filename + path in the preamble).
        if ext in _TEXT_FILE_EXTS and size is not None and size <= _MAX_INLINE_TEXT_BYTES:
            try:
                with codecs.open(path, "r", encoding="utf-8") as f:
                    attachment["content"] = f.read()
            except Exception:
                # Try latin-1 fallback for legacy encodings.
                try:
                    with codecs.open(path, "r", encoding="latin-1") as f:
                        attachment["content"] = f.read()
                except Exception:
                    pass
        return attachment
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------

def _model_pricing(model_key):
    """Returns (in_per_m, out_per_m) USD per million tokens for the model."""
    info = MODELS.get(model_key) or MODELS[DEFAULT_MODEL_KEY]
    return info.get("price_in_per_m", 3.0), info.get("price_out_per_m", 15.0)


def cost_for_usage(input_tokens, output_tokens, model_key):
    in_rate, out_rate = _model_pricing(model_key)
    # NOTE: don't use 1_000_000 underscores - Python 3.6+ syntax,
    # IronPython 2.7 raises "unexpected token '_000_000'".
    return (int(input_tokens or 0) * in_rate
            + int(output_tokens or 0) * out_rate) / 1000000.0


def conversation_cost(conv, current_model_key):
    """Sum of token costs across an entire conversation, in USD.

    Returns (total_cost, total_input_tokens, total_output_tokens).

    Each assistant message stores a `usage` dict from the API. We use
    the *current* model's pricing for old turns - it's a small lie but
    the user usually doesn't switch models mid-chat, and they only see
    one number anyway."""
    total_in, total_out = 0, 0
    for m in conv.get("messages", []):
        usage = m.get("usage")
        if not usage:
            continue
        total_in  += int(usage.get("input_tokens",  0) or 0)
        total_out += int(usage.get("output_tokens", 0) or 0)
    return cost_for_usage(total_in, total_out, current_model_key), total_in, total_out


def format_cost(cost):
    """Format a USD cost. Sub-cent values get more precision."""
    if cost <= 0:
        return "$0.00"
    if cost < 0.005:
        return "$<0.01"
    return "${:.2f}".format(cost)


# Rough character-to-token ratio for pre-send estimates. Anthropic's
# tokenizer is BPE-like; on English text 1 token ~ 4 chars is a fair
# estimate. We over-estimate input + assume the model uses the full
# max_tokens output budget - that's the worst case the user could see.
_CHARS_PER_TOKEN_ESTIMATE = 4.0


def estimate_request_cost(messages, system_prompt, tools, max_output_tokens, model_key):
    """Realistic pre-send cost estimate, USD. Factors in prompt
    caching: the system prompt + tools + all but the last 2 messages
    are typically a cache HIT on the second turn onward (Anthropic
    caches the prefix marked with cache_control: ephemeral). Cache
    reads cost ~10% of normal input. We bias toward the cached-cost
    case for any chat with prior history; first-turn estimates fall
    back to full price.
    """
    # System prompt + tools size (these always get cache_control).
    sys_chars  = len(system_prompt or "")
    try:
        tools_chars = len(json.dumps(tools, ensure_ascii=False))
    except Exception:
        tools_chars = 0

    # Split messages into "cacheable prefix" and "fresh suffix". The
    # prefix is everything UP TO but not including the last two messages
    # (which are the new user msg + whatever it might trigger). On the
    # second turn onward, that prefix is in Anthropic's cache.
    fresh_msgs   = messages[-2:] if len(messages) > 2 else messages
    cached_msgs  = messages[:-2] if len(messages) > 2 else []
    try:
        cached_chars = len(json.dumps(cached_msgs, ensure_ascii=False))
    except Exception:
        cached_chars = 0
    try:
        fresh_chars = len(json.dumps(fresh_msgs, ensure_ascii=False))
    except Exception:
        fresh_chars = 0

    if cached_msgs:
        # Active chat with prior history: assume cache hits.
        # Cache hit input cost = ~10% of normal input cost.
        cached_tokens = int((sys_chars + tools_chars + cached_chars)
                            / _CHARS_PER_TOKEN_ESTIMATE)
        fresh_tokens  = int(fresh_chars / _CHARS_PER_TOKEN_ESTIMATE) + 100
        cached_cost   = cost_for_usage(cached_tokens, 0, model_key) * 0.10
        fresh_cost    = cost_for_usage(fresh_tokens, max_output_tokens, model_key)
        return cached_cost + fresh_cost

    # First turn of the chat: cache hasn't been written yet, so price
    # everything at the standard input rate (small overshoot for the
    # cache-write surcharge of ~1.25x but close enough for a soft cap).
    total_tokens = int((sys_chars + tools_chars + fresh_chars)
                       / _CHARS_PER_TOKEN_ESTIMATE) + 100
    return cost_for_usage(total_tokens, max_output_tokens, model_key)


# ---------------------------------------------------------------------------
# Conversation export (Markdown / JSON)
# ---------------------------------------------------------------------------

_FILENAME_SAFE_RE = re.compile(r'[^\w\s-]+')


def _safe_filename(name):
    base = _FILENAME_SAFE_RE.sub('', name or "").strip()
    base = re.sub(r'\s+', '_', base)
    return base or "chat"


def _exports_dir():
    d = os.path.join(_appdata_root(), "exports")
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def export_conversation_markdown(conv, path):
    """Write the conversation to `path` as a clean human-readable
    Markdown document. Tool calls are inlined as quoted lines."""
    lines = []
    title = conv.get("title") or "Chatbot conversation"
    lines.append("# {}".format(title))
    lines.append("")
    lines.append("- Project: {}".format(conv.get("project_label", "(unknown)")))
    if conv.get("created_at"):
        lines.append("- Started: {}".format(conv["created_at"]))
    if conv.get("updated_at"):
        lines.append("- Updated: {}".format(conv["updated_at"]))
    lines.append("- Exported: {}".format(_now_iso()))
    lines.append("")
    lines.append("---")
    lines.append("")

    # Build tool_use -> tool_result content lookup so we can show
    # each tool call's outcome inline.
    results_by_id = {}
    for m in conv.get("messages", []):
        c = m.get("content")
        if isinstance(c, list):
            for blk in c:
                if blk.get("type") == "tool_result":
                    results_by_id[blk.get("tool_use_id")] = blk.get("content", "")

    for m in conv.get("messages", []):
        role    = m.get("role")
        content = m.get("content")

        if role == "user" and isinstance(content, str):
            lines.append("## You")
            lines.append("")
            attachments = m.get("_attachments") or []
            if attachments:
                lines.append("_Attached:_")
                for a in attachments:
                    lines.append("- {}".format(_attachment_short_label(a)))
                lines.append("")
            shown = m.get("_user_text", content)
            if shown:
                lines.append(shown)
                lines.append("")
            continue

        if role == "user" and isinstance(content, list):
            # Synthetic tool_result message - skip; results are rendered
            # inline with their matching tool_use above.
            continue

        if role == "assistant":
            lines.append("## Assistant")
            lines.append("")
            if isinstance(content, str):
                if content.strip():
                    lines.append(content)
                    lines.append("")
            elif isinstance(content, list):
                for blk in content:
                    btype = blk.get("type")
                    if btype == "text":
                        txt = blk.get("text", "")
                        if txt.strip():
                            lines.append(txt)
                            lines.append("")
                    elif btype == "tool_use":
                        tid = blk.get("id", "")
                        result_str = results_by_id.get(tid, "")
                        try:
                            parsed = json.loads(result_str)
                        except Exception:
                            parsed = result_str
                        summary = summarize_tool_result(parsed)
                        lines.append("> 🔧 `{}` → {}".format(
                            blk.get("name", "?"), summary))
                        lines.append("")
            continue

    with codecs.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def export_conversation_json(conv, path):
    """Write the raw conversation JSON. Useful for backups and debugging."""
    with codecs.open(path, "w", encoding="utf-8") as f:
        json.dump(conv, f, indent=2, ensure_ascii=False)


def _openable_ids_from_result(tool_name, result):
    """For tools that create/affect openable elements (sheets, views,
    schedules), return a list of {id, label, kind} for the ones a user
    might want to jump to in Revit.

    The UI uses this to render an "Open in Revit" chip next to the
    tool's status pill. Returns [] for tools that don't produce
    openable elements (e.g. rename_element, set_element_parameters)."""
    if not isinstance(result, dict):
        return []
    out = []

    if tool_name == "create_sheets":
        for s in result.get("sheets", []):
            if not isinstance(s, dict) or "id" not in s:
                continue
            num = s.get("number") or ""
            nm  = s.get("name") or ""
            label = "{}  -  {}".format(num, nm).strip(" -").strip()
            if not label:
                label = "sheet {}".format(s["id"])
            out.append({"id": s["id"], "label": label, "kind": "sheet"})

    elif tool_name in ("create_schedule", "create_view_plan", "create_view_3d"):
        if "id" in result:
            label = result.get("name") or "view {}".format(result["id"])
            out.append({"id": result["id"], "label": label, "kind": "view"})

    elif tool_name == "duplicate_view":
        if "new_view_id" in result:
            label = result.get("new_view_name") or "view {}".format(result["new_view_id"])
            out.append({"id": result["new_view_id"], "label": label, "kind": "view"})

    elif tool_name == "place_view_on_sheet":
        # Opening the sheet makes sense - that's the deliverable.
        if "sheet_id" in result:
            out.append({"id": result["sheet_id"], "label": "sheet", "kind": "sheet"})

    return out


def summarize_tool_result(result):
    """One-line preview shown next to the tool name in the chat."""
    if isinstance(result, dict):
        if result.get("cancelled"):
            return "cancelled by user"
        if "error" in result:
            msg = str(result["error"])
            if len(msg) > 80:
                msg = msg[:80] + "..."
            return "error: " + msg

        # Write tools
        if "created_count" in result:
            n = result["created_count"]
            return "created {} sheet{}".format(n, "" if n == 1 else "s")
        if "deleted_count" in result:
            n = result["deleted_count"]
            return "deleted {} element{}".format(n, "" if n == 1 else "s")
        if "applied_count" in result:
            n = result["applied_count"]
            failed = result.get("failed_count", 0)
            if failed:
                return "set {} param{}, {} failed".format(
                    n, "" if n == 1 else "s", failed)
            return "set {} param{}".format(n, "" if n == 1 else "s")
        if "new_view_id" in result:
            nm = result.get("new_view_name") or "?"
            return "duplicated -> '{}'".format(nm)
        if "updated_count" in result and "mode" in result:
            n = result["updated_count"]
            return "{} ({} view{})".format(
                result["mode"], n, "" if n == 1 else "s")
        if "tagged_count" in result:
            n = result["tagged_count"]
            failed = result.get("failed_count", 0)
            if failed:
                return "tagged {}, {} failed".format(n, failed)
            return "tagged {} element{}".format(n, "" if n == 1 else "s")
        if "text_note_id" in result:
            return "placed text note"
        if "filter_id" in result and "rule_count" in result:
            return "created filter '{}'".format(result.get("filter_name") or "?")
        # v4.3 - placement / geometry editing
        if "placed_id" in result:
            return "placed '{}'".format(result.get("type") or "instance")
        if "copies_made" in result and "step_degrees" in result:
            return "radial array x{}".format(result.get("count") or "?")
        if "copies_made" in result and "translation" in result:
            return "linear array x{}".format(result.get("count") or "?")
        if "copied_count" in result:
            n = result["copied_count"]
            return "copied {} element{}".format(n, "" if n == 1 else "s")
        if "moved_count" in result:
            n = result["moved_count"]
            return "moved {} element{}".format(n, "" if n == 1 else "s")
        if "mirrored_count" in result:
            n = result["mirrored_count"]
            kept = " (kept original)" if result.get("keep_original") else ""
            return "mirrored {} element{}{}".format(n, "" if n == 1 else "s", kept)
        if "rotated_count" in result:
            n = result["rotated_count"]
            return "rotated {} element{} by {} deg".format(
                n, "" if n == 1 else "s", result.get("angle_degrees", "?"))
        if "group_id" in result and "member_count" in result:
            return "grouped {} member{}".format(
                result["member_count"], "" if result["member_count"] == 1 else "s")
        if "ungrouped_count" in result:
            n = result["ungrouped_count"]
            return "ungrouped {} group{}".format(n, "" if n == 1 else "s")
        if "updated_count" in result and "pinned" in result:
            n = result["updated_count"]
            return "{} {} element{}".format(
                "pinned" if result["pinned"] else "unpinned",
                n, "" if n == 1 else "s")

        # Read tools
        if "count" in result:
            n = result["count"]
            label = "result"
            for k in ("sheets", "views", "schedules", "elements", "rows",
                      "links", "rooms", "tag_types", "filters"):
                if k in result:
                    label = k
                    break
            extra = " (truncated)" if result.get("truncated") else ""
            return "{} {}{}".format(n, label, extra)
        return "ok"
    if isinstance(result, list):
        return "{} items".format(len(result))
    return "done"


# ---------------------------------------------------------------------------
# API streaming - now with tool_use support
# ---------------------------------------------------------------------------

class StreamCancelled(Exception):
    pass


def _find_clean_trim_start(history_messages, default_start):
    """Find a 'clean' API-history boundary at or after `default_start`.

    A clean boundary is a user message with string content - that's a
    real user question, the natural start of a new turn. We avoid
    starting the API history mid-turn (right after an assistant
    tool_use, where the trim would leave a tool_result orphaned at
    position 0 - the API rejects that with 'unexpected tool_use_id
    found in tool_result blocks').

    If no clean boundary exists within the remaining history, fall
    back to default_start (the API call will fail, but the defensive
    pass downstream will still try to repair).
    """
    n = len(history_messages)
    if default_start <= 0:
        return 0
    # Look forward from default_start for a real user question.
    for i in range(default_start, n):
        m = history_messages[i]
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return i
    # No clean boundary forward - the whole tail is a multi-round
    # tool loop without an interleaved user message. Look BACKWARD
    # instead so we never start mid-turn.
    for i in range(default_start - 1, -1, -1):
        m = history_messages[i]
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return i
    return 0


def _strip_orphan_tool_results_at_start(out):
    """Drop tool_result blocks from the FIRST message(s) of `out`
    that have no preceding assistant tool_use. Safety net for cases
    where the trim window cuts mid-turn despite the clean-boundary
    helper - rather than 400-error the whole request, we drop the
    orphan tool_results. The user loses a sliver of history context
    but the chat keeps working.
    """
    while out:
        first = out[0]
        if first.get("role") != "user":
            return
        c = first.get("content")
        if not isinstance(c, list):
            return
        non_tr = [b for b in c
                  if not (isinstance(b, dict)
                          and b.get("type") == "tool_result")]
        if len(non_tr) == len(c):
            # No tool_result blocks at all - this message is clean.
            return
        if not non_tr:
            # Entirely tool_result blocks - drop the whole message.
            out.pop(0)
            continue
        # Mixed - strip just the tool_result blocks.
        first["content"] = non_tr
        return


def _build_api_messages(history_messages, history_window=HISTORY_WINDOW):
    """Convert our stored history into the API's messages array.
    Strips our metadata (ts, usage) and keeps role + content. Content is
    passed through as-is (string OR list of blocks).

    Defensive repair: if any assistant turn has tool_use blocks without
    matching tool_result blocks in the next message, synthesize the
    missing tool_results before sending. This protects against
    interrupted agent loops (network blip, user-stop, API error mid-flow)
    that would otherwise wedge the conversation - the API rejects history
    with orphan tool_use blocks ('messages.N: tool_use ids were found
    without tool_result blocks').
    """
    # Trim to a clean user-question boundary instead of arbitrarily
    # slicing the last N messages. The arbitrary slice can land mid-turn
    # (right after an assistant tool_use), leaving the matching
    # tool_result orphaned at position 0 - which the API rejects.
    default_start = max(0, len(history_messages) - history_window)
    trim_start = _find_clean_trim_start(history_messages, default_start)
    trimmed = history_messages[trim_start:]
    out = []
    for m in trimmed:
        c = m["content"]
        # Promote a string-content user message to a mixed content list
        # if it has image attachments. Anthropic's API expects images as
        # their own `{"type":"image","source":{...}}` blocks, so we
        # build that here at send time rather than mangling the stored
        # message (which still keeps a clean text content + sidecar
        # `_attachments` for replay).
        if m.get("role") == "user" and isinstance(c, str):
            atts = m.get("_attachments") or []
            images = [a for a in atts
                      if isinstance(a, dict)
                      and a.get("kind") == "image"
                      and a.get("data_b64")]
            if images:
                content_list = []
                for img in images:
                    content_list.append({
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": img.get("media_type", "image/png"),
                            "data":       img["data_b64"],
                        },
                    })
                # Always include a text block - Anthropic requires
                # non-empty content; a placeholder caption keeps the
                # request valid for image-only sends.
                content_list.append({
                    "type": "text",
                    "text": c if (c or "").strip() else "(see attached image)",
                })
                c = content_list
        # Deep-copy list content so the compaction pass below can mutate
        # tool_result blocks without touching our stored conversation.
        if isinstance(c, list):
            c = [dict(blk) if isinstance(blk, dict) else blk for blk in c]
        out.append({"role": m["role"], "content": c})

    # Walk the API messages and insert synthetic tool_results where needed.
    i = 0
    while i < len(out):
        msg = out[i]
        content = msg.get("content")
        if msg.get("role") != "assistant" or not isinstance(content, list):
            i += 1
            continue

        pending_ids = [
            b.get("id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
        ]
        if not pending_ids:
            i += 1
            continue

        next_msg = out[i + 1] if i + 1 < len(out) else None
        next_content = next_msg.get("content") if next_msg else None
        present_ids = set()
        if isinstance(next_content, list):
            present_ids = {
                b.get("tool_use_id") for b in next_content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            }

        missing = [tid for tid in pending_ids if tid not in present_ids]
        if not missing:
            i += 1
            continue

        synth_blocks = [
            {
                "type":         "tool_result",
                "tool_use_id":  tid,
                "content":      json.dumps({
                    "error": ("Tool result missing (the prior agent loop was "
                              "interrupted before this tool finished). "
                              "Re-run the request if you still need it.")
                }),
                "is_error":     True,
            }
            for tid in missing
        ]

        if (next_msg is not None
                and next_msg.get("role") == "user"
                and isinstance(next_content, list)):
            # Glue onto the existing tool_result message.
            next_msg["content"] = list(next_content) + synth_blocks
        else:
            # Insert a new tool_result message right after this assistant turn.
            out.insert(i + 1, {"role": "user", "content": synth_blocks})

        i += 1

    # Compact older tool results so the conversation history doesn't
    # bloat the per-request input-token count. Keeping a fixed window
    # of recent tool results in full preserves the model's working
    # context (it can still reference what it just queried); older
    # results are replaced with a brief one-line summary like
    # "[Result compacted, was: 109 fields]". This is the single
    # biggest win against the 30k input-tokens/min rate limit because
    # list_* tools can return hundreds of items each.
    _compact_old_tool_results(out, keep_full=_TOOL_RESULTS_KEEP_FULL)

    # Safety net: even with the clean-boundary trim above, edge cases
    # (a chat that starts with a tool round, or a corrupted history)
    # might still produce an orphan tool_result at the start. Drop
    # those rather than fail the whole request.
    _strip_orphan_tool_results_at_start(out)

    # Strip image data from older user messages. Each image is roughly
    # 1500-2000 tokens; without this, every long chat with screenshots
    # ships them all on every turn. We keep ONLY the most recent
    # image-bearing user message intact - everything older has its
    # image content blocks replaced with a tiny text placeholder. If
    # the user references an old screenshot, they can re-attach it.
    _strip_old_images(out)

    # Prompt-cache breakpoint on the SECOND-to-last message. Anthropic
    # caches the conversation prefix up to and including this marker
    # for 5 minutes. On the NEXT turn, the prefix (system + tools +
    # everything through this point) hits the cache, so we only pay
    # for the new user message + assistant response. Huge win on long
    # conversations - the input-token bill stays roughly constant per
    # turn instead of growing with history length.
    _mark_last_history_cache_breakpoint(out)

    return out


def _strip_old_images(messages):
    """In-place: replace image content blocks in older user messages
    with a tiny text placeholder. Keeps the MOST RECENT image-bearing
    user message intact so the current question can still reference a
    just-attached screenshot."""
    # Walk from the end to find the most-recent image-bearing user
    # message. That one stays full fidelity.
    keep_index = -1
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "user":
            continue
        c = msg.get("content")
        if not isinstance(c, list):
            continue
        has_image = any(
            isinstance(b, dict) and b.get("type") == "image"
            for b in c)
        if has_image:
            keep_index = i
            break

    # Now strip images from every OTHER user message.
    for i, msg in enumerate(messages):
        if i == keep_index:
            continue
        if msg.get("role") != "user":
            continue
        c = msg.get("content")
        if not isinstance(c, list):
            continue
        new_blocks = []
        had_image = False
        for b in c:
            if isinstance(b, dict) and b.get("type") == "image":
                had_image = True
                continue  # drop the image block entirely
            new_blocks.append(b)
        if had_image:
            # Prepend a tiny placeholder so the model knows an image
            # was attached but is no longer in context.
            new_blocks.insert(0, {
                "type": "text",
                "text": "[image attached earlier in this conversation, "
                        "omitted from current request to save tokens - "
                        "ask the user to re-attach if you need to see it again]",
            })
            msg["content"] = new_blocks


def _mark_last_history_cache_breakpoint(messages):
    """In-place: add a cache_control: ephemeral marker to the LAST
    content block of the second-to-last message in `messages` (i.e.
    the message that ends the prior turn's history). Anthropic uses
    this to cache the conversation prefix through that point; on the
    next request the cache covers everything before the new user
    message.

    No-op if there are fewer than 2 messages or the target message
    can't accept a cache_control marker."""
    if len(messages) < 2:
        return
    target = messages[-2]
    content = target.get("content")
    if isinstance(content, str):
        # Convert string content to a single text block so we can
        # attach cache_control. Anthropic accepts either form.
        target["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
        return
    if isinstance(content, list) and content:
        # Mutate the last block's dict to include cache_control. We
        # already deep-copied list content earlier in the build, so
        # this doesn't touch the stored conversation.
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block["cache_control"] = {"type": "ephemeral"}


def _compact_old_tool_results(messages, keep_full):
    """In-place: replace older tool_result content with a summary.
    `keep_full` most-recent tool_result blocks are left untouched."""
    positions = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        c = msg.get("content")
        if not isinstance(c, list):
            continue
        for bi, blk in enumerate(c):
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                positions.append((mi, bi))
    if len(positions) <= keep_full:
        return
    to_compact = positions[:-keep_full] if keep_full > 0 else positions
    for mi, bi in to_compact:
        blk = messages[mi]["content"][bi]
        raw = blk.get("content", "")
        # The tool_result content is a JSON string in our wire format;
        # parse it to compute a faithful summary, then replace.
        parsed = raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw
        try:
            summary = summarize_tool_result(parsed)
        except Exception:
            summary = "ok"
        blk["content"] = "[Result compacted, was: {}]".format(summary)


# How many of the latest tool_result blocks to keep in their full
# verbose form when building API messages. Older ones get compacted to
# a one-line summary. Picked empirically: 4 is enough that the model
# has access to the last two rounds' details without history bloat
# pushing us over the per-minute rate limit.
_TOOL_RESULTS_KEEP_FULL = 2


def _post_stream_round(api_key, model_id, system_prompt, tools, messages,
                       on_text_delta, on_tool_use_start, on_tool_use_complete,
                       on_error, is_cancelled):
    """One round-trip with the Messages API. Streams the response.

    Returns: (blocks, stop_reason, usage)
        blocks      - list of finalized content blocks (text and tool_use)
        stop_reason - "end_turn" | "tool_use" | "max_tokens" | "stop_sequence" | None
        usage       - {"input_tokens": int, "output_tokens": int}

    Returns (None, None, usage) if on_error was invoked.

    Callbacks (all invoked on the worker thread):
        on_text_delta(str)               - each text chunk for streaming UI
        on_tool_use_start(dict)          - {id,name} when a tool_use block begins
        on_tool_use_complete(dict)       - {id,name,input} when its input is parsed
        on_error(label, detail)
        is_cancelled() -> bool
    """
    # ensure_ascii=False so non-ASCII characters survive the dump call
    # (IronPython 2.7's ascii encoder bug, see save_conversation()).
    # Encoding.UTF8.GetBytes correctly converts the resulting string to
    # the UTF-8 bytes the Anthropic API expects.
    #
    # Prompt caching: mark the system prompt and tools array with
    # cache_control=ephemeral so subsequent requests don't pay full
    # input-token cost on those (huge, stable) prefixes. With our 24+
    # tools and ~3k-token system prompt, this drops effective input
    # consumption by ~80% and keeps us well under the 30k tokens/min
    # rate limit even on long agent loops.
    system_blocks = [{
        "type": "text",
        "text": system_prompt or "",
        "cache_control": {"type": "ephemeral"},
    }]

    # Caching at the tool boundary: marker on the LAST tool tells the
    # API "cache everything up through here." We copy the last tool
    # rather than mutating TOOL_DEFS in place.
    tools_for_api = list(tools or [])
    if tools_for_api:
        last_tool = dict(tools_for_api[-1])
        last_tool["cache_control"] = {"type": "ephemeral"}
        tools_for_api[-1] = last_tool

    body = json.dumps({
        "model":      model_id,
        "max_tokens": MAX_TOKENS,
        "stream":     True,
        "system":     system_blocks,
        "tools":      tools_for_api,
        "messages":   messages,
    }, ensure_ascii=False)
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
        return None, None, {"input_tokens": 0, "output_tokens": 0}

    blocks      = []
    current     = None
    stop_reason = None
    usage       = {"input_tokens": 0, "output_tokens": 0}

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

                if etype == "content_block_start":
                    cb = evt.get("content_block", {}) or {}
                    btype = cb.get("type")
                    current = {"type": btype}
                    if btype == "text":
                        current["text"] = ""
                    elif btype == "tool_use":
                        current["id"]    = cb.get("id", "")
                        current["name"]  = cb.get("name", "")
                        current["_buf"]  = ""   # accumulating partial JSON
                        current["input"] = {}
                        on_tool_use_start({"id": current["id"], "name": current["name"]})
                    blocks.append(current)

                elif etype == "content_block_delta":
                    delta = evt.get("delta", {}) or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        txt = delta.get("text", "")
                        if txt and current is not None and current.get("type") == "text":
                            current["text"] += txt
                            on_text_delta(txt)
                    elif dtype == "input_json_delta":
                        pj = delta.get("partial_json", "")
                        if current is not None and current.get("type") == "tool_use":
                            current["_buf"] += pj

                elif etype == "content_block_stop":
                    if current is not None and current.get("type") == "tool_use":
                        try:
                            current["input"] = json.loads(current.get("_buf") or "{}")
                        except Exception:
                            current["input"] = {}
                        on_tool_use_complete({
                            "id":    current["id"],
                            "name":  current["name"],
                            "input": current["input"],
                        })
                    current = None

                elif etype == "message_start":
                    u = (evt.get("message", {}) or {}).get("usage", {}) or {}
                    usage["input_tokens"]  = u.get("input_tokens", 0)
                    usage["output_tokens"] = u.get("output_tokens", 0)

                elif etype == "message_delta":
                    d = evt.get("delta", {}) or {}
                    if "stop_reason" in d:
                        stop_reason = d["stop_reason"]
                    u = evt.get("usage", {}) or {}
                    if "output_tokens" in u:
                        usage["output_tokens"] = u["output_tokens"]

                elif etype == "error":
                    err = evt.get("error", {}) or {}
                    on_error("api", err.get("message", "Unknown streaming error"))
                    return None, None, usage
        finally:
            reader.Close()
            stream.Close()
    except StreamCancelled:
        return None, "cancelled", usage
    except WebException as we:
        _handle_web_exception(we, on_error)
        return None, None, usage
    except Exception as e:
        on_error("internal", "{}: {}".format(type(e).__name__, e))
        return None, None, usage
    finally:
        try:
            resp.Close()
        except Exception:
            pass

    # Strip internal accumulator before returning.
    for b in blocks:
        if "_buf" in b:
            del b["_buf"]
    return blocks, stop_reason, usage


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
        on_error("rate_limit", "Rate-limited by the Anthropic API. " + (msg or ""))
    elif status == 529:
        on_error("overloaded", "Anthropic is overloaded. Wait a moment and try again.")
    else:
        on_error("api", "HTTP {}: {}".format(status, msg or "API request failed"))


# ---------------------------------------------------------------------------
# Settings sub-dialog
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

        # Spend threshold (USD per request soft-cap).
        threshold = current_config.get("spend_threshold", DEFAULT_SPEND_THRESHOLD)
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            threshold = DEFAULT_SPEND_THRESHOLD
        self.txt_spend_threshold.Text = "{:.2f}".format(threshold)

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

        # Parse spend threshold. Negative / unparseable -> revert to default.
        try:
            spend_threshold = float((self.txt_spend_threshold.Text or "").strip())
            if spend_threshold < 0:
                spend_threshold = DEFAULT_SPEND_THRESHOLD
        except ValueError:
            spend_threshold = DEFAULT_SPEND_THRESHOLD

        try:
            save_config(api_key, model_key, system_prompt, spend_threshold)
        except Exception as e:
            self.bnr_key_error.Visibility = Visibility.Visible
            self.txt_key_error.Text = "Could not save config: {}".format(e)
            return

        self._result = {
            "api_key":         api_key,
            "model_key":       model_key,
            "system_prompt":   system_prompt,
            "spend_threshold": spend_threshold,
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


# ---------------------------------------------------------------------------
# Pick prompt window
#
# Small floating always-on-top window shown during "+ -> Pick element(s)".
# Revit's PickObjects API locks input to canvas-only, which means the user
# can't click sheets/views in the Project Browser during a pick. Instead
# of using PickObjects, we just minimize the chatbot, show this prompt,
# and let the user select freely (canvas, browser, Ctrl-click multi-select).
# They click "Attach selection" when ready and we read whatever's selected.
# ---------------------------------------------------------------------------

class PickPromptWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, PICK_PROMPT_XAML)
        # _result becomes "done" on Attach, "cancel" on Cancel or X.
        self._result = None
        self.btn_done.Click   += self._on_done
        self.btn_cancel.Click += self._on_cancel

    def _on_done(self, sender, args):
        self._result = "done"
        self.Close()

    def _on_cancel(self, sender, args):
        self._result = "cancel"
        self.Close()


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
# Brushes
# ---------------------------------------------------------------------------

_BRUSH_USER_BG    = SolidColorBrush(Color.FromRgb( 43, 108, 176))
_BRUSH_USER_FG    = SolidColorBrush(Colors.White)
_BRUSH_BOT_BG     = SolidColorBrush(Colors.White)
_BRUSH_BOT_BORDER = SolidColorBrush(Color.FromRgb(226, 232, 240))
_BRUSH_BOT_FG     = SolidColorBrush(Color.FromRgb( 45,  55,  72))
_BRUSH_AI_ACCENT  = SolidColorBrush(Color.FromRgb(107,  70, 193))
_BRUSH_BOT_LABEL  = SolidColorBrush(Color.FromRgb(113, 128, 150))
_BRUSH_ERROR_BG   = SolidColorBrush(Color.FromRgb(255, 245, 245))
_BRUSH_ERROR_BORD = SolidColorBrush(Color.FromRgb(252, 129, 129))
_BRUSH_ERROR_FG   = SolidColorBrush(Color.FromRgb(116,  42,  42))

_BRUSH_TRANSPARENT  = SolidColorBrush(Colors.Transparent)
_BRUSH_ROW_HOVER    = SolidColorBrush(Color.FromRgb(247, 250, 252))
_BRUSH_ROW_ACTIVE   = SolidColorBrush(Color.FromRgb(235, 248, 255))
_BRUSH_ROW_ACCENT   = SolidColorBrush(Color.FromRgb( 49, 130, 206))
_BRUSH_ROW_TITLE    = SolidColorBrush(Color.FromRgb( 45,  55,  72))
_BRUSH_ROW_DATE     = SolidColorBrush(Color.FromRgb(160, 174, 192))
_BRUSH_ROW_DEL      = SolidColorBrush(Color.FromRgb(160, 174, 192))
_BRUSH_ROW_DEL_HOT  = SolidColorBrush(Color.FromRgb(229,  62,  62))
_BRUSH_BUCKET_HEAD  = SolidColorBrush(Color.FromRgb(160, 174, 192))

# Tool call bubble palette
_BRUSH_TOOL_BG      = SolidColorBrush(Color.FromRgb(247, 250, 252))
_BRUSH_TOOL_BORDER  = SolidColorBrush(Color.FromRgb(226, 232, 240))
_BRUSH_TOOL_NAME    = SolidColorBrush(Color.FromRgb( 45,  55,  72))
_BRUSH_TOOL_ARROW   = SolidColorBrush(Color.FromRgb(160, 174, 192))
_BRUSH_TOOL_STATUS  = SolidColorBrush(Color.FromRgb(113, 128, 150))
_BRUSH_TOOL_ERROR   = SolidColorBrush(Color.FromRgb(197,  48,  48))

# Inline tool "pill" inside an assistant turn bubble. Designed to read
# as quiet metadata that the eye skims past - subtle gray rounded pill,
# light text colors, slightly smaller font than the surrounding markdown.
_BRUSH_TOOL_PILL_BG    = SolidColorBrush(Color.FromRgb(237, 242, 247))  # #EDF2F7
_BRUSH_TOOL_PILL_NAME  = SolidColorBrush(Color.FromRgb( 74,  85, 104))  # #4A5568 (semi-bold)
_BRUSH_TOOL_PILL_TEXT  = SolidColorBrush(Color.FromRgb(160, 174, 192))  # #A0AEC0 (light gray)

# "Open in Revit" action button - rendered at the END of an assistant
# turn when the turn produced one or more openable elements (sheet, view,
# schedule). Bigger and squared-off so it reads as a real action button,
# not a quiet breadcrumb chip. Uses the firm primary blue palette.
_BRUSH_OPEN_CHIP_BG       = SolidColorBrush(Color.FromRgb(235, 248, 255))  # #EBF8FF (very light blue)
_BRUSH_OPEN_CHIP_BORDER   = SolidColorBrush(Color.FromRgb(144, 205, 244))  # #90CDF4
_BRUSH_OPEN_CHIP_TEXT     = SolidColorBrush(Color.FromRgb( 43, 108, 176))  # #2B6CB0 (firm primary blue)
_BRUSH_OPEN_CHIP_HOVER_BG = SolidColorBrush(Color.FromRgb(190, 227, 248))  # #BEE3F8 (slightly darker on hover)

# Match-highlight: a brief amber outline applied to the bubble the user
# landed on after clicking a search result. Clears itself after a few
# seconds via DispatcherTimer.
_BRUSH_MATCH_HIGHLIGHT    = SolidColorBrush(Color.FromRgb(214, 158,  46))  # #D69E2E (firm warn border)

# Attachment chip palette
# Two contexts: chips in the input area (on white app surface) and chips
# inside a user bubble (on the blue user-bubble surface). Different colors.
_BRUSH_CHIP_BG          = SolidColorBrush(Color.FromRgb(235, 248, 255))  # #EBF8FF
_BRUSH_CHIP_BORDER      = SolidColorBrush(Color.FromRgb(144, 205, 244))  # #90CDF4
_BRUSH_CHIP_TEXT        = SolidColorBrush(Color.FromRgb( 44,  82, 130))  # #2C5282
_BRUSH_CHIP_X           = SolidColorBrush(Color.FromRgb( 74,  85, 104))  # #4A5568
_BRUSH_CHIP_X_HOT       = SolidColorBrush(Color.FromRgb(229,  62,  62))  # #E53E3E

_BRUSH_USER_CHIP_BG     = SolidColorBrush(Color.FromArgb( 60, 255, 255, 255))
_BRUSH_USER_CHIP_BORDER = SolidColorBrush(Color.FromArgb(160, 255, 255, 255))

# Custom popup item hover (modern dropdown menu)
_BRUSH_POPUP_HOVER      = SolidColorBrush(Color.FromRgb(247, 250, 252))  # #F7FAFC

# Question-styling for an assistant turn whose final text ends in "?"
# Light-blue bubble background + violet left-border accent.
_BRUSH_QUESTION_BG      = SolidColorBrush(Color.FromRgb(245, 250, 255))  # very light blue
_BRUSH_QUESTION_BORDER  = SolidColorBrush(Color.FromRgb(107,  70, 193))  # violet (matches AI accent)
_BRUSH_QUESTION_LABEL   = SolidColorBrush(Color.FromRgb(107,  70, 193))  # violet label


def _is_descendant_of_button(elem):
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


# ===========================================================================
# REVIT ACTION HANDLER (modeless plumbing)
# ===========================================================================
#
# Modeless WPF dialogs do NOT hold a Revit API context. Calling
# DB methods directly from a WPF event or a worker thread after main()
# returns would throw "Attempted to access an external API outside an
# external command/event handler context." Solution: route every Revit
# API call through this single shared handler. ExternalEvent.Raise()
# queues a request; Revit invokes Execute() on its UI thread when it's
# ready, granting API context for the duration.
#
# The handler supports two calling styles:
#   queue_action(fn, on_complete=cb)
#       Async. Use from UI thread for "do this, update UI when done".
#       on_complete(result, exception) runs on Revit's UI thread.
#   queue_action_blocking(fn, timeout_ms=...)
#       Sync. Use ONLY from background threads. Blocks the caller until
#       Execute() runs fn and returns its result. NEVER call from the
#       UI thread - that deadlocks (UI thread waits on itself).
# ---------------------------------------------------------------------------

class _LockGuard(object):
    """Context manager around .NET Monitor for IronPython-friendly locking."""
    def __init__(self, obj):
        self._obj = obj
    def __enter__(self):
        Monitor.Enter(self._obj)
        return self
    def __exit__(self, exc_type, exc_val, tb):
        Monitor.Exit(self._obj)


class _ActionRequest(object):
    """Plain data: a queued (fn, on_complete) pair."""
    __slots__ = ("fn", "on_complete")
    def __init__(self, fn, on_complete):
        self.fn          = fn
        self.on_complete = on_complete


class _RevitActionHandler(IExternalEventHandler):
    """Single shared IExternalEventHandler. Drains a FIFO queue of
    actions on each Execute() invocation."""

    def __init__(self):
        self._lock     = System.Object()
        self._queue    = []
        self._event    = None       # set by attach_event()
        self._shutdown = False

    def attach_event(self, ext_event):
        self._event = ext_event

    def shutdown(self):
        """Mark the handler dead. queue_action_blocking will raise
        immediately rather than wait forever after the window has closed."""
        self._shutdown = True
        # Signal anything still waiting on a completion handle.
        with _LockGuard(self._lock):
            queue = list(self._queue)
            self._queue = []
        # Fire any on_complete callbacks with an error so callers unblock.
        for req in queue:
            try:
                if req.on_complete is not None:
                    req.on_complete(None, RuntimeError("Chatbot is shutting down"))
            except Exception:
                pass

    def queue_action(self, fn, on_complete=None):
        """Queue fn() to run on Revit's UI thread. Non-blocking.
        on_complete(result, exception) runs on the UI thread after fn returns
        (or after the handler is shut down)."""
        if self._shutdown:
            if on_complete is not None:
                try:
                    on_complete(None, RuntimeError("Chatbot is shutting down"))
                except Exception:
                    pass
            return
        with _LockGuard(self._lock):
            self._queue.append(_ActionRequest(fn, on_complete))
        try:
            self._event.Raise()
        except Exception:
            pass

    def queue_action_blocking(self, fn, timeout_ms=120000):
        """Queue fn() and BLOCK until it runs. Returns its result, or
        raises its exception. Use only from background threads."""
        if self._shutdown:
            raise RuntimeError("Chatbot is shutting down")

        completion = ManualResetEventSlim(False)
        holder = {"result": None, "exception": None}

        def on_done(result, exception):
            holder["result"]    = result
            holder["exception"] = exception
            completion.Set()

        self.queue_action(fn, on_complete=on_done)

        if not completion.Wait(timeout_ms):
            raise RuntimeError(
                "Revit action timed out after {} ms".format(timeout_ms))
        if holder["exception"] is not None:
            raise holder["exception"]
        return holder["result"]

    # --- IExternalEventHandler interface -----------------------------------

    def Execute(self, app):
        with _LockGuard(self._lock):
            queue = list(self._queue)
            self._queue = []
        for req in queue:
            result, exception = None, None
            try:
                result = req.fn()
            except Exception as e:
                exception = e
            if req.on_complete is not None:
                try:
                    req.on_complete(result, exception)
                except Exception:
                    # Don't let a callback failure crash Execute - that
                    # would leave the queue in a bad state.
                    pass

    def GetName(self):
        return "dbHMS Chatbot Action"


# ===========================================================================
# MARKDOWN RENDERING
# ===========================================================================
#
# Claude's text responses are markdown. We parse them into block-level
# structures (paragraph / heading / code_block / list / table / blockquote /
# hr) and render each as a real WPF control - tables become actual Grids
# with header rows and gridlines, code blocks get mono font + gray panel,
# bold/italic/inline-code render via Run/Bold/Italic Inlines.
#
# Streaming UX: while text is arriving we just append to a plain TextBlock
# (so it feels live, no flicker from re-parsing every chunk). When that
# text block ends, the bubble's content panel gets cleared and re-populated
# with the markdown-rendered version.
#
# Limits (acceptable for a chat assistant; can extend later):
#   - No nested formatting (bold inside italic, etc.)
#   - No inline links rendered as clickable - shown as plain text
#   - No nested lists
#   - Tables assumed to have a header + separator row + zero or more data rows
# ---------------------------------------------------------------------------

_MD_TABLE_SEP_RE = re.compile(r'^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$')
_MD_LIST_BULLET_RE = re.compile(r'^\s*[-*+]\s+(.*)$')
_MD_LIST_ORDERED_RE = re.compile(r'^\s*\d+[.)]\s+(.*)$')
_MD_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')
_MD_HR_RE = re.compile(r'^[-*_]{3,}$')

# Inline matcher: bold / italic / code, in priority order.
_MD_INLINE_RE = re.compile(
    r'(?P<bold>\*\*[^*\n]+?\*\*)'
    r'|(?P<italic>\*[^*\n]+?\*)'
    r'|(?P<code>`[^`\n]+?`)'
)


def _parse_markdown(text):
    """Parse markdown text into a flat list of block dicts."""
    if not text:
        return []
    lines = text.split("\n")
    blocks = []
    i = 0
    n = len(lines)

    def is_block_start(line):
        s = line.strip()
        if not s:
            return True  # blank ends a paragraph
        if s.startswith("```"): return True
        if s.startswith("#"):   return True
        if s.startswith(">"):   return True
        if _MD_HR_RE.match(s):  return True
        if _MD_LIST_BULLET_RE.match(line):  return True
        if _MD_LIST_ORDERED_RE.match(line): return True
        # Table-row-looking line: starts AND ends with '|' and has at
        # least one interior '|' (so >= 2 cells). We can't see the
        # separator row from here (no lookahead), but treating this as
        # a paragraph-terminator lets the main parsing loop pick the
        # table up cleanly on the next iteration. Without this check,
        # a preceding bold/plain paragraph would gobble the entire
        # table as raw text and pipes would render as literal '|'.
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            return True
        return False

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Blank line - skip
        if not stripped:
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            code_lines = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1  # consume closing ```
            blocks.append({"type": "code_block", "lang": lang,
                           "text": "\n".join(code_lines)})
            continue

        # Heading
        m = _MD_HEADING_RE.match(stripped)
        if m:
            blocks.append({
                "type":  "heading",
                "level": len(m.group(1)),
                "text":  m.group(2).strip(),
            })
            i += 1
            continue

        # Horizontal rule
        if _MD_HR_RE.match(stripped):
            blocks.append({"type": "hr"})
            i += 1
            continue

        # Table: this line plus the next line is a separator.
        if "|" in line and i + 1 < n and _MD_TABLE_SEP_RE.match(lines[i + 1].strip()):
            header = _split_table_row(line)
            i += 2  # skip header + separator
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1
            blocks.append({"type": "table", "header": header, "rows": rows})
            continue

        # Lists (consume contiguous list items)
        if _MD_LIST_BULLET_RE.match(line) or _MD_LIST_ORDERED_RE.match(line):
            ordered = bool(_MD_LIST_ORDERED_RE.match(line))
            items = []
            while i < n:
                lm = (_MD_LIST_ORDERED_RE.match(lines[i]) if ordered
                      else _MD_LIST_BULLET_RE.match(lines[i]))
                if lm:
                    items.append(lm.group(1))
                    i += 1
                    continue
                # If the alternate kind shows up, treat list as ended.
                if (_MD_LIST_ORDERED_RE.match(lines[i])
                        or _MD_LIST_BULLET_RE.match(lines[i])):
                    break
                break
            blocks.append({"type": "list", "ordered": ordered, "items": items})
            continue

        # Blockquote
        if stripped.startswith(">"):
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(re.sub(r'^\s*>\s?', '', lines[i]))
                i += 1
            blocks.append({"type": "blockquote", "text": "\n".join(quote_lines)})
            continue

        # Plain paragraph - gather contiguous non-block-start lines
        para_lines = [line]
        i += 1
        while i < n and lines[i].strip() and not is_block_start(lines[i]):
            para_lines.append(lines[i])
            i += 1
        blocks.append({"type": "paragraph", "text": "\n".join(para_lines)})

    return blocks


def _split_table_row(line):
    """Split '| a | b | c |' into ['a', 'b', 'c']. Tolerates missing
    leading/trailing pipes."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


# Brushes for markdown rendering
_BRUSH_MD_CODE_BG       = SolidColorBrush(Color.FromRgb(247, 250, 252))  # #F7FAFC
_BRUSH_MD_CODE_BORDER   = SolidColorBrush(Color.FromRgb(226, 232, 240))  # #E2E8F0
_BRUSH_MD_INLINE_CODE   = SolidColorBrush(Color.FromRgb(237, 242, 247))  # #EDF2F7
_BRUSH_MD_TABLE_BORDER  = SolidColorBrush(Color.FromRgb(226, 232, 240))
_BRUSH_MD_TABLE_HEADBG  = SolidColorBrush(Color.FromRgb(247, 250, 252))
_BRUSH_MD_HR            = SolidColorBrush(Color.FromRgb(226, 232, 240))
_BRUSH_MD_QUOTE_BAR     = SolidColorBrush(Color.FromRgb(160, 174, 192))  # #A0AEC0
_BRUSH_MD_QUOTE_TEXT    = SolidColorBrush(Color.FromRgb(113, 128, 150))  # #718096

_FONT_MONO = FontFamily("Consolas")


def _add_inline_markdown(inline_host, text):
    """Populate the `.Inlines` collection of a TextBlock or Paragraph
    with bold / italic / code / plain runs parsed from `text`. Both
    types expose the same InlineCollection API so the body doesn't
    care which one it gets. Foreground / FontSize / FontWeight set on
    the host (or its owning FlowDocument) are inherited by every Run
    unless explicitly overridden."""
    if not text:
        return
    pos = 0
    for m in _MD_INLINE_RE.finditer(text):
        if m.start() > pos:
            inline_host.Inlines.Add(Run(text[pos:m.start()]))
        if m.group("bold"):
            inner = m.group("bold")[2:-2]
            b = Bold()
            b.Inlines.Add(Run(inner))
            inline_host.Inlines.Add(b)
        elif m.group("italic"):
            inner = m.group("italic")[1:-1]
            it = Italic()
            it.Inlines.Add(Run(inner))
            inline_host.Inlines.Add(it)
        elif m.group("code"):
            inner = m.group("code")[1:-1]
            r = Run(inner)
            r.FontFamily = _FONT_MONO
            r.Background = _BRUSH_MD_INLINE_CODE
            inline_host.Inlines.Add(r)
        pos = m.end()
    if pos < len(text):
        inline_host.Inlines.Add(Run(text[pos:]))


def _make_markdown_text(text, foreground, font_size=13, font_weight=None):
    """Return a 'TextBlock-styled' RichTextBox whose FlowDocument
    contains a single Paragraph with the inline markdown (bold,
    italic, code) parsed from `text`.

    Why RichTextBox + FlowDocument and not TextBlock+IsTextSelectionEnabled:
    `TextBlock.IsTextSelectionEnabled` (.NET 4.5+) is supposed to make
    a TextBlock selectable, but in pyRevit's WPF host the parent
    ScrollViewer often wins the mouse-drag race and selection never
    starts. RichTextBox has built-in TextSelection support that's
    independent of the surrounding scroll machinery, so click-drag +
    Ctrl+C work in every host.

    Looks like a TextBlock: no border, transparent background, no
    padding, no focus ring, no scroll bars. Paragraph margin is zero
    so vertical rhythm matches the old TextBlock layout."""
    rtb = RichTextBox()
    rtb.IsReadOnly       = True
    rtb.BorderThickness  = Thickness(0)
    rtb.Background       = SolidColorBrush(Colors.Transparent)
    rtb.Padding          = Thickness(0)
    rtb.Cursor           = Cursors.IBeam
    rtb.VerticalScrollBarVisibility   = ScrollBarVisibility.Disabled
    rtb.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
    # AutoWordSelection makes drag-selection snap to whole-word
    # boundaries once you cross the first one, which feels jumpy. The
    # WPF default is supposed to be False but we set it explicitly
    # here because the user saw word-snapping in pyRevit's WPF host
    # (something downstream may be flipping the default). With this
    # off, selection follows the mouse character-by-character just
    # like a plain TextBox.
    try:
        rtb.AutoWordSelection = False
    except Exception:
        pass
    # IsDocumentEnabled controls whether embedded UIElements + hyperlinks
    # are interactive. We don't embed any, and disabling it stops the
    # RichTextBox from grabbing tab focus by accident.
    try:
        rtb.IsDocumentEnabled = False
    except Exception:
        pass
    try:
        rtb.IsUndoEnabled = False
    except Exception:
        pass
    try:
        rtb.FocusVisualStyle = None
    except Exception:
        pass

    doc = FlowDocument()
    doc.PagePadding = Thickness(0)
    doc.FontSize    = font_size
    if foreground is not None:
        doc.Foreground = foreground
    if font_weight is not None:
        doc.FontWeight = font_weight
    # FlowDocument defaults to Justify, which makes paragraph text look
    # weirdly stretched in chat bubbles. Force left alignment.
    try:
        doc.TextAlignment = System.Windows.TextAlignment.Left
    except Exception:
        pass

    p = Paragraph()
    p.Margin = Thickness(0)
    _add_inline_markdown(p, text)
    doc.Blocks.Add(p)

    rtb.Document = doc
    return rtb


def _enable_text_selection(tb):
    """Turn on mouse-drag selection + Ctrl+C on a TextBlock.

    IsTextSelectionEnabled is .NET 4.5+ and SHOULD just work, but in
    some WPF hosts the parent ScrollViewer steals the mouse-drag
    before the TextBlock sees it. Setting Focusable + IBeam cursor
    forces WPF to treat the TextBlock as a text-input target so the
    auto-attached TextEditor wins the routing battle. Wrapped in
    try/except so any single missing property doesn't break rendering.
    """
    try:
        tb.IsTextSelectionEnabled = True
    except Exception:
        pass
    try:
        tb.Focusable = True
    except Exception:
        pass
    try:
        tb.Cursor = Cursors.IBeam
    except Exception:
        pass


def _make_selectable_text(text, foreground, font_size=13, font_weight=None,
                          wrap=True):
    """Return a 'TextBlock-styled' read-only TextBox. Behaves like a
    TextBlock visually but supports native click-drag selection +
    Ctrl+C in every WPF host (no dependency on .NET 4.5+ flags). Use
    this for PLAIN text that doesn't need inline markdown formatting
    (user-typed messages, streaming chunks before markdown render,
    error bodies, etc.).

    For markdown-formatted content with inline bold/italic/code
    spans, keep using `_make_markdown_text` - TextBox can't render
    inline Runs."""
    tb = TextBox()
    tb.Text          = text or ""
    tb.IsReadOnly    = True
    tb.BorderThickness = Thickness(0)
    tb.Background    = SolidColorBrush(Colors.Transparent)
    tb.Padding       = Thickness(0)
    tb.FontSize      = font_size
    if foreground is not None:
        tb.Foreground = foreground
    if font_weight is not None:
        tb.FontWeight = font_weight
    if wrap:
        tb.TextWrapping = TextWrapping.Wrap
    tb.IsUndoEnabled = False  # we'd never need undo for read-only display
    tb.Cursor        = Cursors.IBeam
    # No focus rectangle - it should LOOK like a TextBlock.
    try:
        tb.FocusVisualStyle = None
    except Exception:
        pass
    return tb


def _build_table(header, rows):
    """Render a markdown table as a bordered Grid. Returns a Border.

    Note: written for IronPython 2.7, which is Python 2.7 + extras and
    therefore does NOT support max()'s `default=` kwarg (added in CPython 3.4).
    """
    n_cols = len(header)
    for r in rows:
        if len(r) > n_cols:
            n_cols = len(r)
    if n_cols == 0:
        return None
    n_rows = 1 + len(rows)

    outer = Border()
    outer.BorderBrush     = _BRUSH_MD_TABLE_BORDER
    outer.BorderThickness = Thickness(1)
    outer.CornerRadius    = System.Windows.CornerRadius(4)
    outer.Margin          = Thickness(0, 6, 0, 6)

    grid = Grid()
    for _ in range(n_cols):
        cd = ColumnDefinition()
        cd.Width = GridLength(1, GridUnitType.Star)
        grid.ColumnDefinitions.Add(cd)
    for _ in range(n_rows):
        rd = RowDefinition()
        rd.Height = GridLength.Auto
        grid.RowDefinitions.Add(rd)

    # Header row
    for j in range(n_cols):
        cell = Border()
        cell.Background     = _BRUSH_MD_TABLE_HEADBG
        cell.BorderBrush    = _BRUSH_MD_TABLE_BORDER
        cell.BorderThickness = Thickness(
            0, 0,
            1 if j < n_cols - 1 else 0,
            1,
        )
        cell.Padding = Thickness(8, 6, 8, 6)
        Grid.SetRow(cell, 0)
        Grid.SetColumn(cell, j)
        cell_text = header[j] if j < len(header) else ""
        tb = _make_markdown_text(cell_text, _BRUSH_BOT_FG, font_size=12,
                                 font_weight=FontWeights.SemiBold)
        cell.Child = tb
        grid.Children.Add(cell)

    # Data rows
    for i, row in enumerate(rows):
        for j in range(n_cols):
            cell = Border()
            cell.Background = SolidColorBrush(Colors.White)
            cell.BorderBrush = _BRUSH_MD_TABLE_BORDER
            cell.BorderThickness = Thickness(
                0, 0,
                1 if j < n_cols - 1 else 0,
                1 if i < len(rows) - 1 else 0,
            )
            cell.Padding = Thickness(8, 5, 8, 5)
            Grid.SetRow(cell, i + 1)
            Grid.SetColumn(cell, j)
            cell_text = row[j] if j < len(row) else ""
            tb = _make_markdown_text(cell_text, _BRUSH_BOT_FG, font_size=12)
            cell.Child = tb
            grid.Children.Add(cell)

    outer.Child = grid
    return outer


def _render_markdown_into(panel, text):
    """Parse `text` as markdown and append rendered children to `panel`.
    Designed to be called against an empty StackPanel; will wipe nothing."""
    blocks = _parse_markdown(text)
    if not blocks:
        # Empty - render as a single empty paragraph so layout doesn't collapse.
        return

    for idx, block in enumerate(blocks):
        btype = block.get("type")

        if btype == "paragraph":
            tb = _make_markdown_text(block["text"], _BRUSH_BOT_FG, font_size=13)
            tb.Margin = Thickness(0, 0, 0, 6 if idx < len(blocks) - 1 else 0)
            panel.Children.Add(tb)

        elif btype == "heading":
            sz = {1: 18, 2: 16, 3: 14, 4: 13, 5: 12, 6: 12}.get(block["level"], 14)
            tb = _make_markdown_text(block["text"], _BRUSH_BOT_FG,
                                     font_size=sz, font_weight=FontWeights.Bold)
            tb.Margin = Thickness(0, 8 if idx > 0 else 0, 0, 4)
            panel.Children.Add(tb)

        elif btype == "code_block":
            border = Border()
            border.Background     = _BRUSH_MD_CODE_BG
            border.BorderBrush    = _BRUSH_MD_CODE_BORDER
            border.BorderThickness = Thickness(1)
            border.CornerRadius   = System.Windows.CornerRadius(4)
            border.Padding        = Thickness(10, 8, 10, 8)
            border.Margin         = Thickness(0, 4, 0, 6)
            # Read-only TextBox so the user can highlight + Ctrl+C
            # code snippets directly. Code blocks are plain text, so
            # we don't need TextBlock's inline-Run support here.
            tb = _make_selectable_text(block["text"],
                                       foreground=_BRUSH_BOT_FG,
                                       font_size=11)
            tb.FontFamily = _FONT_MONO
            border.Child = tb
            panel.Children.Add(border)

        elif btype == "list":
            list_panel = StackPanel()
            list_panel.Margin = Thickness(0, 2, 0, 6)
            for li_idx, item in enumerate(block["items"]):
                row = Grid()
                bullet_col = ColumnDefinition()
                bullet_col.Width = GridLength.Auto
                row.ColumnDefinitions.Add(bullet_col)
                content_col = ColumnDefinition()
                content_col.Width = GridLength(1, GridUnitType.Star)
                row.ColumnDefinitions.Add(content_col)

                bullet = TextBlock()
                bullet.Text = ("{}.".format(li_idx + 1)
                               if block["ordered"] else u"•")
                bullet.Margin = Thickness(0, 0, 8, 0)
                bullet.Foreground = _BRUSH_BOT_FG
                bullet.FontSize = 13
                bullet.MinWidth = 16
                Grid.SetColumn(bullet, 0)
                row.Children.Add(bullet)

                content_tb = _make_markdown_text(item, _BRUSH_BOT_FG, font_size=13)
                Grid.SetColumn(content_tb, 1)
                row.Children.Add(content_tb)

                row.Margin = Thickness(0, 1, 0, 1)
                list_panel.Children.Add(row)
            panel.Children.Add(list_panel)

        elif btype == "table":
            t = _build_table(block["header"], block["rows"])
            if t is not None:
                panel.Children.Add(t)

        elif btype == "hr":
            border = Border()
            border.Height     = 1
            border.Background = _BRUSH_MD_HR
            border.Margin     = Thickness(0, 8, 0, 8)
            panel.Children.Add(border)

        elif btype == "blockquote":
            border = Border()
            border.BorderBrush     = _BRUSH_MD_QUOTE_BAR
            border.BorderThickness = Thickness(3, 0, 0, 0)
            border.Padding         = Thickness(10, 4, 0, 4)
            border.Margin          = Thickness(0, 4, 0, 6)
            tb = _make_markdown_text(block["text"], _BRUSH_MD_QUOTE_TEXT,
                                     font_size=12)
            tb.FontStyle = FontStyles.Italic
            border.Child = tb
            panel.Children.Add(border)


# ---------------------------------------------------------------------------
# Main chat form
# ---------------------------------------------------------------------------

class ChatbotForm(forms.WPFWindow):

    def __init__(self, telemetry_session=None):
        forms.WPFWindow.__init__(self, CHATBOT_FORM_XAML)

        # Telemetry: end()'d on Window.Closed (modeless lifecycle).
        self._telemetry_session = telemetry_session

        # ExternalEvent + handler — the bridge between background
        # threads / WPF event handlers and Revit's API context.
        self._action_handler = _RevitActionHandler()
        self._external_event = ExternalEvent.Create(self._action_handler)
        self._action_handler.attach_event(self._external_event)

        # True while a pick is in flight; UI is partially disabled.
        self._pick_in_progress = False

        try:
            self._doc = revit.doc
        except Exception:
            self._doc = None
        self._project_key   = _project_key(self._doc)
        self._project_label = _project_label(self._doc)

        self._config        = load_config()
        self._conversations = list_conversations(self._project_key)
        self._current_conv  = None

        # Streaming + turn state. One "turn" spans from a user send to
        # the agent's end_turn (possibly multiple API rounds in between).
        # All assistant text + tool calls in a turn share ONE bubble.
        self._cancelled            = False
        self._streaming_buffer     = ""
        self._streaming_text_block = None  # current TextBlock being filled by deltas
        self._turn_bubble          = None  # outer Border for the active turn
        self._turn_content         = None  # StackPanel inside it (text + tool rows)
        self._turn_label           = None  # the "Assistant" TextBlock (mutated to "Question")
        # All committed (non-streaming) text in the current turn, joined.
        # Used for question detection - we treat the bubble as a "question"
        # if a '?' appears ANYWHERE in the assistant's reply, not just at
        # the end (Claude often follows a question with helpful examples).
        self._turn_all_text        = ""

        # Tool call state.
        # Maps tool_use_id -> {"bubble": Border, "status_tb": TextBlock}
        # so the "running..." indicator can be updated when the tool finishes.
        self._tool_bubbles = {}

        # Sidebar collapse state.
        self._sidebar_collapsed   = False
        self._sidebar_last_width  = 260.0

        # Attachments: items the user has pinned to the next message
        # (elements picked from Revit, sheets from a list, etc.). Cleared
        # automatically once the message is sent.
        self._attachments = []
        # Many-attachments overflow: when > _ATTACHMENT_VISIBLE_LIMIT
        # are pinned, the chip strip shows only the first N + a
        # "+M more" chip. _attachments_expanded toggles the strip to
        # show all chips + a "show less" chip. Reset on send.
        self._attachments_expanded = False

        # Static UI.
        self.Title = "Chatbot - " + self._project_label
        self.txt_project_name.Text = self._project_label
        self._refresh_model_label()
        self._refresh_key_banner()

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

        # Sidebar + title bar are fast (just summaries) - render them
        # synchronously so the window shows them on first paint.
        self._render_sidebar()
        self._refresh_chat_title()
        self._refresh_msg_count()

        # Chat pane render is the slow part for long histories - every
        # message becomes a markdown-rendered bubble, every tool call
        # becomes a pill. For a chat with 50+ messages, that's a few
        # seconds of UI-thread work. Defer it onto the dispatcher at
        # Background priority so the window appears (and is
        # interactive) immediately; the chat history fills in on the
        # next dispatcher pass.
        self.Dispatcher.BeginInvoke(
            System.Action(self._render_chat_pane),
            DispatcherPriority.Background)

        self.btn_send.Click                       += self._on_send
        self.btn_stop.Click                       += self._on_stop
        self.btn_settings.Click                   += self._on_open_settings
        self.btn_open_settings_from_banner.Click  += self._on_open_settings
        self.btn_new_chat.Click                   += self._on_new_chat
        self.btn_toggle_sidebar.Click             += self._on_toggle_sidebar
        self.btn_export.Click                     += self._on_export_click
        self.btn_add.Click                        += self._on_add_click

        # Search: live filter on text change. Clear button hides itself
        # when the search box is empty.
        self._search_query = ""
        self.txt_chat_search.TextChanged += self._on_search_changed
        self.btn_clear_search.Click      += self._on_clear_search

        # Drag-drop: accept file/folder drops anywhere on the chat pane.
        # PreviewDragOver fires before children get a shot at handling
        # the event - we want chat-wide accept regardless of where on
        # the pane the user releases.
        self.pnl_chat_pane.PreviewDragOver += self._on_drag_over
        self.pnl_chat_pane.PreviewDrop     += self._on_drop
        # Modern Popup-based menu items (Border children, not MenuItems).
        # Wire MouseLeftButtonDown for click; MouseEnter/Leave for hover state.
        for border, handler in (
            (self.popup_pick_element,   self._on_add_pick_element),
            (self.popup_use_selection,  self._on_add_current_selection),
            (self.popup_capture_view,   self._on_add_capture_view),
            (self.popup_pick_sheet,     self._on_add_sheet),
        ):
            border.MouseLeftButtonDown += (
                lambda s, a, h=handler: self._fire_popup_item(h, s, a))
            border.MouseEnter += (lambda s, a, b=border:
                                  setattr(b, "Background", _BRUSH_POPUP_HOVER))
            border.MouseLeave += (lambda s, a, b=border:
                                  setattr(b, "Background", _BRUSH_TRANSPARENT))
        self.txt_input.PreviewKeyDown             += self._on_input_keydown

        self.Loaded += self._on_loaded
        # Modeless lifecycle: tear down telemetry + cancel any in-flight
        # work when the user closes the window.
        self.Closed += self._on_closed

    def _on_loaded(self, sender, args):
        self.txt_input.Focus()
        self._scroll_to_bottom()

    def _on_closed(self, sender, args):
        """Modeless lifecycle: cancel anything in flight, tear down the
        ExternalEvent handler, end the telemetry session."""
        self._cancelled = True
        try:
            self._action_handler.shutdown()
        except Exception:
            pass
        # End telemetry. Status reflects whether the chat had a clean run.
        try:
            if self._telemetry_session is not None:
                dbhms_telemetry.end(self._telemetry_session, status="completed")
        except Exception:
            pass
        # Drop the module-level pin so the window can be GC'd. Also lets
        # the single-instance guard in main() see "no window open".
        try:
            global __window__
            if __window__ is self:
                __window__ = None
        except Exception:
            pass

    # -----------------------------------------------------------------
    # UI helpers
    # -----------------------------------------------------------------

    def _refresh_msg_count(self):
        n = len(self._conversations)
        self.txt_msg_count.Text = "{} chat{}".format(n, "" if n == 1 else "s")

    def _refresh_model_label(self):
        info = MODELS.get(self._config["model_key"], MODELS[DEFAULT_MODEL_KEY])
        self.txt_model_label.Text = info["label"] + "  *  " + str(len(TOOL_DEFS)) + " tools"

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
        self.txt_chat_title.Text = self._current_conv.get("title") or "New chat"
        msgs = self._current_conv.get("messages", [])
        if not msgs:
            self.txt_chat_subtitle.Text = "No messages yet."
            return
        n = len(msgs)
        parts = [
            "{} message{}".format(n, "" if n == 1 else "s"),
            _humanize_date(self._current_conv.get("updated_at")),
        ]
        # Per-chat running cost. Hide if zero (e.g. a fresh chat).
        cost, _, _ = conversation_cost(self._current_conv,
                                       self._config.get("model_key", DEFAULT_MODEL_KEY))
        if cost > 0:
            parts.append(format_cost(cost))
        self.txt_chat_subtitle.Text = "  *  ".join(parts)

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
            self.col_sidebar.MinWidth = 200
            self.col_sidebar.Width = GridLength(self._sidebar_last_width or 260.0)
            self.btn_toggle_sidebar.Content = u"❮"
            self.btn_toggle_sidebar.ToolTip = "Hide sidebar"
            self._sidebar_collapsed = False
        else:
            try:
                cur = float(self.col_sidebar.ActualWidth)
                if cur > 0:
                    self._sidebar_last_width = cur
            except Exception:
                pass
            self.col_sidebar.MinWidth = 0
            self.col_sidebar.Width = GridLength(0)
            self.btn_toggle_sidebar.Content = u"❯"
            self.btn_toggle_sidebar.ToolTip = "Show sidebar"
            self._sidebar_collapsed = True

    # -----------------------------------------------------------------
    # Drag-drop attachments (files / folders from Explorer only).
    #
    # Note: dragging sheets/views from Revit's Project Browser was
    # investigated and ruled out - the browser is rendered via
    # Chromium, and the drag arrives as opaque `DragContext` +
    # `chromium/x-renderer-taint` formats with no way to map back to
    # element identity from outside Revit's WebView host. Would
    # require a separate native Revit add-in. Use `+ -> Choose sheet`
    # / `+ -> Pick element` for that workflow instead.
    # -----------------------------------------------------------------

    def _on_drag_over(self, sender, args):
        try:
            if args.Data.GetDataPresent(DataFormats.FileDrop):
                args.Effects = DragDropEffects.Copy
            else:
                # Reject anything we can't actually consume so the user
                # gets a "no-drop" cursor instead of a misleading
                # "copy" cursor that does nothing on release.
                args.Effects = getattr(DragDropEffects, "None")
            args.Handled = True
        except Exception:
            pass

    def _on_drop(self, sender, args):
        try:
            if not args.Data.GetDataPresent(DataFormats.FileDrop):
                return
            paths = args.Data.GetData(DataFormats.FileDrop)
        except Exception:
            return
        if paths is None:
            return
        added = 0
        for path in paths:
            att = _attachment_from_path(path)
            if att is None:
                continue
            self._attachments.append(att)
            added += 1
        if added:
            self._render_attachments()
            self._set_status("Attached {} item{} from drag-drop.".format(
                added, "" if added == 1 else "s"))
        args.Handled = True

    # -----------------------------------------------------------------
    # Conversation export (Markdown / JSON)
    # -----------------------------------------------------------------

    def _on_export_click(self, sender, args):
        if not self._current_conv:
            return
        # Pick a default filename based on the chat's title + today.
        default_name = "{}_{}.md".format(
            _safe_filename(self._current_conv.get("title", "chat")),
            time.strftime("%Y-%m-%d"))
        # Use WPF's SaveFileDialog (Microsoft.Win32). Importing inline
        # so this module still parses under CPython 3 for the test suite.
        try:
            import clr as _clr
            _clr.AddReference("PresentationFramework")
            from Microsoft.Win32 import SaveFileDialog
        except Exception:
            dbhms_ui.info("Could not open the file picker.", title="Export")
            return

        dlg = SaveFileDialog()
        dlg.Filter = ("Markdown (*.md)|*.md|"
                      "JSON (*.json)|*.json")
        dlg.FileName = default_name
        dlg.InitialDirectory = _exports_dir()
        ok = dlg.ShowDialog()
        # ShowDialog returns Nullable<bool>. True -> user picked Save.
        if ok != True:
            return

        out_path = dlg.FileName
        ext = os.path.splitext(out_path)[1].lower()
        try:
            if ext == ".json":
                export_conversation_json(self._current_conv, out_path)
            else:
                export_conversation_markdown(self._current_conv, out_path)
        except Exception as e:
            dbhms_ui.info("Export failed: {}".format(e), title="Export")
            return

        self._set_status("Exported to {}".format(os.path.basename(out_path)))

    # -----------------------------------------------------------------
    # Attachments: + Add menu, chip rendering, picker handlers
    # -----------------------------------------------------------------

    def _on_add_click(self, sender, args):
        # Open the modern Popup. Toggling IsOpen flips visibility.
        if self.popup_add is not None:
            self.popup_add.IsOpen = not self.popup_add.IsOpen

    def _fire_popup_item(self, handler, sender, args):
        """Close the popup, then run the chosen menu item handler."""
        try:
            self.popup_add.IsOpen = False
        except Exception:
            pass
        try:
            handler(sender, args)
        except Exception as e:
            dbhms_ui.info("Action failed: {}".format(e), title="Chatbot")

    def _on_add_pick_element(self, sender, args):
        """Pick elements to attach. Shows a small floating "Done /
        Cancel" prompt window while Revit's selection is fully free.
        The user can click in the canvas, click sheets/views in the
        Project Browser, Ctrl-click for multi-select - whatever Revit
        normally allows. When they click "Attach selection" we read
        `uidoc.Selection.GetElementIds()` and attach everything.

        We deliberately do NOT use `PickObjects` here: it locks Revit's
        input to canvas-only, which would prevent clicking the Project
        Browser. That's the whole reason this prompt exists."""
        if self._doc is None:
            dbhms_ui.info("No active Revit document. Open a project, then try again.",
                          title="Chatbot")
            return
        if revit.uidoc is None:
            dbhms_ui.info("No active Revit UI document.", title="Chatbot")
            return
        if self._pick_in_progress:
            return  # already picking; ignore double-click

        self._set_pick_in_progress(True)
        doc = self._doc

        # Show the prompt window. It's modeless + topmost so the user
        # can still interact with Revit underneath it.
        prompt = PickPromptWindow()
        # Owner=None: don't tie it to the (minimized) chatbot. Otherwise
        # WPF tries to minimize the prompt along with its parent.

        def on_prompt_closed(sender, args):
            try:
                outcome = getattr(prompt, "_result", None) or "cancel"
            except Exception:
                outcome = "cancel"

            if outcome != "done":
                # User cancelled or closed the prompt.
                self._set_pick_in_progress(False)
                self._set_status("Pick cancelled.")
                return

            # User clicked "Attach selection" - read Revit's current
            # selection on the UI thread (browser + canvas combined).
            def read_selection():
                uidoc = revit.uidoc
                sel_ids = []
                try:
                    raw = uidoc.Selection.GetElementIds()
                    sel_ids = list(raw) if raw is not None else []
                except Exception:
                    sel_ids = []
                attachments = []
                for eid in sel_ids:
                    el = doc.GetElement(eid)
                    if el is None:
                        continue
                    attachments.append(_attachment_from_element(doc, el))
                return attachments

            def on_read_done(result, exception):
                self._set_pick_in_progress(False)
                if exception is not None:
                    self._set_status("Read selection failed: {}".format(exception))
                    return
                if not result:
                    self._set_status(
                        "Nothing was selected in Revit. Try again - click items in the canvas or Project Browser before clicking Attach.")
                    return
                self._attachments.extend(result)
                self._render_attachments()
                self._set_status("Attached {} item{}.".format(
                    len(result), "" if len(result) == 1 else "s"))

            self._action_handler.queue_action(
                read_selection, on_complete=on_read_done)

        prompt.Closed += on_prompt_closed
        prompt.Show()

    def _on_add_current_selection(self, sender, args):
        if self._doc is None:
            dbhms_ui.info("No active Revit document.", title="Chatbot")
            return
        if revit.uidoc is None:
            dbhms_ui.info("No active Revit UI document.", title="Chatbot")
            return

        doc = self._doc

        def get_selection():
            uidoc = revit.uidoc
            sel_ids = uidoc.Selection.GetElementIds()
            ids_list = list(sel_ids) if sel_ids is not None else []
            attachments = []
            for eid in ids_list:
                el = doc.GetElement(eid)
                if el is None:
                    continue
                attachments.append(_attachment_from_element(doc, el))
            return attachments

        def on_done(result, exception):
            if exception is not None:
                dbhms_ui.info("Could not read selection: {}".format(exception),
                              title="Chatbot")
                return
            if not result:
                dbhms_ui.info(
                    "Nothing is selected in Revit. Click some elements in the model "
                    "first, then try again.", title="Chatbot")
                return
            self._attachments.extend(result)
            self._render_attachments()
            self._set_status("Attached {} selected element{}.".format(
                len(result), "" if len(result) == 1 else "s"))

        self._action_handler.queue_action(get_selection, on_complete=on_done)

    def _on_add_sheet(self, sender, args):
        if self._doc is None:
            dbhms_ui.info("No active Revit document.", title="Chatbot")
            return

        doc = self._doc

        def collect_sheets():
            col = FilteredElementCollector(doc).OfClass(ViewSheet)
            out = []
            for s in col:
                out.append({
                    "id":     _eid_int(s.Id),
                    "number": s.SheetNumber or "",
                    "name":   s.Name or "",
                })
            out.sort(key=lambda x: (x["number"] or "", x["name"] or ""))
            return out

        def on_collected(result, exception):
            if exception is not None:
                dbhms_ui.info("Could not list sheets: {}".format(exception),
                              title="Chatbot")
                return
            if not result:
                dbhms_ui.info("This project has no sheets.", title="Chatbot")
                return
            self._show_sheet_picker(result)

        self._action_handler.queue_action(collect_sheets, on_complete=on_collected)

    def _show_sheet_picker(self, sheets_info):
        """forms.SelectFromList is pure WPF and doesn't need API context,
        so we run it directly on the UI thread after the collection
        finishes. (Splitting collection from the picker keeps API access
        confined to the ExternalEvent path.)"""
        labels = []
        info_map = {}
        for info in sheets_info:
            label = "{}  -  {}".format(
                info["number"] or "(no #)", info["name"] or "(no name)")
            if label in info_map:
                label = "{}  [id {}]".format(label, info["id"])
            labels.append(label)
            info_map[label] = info
        try:
            selected = forms.SelectFromList.show(
                labels,
                title="Choose sheet(s) to attach",
                multiselect=True,
                button_name="Attach")
        except Exception as e:
            dbhms_ui.info("Sheet picker failed: {}".format(e), title="Chatbot")
            return
        if not selected:
            return
        if not isinstance(selected, (list, tuple)):
            selected = [selected]
        added = 0
        for label in selected:
            info = info_map.get(label)
            if info is None:
                continue
            self._attachments.append({
                "kind":   "sheet",
                "id":     info["id"],
                "number": info["number"],
                "name":   info["name"],
            })
            added += 1
        self._render_attachments()
        if added:
            self._set_status("Attached {} sheet{}.".format(
                added, "" if added == 1 else "s"))

    def _on_add_capture_view(self, sender, args):
        """Snapshot the currently active Revit view to a PNG and attach
        it. Runs Revit's ExportImage on the API thread (via
        ExternalEvent), reads the resulting PNG from a temp file, and
        cleans up. The user sees the chip appear in the input area
        within a few seconds."""
        if self._doc is None:
            dbhms_ui.info("No active Revit document.", title="Chatbot")
            return
        if revit.uidoc is None:
            dbhms_ui.info("No active Revit UI document.", title="Chatbot")
            return

        doc = self._doc

        def export_view():
            uidoc = revit.uidoc
            if uidoc is None:
                return {"error": "No active UI document."}
            view = uidoc.ActiveView
            if view is None:
                return {"error": "No active view."}

            # Pick a stable filename based on view name + timestamp so
            # parallel captures don't clobber each other. ExportImage
            # appends a suffix like " - Floor Plan - Level 1.png", so we
            # pass the FilePath WITHOUT the extension and figure out the
            # actual produced filename after the export completes.
            tmp_dir = os.path.join(
                os.environ.get("TEMP") or os.path.expanduser("~"),
                "dbhms_chatbot_captures")
            try:
                if not os.path.isdir(tmp_dir):
                    os.makedirs(tmp_dir)
            except Exception as e:
                return {"error": "Could not create temp dir: {}".format(e)}
            stem = "view_{}_{}".format(int(time.time()), _short_id())
            file_path_no_ext = os.path.join(tmp_dir, stem)

            opts = ImageExportOptions()
            opts.FilePath = file_path_no_ext
            opts.HLRandWFViewsFileType = ImageFileType.PNG
            opts.ShadowViewsFileType   = ImageFileType.PNG
            opts.ImageResolution       = ImageResolution.DPI_150
            opts.ZoomType              = ZoomFitType.FitToPage
            opts.PixelSize             = 1600
            opts.ExportRange           = ExportRange.SetOfViews
            # SetOfViews takes a typed collection of ElementIds.
            view_id_list = NetList[ElementId]()
            view_id_list.Add(view.Id)
            try:
                opts.SetViewsAndSheets(view_id_list)
            except Exception as e:
                return {"error": "Could not configure export: {}".format(e)}

            try:
                doc.ExportImage(opts)
            except Exception as e:
                return {"error": "Revit ExportImage failed: {}".format(e)}

            # Locate the produced file. Revit decorates the base path with
            # a view-type suffix, so scan the temp dir for files starting
            # with our stem.
            produced = None
            try:
                for fn in os.listdir(tmp_dir):
                    if fn.startswith(stem) and fn.lower().endswith(".png"):
                        produced = os.path.join(tmp_dir, fn)
                        break
            except Exception:
                pass
            if produced is None or not os.path.isfile(produced):
                return {"error": "Export reported success but no PNG was found."}

            try:
                raw_bytes = NetFile.ReadAllBytes(produced)
            except Exception as e:
                return {"error": "Could not read exported PNG: {}".format(e)}

            # Build a friendlier attachment name: "<view name>.png".
            view_name = _safe_name(view) or "Active view"
            att_name  = "{}.png".format(view_name)

            # Best-effort cleanup of the temp file - the data is in
            # memory now, no reason to leave it on disk.
            try:
                os.remove(produced)
            except Exception:
                pass

            att = _attachment_from_image_bytes(
                raw_bytes, name=att_name, source="active_view",
                media_type="image/png")
            if att is None:
                return {"error": "Could not encode the captured PNG."}
            return {"attachment": att, "view_name": view_name}

        def on_done(result, exception):
            if exception is not None:
                self._set_status("Capture failed: {}".format(exception))
                return
            if isinstance(result, dict) and result.get("error"):
                self._set_status("Capture failed: {}".format(result["error"]))
                return
            att = result.get("attachment") if isinstance(result, dict) else None
            if att is None:
                self._set_status("Capture produced no image.")
                return
            self._attachments.append(att)
            self._render_attachments()
            self._set_status("Captured \"{}\" and attached.".format(
                result.get("view_name") or "view"))

        self._action_handler.queue_action(export_view, on_complete=on_done)

    def _set_pick_in_progress(self, picking):
        """Minimize the window during a pick and disable input. The
        window auto-restores to whatever WindowState it was in before
        (Normal or Maximized) when the pick completes or is cancelled."""
        self._pick_in_progress = bool(picking)
        if picking:
            # Snapshot the state so we can restore Maximized -> Maximized
            # rather than always coming back as Normal.
            try:
                self._state_before_pick = self.WindowState
            except Exception:
                self._state_before_pick = WindowState.Normal
            self._set_status(
                "Pick element(s) in Revit, then click Finish on the toolbar  (Esc to cancel)")
            self.btn_send.IsEnabled  = False
            self.btn_add.IsEnabled   = False
            self.txt_input.IsEnabled = False
            try:
                self.WindowState = WindowState.Minimized
            except Exception:
                pass
        else:
            try:
                self.WindowState = getattr(self, "_state_before_pick", None) or WindowState.Normal
            except Exception:
                pass
            # Bring the window back to the front and put the cursor in
            # the textbox so the user can immediately type their question.
            try:
                self.Activate()
            except Exception:
                pass
            self.btn_send.IsEnabled  = True
            self.btn_add.IsEnabled   = True
            self.txt_input.IsEnabled = True
            try:
                self.txt_input.Focus()
            except Exception:
                pass

    def _remove_attachment(self, idx):
        if 0 <= idx < len(self._attachments):
            del self._attachments[idx]
        # If removal drops us back under the limit, auto-collapse so we
        # don't keep a stale "show less" affordance hanging around.
        if len(self._attachments) <= _ATTACHMENT_VISIBLE_LIMIT:
            self._attachments_expanded = False
        self._render_attachments()

    def _render_attachments(self):
        """Rebuild the chip strip above the input box.

        When more than _ATTACHMENT_VISIBLE_LIMIT chips are pinned, the
        strip shows only the first N + a "+M more" chip. Clicking it
        sets _attachments_expanded=True; we then show all chips plus a
        "show less" chip to collapse again.
        """
        self.pnl_attachments.Children.Clear()
        n = len(self._attachments)
        if n == 0:
            self.pnl_attachments_wrap.Visibility = Visibility.Collapsed
            return
        self.pnl_attachments_wrap.Visibility = Visibility.Visible

        limit = _ATTACHMENT_VISIBLE_LIMIT
        if n <= limit or self._attachments_expanded:
            # Show all chips.
            for idx, att in enumerate(self._attachments):
                self.pnl_attachments.Children.Add(self._make_input_chip(att, idx))
            # If expanded AND over the limit, add a "show less" chip.
            if self._attachments_expanded and n > limit:
                self.pnl_attachments.Children.Add(
                    self._make_overflow_chip(label=u"− show less", expand=False))
        else:
            # Show first `limit` chips + a "+N more" chip.
            for idx in range(limit):
                self.pnl_attachments.Children.Add(
                    self._make_input_chip(self._attachments[idx], idx))
            self.pnl_attachments.Children.Add(
                self._make_overflow_chip(label="+{} more".format(n - limit), expand=True))

    def _make_overflow_chip(self, label, expand):
        """Pill chip that toggles _attachments_expanded.

        expand=True  -> clicking expands (shows everything)
        expand=False -> clicking collapses back to the first N
        """
        border = Border()
        border.Background      = _BRUSH_CHIP_BG
        border.BorderBrush     = _BRUSH_CHIP_BORDER
        border.BorderThickness = Thickness(1)
        border.CornerRadius    = System.Windows.CornerRadius(12)
        border.Padding         = Thickness(10, 3, 10, 3)
        border.Margin          = Thickness(0, 0, 6, 6)
        border.Cursor          = Cursors.Hand
        border.ToolTip         = ("Show all attachments"
                                  if expand else "Collapse to the first {}".format(
                                      _ATTACHMENT_VISIBLE_LIMIT))

        tb = TextBlock()
        tb.Text       = label
        tb.Foreground = _BRUSH_CHIP_TEXT
        tb.FontSize   = 11
        tb.FontWeight = FontWeights.SemiBold
        tb.VerticalAlignment = VerticalAlignment.Center
        border.Child = tb

        target = bool(expand)
        border.MouseLeftButtonDown += (
            lambda s, a, t=target: self._on_overflow_chip_click(t))
        return border

    def _on_overflow_chip_click(self, expand):
        self._attachments_expanded = bool(expand)
        self._render_attachments()

    def _make_input_chip(self, attachment, idx):
        """Pill-shaped Border for the input area; has a click-to-remove x."""
        border = Border()
        border.Background      = _BRUSH_CHIP_BG
        border.BorderBrush     = _BRUSH_CHIP_BORDER
        border.BorderThickness = Thickness(1)
        border.CornerRadius    = System.Windows.CornerRadius(12)
        border.Padding         = Thickness(8, 3, 4, 3)
        border.Margin          = Thickness(0, 0, 6, 6)

        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal

        # For image attachments, show a small thumbnail at the left of
        # the chip so the user can quickly tell which image is which
        # (especially useful when multiple screenshots are queued up).
        is_image = (attachment.get("kind") == "image")
        if is_image:
            thumb = self._make_attachment_thumbnail(attachment, side=24)
            if thumb is not None:
                sp.Children.Add(thumb)

        kind_tb = TextBlock()
        kind_tb.Text = _attachment_kind_label(attachment)
        kind_tb.Foreground = _BRUSH_CHIP_BORDER
        kind_tb.FontSize = 10
        kind_tb.FontWeight = FontWeights.SemiBold
        kind_tb.Margin = Thickness(0, 0, 6, 0)
        kind_tb.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Add(kind_tb)

        label_tb = TextBlock()
        label_tb.Text = _attachment_short_label(attachment)
        label_tb.Foreground = _BRUSH_CHIP_TEXT
        label_tb.FontSize = 11
        label_tb.FontWeight = FontWeights.SemiBold
        label_tb.Margin = Thickness(0, 0, 6, 0)
        label_tb.VerticalAlignment = VerticalAlignment.Center
        label_tb.MaxWidth = 240
        label_tb.TextTrimming = TextTrimming.CharacterEllipsis
        label_tb.ToolTip = _attachment_full_label(attachment)
        sp.Children.Add(label_tb)

        rm = Button()
        rm.Content = "x"
        rm.Width = 18
        rm.Height = 18
        rm.Padding = Thickness(0)
        rm.FontSize = 11
        rm.FontWeight = FontWeights.Bold
        rm.Background = _BRUSH_TRANSPARENT
        rm.BorderThickness = Thickness(0)
        rm.Foreground = _BRUSH_CHIP_X
        rm.Cursor = Cursors.Hand
        rm.ToolTip = "Remove attachment"
        captured_idx = idx
        rm.Click += (lambda s, a, i=captured_idx: self._remove_attachment(i))
        rm.MouseEnter += (lambda s, a, b=rm:
                          setattr(b, "Foreground", _BRUSH_CHIP_X_HOT))
        rm.MouseLeave += (lambda s, a, b=rm:
                          setattr(b, "Foreground", _BRUSH_CHIP_X))
        sp.Children.Add(rm)

        border.Child = sp
        return border

    def _make_user_bubble_chip(self, attachment):
        """Smaller pill, styled for the inside of a blue user bubble.
        No remove button - the message has already been sent.

        For image attachments, render as a larger thumbnail tile
        (image on top, filename underneath) rather than a text pill,
        so the user can visually identify the screenshot in the
        replayed conversation."""
        if attachment.get("kind") == "image":
            return self._make_user_bubble_image_tile(attachment)

        border = Border()
        border.Background      = _BRUSH_USER_CHIP_BG
        border.BorderBrush     = _BRUSH_USER_CHIP_BORDER
        border.BorderThickness = Thickness(1)
        border.CornerRadius    = System.Windows.CornerRadius(10)
        border.Padding         = Thickness(8, 2, 8, 2)
        border.Margin          = Thickness(0, 0, 4, 4)

        tb = TextBlock()
        tb.Text = u"{} {}".format(
            _attachment_kind_label(attachment),
            _attachment_short_label(attachment))
        tb.Foreground = _BRUSH_USER_FG
        tb.FontSize = 10
        tb.FontWeight = FontWeights.SemiBold
        tb.MaxWidth = 320
        tb.TextTrimming = TextTrimming.CharacterEllipsis
        tb.ToolTip = _attachment_full_label(attachment)
        border.Child = tb
        return border

    def _make_user_bubble_image_tile(self, attachment):
        """An image attachment shown as a small thumbnail tile inside
        the user bubble. ~120px wide, file label underneath."""
        border = Border()
        border.Background      = _BRUSH_USER_CHIP_BG
        border.BorderBrush     = _BRUSH_USER_CHIP_BORDER
        border.BorderThickness = Thickness(1)
        border.CornerRadius    = System.Windows.CornerRadius(6)
        border.Padding         = Thickness(4)
        border.Margin          = Thickness(0, 0, 4, 4)
        border.ToolTip         = _attachment_full_label(attachment)

        sp = StackPanel()

        thumb = self._make_attachment_thumbnail(attachment, side=120)
        if thumb is not None:
            sp.Children.Add(thumb)

        label_tb = TextBlock()
        label_tb.Text         = _attachment_short_label(attachment)
        label_tb.Foreground   = _BRUSH_USER_FG
        label_tb.FontSize     = 9
        label_tb.FontWeight   = FontWeights.SemiBold
        label_tb.MaxWidth     = 120
        label_tb.TextTrimming = TextTrimming.CharacterEllipsis
        label_tb.Margin       = Thickness(0, 3, 0, 0)
        sp.Children.Add(label_tb)

        border.Child = sp
        return border

    def _make_attachment_thumbnail(self, attachment, side=24):
        """Build a square Image control rendering the attachment's
        thumbnail. Returns None if decoding fails (caller should fall
        back to a text-only chip)."""
        b64 = attachment.get("data_b64")
        if not b64:
            return None
        # Decode at ~2x the display size for crisp rendering on hi-dpi
        # screens, capped to avoid loading a 12-MP screenshot for a 24px
        # chip thumbnail.
        decode_w = min(int(side * 2), 320)
        bmp = _bitmap_image_from_b64(b64, decode_pixel_width=decode_w)
        if bmp is None:
            return None
        img = WpfImage()
        img.Source = bmp
        img.Width  = side
        img.Height = side
        img.Stretch = System.Windows.Media.Stretch.UniformToFill
        img.Margin = Thickness(0, 0, 6, 0)
        img.VerticalAlignment   = VerticalAlignment.Center
        img.HorizontalAlignment = HorizontalAlignment.Center
        return img

    # -----------------------------------------------------------------
    # Sidebar render
    # -----------------------------------------------------------------

    def _render_sidebar(self):
        # When a search is active, route to the search-results renderer
        # instead of the standard date-grouped list.
        q = (getattr(self, "_search_query", "") or "").strip()
        if q:
            self._render_search_results(q)
            return

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

        last_bucket = None
        for summary in self._conversations:
            bucket = _bucket_for(summary.get("updated_at", ""))
            if bucket != last_bucket:
                self.pnl_conversations.Children.Add(
                    self._make_bucket_header(bucket))
                last_bucket = bucket
            self.pnl_conversations.Children.Add(
                self._make_conv_row(summary, summary["id"] == active_id))

    # -----------------------------------------------------------------
    # Cross-chat search
    # -----------------------------------------------------------------

    def _on_search_changed(self, sender, args):
        # Sync the hint TextBlock + clear button visibility, then re-render.
        try:
            raw = self.txt_chat_search.Text or ""
        except Exception:
            raw = ""
        self._search_query = raw
        has_text = bool(raw.strip())
        try:
            self.txt_search_hint.Visibility = (
                Visibility.Collapsed if has_text else Visibility.Visible)
        except Exception:
            pass
        try:
            self.btn_clear_search.Visibility = (
                Visibility.Visible if has_text else Visibility.Collapsed)
        except Exception:
            pass
        self._render_sidebar()

    def _on_clear_search(self, sender, args):
        try:
            self.txt_chat_search.Text = ""
        except Exception:
            pass
        # _on_search_changed will fire from the Text reset above.

    def _render_search_results(self, query):
        """Render a flat list of search hits across every saved
        conversation in the current project. Each hit row shows the
        chat title + a short snippet of the matched message. Click the
        row to load that chat.

        Loading every conversation from disk is fine: histories are
        scoped per Revit project, typically a few dozen files at most.
        """
        self.pnl_conversations.Children.Clear()

        q_lower = query.lower()

        # Title strip at the top so the user knows they're looking at
        # search results, not the normal list.
        head = TextBlock()
        head.Text       = u"Search results"
        head.FontSize   = 10
        head.FontWeight = FontWeights.SemiBold
        head.Foreground = _BRUSH_BUCKET_HEAD
        head.Margin     = Thickness(8, 8, 0, 4)
        self.pnl_conversations.Children.Add(head)

        hits = []  # list of (summary, msg_index, snippet, total_matches_in_chat)
        for summary in self._conversations:
            try:
                conv = load_conversation(self._project_key, summary["id"])
            except Exception:
                conv = None
            if conv is None:
                continue
            matches_in_chat = 0
            first_idx = -1
            first_snippet = ""
            # Title hits surface too - the user might search for a chat by name.
            title = summary.get("title", "") or ""
            if q_lower in title.lower():
                matches_in_chat += 1
                if first_idx < 0:
                    first_idx = -2  # sentinel meaning "title match"
                    first_snippet = self._make_snippet(title, q_lower)

            for i, m in enumerate(conv.get("messages", [])):
                text = self._plain_text_for_search(m)
                if not text:
                    continue
                if q_lower in text.lower():
                    matches_in_chat += 1
                    if first_idx < 0:
                        first_idx = i
                        first_snippet = self._make_snippet(text, q_lower)
            if matches_in_chat > 0:
                hits.append((summary, first_idx, first_snippet, matches_in_chat))

        if not hits:
            tb = TextBlock()
            tb.Text = u"No matches in this project."
            tb.Foreground = _BRUSH_BOT_LABEL
            tb.FontSize = 11
            tb.TextWrapping = TextWrapping.Wrap
            tb.TextAlignment = System.Windows.TextAlignment.Center
            tb.Margin = Thickness(8, 16, 8, 0)
            self.pnl_conversations.Children.Add(tb)
            return

        # Sort by total matches desc, then by updated_at desc so the
        # "best" match in the most recently used chat floats up.
        hits.sort(
            key=lambda h: (h[3], h[0].get("updated_at", "")),
            reverse=True)

        active_id = self._current_conv["id"] if self._current_conv else None
        for summary, first_idx, snippet, count in hits:
            self.pnl_conversations.Children.Add(
                self._make_search_result_row(
                    summary, snippet, count,
                    summary["id"] == active_id,
                    first_idx))

    def _plain_text_for_search(self, message):
        """Pull a single plain-text string from a stored message so it
        can be substring-searched. Handles both string content (user
        messages) and list-of-blocks content (assistant turns with
        tool_use / text blocks, or tool_result interludes). Tool inputs
        and results are joined in too so the user can find a chat by
        what Claude did, not just what was said."""
        parts = []
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text":
                    parts.append(blk.get("text", "") or "")
                elif btype == "tool_use":
                    parts.append(blk.get("name", "") or "")
                    inp = blk.get("input")
                    if isinstance(inp, dict):
                        for v in inp.values():
                            if isinstance(v, str):
                                parts.append(v)
                elif btype == "tool_result":
                    inner = blk.get("content")
                    if isinstance(inner, str):
                        parts.append(inner)
        # User-typed prompt + attachment labels (stored on synthesized
        # user messages) are searchable too.
        if message.get("_user_text"):
            parts.append(message["_user_text"])
        for att in message.get("_attachments", []) or []:
            if isinstance(att, dict):
                lbl = att.get("label") or att.get("name") or ""
                if lbl:
                    parts.append(lbl)
        return " \n ".join(p for p in parts if p)

    def _make_snippet(self, text, q_lower, span=60):
        """Return a short snippet of `text` centered on the first
        occurrence of `q_lower`, with ellipses on the trimmed sides."""
        if not text:
            return ""
        # Collapse internal whitespace so the snippet stays one line.
        flat = " ".join(text.split())
        idx = flat.lower().find(q_lower)
        if idx < 0:
            # Fallback (caller already confirmed a match, so this shouldn't
            # fire, but truncate to span just in case).
            if len(flat) > span * 2:
                return flat[:span * 2] + u"…"
            return flat
        start = max(0, idx - span)
        end   = min(len(flat), idx + len(q_lower) + span)
        snippet = flat[start:end]
        if start > 0:
            snippet = u"…" + snippet
        if end < len(flat):
            snippet = snippet + u"…"
        return snippet

    def _make_search_result_row(self, summary, snippet, count, is_active,
                                target_msg_idx):
        border = Border()
        border.Padding      = Thickness(10, 8, 8, 8)
        border.Margin       = Thickness(0, 0, 0, 4)
        border.CornerRadius = System.Windows.CornerRadius(4)
        border.Cursor       = Cursors.Hand

        if is_active:
            border.Background      = _BRUSH_ROW_ACTIVE
            border.BorderBrush     = _BRUSH_ROW_ACCENT
            border.BorderThickness = Thickness(2, 0, 0, 0)
        else:
            border.Background = _BRUSH_TRANSPARENT
            border.BorderBrush = _BRUSH_BOT_BORDER
            border.BorderThickness = Thickness(1)

        sp = StackPanel()

        # Title row: chat title + match count badge.
        head_row = StackPanel()
        head_row.Orientation = Orientation.Horizontal

        title_tb = TextBlock()
        title_tb.Text         = summary.get("title", "(untitled)")
        title_tb.FontSize     = 12
        title_tb.FontWeight   = FontWeights.SemiBold
        title_tb.Foreground   = _BRUSH_ROW_TITLE
        title_tb.TextTrimming = TextTrimming.CharacterEllipsis
        title_tb.MaxWidth     = 180
        title_tb.VerticalAlignment = VerticalAlignment.Center
        head_row.Children.Add(title_tb)

        count_tb = TextBlock()
        count_tb.Text       = u" • {} hit{}".format(
            count, "" if count == 1 else "s")
        count_tb.FontSize   = 10
        count_tb.Foreground = _BRUSH_BOT_LABEL
        count_tb.VerticalAlignment = VerticalAlignment.Center
        count_tb.Margin     = Thickness(4, 0, 0, 0)
        head_row.Children.Add(count_tb)

        sp.Children.Add(head_row)

        snippet_tb = TextBlock()
        snippet_tb.Text         = snippet
        snippet_tb.FontSize     = 11
        snippet_tb.Foreground   = _BRUSH_BOT_FG
        snippet_tb.TextWrapping = TextWrapping.Wrap
        snippet_tb.Margin       = Thickness(0, 3, 0, 0)
        sp.Children.Add(snippet_tb)

        border.Child = sp

        cid_for_click = summary["id"]
        border.MouseLeftButtonDown += (
            lambda s, a, c=cid_for_click, k=target_msg_idx:
                self._on_search_result_click(c, k))
        if not is_active:
            border.MouseEnter += (lambda s, a, b=border:
                                  setattr(b, "Background", _BRUSH_ROW_HOVER))
            border.MouseLeave += (lambda s, a, b=border:
                                  setattr(b, "Background", _BRUSH_TRANSPARENT))
        return border

    def _on_search_result_click(self, conv_id, target_msg_idx):
        # Switch chats AND scroll to the matched message. target_msg_idx
        # < 0 is a title-only match (no specific message to land on, so
        # just open the chat). Leave the search query in place so the
        # user can jump between hits without re-typing.
        scroll_target = target_msg_idx if target_msg_idx is not None and target_msg_idx >= 0 else None
        self._switch_to_conversation(conv_id, scroll_to_msg_idx=scroll_target)

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
        if _is_descendant_of_button(args.OriginalSource):
            return
        self._switch_to_conversation(conv_id)

    def _switch_to_conversation(self, conv_id, scroll_to_msg_idx=None):
        """Switch to `conv_id`. If `scroll_to_msg_idx` is given (used by
        search-result clicks), scroll to that message's bubble and
        briefly highlight it after the render completes."""
        already_active = (
            self._current_conv and self._current_conv["id"] == conv_id)
        if already_active:
            # Even if already on this chat, honour the scroll request -
            # the user clicked a search hit and expects to land at it.
            if scroll_to_msg_idx is not None:
                self._scroll_to_message_and_highlight(scroll_to_msg_idx)
            return
        self._prune_if_empty(self._current_conv)
        conv = load_conversation(self._project_key, conv_id)
        if conv is None:
            self._conversations = [c for c in self._conversations if c["id"] != conv_id]
            self._render_sidebar()
            self._refresh_msg_count()
            return
        self._current_conv = conv
        self._render_chat_pane()
        self._render_sidebar()
        self._refresh_chat_title()
        self._refresh_msg_count()
        if scroll_to_msg_idx is not None:
            self._scroll_to_message_and_highlight(scroll_to_msg_idx)

    def _scroll_to_message_and_highlight(self, msg_idx):
        """Scroll the chat pane to make the bubble for `msg_idx` visible
        and flash a brief amber outline so the user can spot it. Both
        actions are deferred to the dispatcher so they fire after WPF
        has measured/arranged the freshly-rendered bubbles."""
        try:
            bubble = self._msg_index_to_bubble.get(msg_idx)
        except AttributeError:
            bubble = None
        if bubble is None:
            return

        def _do():
            try:
                bubble.BringIntoView()
            except Exception:
                pass
            self._flash_match_highlight(bubble)

        # DispatcherPriority.Background runs after Layout/Render, so the
        # ScrollViewer has the correct extent by the time BringIntoView
        # is called.
        self.Dispatcher.BeginInvoke(
            System.Action(_do), DispatcherPriority.Background)

    def _flash_match_highlight(self, bubble):
        """Apply a 2-pixel amber outline to `bubble`, then restore the
        original brush + thickness after ~2.5 seconds. No-op (safe) if
        WPF rejects any of the operations."""
        try:
            from System.Windows.Threading import DispatcherTimer
            from System import TimeSpan
        except Exception:
            return
        try:
            old_brush     = bubble.BorderBrush
            old_thickness = bubble.BorderThickness
            bubble.BorderBrush     = _BRUSH_MATCH_HIGHLIGHT
            bubble.BorderThickness = Thickness(2)
        except Exception:
            return

        timer = DispatcherTimer()
        try:
            timer.Interval = TimeSpan.FromMilliseconds(2500)
        except Exception:
            return

        def _on_tick(s, a, t=timer, b=bubble, br=old_brush, th=old_thickness):
            try:
                b.BorderBrush     = br
                b.BorderThickness = th
            except Exception:
                pass
            try:
                t.Stop()
            except Exception:
                pass
        timer.Tick += _on_tick
        try:
            timer.Start()
        except Exception:
            pass

    def _prune_if_empty(self, conv):
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
        if self._current_conv and not self._current_conv.get("messages"):
            self._set_status("You're already in a new chat.")
            self.txt_input.Focus()
            return
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
        self._conversations = [c for c in self._conversations if c["id"] != conv_id]
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
                self._current_conv = _new_conversation(
                    self._project_key, self._project_label)
                save_conversation(self._current_conv)
                self._conversations.insert(0, _summarize_conv(self._current_conv))
            self._render_chat_pane()
        self._render_sidebar()
        self._refresh_chat_title()
        self._refresh_msg_count()

    # -----------------------------------------------------------------
    # Chat pane render (handles string + list-of-blocks content)
    # -----------------------------------------------------------------

    def _render_chat_pane(self):
        """Replay the active conversation into the chat pane.

        Groups consecutive assistant messages (plus the synthetic
        tool_result user messages between them) into ONE turn bubble,
        so each user-question -> assistant-reply pair feels continuous
        regardless of how many API rounds Claude needed for tool calls.
        """
        kept = [c for c in list(self.pnl_messages.Children)
                if c == self.bnr_empty_state]
        self.pnl_messages.Children.Clear()
        for c in kept:
            self.pnl_messages.Children.Add(c)
        self._tool_bubbles  = {}
        self._turn_bubble   = None
        self._turn_content  = None
        self._turn_label    = None
        self._turn_all_text = ""
        self._turn_openables = []
        self._turn_openable_ids_seen = set()
        # Tool-pill collapse state, tracked PER BATCH. A "batch" is a
        # run of consecutive tool calls with no intervening text - the
        # natural visual grouping (e.g. "look at 11 sheets" produces
        # one batch of 11 pills; "do A, then describe, then do B"
        # produces two batches of 1 pill each). Each batch keeps its
        # own row list + its own expander widget so collapse / expand
        # only affects that batch, not unrelated tool calls earlier or
        # later in the turn.
        self._turn_tool_batches      = []     # list of {"rows":[], "expander":None}
        self._turn_last_added_was_tool = False
        self._streaming_text_block = None
        self._streaming_buffer     = ""
        # message-index -> bubble Border, populated as we render. Search
        # result clicks use this to scroll to the exact message inside
        # the loaded chat (and briefly highlight it).
        self._msg_index_to_bubble = {}

        msgs = self._current_conv.get("messages", []) if self._current_conv else []
        if not msgs:
            self._show_empty_state()
            self._scroll_to_bottom()
            return
        self._hide_empty_state()

        # Pair every tool_use id to its tool_result content (across the
        # whole history) so we can paint the matching summary inline.
        results_by_id = {}
        for m in msgs:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for blk in content:
                if blk.get("type") == "tool_result":
                    results_by_id[blk.get("tool_use_id")] = blk.get("content", "")

        i = 0
        n = len(msgs)
        while i < n:
            m = msgs[i]
            role    = m.get("role", "user")
            content = m.get("content", "")

            # Real user message (string content): render right-aligned bubble.
            if role == "user" and isinstance(content, str):
                # Close any pending assistant turn so styling (incl.
                # question detection) finalizes before the next bubble.
                self._close_turn()
                if m.get("_attachments"):
                    bubble = self._add_user_bubble(
                        m.get("_user_text", ""),
                        attachments=m.get("_attachments"))
                else:
                    bubble = self._add_user_bubble(content)
                if bubble is not None:
                    self._msg_index_to_bubble[i] = bubble
                i += 1
                continue

            # Synthetic tool-result message (user role, list content):
            # contributes to tool indicators above; nothing to render here.
            if role == "user" and isinstance(content, list):
                i += 1
                continue

            # Assistant block(s) - merge consecutive assistant + intervening
            # synthetic tool_result messages into ONE turn bubble.
            if role == "assistant":
                j = i
                while j < n:
                    m2 = msgs[j]
                    r2 = m2.get("role")
                    c2 = m2.get("content")
                    if r2 == "assistant":
                        j += 1
                    elif r2 == "user" and isinstance(c2, list):
                        j += 1  # tool_result interlude, still part of this turn
                    else:
                        break

                # Render messages[i:j] into a single turn bubble.
                for k in range(i, j):
                    m3 = msgs[k]
                    if m3.get("role") != "assistant":
                        continue
                    c3 = m3.get("content", "")
                    if isinstance(c3, str):
                        if c3.strip():
                            self._add_text_to_turn(text=c3)
                    elif isinstance(c3, list):
                        for blk in c3:
                            btype = blk.get("type")
                            if btype == "text":
                                txt = blk.get("text", "")
                                if txt.strip():
                                    self._add_text_to_turn(text=txt)
                            elif btype == "tool_use":
                                tid = blk.get("id", "")
                                result_str = results_by_id.get(tid)
                                if result_str is not None:
                                    try:
                                        parsed = json.loads(result_str)
                                    except Exception:
                                        parsed = result_str
                                    summary  = summarize_tool_result(parsed)
                                    is_error = (isinstance(parsed, dict)
                                                and "error" in parsed)
                                else:
                                    summary  = "(no result)"
                                    is_error = False
                                row, _ = self._add_tool_to_turn(
                                    blk.get("name", "?"),
                                    status_text=summary,
                                    is_error=is_error)
                                # Accumulate "Open in Revit" buttons for
                                # any openable elements this tool created.
                                # They render in a row at the END of the
                                # turn (in _close_turn), not inline next
                                # to the pill, so they read as a real
                                # call-to-action after the reply.
                                if not is_error and isinstance(parsed, dict):
                                    openables = _openable_ids_from_result(
                                        blk.get("name", ""), parsed)
                                    self._collect_turn_openables(openables)

                # Snapshot the bubble BEFORE _close_turn clears
                # self._turn_bubble, then map every message index this
                # turn spans (assistant rounds + tool_result interludes)
                # to that bubble so search-hit clicks can jump to it.
                turn_bubble = self._turn_bubble
                if turn_bubble is not None:
                    for k in range(i, j):
                        self._msg_index_to_bubble[k] = turn_bubble

                # Close the turn now (applies question styling if the
                # final text ends in '?').
                self._close_turn()
                i = j
                continue

            # Any other message type - skip safely.
            i += 1

        self._scroll_to_bottom()

    # -----------------------------------------------------------------
    # Bubble construction
    # -----------------------------------------------------------------

    def _add_user_bubble(self, text, attachments=None):
        """User message: right-aligned blue bubble. Always closes any
        active assistant turn first so the user message visually breaks
        the conversation."""
        self._close_turn()
        self._hide_empty_state()

        bubble = Border()
        bubble.CornerRadius        = System.Windows.CornerRadius(8)
        bubble.Padding             = Thickness(12, 8, 12, 8)
        bubble.Margin              = Thickness(0, 0, 0, 10)
        bubble.MaxWidth            = 760
        bubble.Background          = _BRUSH_USER_BG
        bubble.HorizontalAlignment = HorizontalAlignment.Right

        content_panel = StackPanel()

        # Attachment chips at the top of the user bubble.
        if attachments:
            chip_wrap = WrapPanel()
            chip_wrap.Orientation = Orientation.Horizontal
            chip_wrap.Margin = Thickness(0, 0, 0, 4)
            for att in attachments:
                chip_wrap.Children.Add(self._make_user_bubble_chip(att))
            content_panel.Children.Add(chip_wrap)

        # Read-only TextBox so the user can highlight + Ctrl+C their
        # own messages with native WPF text selection (TextBlock's
        # IsTextSelectionEnabled is ScrollViewer-sensitive in some
        # hosts; TextBox is bulletproof).
        tb = _make_selectable_text(text or "",
                                   foreground=_BRUSH_USER_FG,
                                   font_size=13)
        content_panel.Children.Add(tb)

        bubble.Child = content_panel
        # Right-click "Copy message" support. Plain user text is final at
        # bubble-creation time, so we can capture it directly here.
        self._attach_copy_context_menu(bubble, text or "")
        self.pnl_messages.Children.Add(bubble)
        self._scroll_to_bottom()
        return bubble

    # -----------------------------------------------------------------
    # Assistant turn bubble: ONE bubble per logical reply (spans all the
    # text + tool_use blocks the agent emits between two user messages).
    # -----------------------------------------------------------------

    def _ensure_turn_bubble(self):
        """Return (bubble, content_panel) for the active turn, creating
        one if none is open. Subsequent text + tool additions go inside
        this single bubble for visual continuity."""
        if self._turn_bubble is not None and self._turn_content is not None:
            return self._turn_bubble, self._turn_content

        self._hide_empty_state()

        bubble = Border()
        bubble.CornerRadius        = System.Windows.CornerRadius(8)
        bubble.Padding             = Thickness(12, 8, 12, 8)
        bubble.Margin              = Thickness(0, 0, 0, 10)
        bubble.MaxWidth            = 760
        bubble.Background          = _BRUSH_BOT_BG
        bubble.BorderBrush         = _BRUSH_BOT_BORDER
        bubble.BorderThickness     = Thickness(1)
        bubble.HorizontalAlignment = HorizontalAlignment.Left

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

        content = StackPanel()
        inner.Children.Add(content)

        bubble.Child = inner
        self.pnl_messages.Children.Add(bubble)

        self._turn_bubble   = bubble
        self._turn_content  = content
        self._turn_label    = label
        self._turn_all_text = ""
        # Openables accumulate across the whole turn (every tool_use that
        # produces an element). Rendered as a row of larger "Open in Revit"
        # buttons at the bottom of the bubble in _close_turn.
        self._turn_openables = []
        self._turn_openable_ids_seen = set()
        # Tool-pill collapse state, tracked PER BATCH. A "batch" is a
        # run of consecutive tool calls with no intervening text - the
        # natural visual grouping (e.g. "look at 11 sheets" produces
        # one batch of 11 pills; "do A, then describe, then do B"
        # produces two batches of 1 pill each). Each batch keeps its
        # own row list + its own expander widget so collapse / expand
        # only affects that batch, not unrelated tool calls earlier or
        # later in the turn.
        self._turn_tool_batches      = []     # list of {"rows":[], "expander":None}
        self._turn_last_added_was_tool = False

        self._scroll_to_bottom()
        return bubble, content

    def _add_text_to_turn(self, text=""):
        """Add a text segment to the active turn bubble.

        - text="" (streaming): a plain TextBlock is added and returned so
          streaming deltas can mutate its .Text. Convert to markdown on
          turn close.
        - text="..." (non-streaming / replay): a markdown-rendered
          StackPanel is appended directly. Returns None.
        """
        _, content = self._ensure_turn_bubble()

        if text:
            md_panel = StackPanel()
            _render_markdown_into(md_panel, text)
            content.Children.Add(md_panel)
            # Accumulate for question detection (any '?' in the turn -> question).
            self._turn_all_text = (self._turn_all_text + "\n" + text) if self._turn_all_text else text
            # Text break: next tool pill starts a new batch.
            self._turn_last_added_was_tool = False
            self._scroll_to_bottom()
            return None

        # Read-only TextBox during streaming so the user can already
        # select partial text mid-stream (Ctrl+C). The whole thing gets
        # replaced with a markdown-rendered StackPanel in
        # _finalize_streaming_text_segment once the turn round ends.
        tb = _make_selectable_text("",
                                   foreground=_BRUSH_BOT_FG,
                                   font_size=13)
        content.Children.Add(tb)
        # New streaming text segment: next tool pill starts a new batch.
        self._turn_last_added_was_tool = False
        self._scroll_to_bottom()
        return tb

    def _add_tool_to_turn(self, tool_name, status_text=None, is_error=False):
        """Append a small inline tool 'pill' inside the active turn
        bubble. Light gray rounded background, light gray text. Designed
        to read as quiet breadcrumbs the eye skims past, not as content.

        Returns (action_row, status_tb). action_row is the horizontal
        StackPanel containing just the pill (kept as a separate panel
        so its layout stays predictable independent of any sibling
        content). "Open in Revit" buttons are no longer appended to
        this row - they accumulate across the turn and render in a
        WrapPanel at the bottom of the bubble in _close_turn."""
        _, content = self._ensure_turn_bubble()

        pill = Border()
        pill.Background          = _BRUSH_TOOL_PILL_BG
        pill.CornerRadius        = System.Windows.CornerRadius(10)
        pill.Padding             = Thickness(10, 3, 10, 3)
        pill.HorizontalAlignment = HorizontalAlignment.Left

        pill_inner = StackPanel()
        pill_inner.Orientation = Orientation.Horizontal

        wrench = TextBlock()
        wrench.Text       = u"🔧"
        wrench.FontSize   = 10
        wrench.Margin     = Thickness(0, 0, 6, 0)
        wrench.Opacity    = 0.65
        wrench.VerticalAlignment = VerticalAlignment.Center
        pill_inner.Children.Add(wrench)

        name_tb = TextBlock()
        name_tb.Text       = tool_name or "?"
        name_tb.Foreground = _BRUSH_TOOL_PILL_NAME
        name_tb.FontSize   = 10
        name_tb.FontWeight = FontWeights.SemiBold
        name_tb.Margin     = Thickness(0, 0, 6, 0)
        name_tb.VerticalAlignment = VerticalAlignment.Center
        pill_inner.Children.Add(name_tb)

        arrow = TextBlock()
        arrow.Text       = u"→"
        arrow.Foreground = _BRUSH_TOOL_PILL_TEXT
        arrow.FontSize   = 10
        arrow.Margin     = Thickness(0, 0, 6, 0)
        arrow.VerticalAlignment = VerticalAlignment.Center
        pill_inner.Children.Add(arrow)

        status_tb = TextBlock()
        if status_text:
            status_tb.Text       = status_text
            status_tb.FontStyle  = FontStyles.Normal
            status_tb.Foreground = _BRUSH_TOOL_ERROR if is_error else _BRUSH_TOOL_PILL_TEXT
        else:
            status_tb.Text       = "running..."
            status_tb.FontStyle  = FontStyles.Italic
            status_tb.Foreground = _BRUSH_TOOL_PILL_TEXT
        status_tb.FontSize   = 10
        status_tb.TextWrapping = TextWrapping.Wrap
        status_tb.VerticalAlignment = VerticalAlignment.Center
        pill_inner.Children.Add(status_tb)

        pill.Child = pill_inner

        # Outer action row holds the pill + any "Open in Revit" chips we
        # later append (for tool calls that created openable elements).
        action_row = StackPanel()
        action_row.Orientation        = Orientation.Horizontal
        action_row.Margin             = Thickness(0, 4, 0, 4)
        action_row.HorizontalAlignment = HorizontalAlignment.Left
        action_row.Children.Add(pill)

        content.Children.Add(action_row)

        # Per-batch tracking. If the LAST thing added to the turn was
        # also a tool pill, this one joins the current batch. Otherwise
        # (text segment intervened, or this is the first pill of the
        # turn) it starts a new batch.
        try:
            batches = self._turn_tool_batches
        except AttributeError:
            self._turn_tool_batches      = batches = []
            self._turn_last_added_was_tool = False
        if not batches or not self._turn_last_added_was_tool:
            batch = {"rows": [], "expander": None}
            batches.append(batch)
        else:
            batch = batches[-1]
        batch["rows"].append(action_row)
        self._turn_last_added_was_tool = True

        self._refresh_batch_visibility(batch)

        self._scroll_to_bottom()
        return action_row, status_tb

    # -----------------------------------------------------------------
    # Tool-pill collapse (keep long batches readable)
    # -----------------------------------------------------------------

    # Up to this many pills render inline; beyond that, the middle ones
    # fold behind an expander so the bubble doesn't become a wall of
    # identical pills.
    _TOOL_PILL_VISIBLE_CAP = 5
    _TOOL_PILL_HEAD_KEEP   = 1
    _TOOL_PILL_TAIL_KEEP   = 3  # head + tail = 4 inline pills + expander = 5

    def _refresh_batch_visibility(self, batch):
        """Apply collapse / expand state to ONE batch of tool pills.
        Hides middle pills + inserts an expander widget once that
        batch's count crosses the visible cap. Idempotent - safe to
        call after each new pill is added to the batch."""
        if self._turn_content is None or batch is None:
            return
        rows = batch.get("rows") or []
        cap  = self._TOOL_PILL_VISIBLE_CAP
        head = self._TOOL_PILL_HEAD_KEEP

        # First crossing into collapse territory: lazily create this
        # batch's expander and anchor it after the head pill of THIS
        # batch. The expander captures both the rows-list AND a
        # back-reference to the batch dict, so its click handler keeps
        # working after the turn ends.
        if len(rows) > cap and batch.get("expander") is None:
            batch["expander"] = self._make_tool_pill_expander(rows)
            self._insert_batch_expander_into_content(batch, head)

        # Always re-apply visibility based on the expander's captured
        # state (or just the row count when no expander exists yet).
        self._apply_expander_visibility(batch.get("expander"), rows)

    def _apply_expander_visibility(self, expander, rows):
        """Update row Visibility + expander text based on the
        expander's captured `expanded` flag. Works whether or not
        we're inside the active turn - `expander.Tag` holds the rows
        list reference and the expanded flag, so this is fully
        self-contained for post-turn clicks."""
        if rows is None:
            if expander is not None and expander.Tag is not None:
                rows = expander.Tag.get("rows") or []
            else:
                rows = []
        n = len(rows)
        cap  = self._TOOL_PILL_VISIBLE_CAP
        head = self._TOOL_PILL_HEAD_KEEP
        tail = self._TOOL_PILL_TAIL_KEEP

        expanded = False
        if expander is not None and expander.Tag is not None:
            expanded = bool(expander.Tag.get("expanded", False))

        # Determine which rows are visible.
        if n <= cap or expanded:
            for r in rows:
                try:
                    r.Visibility = Visibility.Visible
                except Exception:
                    pass
        else:
            for i, r in enumerate(rows):
                visible = (i < head) or (i >= n - tail)
                try:
                    r.Visibility = (Visibility.Visible if visible
                                    else Visibility.Collapsed)
                except Exception:
                    pass

        # Expander visibility + text.
        if expander is None:
            return
        if n <= cap:
            # No need for the expander at all.
            try:
                expander.Visibility = Visibility.Collapsed
            except Exception:
                pass
            return
        # Expander needed.
        try:
            expander.Visibility = Visibility.Visible
        except Exception:
            pass
        hidden_count = max(0, n - head - tail)
        self._update_tool_pill_expander_text(expander, hidden_count, expanded)
        # Reposition: when COLLAPSED the expander sits between the head
        # and the tail (anchoring the hidden middle). When EXPANDED it
        # moves to AFTER the last pill so it reads as a clear "click
        # to re-collapse" affordance at the bottom of the batch -
        # leaving it in the middle looks like there's still something
        # hidden between the pills.
        self._reposition_expander_in_content(expander, rows, expanded)

    def _reposition_expander_in_content(self, expander, rows, expanded):
        """Move the expander to the correct slot in _turn_content for
        the current state. No-op if anything required is missing
        (e.g. the expander was created and then the turn ended, but
        the user just clicked it - we still want to update position
        if _turn_content reference can be recovered from the rows'
        parent)."""
        # Try the active turn first, then fall back to the parent of
        # the rows (works for clicks after _close_turn).
        content = self._turn_content
        if content is None and rows:
            try:
                content = rows[0].Parent
            except Exception:
                content = None
        if content is None or expander is None:
            return
        try:
            children = content.Children
        except Exception:
            return

        # Remove expander from its current position (if present).
        cur_idx = -1
        for i, c in enumerate(children):
            if c == expander:
                cur_idx = i
                break
        if cur_idx >= 0:
            try:
                children.RemoveAt(cur_idx)
            except Exception:
                return

        # Find target anchor + insert just AFTER it.
        head = self._TOOL_PILL_HEAD_KEEP
        if expanded:
            anchor = rows[-1] if rows else None
        else:
            anchor = rows[min(head - 1, len(rows) - 1)] if rows else None
        if anchor is None:
            try:
                children.Add(expander)
            except Exception:
                pass
            return
        target_idx = -1
        for i, c in enumerate(children):
            if c == anchor:
                target_idx = i
                break
        if target_idx < 0:
            try:
                children.Add(expander)
            except Exception:
                pass
        else:
            try:
                children.Insert(target_idx + 1, expander)
            except Exception:
                pass

    def _insert_batch_expander_into_content(self, batch, head):
        """Place this batch's expander widget in _turn_content right
        AFTER the last head-row of THIS batch (so the first head
        pills come before it and the middle/tail come after)."""
        expander = batch.get("expander")
        rows     = batch.get("rows") or []
        if expander is None or self._turn_content is None:
            return
        if head <= 0 or not rows:
            self._turn_content.Children.Add(expander)
            return
        anchor = rows[min(head - 1, len(rows) - 1)]
        idx = -1
        for i, child in enumerate(self._turn_content.Children):
            if child == anchor:
                idx = i
                break
        if idx < 0:
            self._turn_content.Children.Add(expander)
        else:
            self._turn_content.Children.Insert(idx + 1, expander)

    def _make_tool_pill_expander(self, rows_list):
        """Tiny clickable row that says '+ N more tool calls (click to
        show)' / '- show fewer tool calls'. Same quiet styling as the
        tool pill itself so it reads as a breadcrumb.

        `rows_list` is a reference to the per-turn action_row list -
        the expander captures it so clicks work indefinitely (even
        after _close_turn clears the `self._turn_tool_*` attributes).
        """
        border = Border()
        border.Background          = _BRUSH_TOOL_PILL_BG
        border.CornerRadius        = System.Windows.CornerRadius(10)
        border.Padding             = Thickness(10, 3, 10, 3)
        border.Margin              = Thickness(0, 4, 0, 4)
        border.HorizontalAlignment = HorizontalAlignment.Left
        border.Cursor              = Cursors.Hand
        border.ToolTip             = "Click to show / hide hidden tool calls"

        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal

        glyph = TextBlock()
        glyph.Text       = u"…"
        glyph.Foreground = _BRUSH_TOOL_PILL_NAME
        glyph.FontSize   = 11
        glyph.FontWeight = FontWeights.SemiBold
        glyph.Margin     = Thickness(0, 0, 6, 0)
        glyph.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Add(glyph)

        label = TextBlock()
        label.Text       = ""
        label.Foreground = _BRUSH_TOOL_PILL_TEXT
        label.FontSize   = 10
        label.FontWeight = FontWeights.SemiBold
        label.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Add(label)

        border.Child = sp
        # All per-expander state lives in Tag (a dict) so the click
        # handler doesn't depend on the per-turn `self.*` attributes
        # being intact.
        border.Tag = {
            "rows":     rows_list,
            "expanded": False,
            "label_tb": label,
        }
        border.MouseLeftButtonDown += self._on_tool_pill_expander_click
        return border

    def _update_tool_pill_expander_text(self, expander, hidden_count, expanded):
        """Set the label inside the expander based on state."""
        if expander is None or expander.Tag is None:
            return
        label = expander.Tag.get("label_tb")
        if label is None:
            return
        if expanded:
            try:
                label.Text = u"− show fewer tool calls"
            except Exception:
                pass
        else:
            try:
                label.Text = u"+ {} more tool call{} (click to show)".format(
                    hidden_count, "" if hidden_count == 1 else "s")
            except Exception:
                pass

    def _on_tool_pill_expander_click(self, sender, args):
        """Toggle expanded state on the expander itself (NOT on
        `self`), then re-apply visibility. Works after the turn has
        ended because Tag holds the captured rows list."""
        expander = sender
        if expander is None or expander.Tag is None:
            return
        state = expander.Tag
        state["expanded"] = not bool(state.get("expanded", False))
        rows = state.get("rows") or []
        self._apply_expander_visibility(expander, rows)

    def _make_open_in_revit_chip(self, openable):
        """Larger squared-off button: "↗ Open <label> in Revit". Rendered
        in a row at the END of an assistant turn (not inline next to the
        tool pill) so it reads as a clear call-to-action after Claude
        finishes its reply.

        Click opens the element in Revit (sets it as the active view)
        and closes the chatbot.

        `openable` is a dict with id + label (+ optional kind), as
        produced by _openable_ids_from_result()."""
        border = Border()
        border.Background      = _BRUSH_OPEN_CHIP_BG
        border.BorderBrush     = _BRUSH_OPEN_CHIP_BORDER
        border.BorderThickness = Thickness(1)
        # Squared-off corners so it reads as a button, not a pill.
        border.CornerRadius    = System.Windows.CornerRadius(3)
        # Bigger hit target. Padded like the firm's PrimaryButton (14,6)
        # plus a touch more vertical so it dominates the breadcrumb pills.
        border.Padding         = Thickness(14, 7, 14, 7)
        # WrapPanel handles inter-button spacing on both axes.
        border.Margin          = Thickness(0, 0, 8, 6)
        border.Cursor          = Cursors.Hand
        border.ToolTip         = "Open this element as the active view in Revit (closes the chatbot)"

        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal

        arrow = TextBlock()
        arrow.Text       = u"↗"
        arrow.Foreground = _BRUSH_OPEN_CHIP_TEXT
        arrow.FontSize   = 13
        arrow.FontWeight = FontWeights.Bold
        arrow.Margin     = Thickness(0, 0, 6, 0)
        arrow.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Add(arrow)

        prefix = TextBlock()
        prefix.Text         = "Open "
        prefix.Foreground   = _BRUSH_OPEN_CHIP_TEXT
        prefix.FontSize     = 12
        prefix.FontWeight   = FontWeights.SemiBold
        prefix.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Add(prefix)

        label = TextBlock()
        label.Text         = openable.get("label") or "element"
        label.Foreground   = _BRUSH_OPEN_CHIP_TEXT
        label.FontSize     = 12
        label.FontWeight   = FontWeights.SemiBold
        label.MaxWidth     = 280
        label.TextTrimming = TextTrimming.CharacterEllipsis
        label.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Add(label)

        suffix = TextBlock()
        suffix.Text         = " in Revit"
        suffix.Foreground   = _BRUSH_OPEN_CHIP_TEXT
        suffix.FontSize     = 12
        suffix.FontWeight   = FontWeights.SemiBold
        suffix.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Add(suffix)

        border.Child = sp
        captured_id = openable.get("id")
        border.MouseLeftButtonDown += (
            lambda s, a, i=captured_id: self._on_open_in_revit_click(i))
        border.MouseEnter += (lambda s, a, b=border:
                              setattr(b, "Background", _BRUSH_OPEN_CHIP_HOVER_BG))
        border.MouseLeave += (lambda s, a, b=border:
                              setattr(b, "Background", _BRUSH_OPEN_CHIP_BG))
        return border

    def _on_open_in_revit_click(self, element_id):
        """Make the element the active view in Revit, then close the
        chatbot window. Runs through the action handler so the API
        call lands on Revit's UI thread."""
        if element_id is None:
            return
        if self._doc is None:
            self._set_status("No active Revit document.")
            return
        doc = self._doc
        try:
            eid_int_val = int(element_id)
        except (TypeError, ValueError):
            return

        def open_action():
            el = doc.GetElement(_make_eid(eid_int_val))
            if el is None:
                return {"error": "Element {} not found in the active document.".format(eid_int_val)}
            if not isinstance(el, View):
                return {"error": "Element {} is not a view (it's a {}); can't open as an active view.".format(
                    eid_int_val, type(el).__name__)}
            uidoc = revit.uidoc
            if uidoc is None:
                return {"error": "No active UI document."}
            try:
                uidoc.ActiveView = el
            except Exception as e:
                return {"error": "Could not activate view '{}': {}".format(_safe_name(el), e)}
            return {"opened_id": eid_int_val, "opened_name": _safe_name(el)}

        def on_open_done(result, exception):
            # Surface errors in the status bar; only close on success.
            if exception is not None:
                self._set_status("Couldn't open: {}".format(exception))
                return
            if isinstance(result, dict) and result.get("error"):
                self._set_status("Couldn't open: {}".format(result["error"]))
                return
            # Success - close the chatbot so the user can work with the
            # element they just jumped to.
            try:
                self.Close()
            except Exception:
                pass

        self._action_handler.queue_action(open_action, on_complete=on_open_done)

    def _finalize_streaming_text_segment(self):
        """Promote the active streaming TextBlock (if any) to a
        markdown-rendered StackPanel in place. Called when a tool call
        starts mid-turn AND when the turn itself ends."""
        if self._streaming_text_block is None:
            self._streaming_buffer = ""
            return
        text = self._streaming_buffer or ""
        if text.strip() and self._turn_content is not None:
            idx = -1
            for i, child in enumerate(self._turn_content.Children):
                if child == self._streaming_text_block:
                    idx = i
                    break
            if idx >= 0:
                self._turn_content.Children.RemoveAt(idx)
                md_panel = StackPanel()
                _render_markdown_into(md_panel, text)
                self._turn_content.Children.Insert(idx, md_panel)
            # Commit this segment to the all-text accumulator for the
            # turn's question detection.
            self._turn_all_text = (self._turn_all_text + "\n" + text) if self._turn_all_text else text
        self._streaming_text_block = None
        self._streaming_buffer = ""

    def _close_turn(self):
        """End the active assistant turn. Finalize any in-flight
        streaming text segment, render the "Open in Revit" buttons row
        for any openables this turn produced, then apply 'question'
        styling if the reply contains a question mark anywhere."""
        self._finalize_streaming_text_segment()
        if self._turn_bubble is not None:
            self._render_turn_open_buttons()
            self._attach_copy_menu_to_assistant_turn()
            self._apply_question_styling_if_needed()
        self._turn_bubble   = None
        self._turn_content  = None
        self._turn_label    = None
        self._turn_all_text = ""
        self._turn_openables = []
        self._turn_openable_ids_seen = set()
        # Tool-pill collapse state, tracked PER BATCH. A "batch" is a
        # run of consecutive tool calls with no intervening text - the
        # natural visual grouping (e.g. "look at 11 sheets" produces
        # one batch of 11 pills; "do A, then describe, then do B"
        # produces two batches of 1 pill each). Each batch keeps its
        # own row list + its own expander widget so collapse / expand
        # only affects that batch, not unrelated tool calls earlier or
        # later in the turn.
        self._turn_tool_batches      = []     # list of {"rows":[], "expander":None}
        self._turn_last_added_was_tool = False

    def _render_turn_open_buttons(self):
        """Render a row of large 'Open <X> in Revit' buttons at the
        bottom of the active turn bubble, one per openable collected
        during the turn. No-op if nothing was created."""
        try:
            openables = self._turn_openables
        except AttributeError:
            return
        if not openables or self._turn_content is None:
            return

        # Sub-label so the user knows what the buttons do at a glance.
        caption = TextBlock()
        caption.Text       = "Created in Revit:"
        caption.Foreground = _BRUSH_BOT_LABEL
        caption.FontSize   = 10
        caption.FontWeight = FontWeights.SemiBold
        caption.Margin     = Thickness(0, 10, 0, 4)
        self._turn_content.Children.Add(caption)

        # WrapPanel so multiple buttons flow onto a second line when needed.
        wrap = WrapPanel()
        wrap.Orientation        = Orientation.Horizontal
        wrap.HorizontalAlignment = HorizontalAlignment.Left
        wrap.Margin             = Thickness(0, 0, 0, 0)
        for o in openables:
            try:
                wrap.Children.Add(self._make_open_in_revit_chip(o))
            except Exception:
                # A bad button shouldn't break the rest of the UI.
                pass
        self._turn_content.Children.Add(wrap)

    # -----------------------------------------------------------------
    # Clipboard / right-click "Copy message" support
    # -----------------------------------------------------------------

    def _copy_to_clipboard(self, text):
        """Push text to the Windows clipboard. Wrapped because the
        clipboard API occasionally throws (concurrent access from
        another app); failures degrade silently to a status hint."""
        try:
            if text is None:
                text = ""
            System.Windows.Clipboard.SetText(text)
            self._set_status("Copied to clipboard.")
        except Exception as e:
            self._set_status("Couldn't copy: {}".format(e))

    def _attach_copy_context_menu(self, element, text_getter):
        """Wire a right-click ContextMenu with "Copy message" onto the
        given UI element. `text_getter` is either a string OR a no-arg
        callable returning the text to copy (use a callable when the
        text isn't known until copy time, e.g. an assistant turn that
        is still being typed when the menu is attached)."""
        try:
            cm = ContextMenu()
            cm.HasDropShadow = True

            mi_copy = MenuItem()
            mi_copy.Header = "Copy message"
            def _on_copy(s, a, getter=text_getter):
                try:
                    txt = getter() if callable(getter) else getter
                except Exception:
                    txt = ""
                self._copy_to_clipboard(txt or "")
            mi_copy.Click += _on_copy
            cm.Items.Add(mi_copy)

            element.ContextMenu = cm
        except Exception:
            # ContextMenu wiring should never break bubble rendering.
            pass

    def _attach_copy_menu_to_assistant_turn(self):
        """Snapshot the current turn's accumulated plain text and wire
        a right-click "Copy message" menu to the turn bubble. Called
        from _close_turn so the snapshot reflects the FINAL text after
        all streaming/markdown finalization is done."""
        if self._turn_bubble is None:
            return
        text_snapshot = (self._turn_all_text or "").strip()
        self._attach_copy_context_menu(self._turn_bubble, text_snapshot)

    def _apply_question_styling_if_needed(self):
        """Recolor the bubble as a Question if any '?' appears in the
        turn's combined text AFTER trailing closing pleasantries
        ("anything else?", "let me know if...", etc.) are stripped.

        Visual contract:
          - light blue background
          - violet left-border (4px) accent
          - label text 'Question' in violet
        """
        scanned = _strip_trailing_pleasantries(self._turn_all_text or "")
        if "?" not in scanned:
            return
        try:
            self._turn_bubble.Background      = _BRUSH_QUESTION_BG
            self._turn_bubble.BorderBrush     = _BRUSH_QUESTION_BORDER
            self._turn_bubble.BorderThickness = Thickness(4, 1, 1, 1)
            if self._turn_label is not None:
                self._turn_label.Text       = "Question"
                self._turn_label.Foreground = _BRUSH_QUESTION_LABEL
        except Exception:
            pass

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
        head = _make_selectable_text(label,
                                     foreground=_BRUSH_ERROR_FG,
                                     font_size=12,
                                     font_weight=FontWeights.SemiBold)
        sp.Children.Add(head)

        body = _make_selectable_text(detail,
                                     foreground=_BRUSH_ERROR_FG,
                                     font_size=11)
        body.Margin = Thickness(0, 4, 0, 0)
        sp.Children.Add(body)

        bubble.Child = sp
        # Copy the full error text on right-click (label + detail) so the
        # user can paste it into a bug report or a follow-up question.
        self._attach_copy_context_menu(
            bubble,
            (label or "") + (": " + detail if detail else ""))
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
            return

        # Defensive paste shortcut. WPF's TextBox handles Ctrl+V natively
        # via ApplicationCommands.Paste, but some pyRevit hosts have been
        # observed to swallow command routing. Handling it explicitly
        # here guarantees Ctrl+V works for both text AND image clipboard
        # contents (image becomes a vision attachment, text gets pasted
        # into the input).
        if (args.Key == Key.V
                and (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control
                and (Keyboard.Modifiers & ModifierKeys.Alt) != ModifierKeys.Alt):
            # Image first - if the user copied a screenshot from
            # Snipping Tool / browser / etc., attaching it is almost
            # certainly what they want.
            if self._try_paste_clipboard_image():
                args.Handled = True
                return
            self._paste_clipboard_into_input(sender)
            args.Handled = True
            return

    def _try_paste_clipboard_image(self):
        """If the system clipboard holds an image (bitmap from Snipping
        Tool, a browser copy, etc.), encode it to PNG and add it as a
        vision attachment. Returns True if an image was attached,
        False if there was no image (so the caller can fall through to
        text paste)."""
        try:
            if not System.Windows.Clipboard.ContainsImage():
                return False
            bmp = System.Windows.Clipboard.GetImage()
        except Exception:
            return False
        if bmp is None:
            return False
        png_bytes = _encode_bitmap_source_to_png_bytes(bmp)
        if png_bytes is None:
            self._set_status("Couldn't encode the clipboard image.")
            return False
        att = _attachment_from_image_bytes(
            png_bytes,
            name="clipboard_{}.png".format(time.strftime("%H%M%S")),
            source="clipboard",
            media_type="image/png")
        if att is None:
            self._set_status("Couldn't build attachment from clipboard image.")
            return False
        self._attachments.append(att)
        self._render_attachments()
        self._set_status("Attached image from clipboard.")
        return True

    def _paste_clipboard_into_input(self, textbox):
        """Insert clipboard text at the caret (replacing any current
        selection), then move the caret to the end of what was pasted.
        Silent no-op if the clipboard doesn't contain text (e.g., an
        image or a file path list from Explorer)."""
        try:
            if not System.Windows.Clipboard.ContainsText():
                return
            clip = System.Windows.Clipboard.GetText() or ""
        except Exception:
            return
        if not clip:
            return
        try:
            sel_start  = textbox.SelectionStart
            sel_length = textbox.SelectionLength
            existing   = textbox.Text or ""
            before = existing[:sel_start]
            after  = existing[sel_start + sel_length:]
            textbox.Text = before + clip + after
            textbox.CaretIndex = sel_start + len(clip)
        except Exception:
            # Fallback: just append at end.
            try:
                textbox.AppendText(clip)
                textbox.CaretIndex = len(textbox.Text or "")
            except Exception:
                pass

    def _on_send(self, sender, args):
        self._do_send()

    def _do_send(self):
        if not self._config.get("api_key"):
            dbhms_ui.info(
                "No API key configured. Click Settings to paste one in.",
                title="Chatbot")
            return

        user_text = (self.txt_input.Text or "").strip()
        if not user_text and not self._attachments:
            return

        # Snapshot attachments WITHOUT mutating self._attachments yet -
        # the soft-cap dialog may abort this send, in which case the
        # user expects their chips to still be sitting in the input.
        msg_attachments_snapshot = list(self._attachments)

        # Build the API-facing message content. If text attachments
        # (elements/sheets/files) are present, prepend a context preamble.
        # Image attachments are NOT preamble'd here - they become their
        # own image content blocks in _build_api_messages at send time.
        # The user_text stays separately so the rendered bubble shows
        # just what the user typed.
        if msg_attachments_snapshot:
            preamble = _build_attachment_preamble(msg_attachments_snapshot)
            has_images = any(a.get("kind") == "image"
                             for a in msg_attachments_snapshot)
            if preamble:
                api_content = preamble + "\n\n" + (
                    user_text or "(no question text - just providing the context above)")
            elif has_images:
                # Image-only message - no preamble needed; placeholder
                # caption gets supplied at API-build time if user_text
                # is empty.
                api_content = user_text or ""
            else:
                api_content = user_text
        else:
            api_content = user_text

        # Soft-cap pre-send check. Estimate cost of THIS request and
        # ask the user to confirm if it exceeds the threshold. Threshold
        # of 0 disables the check entirely.
        threshold = float(self._config.get("spend_threshold", DEFAULT_SPEND_THRESHOLD))
        if threshold > 0:
            preview_msgs = _build_api_messages(
                self._current_conv["messages"]
                + [{"role": "user", "content": api_content}])
            system_prompt_text = self._config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
            model_key = self._config.get("model_key", DEFAULT_MODEL_KEY)
            est = estimate_request_cost(
                preview_msgs, system_prompt_text, TOOL_DEFS,
                MAX_TOKENS, model_key)
            if est > threshold:
                msg = (
                    "This request could cost up to {est} (estimated, including "
                    "the full {max_out}-token output budget).\n\n"
                    "Soft cap is set to {cap} in Settings.\n\n"
                    "Send anyway?"
                ).format(est=format_cost(est),
                         max_out=MAX_TOKENS,
                         cap=format_cost(threshold))
                proceed = forms.alert(msg, title="Cost check",
                                      yes=True, no=True)
                if not proceed:
                    return  # user cancelled - chips and input text preserved

        # User confirmed (or no soft-cap). Now commit the snapshot and
        # clear the input pane.
        msg_attachments = msg_attachments_snapshot
        self._attachments = []
        self._attachments_expanded = False
        self._render_attachments()

        self.txt_input.Text = ""
        self._tool_bubbles = {}

        # Persist - keep `_user_text` and `_attachments` for nice replay,
        # while the API-facing `content` includes the preamble.
        msg_record = {
            "role":    "user",
            "content": api_content,
            "ts":      _now_iso(),
        }
        if msg_attachments:
            msg_record["_user_text"]   = user_text
            msg_record["_attachments"] = msg_attachments
        self._current_conv["messages"].append(msg_record)

        # Display the bubble: show chips + the user's actual text only.
        # _add_user_bubble closes any open assistant turn so the user
        # message visually breaks the conversation. The next streaming
        # delta will open a fresh turn bubble.
        self._add_user_bubble(user_text or "", attachments=msg_attachments)

        was_first = sum(1 for m in self._current_conv["messages"]
                        if m.get("role") == "user"
                        and isinstance(m.get("content"), str)) == 1
        if was_first:
            self._current_conv["title"] = _derive_title(self._current_conv["messages"])
            for c in self._conversations:
                if c["id"] == self._current_conv["id"]:
                    c["title"] = self._current_conv["title"]
                    break
            self._refresh_chat_title()
            self._render_sidebar()

        self._save_current_conv_safe()

        # Open the streaming text bubble for the assistant's first text block
        # of this turn. Subsequent text blocks (e.g. after a tool call) get
        # their own bubbles via _on_content_block_start.
        self._streaming_text_block = None
        self._streaming_buffer     = ""

        self._cancelled = False
        self._set_streaming_ui(True)
        self._set_status("Asking " + MODELS[self._config["model_key"]]["label"] + "...")

        api_key       = self._config["api_key"]
        model_id      = MODELS[self._config["model_key"]]["id"]
        system_prompt = self._config["system_prompt"] or DEFAULT_SYSTEM_PROMPT

        def worker():
            # Defense in depth: an unhandled exception on a background
            # thread crashes silently AND IronPython prints the traceback
            # to stderr, which pops pyRevit's output window. Catch and
            # route through the normal error-bubble UI instead.
            try:
                self._run_agent_loop(api_key, model_id, system_prompt)
            except Exception as e:
                try:
                    self._dispatch_error(
                        "internal",
                        "Agent worker crashed: {}: {}".format(
                            type(e).__name__, e))
                except Exception:
                    pass
                try:
                    self._dispatch_finish()
                except Exception:
                    pass

        t = Thread(ThreadStart(worker))
        t.IsBackground = True
        t.Start()

    def _on_stop(self, sender, args):
        self._cancelled = True
        self._set_status("Stopping...")

    # -----------------------------------------------------------------
    # Agent loop (worker thread)
    # -----------------------------------------------------------------

    def _run_agent_loop(self, api_key, model_id, system_prompt):
        """Drives the agent until end_turn, max rounds, or error.
        Each iteration sends the full message history, parses the stream
        (text + tool_use blocks), and if needed executes tools on the UI
        thread and appends the tool_result message before looping."""
        for round_idx in range(MAX_AGENT_ROUNDS):
            if self._cancelled:
                self._dispatch_cancelled()
                return

            messages = _build_api_messages(self._current_conv["messages"])

            blocks, stop_reason, usage = _post_stream_round(
                api_key, model_id, system_prompt, TOOL_DEFS, messages,
                on_text_delta        = self._dispatch_text_delta,
                on_tool_use_start    = self._dispatch_tool_start,
                on_tool_use_complete = self._dispatch_tool_complete,
                on_error             = self._dispatch_error,
                is_cancelled         = lambda: self._cancelled,
            )

            if blocks is None and stop_reason == "cancelled":
                self._dispatch_cancelled()
                return
            if blocks is None:
                # Error already dispatched by _post_stream_round.
                self._dispatch_finish()
                return

            # Persist this assistant turn.
            self._dispatch_assistant_turn(blocks, usage)

            if stop_reason != "tool_use":
                # end_turn / max_tokens / stop_sequence -> we're done.
                self._dispatch_finish()
                return

            # Execute each tool_use block on the UI thread.
            tool_results = []
            for blk in blocks:
                if blk.get("type") != "tool_use":
                    continue
                if self._cancelled:
                    self._dispatch_cancelled()
                    return
                tname  = blk.get("name", "")
                tinput = blk.get("input", {})
                tid    = blk.get("id", "")

                # Route through ExternalEvent so the tool runs on Revit's
                # UI thread WITH a valid API context. This is mandatory in
                # modeless mode - script.py has already returned, so we
                # have no implicit API permission. Blocks the worker
                # thread until Revit invokes the action handler.
                try:
                    captured_doc = self._doc
                    captured_n   = tname
                    captured_i   = tinput
                    def call_tool(n=captured_n, i=captured_i, d=captured_doc):
                        return execute_tool(n, i, d)
                    result = self._action_handler.queue_action_blocking(
                        call_tool, timeout_ms=120000)
                except Exception as e:
                    result = {"error": "tool dispatch failed: {}: {}".format(
                        type(e).__name__, e)}

                # Update the tool bubble with the result summary +
                # any "Open in Revit" affordances for things this tool
                # just created (sheets, views, schedules).
                summary   = summarize_tool_result(result)
                is_error  = ("error" in result) if isinstance(result, dict) else False
                openables = _openable_ids_from_result(tname, result)
                self._dispatch_tool_done(tid, summary, is_error, openables)

                # Serialize result for the API. ensure_ascii=False so
                # element names with non-ASCII chars don't crash IronPython's
                # ascii encoder (see save_conversation note).
                try:
                    result_str = json.dumps(result, default=str, ensure_ascii=False)
                except Exception:
                    result_str = json.dumps({"error": "tool result was not JSON-serializable"},
                                            ensure_ascii=False)
                tool_results.append({
                    "type":         "tool_result",
                    "tool_use_id":  tid,
                    "content":      result_str,
                })

            self._dispatch_tool_results(tool_results)

        # Hit MAX_AGENT_ROUNDS without converging.
        self._dispatch_error("internal",
                             "Agent loop exceeded {} rounds without finishing. "
                             "This usually means the model kept calling tools. "
                             "Try a more specific question.".format(MAX_AGENT_ROUNDS))
        self._dispatch_finish()

    # -----------------------------------------------------------------
    # Worker -> UI marshalling
    # -----------------------------------------------------------------

    def _dispatch_text_delta(self, text):
        self.Dispatcher.BeginInvoke(
            System.Action(lambda t=text: self._on_text_delta_ui(t)))

    def _on_text_delta_ui(self, text):
        if self._cancelled:
            return
        if self._streaming_text_block is None:
            # Open a new text segment in the active turn bubble (creating
            # the bubble itself if this is the first segment).
            self._streaming_text_block = self._add_text_to_turn(text="")
            self._streaming_buffer     = ""
        self._streaming_buffer += text
        self._streaming_text_block.Text = self._streaming_buffer
        # Streaming text gets committed to _turn_all_text inside
        # _finalize_streaming_text_segment (called on tool start or
        # turn close). No per-delta accumulation needed here.
        self._scroll_to_bottom()

    def _dispatch_tool_start(self, info):
        self.Dispatcher.BeginInvoke(
            System.Action(lambda i=info: self._on_tool_start_ui(i)))

    def _on_tool_start_ui(self, info):
        # End the current streaming text segment (becomes markdown) so the
        # subsequent tool row sits below cleanly-rendered text rather than
        # mid-stream raw text.
        self._finalize_streaming_text_segment()
        row, status_tb = self._add_tool_to_turn(info.get("name", "?"))
        tid = info.get("id", "")
        if tid:
            self._tool_bubbles[tid] = {"row": row, "status_tb": status_tb}

    def _dispatch_tool_complete(self, info):
        # Reserved for showing input args inline later. No-op for now.
        pass

    def _dispatch_tool_done(self, tid, summary, is_error, openables=None):
        self.Dispatcher.BeginInvoke(
            System.Action(lambda i=tid, s=summary, e=is_error, o=openables:
                          self._on_tool_done_ui(i, s, e, o)))

    def _on_tool_done_ui(self, tid, summary, is_error, openables=None):
        info = self._tool_bubbles.get(tid)
        if info is None:
            return
        status_tb = info.get("status_tb")
        if status_tb is not None:
            status_tb.Text       = summary
            status_tb.FontStyle  = FontStyles.Normal
            # Match the pill's quiet styling - lighter gray for "done", red for errors.
            status_tb.Foreground = _BRUSH_TOOL_ERROR if is_error else _BRUSH_TOOL_PILL_TEXT

        # Accumulate "Open in Revit" buttons for any openable elements
        # this tool produced. They render in a row at the END of the
        # turn (in _close_turn), not inline next to the pill. Only
        # collect from successful calls - no point offering to open
        # something that failed to create.
        if openables and not is_error:
            self._collect_turn_openables(openables)

    def _collect_turn_openables(self, openables):
        """Append openables to the active turn's accumulator, deduping
        by element id so the same created element doesn't render twice
        (e.g., if Claude re-issues a tool call after a transient error)."""
        if not openables:
            return
        try:
            bucket = self._turn_openables
            seen   = self._turn_openable_ids_seen
        except AttributeError:
            # No active turn (defensive); nothing to do.
            return
        for o in openables:
            if not isinstance(o, dict):
                continue
            eid = o.get("id")
            # Skip if no id (can't open) or already collected.
            if eid is None:
                continue
            if eid in seen:
                continue
            seen.add(eid)
            bucket.append(o)

    def _dispatch_assistant_turn(self, blocks, usage):
        # Synchronous: the worker thread MUST not loop back to build the
        # next API request until this turn is appended to history.
        # Otherwise round 2 sends a history with a tool_use but no
        # matching tool_result, and the API returns 400.
        self.Dispatcher.Invoke(
            System.Action(lambda b=blocks, u=usage:
                          self._on_assistant_turn_ui(b, u)))

    def _on_assistant_turn_ui(self, blocks, usage):
        # Persist the assistant turn. Convert a single-text-only set of
        # blocks back to a string for cleaner storage; otherwise store the
        # block list verbatim.
        if (len(blocks) == 1
                and blocks[0].get("type") == "text"):
            content = blocks[0].get("text", "")
        else:
            # Strip our internal scratch fields, keep API-canonical shape.
            content = []
            for b in blocks:
                if b.get("type") == "text":
                    content.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "tool_use":
                    content.append({
                        "type":  "tool_use",
                        "id":    b.get("id", ""),
                        "name":  b.get("name", ""),
                        "input": b.get("input", {}),
                    })
        self._current_conv["messages"].append({
            "role":    "assistant",
            "content": content,
            "ts":      _now_iso(),
            "usage":   usage,
        })
        self._save_current_conv_safe()
        # Finalize any open streaming text segment. The turn bubble stays
        # open across multiple API rounds; only _close_turn (called from
        # _on_finish_ui at end_turn) tears it down.
        self._finalize_streaming_text_segment()

    def _dispatch_tool_results(self, tool_results):
        # Synchronous for the same reason as _dispatch_assistant_turn:
        # the worker thread is about to read history to build the next
        # API request. If this BeginInvoke'd, round 2 would send the
        # assistant tool_use without its matching tool_result and the
        # API would 400.
        self.Dispatcher.Invoke(
            System.Action(lambda tr=tool_results:
                          self._on_tool_results_ui(tr)))

    def _on_tool_results_ui(self, tool_results):
        if not tool_results:
            return
        self._current_conv["messages"].append({
            "role":    "user",
            "content": tool_results,
            "ts":      _now_iso(),
        })
        self._save_current_conv_safe()

    def _dispatch_finish(self):
        self.Dispatcher.BeginInvoke(System.Action(self._on_finish_ui))

    def _on_finish_ui(self):
        # Update sidebar ordering.
        for c in self._conversations:
            if c["id"] == self._current_conv["id"]:
                c["updated_at"]    = self._current_conv.get("updated_at", "")
                c["message_count"] = len(self._current_conv["messages"])
                break
        self._conversations.sort(
            key=lambda c: c.get("updated_at", ""), reverse=True)
        self._render_sidebar()
        self._refresh_chat_title()

        # End the turn: finalize any leftover streaming text and apply
        # question styling if the final paragraph ends with '?'.
        self._close_turn()
        self._tool_bubbles         = {}
        self._set_streaming_ui(False)
        self._set_status("")
        self.txt_input.Focus()

    def _dispatch_cancelled(self):
        self.Dispatcher.BeginInvoke(System.Action(self._on_cancelled_ui))

    def _on_cancelled_ui(self):
        self._add_error_bubble("Stopped", "Cancelled by user.")
        self._on_finish_ui()

    def _dispatch_error(self, label, detail):
        self.Dispatcher.BeginInvoke(
            System.Action(lambda l=label, d=detail: self._on_error_ui(l, d)))

    def _on_error_ui(self, label, detail):
        nice = {
            "auth":       "Authentication failed",
            "rate_limit": "Rate-limited",
            "overloaded": "Anthropic overloaded",
            "network":    "Network error",
            "api":        "API error",
            "internal":   "Internal error",
        }.get(label, "Error")
        self._add_error_bubble(nice, detail or "")

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

# Module-level pin: WPFWindow's only strong reference once main() returns.
# Without this, the GC could collect the window mid-conversation. pyRevit's
# convention names this `__window__`. Cleared in ChatbotForm._on_closed.
__window__ = None


def main():
    # pyRevit's output console can briefly flash on script start. Close
    # it proactively so it doesn't fight with our chat dialog for focus.
    try:
        script.get_output().close()
    except Exception:
        pass
    try:
        import logging
        logging.getLogger('pyrevit').setLevel(logging.CRITICAL)
    except Exception:
        pass

    # Single-instance guard: clicking the toolbar button while the
    # chatbot is already open brings the existing window to front
    # rather than opening a duplicate. Relies on the module-level
    # __window__ pin (which __persistentengine__ keeps alive across
    # script invocations).
    global __window__
    if __window__ is not None:
        try:
            __window__.Activate()
            return
        except Exception:
            # Pinned reference is stale (window was closed without
            # _on_closed clearing it cleanly). Drop and fall through.
            __window__ = None

    # Telemetry: start now, end on Window.Closed (handled inside
    # ChatbotForm). Modeless tools can't use the `with session()`
    # context manager because main() returns immediately while the
    # window outlives this call.
    session = dbhms_telemetry.start(__title__, script_path=__file__)
    try:
        __window__ = ChatbotForm(telemetry_session=session)
        __window__.Show()
    except Exception as e:
        # If construction blew up, end the telemetry session as failed.
        try:
            dbhms_telemetry.end(session, status="failed",
                                error="{}: {}".format(type(e).__name__, e))
        except Exception:
            pass
        raise


main()
