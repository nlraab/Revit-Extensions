# -*- coding: utf-8 -*-
"""Revisions Manager — Comprehensive revision management for Revit.

Features:
  - Full parity with Revit's native Revisions dialog:
      * Seq, Rev #, Date, Description, Issued By, Issued To, Issued
      * Show  (Cloud and Tag / Tag / None)  — RevisionVisibility per revision
      * Numbering (Numeric / Alphanumeric / None) — sequence type per revision
  - Create new revisions / Delete revisions (with cloud / sheet check)
  - Reorder revisions (Move Up / Move Down)
  - Side-by-side sheet panel with multi-select:
      * Click a revision to focus it; sheet checkboxes show coverage
      * Shift-click / Ctrl-click sheets to build a selection
      * Apply to Selected / Remove from Selected  (operate on highlighted rows)
      * Apply to All / Remove from All  (operate on every visible sheet)
      * Clicking a checkbox when multiple sheets are selected applies to all selected
      * Filter, "Only with selected revision" toggle, group by prefix
  - Find Clouds: list every revision cloud with double-click → zoom to
  - Cloud / Tag buttons launch Revit's native commands after closing
"""
__title__ = "Revisions\nManager"
__doc__   = "Manage revisions: create, edit, reorder, apply to sheets, find clouds, cloud/tag."

import clr
import sys

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System")

from Autodesk.Revit.DB import (
    FilteredElementCollector, ViewSheet, View, Revision,
    Transaction, BuiltInParameter, BuiltInCategory,
    ElementId, RevisionVisibility, RevisionCloud
)
# RevisionNumberType exists from Revit 2020+; wrap import so older builds still work.
try:
    from Autodesk.Revit.DB import RevisionNumberType
    _HAS_NUMBER_TYPE = True
except ImportError:
    _HAS_NUMBER_TYPE = False

# RevisionNumberingSequence is the modern (Revit 2022+) API: each revision points
# to a sequence element via Revision.RevisionNumberingSequenceId. The sequence
# element carries the actual settings (Numeric vs Alphanumeric, prefix, suffix,
# etc.). Wrap the import so older Revit versions don't blow up at module load.
try:
    from Autodesk.Revit.DB import RevisionNumberingSequence
    _HAS_NUMBERING_SEQUENCE = True
except ImportError:
    _HAS_NUMBERING_SEQUENCE = False

# RevisionNumbering enum (PerProject / PerSheet) lives on the per-sequence
# settings (NumericRevisionSettings / AlphanumericRevisionSettings) in modern
# Revit. Wrap to stay safe across versions.
try:
    from Autodesk.Revit.DB import RevisionNumbering
    _HAS_REVISION_NUMBERING = True
except ImportError:
    _HAS_REVISION_NUMBERING = False

from Autodesk.Revit.UI import (
    TaskDialog, TaskDialogCommonButtons, TaskDialogResult
)

import System
from System.Windows import (
    Window, Thickness, HorizontalAlignment, VerticalAlignment,
    Visibility, RoutedEventHandler
)
from System.Windows.Controls import (
    DataGrid, DataGridCheckBoxColumn, DataGridTextColumn,
    DataGridTemplateColumn, DataGridEditAction, DataGridLength,
    ComboBox, ComboBoxItem, TextBox,
    ListBox, ListBoxItem, Button, CheckBox, RadioButton,
    StackPanel, Grid, Border, TextBlock, ScrollViewer,
    GroupStyle, Expander, WrapPanel, Orientation
)
from System.Windows.Controls.Primitives import ToggleButton
from System.Windows.Threading import DispatcherPriority
from System import Action
from System.Windows.Media import SolidColorBrush, Color
from System.Windows.Data import (
    CollectionViewSource, PropertyGroupDescription,
    Binding, BindingMode
)
from System.Collections.ObjectModel import ObservableCollection
from System.Collections.Generic import List
from System.ComponentModel import (
    INotifyPropertyChanged, PropertyChangedEventArgs,
    SortDescription, ListSortDirection
)
from System.Windows.Interop import WindowInteropHelper
import System.Diagnostics
import System.Windows.Markup as Markup

doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def eid_int(eid):
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def get_prefix(sheet_number):
    s = (sheet_number or "").strip()
    if s and s[0].isalpha():
        return s[0].upper()
    return s[0] if s else "?"


def get_all_revisions(doc):
    revs = list(
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_Revisions)
        .ToElements()
    )
    def seq(r):
        try:
            return r.SequenceNumber
        except Exception:
            try:
                return r.get_Parameter(
                    BuiltInParameter.PROJECT_REVISION_SEQUENCE_NUM).AsInteger()
            except Exception:
                return 0
    return sorted(revs, key=seq)


def get_all_sheets(doc):
    sheets = list(
        FilteredElementCollector(doc).OfClass(ViewSheet).ToElements()
    )
    return sorted(sheets, key=lambda s: s.SheetNumber)


def get_all_revision_clouds(doc):
    return list(
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_RevisionClouds)
        .WhereElementIsNotElementType()
        .ToElements()
    )


def revision_seq(rev):
    try:
        return rev.SequenceNumber
    except Exception:
        try:
            return rev.get_Parameter(
                BuiltInParameter.PROJECT_REVISION_SEQUENCE_NUM).AsInteger()
        except Exception:
            return 0


def revision_number(rev):
    # Trust the Revit-managed parameter if it exists. When numbering is "None",
    # Revit deliberately returns an empty string here — we must NOT fall back to
    # SequenceNumber in that case (which would show a number that isn't real).
    try:
        p = rev.get_Parameter(BuiltInParameter.PROJECT_REVISION_REVISION_NUM)
        if p:
            v = p.AsString()
            return v if v is not None else ""
    except Exception:
        pass
    # Parameter unavailable — last-resort fallback
    try:
        return str(rev.SequenceNumber)
    except Exception:
        return ""


def _all_numbering_sequences(doc):
    """Return list of every RevisionNumberingSequence element in the document.
    Tries the documented static getter first, falls back to FilteredElementCollector."""
    if not _HAS_NUMBERING_SEQUENCE:
        return []
    # Preferred: RevisionNumberingSequence.GetAllRevisionNumberingSequences(doc)
    try:
        ids = RevisionNumberingSequence.GetAllRevisionNumberingSequences(doc)
        out = []
        for eid in ids:
            el = doc.GetElement(eid)
            if el is not None:
                out.append(el)
        if out:
            return out
    except Exception:
        pass
    # Fallback: collector
    try:
        return list(FilteredElementCollector(doc).OfClass(RevisionNumberingSequence))
    except Exception:
        return []


def _seq_choice_label(seq):
    """Map a RevisionNumberingSequence element to "Numeric" / "Alphanumeric" / "None".
    "None" is detected by name (Revit names the special non-numbering sequence "None")."""
    try:
        nm = (seq.Name or "").strip().lower()
    except Exception:
        nm = ""
    if nm == "none":
        return "None"
    if _HAS_NUMBER_TYPE:
        try:
            if seq.NumberType == RevisionNumberType.Alphanumeric:
                return "Alphanumeric"
        except Exception:
            pass
    return "Numeric"


def get_numbering_sequence_for_choice(doc, choice):
    """Find the document's RevisionNumberingSequence element matching one of
    "Numeric" / "Alphanumeric" / "None". Returns None if not found."""
    for seq in _all_numbering_sequences(doc):
        if _seq_choice_label(seq) == choice:
            return seq
    return None


def get_revision_numbering_str(rev, doc):
    """Return one of "Numeric" / "Alphanumeric" / "None" for a Revision element.
    Tries the modern sequence-id API first, falls back to Revision.NumberType."""
    # Modern API: RevisionNumberingSequenceId points to a sequence element.
    try:
        seq_id = rev.RevisionNumberingSequenceId
        if seq_id is not None and seq_id != ElementId.InvalidElementId:
            seq = doc.GetElement(seq_id)
            if seq is not None:
                return _seq_choice_label(seq)
        # Invalid id — treat as None
        return "None"
    except AttributeError:
        pass
    except Exception:
        pass
    # Fallback: read Revision.NumberType directly. No real "None" support.
    if _HAS_NUMBER_TYPE:
        try:
            if rev.NumberType == RevisionNumberType.Alphanumeric:
                return "Alphanumeric"
        except Exception:
            pass
    return "Numeric"


def set_revision_numbering(rev, doc, choice):
    """Set the numbering for a Revision to "Numeric", "Alphanumeric", or "None".
    Caller is responsible for the surrounding Transaction. Raises Exception with
    a clear message on failure (including the case where the API call appears to
    succeed but the value doesn't actually change — silent no-op)."""

    before = get_revision_numbering_str(rev, doc)

    # Modern API: find the sequence element for the requested choice and assign it.
    seq = get_numbering_sequence_for_choice(doc, choice)
    if seq is not None:
        try:
            rev.RevisionNumberingSequenceId = seq.Id
            after = get_revision_numbering_str(rev, doc)
            if after == choice:
                return
            # Assignment didn't take — read-back doesn't match.
            raise Exception(
                "Set RevisionNumberingSequenceId to '{0}' (id={1}) but read-back "
                "still reports '{2}'. Before: '{3}'.".format(
                    choice, seq.Id, after, before))
        except AttributeError:
            # Property not settable — drop into legacy path below.
            pass

    # If "None" was requested but no "None" sequence exists, try clearing the id.
    if choice == "None":
        try:
            rev.RevisionNumberingSequenceId = ElementId.InvalidElementId
            after = get_revision_numbering_str(rev, doc)
            if after == "None":
                return
            raise Exception(
                "Cleared RevisionNumberingSequenceId but read-back reports '{0}' "
                "(no 'None' sequence found in this project).".format(after))
        except AttributeError:
            pass

    # Legacy fallback: Revision.NumberType (no None support).
    if choice in ("Numeric", "Alphanumeric") and _HAS_NUMBER_TYPE:
        target = (RevisionNumberType.Alphanumeric if choice == "Alphanumeric"
                  else RevisionNumberType.Numeric)
        try:
            rev.NumberType = target
            after = get_revision_numbering_str(rev, doc)
            if after == choice:
                return
            raise Exception(
                "Set Revision.NumberType to '{0}' but read-back reports '{1}'.".format(
                    choice, after))
        except AttributeError:
            pass

    # If we got here, no API path worked.
    seqs_in_doc = _all_numbering_sequences(doc)
    seq_summary = ", ".join(
        "{0}({1})".format((s.Name or "?"), _seq_choice_label(s))
        for s in seqs_in_doc) or "(none found)"
    raise Exception(
        "No working API to set numbering to '{0}'. Sequences in document: {1}. "
        "Revit version may not support this operation.".format(choice, seq_summary))


def get_numbering_scope(doc):
    """Return "Per Project" or "Per Sheet" — the current scope used by the
    numeric sequence. Alphanumeric is expected to match in normal use."""
    if not _HAS_REVISION_NUMBERING:
        return "Per Project"
    seq = get_numbering_sequence_for_choice(doc, "Numeric")
    if seq is None:
        return "Per Project"
    try:
        s = seq.GetNumericRevisionSettings()
        if s.Numbering == RevisionNumbering.PerSheet:
            return "Per Sheet"
        return "Per Project"
    except Exception:
        return "Per Project"


def set_numbering_scope(doc, scope):
    """Set the numbering scope (Per Project / Per Sheet) for both the Numeric
    and Alphanumeric sequences so they stay in sync. Caller is responsible
    for the surrounding Transaction. Raises Exception on failure."""
    if not _HAS_REVISION_NUMBERING:
        raise Exception("RevisionNumbering enum not available in this Revit version.")

    target = (RevisionNumbering.PerSheet if scope == "Per Sheet"
              else RevisionNumbering.PerProject)

    applied_to = []
    errors = []

    # Numeric
    num_seq = get_numbering_sequence_for_choice(doc, "Numeric")
    if num_seq is not None:
        try:
            s = num_seq.GetNumericRevisionSettings()
            s.Numbering = target
            num_seq.SetNumericRevisionSettings(s)
            applied_to.append("Numeric")
        except Exception as ex:
            errors.append("Numeric: {0}".format(str(ex)))

    # Alphanumeric
    alpha_seq = get_numbering_sequence_for_choice(doc, "Alphanumeric")
    if alpha_seq is not None:
        try:
            s = alpha_seq.GetAlphanumericRevisionSettings()
            s.Numbering = target
            alpha_seq.SetAlphanumericRevisionSettings(s)
            applied_to.append("Alphanumeric")
        except Exception as ex:
            errors.append("Alphanumeric: {0}".format(str(ex)))

    if not applied_to:
        raise Exception(
            "Could not set scope on any sequence. Errors: {0}".format(
                "; ".join(errors) if errors else "(no sequences found)"))


def sheet_revision_summary(sheet):
    try:
        ids = list(sheet.GetAllRevisionIds())
        if not ids:
            return ""
        seqs = []
        for rid in ids:
            r = doc.GetElement(rid)
            if r is not None:
                seqs.append(revision_seq(r))
        seqs.sort()
        return ", ".join(str(s) for s in seqs)
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════

class RevisionItem(INotifyPropertyChanged):
    """Observable wrapper around a Revit Revision for two-way WPF binding."""

    PropertyChanged = None

    def add_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Combine(self.PropertyChanged, value)

    def remove_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Remove(self.PropertyChanged, value)

    def _notify(self, name):
        if self.PropertyChanged:
            self.PropertyChanged(self, PropertyChangedEventArgs(name))

    def __init__(self, rev, doc):
        self._rev        = rev
        self._doc        = doc
        self._is_focused = False
        self._refresh_cache()

    def _refresh_cache(self):
        rev = self._rev
        try:    self._seq = rev.SequenceNumber
        except Exception: self._seq = 0
        self._num  = revision_number(rev)
        try:    self._date = rev.RevisionDate or ""
        except Exception: self._date = ""
        try:    self._desc = rev.Description or ""
        except Exception: self._desc = ""
        try:    self._iss_by = rev.IssuedBy or ""
        except Exception: self._iss_by = ""
        try:    self._iss_to = rev.IssuedTo or ""
        except Exception: self._iss_to = ""
        try:    self._issued = bool(rev.Issued)
        except Exception: self._issued = False

        # Show (RevisionVisibility)
        try:
            vis = rev.Visibility
            if vis == RevisionVisibility.TagVisible:
                self._show_str = "Tag"
            elif vis == RevisionVisibility.Hidden:
                self._show_str = "None"
            else:
                self._show_str = "Cloud and Tag"
        except Exception:
            self._show_str = "Cloud and Tag"

        # Numbering: Numeric / Alphanumeric / None (matches Revit native dialog)
        try:
            self._numb_str = get_revision_numbering_str(rev, self._doc)
        except Exception:
            self._numb_str = "Numeric"

        self._sheet_count = 0
        self._cloud_count = 0

    @property
    def Revision(self):     return self._rev
    @property
    def ElementId(self):    return self._rev.Id
    @property
    def SeqNum(self):       return self._seq
    @property
    def RevNumber(self):    return self._num

    # -- IsFocused --
    def _g_focus(self): return self._is_focused
    def _s_focus(self, v):
        self._is_focused = bool(v)
        self._notify("IsFocused")
    IsFocused = property(_g_focus, _s_focus)

    # -- Date --
    def _g_date(self): return self._date
    def _s_date(self, v):
        v = (v or "").strip()
        if v == self._date:
            return
        try:
            with Transaction(self._doc, "Revisions: Edit Date") as t:
                t.Start()
                self._rev.RevisionDate = v
                t.Commit()
            self._date = v
            self._notify("RevDate")
        except Exception as ex:
            TaskDialog.Show("Edit Revision",
                "Could not set Date to '{0}':\n{1}".format(v, str(ex)))
            self._notify("RevDate")
    RevDate = property(_g_date, _s_date)

    # -- Description --
    def _g_desc(self): return self._desc
    def _s_desc(self, v):
        v = (v or "").strip()
        if v == self._desc:
            return
        try:
            with Transaction(self._doc, "Revisions: Edit Description") as t:
                t.Start()
                self._rev.Description = v
                t.Commit()
            self._desc = v
            self._notify("Description")
        except Exception as ex:
            TaskDialog.Show("Edit Revision",
                "Could not set Description:\n{0}".format(str(ex)))
            self._notify("Description")
    Description = property(_g_desc, _s_desc)

    # -- IssuedBy --
    def _g_iby(self): return self._iss_by
    def _s_iby(self, v):
        v = (v or "").strip()
        if v == self._iss_by:
            return
        try:
            with Transaction(self._doc, "Revisions: Edit Issued By") as t:
                t.Start()
                self._rev.IssuedBy = v
                t.Commit()
            self._iss_by = v
            self._notify("IssuedBy")
        except Exception as ex:
            TaskDialog.Show("Edit Revision",
                "Could not set Issued By:\n{0}".format(str(ex)))
            self._notify("IssuedBy")
    IssuedBy = property(_g_iby, _s_iby)

    # -- IssuedTo --
    def _g_ito(self): return self._iss_to
    def _s_ito(self, v):
        v = (v or "").strip()
        if v == self._iss_to:
            return
        try:
            with Transaction(self._doc, "Revisions: Edit Issued To") as t:
                t.Start()
                self._rev.IssuedTo = v
                t.Commit()
            self._iss_to = v
            self._notify("IssuedTo")
        except Exception as ex:
            TaskDialog.Show("Edit Revision",
                "Could not set Issued To:\n{0}".format(str(ex)))
            self._notify("IssuedTo")
    IssuedTo = property(_g_ito, _s_ito)

    # -- Issued (bool) --
    def _g_iss(self): return self._issued
    def _s_iss(self, v):
        v = bool(v)
        if v == self._issued:
            return
        try:
            with Transaction(self._doc, "Revisions: Toggle Issued") as t:
                t.Start()
                self._rev.Issued = v
                t.Commit()
            self._issued = v
            self._notify("Issued")
        except Exception as ex:
            TaskDialog.Show("Edit Revision",
                "Could not toggle Issued:\n{0}".format(str(ex)))
            self._notify("Issued")
    Issued = property(_g_iss, _s_iss)

    # -- Show (RevisionVisibility) --
    def _g_show(self): return self._show_str
    def _s_show(self, v):
        v = v or "Cloud and Tag"
        if v == self._show_str:
            return
        vis_map = {
            "Cloud and Tag": RevisionVisibility.CloudAndTagVisible,
            "Tag":           RevisionVisibility.TagVisible,
            "None":          RevisionVisibility.Hidden,
        }
        vis = vis_map.get(v, RevisionVisibility.CloudAndTagVisible)
        try:
            with Transaction(self._doc, "Revisions: Set Visibility") as t:
                t.Start()
                self._rev.Visibility = vis
                t.Commit()
            self._show_str = v
            self._notify("ShowStr")
        except Exception as ex:
            TaskDialog.Show("Edit Revision",
                "Could not set Show:\n{0}".format(str(ex)))
            self._notify("ShowStr")
    ShowStr = property(_g_show, _s_show)

    @property
    def ShowOptions(self):
        return ["Cloud and Tag", "Tag", "None"]

    # -- Numbering: Numeric / Alphanumeric / None --
    def _g_numb(self): return self._numb_str
    def _s_numb(self, v):
        v = v or "Numeric"
        if v not in ("Numeric", "Alphanumeric", "None"):
            v = "Numeric"
        if v == self._numb_str:
            return
        try:
            with Transaction(self._doc, "Revisions: Set Numbering") as t:
                t.Start()
                set_revision_numbering(self._rev, self._doc, v)
                t.Commit()
            # Re-read everything from Revit — changing the sequence can also
            # change Rev # (Revit auto-renumbers within each sequence).
            self.refresh_from_revit()
        except Exception as ex:
            TaskDialog.Show("Edit Revision",
                "Could not set Numbering:\n{0}".format(str(ex)))
            self._notify("NumberingStr")
    NumberingStr = property(_g_numb, _s_numb)

    @property
    def NumberingOptions(self):
        return ["Numeric", "Alphanumeric", "None"]

    # -- Counts --
    def _g_sc(self): return self._sheet_count
    def _s_sc(self, v):
        self._sheet_count = int(v)
        self._notify("SheetCount")
    SheetCount = property(_g_sc, _s_sc)

    def _g_cc(self): return self._cloud_count
    def _s_cc(self, v):
        self._cloud_count = int(v)
        self._notify("CloudCount")
    CloudCount = property(_g_cc, _s_cc)

    def refresh_from_revit(self):
        self._refresh_cache()
        for n in ("SeqNum", "RevNumber", "RevDate", "Description",
                  "IssuedBy", "IssuedTo", "Issued", "ShowStr", "NumberingStr"):
            self._notify(n)


class SheetRevItem(INotifyPropertyChanged):
    """A sheet row in the sheet panel."""

    PropertyChanged = None

    def add_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Combine(self.PropertyChanged, value)

    def remove_PropertyChanged(self, value):
        self.PropertyChanged = System.Delegate.Remove(self.PropertyChanged, value)

    def _notify(self, name):
        if self.PropertyChanged:
            self.PropertyChanged(self, PropertyChangedEventArgs(name))

    def __init__(self, sheet, doc):
        self._sheet  = sheet
        self._doc    = doc
        self._number = sheet.SheetNumber
        self._name   = sheet.Name
        self._prefix = get_prefix(sheet.SheetNumber)
        self._has_focused_rev = False
        self._all_revs        = sheet_revision_summary(sheet)

    @property
    def Sheet(self):       return self._sheet
    @property
    def ElementId(self):   return self._sheet.Id
    @property
    def SheetNumber(self): return self._number
    @property
    def SheetName(self):   return self._name
    @property
    def Prefix(self):      return self._prefix
    @property
    def GroupKey(self):    return self._prefix

    def _g_hfr(self): return self._has_focused_rev
    def _s_hfr(self, v):
        self._has_focused_rev = v
        self._notify("HasFocusedRev")
    HasFocusedRev = property(_g_hfr, _s_hfr)

    def _g_revs(self): return self._all_revs
    def _s_revs(self, v):
        self._all_revs = v or ""
        self._notify("AllRevs")
    AllRevs = property(_g_revs, _s_revs)

    def refresh_summary(self):
        self._all_revs = sheet_revision_summary(self._sheet)
        self._notify("AllRevs")


class CloudListRow(object):
    def __init__(self, cloud, sheet_num, sheet_name, view_name, comment):
        self.Cloud      = cloud
        self.SheetNum   = sheet_num or ""
        self.SheetName  = sheet_name or ""
        self.ViewName   = view_name or ""
        self.Comment    = comment or ""


# ═══════════════════════════════════════════════════════════════
# SHARED LIGHT THEME
# ═══════════════════════════════════════════════════════════════

SHARED_RESOURCES = """
    <Style x:Key="SectionHeader" TargetType="TextBlock">
      <Setter Property="FontSize"   Value="14"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#1A202C"/>
      <Setter Property="Margin"     Value="0,0,0,6"/>
    </Style>
    <Style x:Key="FieldLabel" TargetType="TextBlock">
      <Setter Property="FontSize"   Value="11"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#4A5568"/>
      <Setter Property="Margin"     Value="0,8,0,2"/>
    </Style>
    <Style x:Key="HelperText" TargetType="TextBlock">
      <Setter Property="FontSize"   Value="11"/>
      <Setter Property="Foreground" Value="#718096"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>

    <Style x:Key="PrimaryButton" TargetType="Button">
      <Setter Property="Background"      Value="#2B6CB0"/>
      <Setter Property="Foreground"      Value="White"/>
      <Setter Property="FontWeight"      Value="SemiBold"/>
      <Setter Property="Padding"         Value="12,5"/>
      <Setter Property="MinWidth"        Value="90"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="Margin"          Value="4,0,0,0"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    CornerRadius="3" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#2C5282"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#2A4365"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="SecondaryButton" TargetType="Button">
      <Setter Property="Background"      Value="#EDF2F7"/>
      <Setter Property="Foreground"      Value="#2D3748"/>
      <Setter Property="Padding"         Value="12,5"/>
      <Setter Property="MinWidth"        Value="90"/>
      <Setter Property="BorderBrush"     Value="#CBD5E0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Cursor"          Value="Hand"/>
      <Setter Property="Margin"          Value="4,0,0,0"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}"
                    CornerRadius="3" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#E2E8F0"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#CBD5E0"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="DangerButton" TargetType="Button" BasedOn="{StaticResource SecondaryButton}">
      <Setter Property="Foreground"  Value="#C53030"/>
      <Setter Property="BorderBrush" Value="#FEB2B2"/>
    </Style>

    <Style TargetType="TextBox">
      <Setter Property="Background"       Value="White"/>
      <Setter Property="Foreground"       Value="#1A202C"/>
      <Setter Property="BorderBrush"      Value="#CBD5E0"/>
      <Setter Property="BorderThickness"  Value="1"/>
      <Setter Property="Padding"          Value="6,4"/>
      <Setter Property="Height"           Value="28"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="CaretBrush"       Value="#2B6CB0"/>
    </Style>

    <Style TargetType="ComboBox">
      <Setter Property="Background"      Value="White"/>
      <Setter Property="Foreground"      Value="#1A202C"/>
      <Setter Property="BorderBrush"     Value="#CBD5E0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding"         Value="6,4"/>
      <Setter Property="Height"          Value="28"/>
      <Setter Property="FontSize"        Value="12"/>
    </Style>
    <Style TargetType="ComboBoxItem">
      <Setter Property="Background"  Value="White"/>
      <Setter Property="Foreground"  Value="#1A202C"/>
      <Setter Property="Padding"     Value="6,4"/>
      <Setter Property="FontSize"    Value="12"/>
    </Style>

    <Style TargetType="CheckBox">
      <Setter Property="Foreground"  Value="#1A202C"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="Margin"      Value="0,3,0,3"/>
    </Style>
    <!-- RadioButton: properly-sized centered bullet -->
    <Style TargetType="RadioButton">
      <Setter Property="Foreground" Value="#1A202C"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="RadioButton">
            <StackPanel Orientation="Horizontal" Background="Transparent">
              <Grid Width="16" Height="16" VerticalAlignment="Center" Margin="0,0,6,0">
                <Ellipse x:Name="outer" Width="14" Height="14"
                         HorizontalAlignment="Center" VerticalAlignment="Center"
                         Stroke="#A0AEC0" StrokeThickness="1.5" Fill="White"/>
                <Ellipse x:Name="dot" Width="7" Height="7"
                         HorizontalAlignment="Center" VerticalAlignment="Center"
                         Fill="#2B6CB0" Visibility="Collapsed"/>
              </Grid>
              <ContentPresenter VerticalAlignment="Center"
                                RecognizesAccessKey="True"/>
            </StackPanel>
            <ControlTemplate.Triggers>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="dot" Property="Visibility" Value="Visible"/>
                <Setter TargetName="outer" Property="Stroke" Value="#2B6CB0"/>
              </Trigger>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="outer" Property="Stroke" Value="#2B6CB0"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style TargetType="DataGrid">
      <Setter Property="Background"               Value="White"/>
      <Setter Property="Foreground"               Value="#1A202C"/>
      <Setter Property="BorderBrush"              Value="#E2E8F0"/>
      <Setter Property="BorderThickness"          Value="1"/>
      <Setter Property="RowBackground"            Value="White"/>
      <Setter Property="AlternatingRowBackground" Value="#F7FAFC"/>
      <Setter Property="GridLinesVisibility"      Value="None"/>
      <Setter Property="HeadersVisibility"        Value="Column"/>
      <Setter Property="SelectionMode"            Value="Extended"/>
      <Setter Property="SelectionUnit"            Value="FullRow"/>
      <Setter Property="CanUserAddRows"           Value="False"/>
      <Setter Property="CanUserDeleteRows"        Value="False"/>
      <Setter Property="AutoGenerateColumns"      Value="False"/>
      <Setter Property="CanUserResizeRows"        Value="False"/>
      <Setter Property="RowHeight"                Value="30"/>
    </Style>
    <Style TargetType="DataGridColumnHeader">
      <Setter Property="Background"        Value="#EDF2F7"/>
      <Setter Property="Foreground"        Value="#2D3748"/>
      <Setter Property="FontSize"          Value="11"/>
      <Setter Property="FontWeight"        Value="SemiBold"/>
      <Setter Property="Padding"           Value="10,8"/>
      <Setter Property="BorderBrush"       Value="#E2E8F0"/>
      <Setter Property="BorderThickness"   Value="0,0,1,1"/>
      <Setter Property="SeparatorVisibility" Value="Visible"/>
      <Setter Property="SeparatorBrush"    Value="#E2E8F0"/>
    </Style>
    <Style TargetType="DataGridRow">
      <Setter Property="Foreground"      Value="#1A202C"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Style.Triggers>
        <Trigger Property="IsMouseOver" Value="True">
          <Setter Property="Background" Value="#EDF2F7"/>
        </Trigger>
        <Trigger Property="IsSelected" Value="True">
          <Setter Property="Background" Value="#BEE3F8"/>
        </Trigger>
      </Style.Triggers>
    </Style>
    <Style TargetType="DataGridCell">
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Foreground"      Value="#1A202C"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
      <Style.Triggers>
        <Trigger Property="IsSelected" Value="True">
          <Setter Property="Background" Value="Transparent"/>
          <Setter Property="Foreground" Value="#1A202C"/>
        </Trigger>
      </Style.Triggers>
    </Style>

    <Style x:Key="RowCheck" TargetType="CheckBox">
      <Setter Property="HorizontalAlignment" Value="Center"/>
      <Setter Property="VerticalAlignment"   Value="Center"/>
      <Setter Property="Margin" Value="0"/>
    </Style>

    <Style x:Key="EditCell" TargetType="TextBox">
      <Setter Property="Background"        Value="White"/>
      <Setter Property="Foreground"        Value="#1A202C"/>
      <Setter Property="BorderBrush"       Value="#2B6CB0"/>
      <Setter Property="BorderThickness"   Value="1"/>
      <Setter Property="Padding"           Value="8,2"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="CaretBrush"        Value="#2B6CB0"/>
      <Setter Property="SelectionBrush"    Value="#BEE3F8"/>
    </Style>

    <!-- Inline ComboBox for grid cells (Show / Numbering columns) -->
    <Style x:Key="GridCombo" TargetType="ComboBox">
      <Setter Property="Background"      Value="Transparent"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Padding"         Value="4,2"/>
      <Setter Property="Height"          Value="26"/>
      <Setter Property="FontSize"        Value="11"/>
      <Setter Property="Foreground"      Value="#1A202C"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>

    <Style x:Key="GroupHdr" TargetType="GroupItem">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="GroupItem">
            <Expander IsExpanded="True">
              <Expander.Style>
                <Style TargetType="Expander">
                  <Setter Property="Background"     Value="#EDF2F7"/>
                  <Setter Property="BorderBrush"    Value="#E2E8F0"/>
                  <Setter Property="BorderThickness" Value="0,0,0,1"/>
                  <Setter Property="Foreground"     Value="#2D3748"/>
                  <Setter Property="Template">
                    <Setter.Value>
                      <ControlTemplate TargetType="Expander">
                        <Border Background="{TemplateBinding Background}"
                                BorderBrush="{TemplateBinding BorderBrush}"
                                BorderThickness="{TemplateBinding BorderThickness}">
                          <StackPanel>
                            <Grid>
                              <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="Auto"/>
                                <ColumnDefinition Width="*"/>
                              </Grid.ColumnDefinitions>
                              <CheckBox Grid.Column="0" Tag="GroupSelector"
                                        IsThreeState="True"
                                        Margin="14,0,4,0" VerticalAlignment="Center"
                                        Cursor="Hand" Focusable="False"
                                        ToolTip="Apply / remove selected revision for every sheet in this group"/>
                              <ToggleButton Grid.Column="1" x:Name="Hdr"
                                            IsChecked="{Binding IsExpanded, RelativeSource={RelativeSource TemplatedParent}}"
                                            Background="Transparent" BorderThickness="0"
                                            Cursor="Hand" Padding="6,8"
                                            HorizontalContentAlignment="Left">
                                <ToggleButton.Template>
                                  <ControlTemplate TargetType="ToggleButton">
                                    <Border Background="{TemplateBinding Background}"
                                            BorderThickness="{TemplateBinding BorderThickness}"
                                            Padding="{TemplateBinding Padding}">
                                      <ContentPresenter/>
                                    </Border>
                                  </ControlTemplate>
                                </ToggleButton.Template>
                                <StackPanel Orientation="Horizontal">
                                  <TextBlock x:Name="arrow" Text="▾ " Foreground="#2B6CB0" FontSize="11"/>
                                  <TextBlock Text="{Binding Name}" Foreground="#2D3748"
                                             FontWeight="SemiBold" FontSize="12" VerticalAlignment="Center"/>
                                  <TextBlock Foreground="#718096" FontSize="11" VerticalAlignment="Center">
                                    <Run Text="  "/>
                                    <Run Text="{Binding ItemCount, Mode=OneWay}"/>
                                    <Run Text=" sheets"/>
                                  </TextBlock>
                                </StackPanel>
                              </ToggleButton>
                            </Grid>
                            <ContentPresenter x:Name="body"/>
                          </StackPanel>
                        </Border>
                        <ControlTemplate.Triggers>
                          <Trigger Property="IsExpanded" Value="False">
                            <Setter TargetName="body" Property="Visibility" Value="Collapsed"/>
                            <Setter TargetName="arrow" Property="Text" Value="▸ "/>
                          </Trigger>
                        </ControlTemplate.Triggers>
                      </ControlTemplate>
                    </Setter.Value>
                  </Setter>
                </Style>
              </Expander.Style>
              <ItemsPresenter/>
            </Expander>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
"""


# ═══════════════════════════════════════════════════════════════
# MAIN WINDOW XAML
# ═══════════════════════════════════════════════════════════════

MAIN_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Revisions Manager" Width="1400" Height="740"
        WindowStartupLocation="CenterScreen"
        Background="#F7FAFC" Foreground="#1A202C"
        FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Window.Resources>
""" + SHARED_RESOURCES + """
  </Window.Resources>

  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <!-- HEADER -->
    <Border Grid.Row="0" Background="#2D3748" Padding="20,14">
      <Grid>
        <StackPanel Orientation="Vertical" HorizontalAlignment="Left">
          <TextBlock Text="Revisions Manager" Foreground="White"
                     FontSize="20" FontWeight="Bold"/>
          <TextBlock x:Name="lbl_project"
                     Text="Create, edit, reorder revisions and manage which sheets they appear on."
                     Foreground="#CBD5E0" FontSize="12" Margin="0,2,0,0"/>
        </StackPanel>
        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
                    VerticalAlignment="Center" Opacity="0.85">
          <TextBlock Text="db" Foreground="#00BFFF" FontSize="32"
                     FontWeight="Bold" FontFamily="Segoe UI" VerticalAlignment="Center"/>
          <TextBlock Text=" | " Foreground="#7A8FA6" FontSize="32"
                     FontWeight="Light" VerticalAlignment="Center"/>
          <TextBlock Text="HMS" Foreground="#00BFFF" FontSize="32"
                     FontWeight="Bold" FontFamily="Segoe UI" VerticalAlignment="Center"/>
        </StackPanel>
      </Grid>
    </Border>

    <!-- TOOLBAR -->
    <Border Grid.Row="1" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,0,0,1"
            Padding="16,10">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <StackPanel Grid.Column="0" Orientation="Horizontal" VerticalAlignment="Center">
          <Button x:Name="btn_new"    Content="✚ New Revision" Style="{StaticResource PrimaryButton}"   Height="30" ToolTip="Create a new revision"/>
          <Button x:Name="btn_delete" Content="🗑 Delete"      Style="{StaticResource DangerButton}"    Height="30" IsEnabled="False" ToolTip="Delete the selected revision"/>
          <Border Width="1" Background="#E2E8F0" Margin="12,4"/>
          <Button x:Name="btn_up"     Content="⬆ Up"           Style="{StaticResource SecondaryButton}" Height="30" MinWidth="60" IsEnabled="False" ToolTip="Move selected revision earlier in sequence"/>
          <Button x:Name="btn_down"   Content="⬇ Down"         Style="{StaticResource SecondaryButton}" Height="30" MinWidth="60" IsEnabled="False" ToolTip="Move selected revision later in sequence"/>
          <Border Width="1" Background="#E2E8F0" Margin="12,4"/>
          <Button x:Name="btn_find"   Content="🔍 Find Clouds" Style="{StaticResource SecondaryButton}" Height="30" MinWidth="110" IsEnabled="False" ToolTip="Show every revision cloud assigned to the selected revision"/>
        </StackPanel>
        <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
          <Border Width="1" Background="#E2E8F0" Margin="12,4"/>
          <TextBlock Text="Numbering scope:" Foreground="#4A5568" FontSize="11"
                     VerticalAlignment="Center" Margin="0,0,8,0"/>
          <ComboBox x:Name="cb_scope" Width="120" Height="26"
                    VerticalContentAlignment="Center" FontSize="12"
                    ToolTip="Per Project: revision numbers are unique across the whole project.&#10;Per Sheet: numbers reset on each sheet.">
            <ComboBoxItem Content="Per Project"/>
            <ComboBoxItem Content="Per Sheet"/>
          </ComboBox>
        </StackPanel>
      </Grid>
    </Border>

    <!-- SPLIT PANE -->
    <Grid Grid.Row="2">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="3*"  MinWidth="560"/>
        <ColumnDefinition Width="5"/>
        <ColumnDefinition Width="2*"  MinWidth="400"/>
      </Grid.ColumnDefinitions>

      <!-- ─── REVISIONS PANEL (LEFT) ─── -->
      <Border Grid.Column="0" Background="White" Padding="16,12">
        <Grid>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
          </Grid.RowDefinitions>

          <StackPanel Grid.Row="0" Orientation="Horizontal" Margin="0,0,0,8">
            <TextBlock Text="Revisions" Style="{StaticResource SectionHeader}"
                       VerticalAlignment="Center" Margin="0"/>
            <TextBlock x:Name="lbl_focus_hint"
                       Text="  •  Click a revision to focus it  •  Click again to deselect"
                       Foreground="#718096" FontSize="11"
                       VerticalAlignment="Center" Margin="0,2,0,0"/>
          </StackPanel>

          <!-- Revision grid — single-select -->
          <DataGrid Grid.Row="1" x:Name="rev_grid"
                    SelectionMode="Single"
                    ScrollViewer.VerticalScrollBarVisibility="Auto"
                    ScrollViewer.HorizontalScrollBarVisibility="Auto">
            <DataGrid.Columns>

              <!-- Focus dot -->
              <DataGridTemplateColumn Header="" Width="28" MinWidth="28" CanUserResize="False" IsReadOnly="True">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <Border Width="12" Height="12" CornerRadius="6"
                            HorizontalAlignment="Center" VerticalAlignment="Center">
                      <Border.Style>
                        <Style TargetType="Border">
                          <Setter Property="Background"      Value="Transparent"/>
                          <Setter Property="BorderBrush"     Value="#CBD5E0"/>
                          <Setter Property="BorderThickness" Value="2"/>
                          <Style.Triggers>
                            <DataTrigger Binding="{Binding IsFocused}" Value="True">
                              <Setter Property="Background"  Value="#2B6CB0"/>
                              <Setter Property="BorderBrush" Value="#2B6CB0"/>
                            </DataTrigger>
                          </Style.Triggers>
                        </Style>
                      </Border.Style>
                    </Border>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>

              <DataGridTextColumn Header="Seq" Binding="{Binding SeqNum}"
                                  Width="40" MinWidth="36" IsReadOnly="True">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="6,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="Foreground"        Value="#2B6CB0"/>
                    <Setter Property="FontWeight"        Value="SemiBold"/>
                    <Setter Property="TextAlignment"     Value="Center"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

              <DataGridTextColumn Header="Rev #" Binding="{Binding RevNumber}"
                                  Width="52" MinWidth="44" IsReadOnly="True">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="6,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="Foreground"        Value="#4A5568"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

              <DataGridTextColumn Header="Date"
                                  Binding="{Binding RevDate, Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
                                  Width="100" MinWidth="80"
                                  EditingElementStyle="{StaticResource EditCell}">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="6,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

              <DataGridTextColumn Header="Description"
                                  Binding="{Binding Description, Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
                                  Width="*" MinWidth="160"
                                  EditingElementStyle="{StaticResource EditCell}">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="6,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

              <DataGridTextColumn Header="Issued By"
                                  Binding="{Binding IssuedBy, Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
                                  Width="80" MinWidth="64"
                                  EditingElementStyle="{StaticResource EditCell}">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="6,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="Foreground"        Value="#4A5568"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

              <DataGridTextColumn Header="Issued To"
                                  Binding="{Binding IssuedTo, Mode=TwoWay, UpdateSourceTrigger=LostFocus}"
                                  Width="80" MinWidth="64"
                                  EditingElementStyle="{StaticResource EditCell}">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="6,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="Foreground"        Value="#4A5568"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

              <!-- Issued checkbox -->
              <DataGridTemplateColumn Header="Issued" Width="56" MinWidth="50">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <CheckBox Tag="IssuedCheck"
                              IsChecked="{Binding Issued, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                              Style="{StaticResource RowCheck}" Cursor="Hand"
                              ToolTip="When Issued, Revit locks this revision's properties."/>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>

              <!-- Show (RevisionVisibility) — inline ComboBox -->
              <DataGridTemplateColumn Header="Show" Width="130" MinWidth="110" IsReadOnly="True">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <ComboBox SelectedItem="{Binding ShowStr, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                              ItemsSource="{Binding ShowOptions}"
                              Style="{StaticResource GridCombo}"
                              ToolTip="Cloud and Tag  /  Tag  /  None — controls what is visible in views"/>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>

              <!-- Numbering: Numeric / Alphanumeric / None — inline ComboBox -->
              <DataGridTemplateColumn Header="Numbering" Width="120" MinWidth="100" IsReadOnly="True">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <ComboBox SelectedItem="{Binding NumberingStr, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                              ItemsSource="{Binding NumberingOptions}"
                              Style="{StaticResource GridCombo}"
                              ToolTip="Numeric / Alphanumeric / None — controls how this revision is numbered"/>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>

              <DataGridTextColumn Header="Sheets" Binding="{Binding SheetCount}"
                                  Width="52" MinWidth="46" IsReadOnly="True">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="6,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="Foreground"        Value="#718096"/>
                    <Setter Property="TextAlignment"     Value="Center"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

              <DataGridTextColumn Header="Clouds" Binding="{Binding CloudCount}"
                                  Width="52" MinWidth="46" IsReadOnly="True">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="6,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="Foreground"        Value="#718096"/>
                    <Setter Property="TextAlignment"     Value="Center"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

            </DataGrid.Columns>
          </DataGrid>
        </Grid>
      </Border>

      <!-- Splitter -->
      <GridSplitter Grid.Column="1" Width="5"
                    HorizontalAlignment="Stretch" VerticalAlignment="Stretch"
                    Background="#E2E8F0" ShowsPreview="True"/>

      <!-- ─── SHEETS PANEL (RIGHT) ─── -->
      <Border Grid.Column="2" Background="White"
              BorderBrush="#E2E8F0" BorderThickness="1,0,0,0"
              Padding="16,12">
        <Grid>
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>

          <StackPanel Grid.Row="0" Orientation="Vertical" Margin="0,0,0,8">
            <TextBlock Text="Sheets" Style="{StaticResource SectionHeader}" Margin="0"/>
            <TextBlock x:Name="lbl_focus_caption"
                       Text="No revision selected — click a revision on the left."
                       Foreground="#718096" FontSize="11" Margin="0,2,0,0"
                       TextWrapping="Wrap"/>
            <StackPanel Orientation="Horizontal" Margin="0,4,0,0">
              <TextBlock Text="Legend: " Foreground="#718096" FontSize="11"
                         VerticalAlignment="Center"/>
              <CheckBox IsChecked="True" IsHitTestVisible="False" Focusable="False"
                        VerticalAlignment="Center" Margin="0,0,4,0"/>
              <TextBlock Text="on every sheet" Foreground="#718096" FontSize="11"
                         VerticalAlignment="Center" Margin="0,0,14,0"/>
              <CheckBox IsChecked="False" IsHitTestVisible="False" Focusable="False"
                        VerticalAlignment="Center" Margin="0,0,4,0"/>
              <TextBlock Text="on none" Foreground="#718096" FontSize="11"
                         VerticalAlignment="Center" Margin="0,0,14,0"/>
              <CheckBox x:Name="chk_legend_mixed" IsThreeState="True"
                        IsHitTestVisible="False" Focusable="False"
                        VerticalAlignment="Center" Margin="0,0,4,0"/>
              <TextBlock Text="mixed (some but not all)" Foreground="#718096" FontSize="11"
                         VerticalAlignment="Center"/>
            </StackPanel>
          </StackPanel>

          <!-- Search + filter bar -->
          <Grid Grid.Row="1" Margin="0,0,0,8">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <Grid Grid.Column="0">
              <TextBox x:Name="txt_search" Height="28" FontSize="12"
                       VerticalContentAlignment="Center" Padding="8,0"/>
              <TextBlock x:Name="ph_search" Text="🔍  Search sheets…"
                         Foreground="#A0AEC0" FontSize="12"
                         IsHitTestVisible="False"
                         VerticalAlignment="Center" Margin="10,0"/>
            </Grid>
            <ToggleButton Grid.Column="1" x:Name="btn_only_with"
                          Content="Only with selected revision"
                          Height="28" Padding="10,4" Margin="8,0,0,0"
                          Cursor="Hand" FontSize="11"
                          Background="#EDF2F7" Foreground="#2D3748"
                          BorderBrush="#CBD5E0" BorderThickness="1"
                          IsEnabled="False"
                          ToolTip="Show only sheets with the selected revision">
              <ToggleButton.Template>
                <ControlTemplate TargetType="ToggleButton">
                  <Border x:Name="bd" Background="{TemplateBinding Background}"
                          BorderBrush="{TemplateBinding BorderBrush}"
                          BorderThickness="{TemplateBinding BorderThickness}"
                          CornerRadius="3" Padding="{TemplateBinding Padding}">
                    <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                  </Border>
                  <ControlTemplate.Triggers>
                    <Trigger Property="IsChecked" Value="True">
                      <Setter TargetName="bd" Property="Background"  Value="#2B6CB0"/>
                      <Setter TargetName="bd" Property="BorderBrush" Value="#2B6CB0"/>
                      <Setter Property="Foreground" Value="White"/>
                    </Trigger>
                    <Trigger Property="IsEnabled" Value="False">
                      <Setter Property="Opacity" Value="0.5"/>
                    </Trigger>
                  </ControlTemplate.Triggers>
                </ControlTemplate>
              </ToggleButton.Template>
            </ToggleButton>
          </Grid>

          <!-- Sheet DataGrid — extended multi-select -->
          <DataGrid Grid.Row="2" x:Name="sheet_grid"
                    SelectionMode="Extended"
                    VirtualizingPanel.IsVirtualizingWhenGrouping="True"
                    EnableRowVirtualization="True"
                    ScrollViewer.VerticalScrollBarVisibility="Auto"
                    ScrollViewer.HorizontalScrollBarVisibility="Auto">
            <DataGrid.GroupStyle>
              <GroupStyle ContainerStyle="{StaticResource GroupHdr}"/>
            </DataGrid.GroupStyle>
            <DataGrid.Columns>
              <DataGridTemplateColumn Header="" Width="36" MinWidth="36" CanUserResize="False">
                <DataGridTemplateColumn.CellTemplate>
                  <DataTemplate>
                    <CheckBox Tag="SheetRevCheck"
                              IsChecked="{Binding HasFocusedRev, Mode=TwoWay,
                                                  UpdateSourceTrigger=PropertyChanged,
                                                  TargetNullValue={x:Null}}"
                              IsThreeState="True"
                              Style="{StaticResource RowCheck}" Cursor="Hand"
                              ToolTip="Check to add revision to this sheet. If multiple sheets are selected, applies to all."/>
                  </DataTemplate>
                </DataGridTemplateColumn.CellTemplate>
              </DataGridTemplateColumn>

              <DataGridTextColumn Header="Sheet #" Binding="{Binding SheetNumber}"
                                  Width="90" MinWidth="80" IsReadOnly="True">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="8,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="Foreground"        Value="#2B6CB0"/>
                    <Setter Property="FontWeight"        Value="SemiBold"/>
                    <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

              <DataGridTextColumn Header="Sheet Name" Binding="{Binding SheetName}"
                                  Width="*" MinWidth="120" IsReadOnly="True">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="8,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="TextTrimming"      Value="CharacterEllipsis"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>

              <DataGridTextColumn Header="Revs on Sheet" Binding="{Binding AllRevs}"
                                  Width="110" MinWidth="90" IsReadOnly="True">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="Padding"           Value="8,0"/>
                    <Setter Property="VerticalAlignment" Value="Center"/>
                    <Setter Property="Foreground"        Value="#718096"/>
                    <Setter Property="FontFamily"        Value="Consolas"/>
                    <Setter Property="FontSize"          Value="11"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>
            </DataGrid.Columns>
          </DataGrid>

          <!-- Sheet action buttons: Selected + All rows -->
          <Grid Grid.Row="3" Margin="0,8,0,0">
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>

            <!-- Row 1: Apply/Remove to SELECTED sheets -->
            <StackPanel Grid.Row="0" Orientation="Horizontal" Margin="0,0,0,4">
              <TextBlock Text="Selected:" Foreground="#4A5568" FontSize="11"
                         VerticalAlignment="Center" MinWidth="60"/>
              <Button x:Name="btn_apply_sel"  Content="✚ Apply to Selected"
                      Style="{StaticResource PrimaryButton}" Height="27" MinWidth="130"
                      IsEnabled="False"
                      ToolTip="Add selected revision to every highlighted sheet"/>
              <Button x:Name="btn_remove_sel" Content="✖ Remove from Selected"
                      Style="{StaticResource DangerButton}" Height="27" MinWidth="150"
                      IsEnabled="False"
                      ToolTip="Remove selected revision from every highlighted sheet"/>
            </StackPanel>

            <!-- Row 2: Apply/Remove ALL visible sheets + count -->
            <Grid Grid.Row="1">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <TextBlock x:Name="lbl_sheet_count" Grid.Column="0"
                         Foreground="#718096" FontSize="11" VerticalAlignment="Center"/>
              <Button x:Name="btn_apply_all"  Grid.Column="1" Content="Apply to All"
                      Style="{StaticResource SecondaryButton}" Height="27" MinWidth="100"
                      IsEnabled="False"
                      ToolTip="Add selected revision to every sheet currently shown"/>
              <Button x:Name="btn_remove_all" Grid.Column="2" Content="Remove from All"
                      Style="{StaticResource DangerButton}" Height="27" MinWidth="120"
                      IsEnabled="False"
                      ToolTip="Remove selected revision from every sheet currently shown"/>
            </Grid>
          </Grid>

        </Grid>
      </Border>
    </Grid>

    <!-- STATUS BAR -->
    <Border Grid.Row="3" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0"
            Padding="16,8">
      <TextBlock x:Name="lbl_status" Foreground="#4A5568"
                 FontSize="12" VerticalAlignment="Center"/>
    </Border>
  </Grid>
</Window>
"""


# ═══════════════════════════════════════════════════════════════
# FIND CLOUDS DIALOG
# ═══════════════════════════════════════════════════════════════

FIND_CLOUDS_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Find Clouds for Revision" Width="780" Height="540"
        WindowStartupLocation="CenterOwner"
        Background="#F7FAFC" Foreground="#1A202C"
        FontFamily="Segoe UI" FontSize="12"
        ResizeMode="CanResizeWithGrip">
  <Window.Resources>
""" + SHARED_RESOURCES + """
  </Window.Resources>

  <DockPanel>
    <Border DockPanel.Dock="Top" Background="#2D3748" Padding="20,12">
      <Grid>
        <StackPanel>
          <TextBlock x:Name="lbl_title" Text="Find Clouds for Revision"
                     Foreground="White" FontSize="18" FontWeight="Bold"/>
          <TextBlock x:Name="lbl_sub"
                     Text="Double-click a row to zoom to the cloud."
                     Foreground="#CBD5E0" FontSize="11" Margin="0,2,0,0"/>
        </StackPanel>
      </Grid>
    </Border>
    <Border DockPanel.Dock="Bottom" Background="White"
            BorderBrush="#E2E8F0" BorderThickness="0,1,0,0" Padding="20,12">
      <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
        <Button x:Name="btn_zoom"  Content="Zoom To" Style="{StaticResource SecondaryButton}"/>
        <Button x:Name="btn_close" Content="Close"   Style="{StaticResource PrimaryButton}"/>
      </StackPanel>
    </Border>
    <Grid Margin="20,16">
      <DataGrid x:Name="cloud_grid">
        <DataGrid.Columns>
          <DataGridTextColumn Header="Sheet #"  Binding="{Binding SheetNum}"  Width="90"   IsReadOnly="True"/>
          <DataGridTextColumn Header="Sheet"    Binding="{Binding SheetName}" Width="2*"   MinWidth="160" IsReadOnly="True"/>
          <DataGridTextColumn Header="View"     Binding="{Binding ViewName}"  Width="2*"   MinWidth="160" IsReadOnly="True"/>
          <DataGridTextColumn Header="Comment"  Binding="{Binding Comment}"   Width="2.5*" MinWidth="180" IsReadOnly="True"/>
        </DataGrid.Columns>
      </DataGrid>
    </Grid>
  </DockPanel>
</Window>
"""


class FindCloudsDialog(object):
    def __init__(self, owner, revision_item, doc):
        self._doc = doc
        self._rev = revision_item.Revision
        rows = self._collect_rows(self._rev)

        w = Markup.XamlReader.Parse(FIND_CLOUDS_XAML)
        self._w = w
        w.Owner = owner

        w.FindName("lbl_title").Text = "Clouds for Revision {0} — {1}".format(
            revision_item.SeqNum, revision_item.Description or "(no description)")
        w.FindName("lbl_sub").Text = "{0} cloud(s) found.  Double-click or use Zoom To.".format(len(rows))

        self._grid = w.FindName("cloud_grid")
        items = ObservableCollection[object]()
        for r in rows:
            items.Add(r)
        self._grid.ItemsSource = items

        self._grid.MouseDoubleClick += self._on_zoom
        w.FindName("btn_zoom").Click  += self._on_zoom
        w.FindName("btn_close").Click += lambda s, e: w.Close()
        w.ShowDialog()

    def _collect_rows(self, revision):
        rid = revision.Id
        rows = []
        view_to_sheet = {}
        for s in get_all_sheets(self._doc):
            try:
                for vp_id in s.GetAllViewports():
                    vp = self._doc.GetElement(vp_id)
                    if vp:
                        view_to_sheet[eid_int(vp.ViewId)] = s
                view_to_sheet[eid_int(s.Id)] = s
            except Exception:
                pass
        for cloud in get_all_revision_clouds(self._doc):
            try:
                if cloud.RevisionId != rid:
                    continue
            except Exception:
                continue
            view = self._doc.GetElement(cloud.OwnerViewId)
            view_name = view.Name if view is not None and hasattr(view, "Name") else ""
            sheet = view_to_sheet.get(eid_int(cloud.OwnerViewId))
            sheet_num  = sheet.SheetNumber if sheet else "—"
            sheet_name = sheet.Name if sheet else ""
            try:
                comment = cloud.get_Parameter(
                    BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS).AsString() or ""
            except Exception:
                comment = ""
            rows.append(CloudListRow(cloud, sheet_num, sheet_name, view_name, comment))
        return rows

    def _on_zoom(self, sender, e):
        sel = self._grid.SelectedItem
        if sel is None:
            return
        try:
            cloud = sel.Cloud
            view = self._doc.GetElement(cloud.OwnerViewId)
            if view is not None:
                uidoc.ActiveView = view
                uidoc.ShowElements(cloud.Id)
        except Exception as ex:
            TaskDialog.Show("Zoom To Cloud", str(ex))


# ═══════════════════════════════════════════════════════════════
# MAIN WINDOW
# ═══════════════════════════════════════════════════════════════

class RevisionsManagerWindow(object):
    def __init__(self):
        self._all_revs   = []
        self._all_sheets = []
        self._filtered   = []
        self._focused    = None

        # Prevent SelectionChanged from clearing focus during programmatic reloads
        self._suppress_sel_change = False
        # Track whether user clicked an already-selected row (for deselect-on-click)
        self._click_on_selected   = False

        w = Markup.XamlReader.Parse(MAIN_XAML)
        self._w = w

        # Parent window to Revit so it doesn't float as an independent taskbar window
        try:
            hwnd = System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle
            WindowInteropHelper(w).Owner = hwnd
        except Exception:
            pass

        # Find controls
        self._lbl_project    = w.FindName("lbl_project")
        self._rev_grid       = w.FindName("rev_grid")
        self._sheet_grid     = w.FindName("sheet_grid")
        self._txt_search     = w.FindName("txt_search")
        self._ph_search      = w.FindName("ph_search")
        self._btn_only_with  = w.FindName("btn_only_with")
        self._lbl_focus_cap  = w.FindName("lbl_focus_caption")
        self._lbl_sheet_ct   = w.FindName("lbl_sheet_count")
        self._lbl_status     = w.FindName("lbl_status")

        # The legend's "mixed" checkbox needs to render in the indeterminate
        # state so the visual matches what a real tri-state row looks like.
        # IsThreeState=True alone starts the box at False; push None here.
        try:
            chk_mixed = w.FindName("chk_legend_mixed")
            if chk_mixed is not None:
                chk_mixed.IsChecked = None
        except Exception:
            pass

        self._btn_new        = w.FindName("btn_new")
        self._btn_delete     = w.FindName("btn_delete")
        self._btn_up         = w.FindName("btn_up")
        self._btn_down       = w.FindName("btn_down")
        self._btn_find       = w.FindName("btn_find")
        self._btn_apply_sel  = w.FindName("btn_apply_sel")
        self._btn_remove_sel = w.FindName("btn_remove_sel")
        self._btn_apply_all  = w.FindName("btn_apply_all")
        self._btn_remove_all = w.FindName("btn_remove_all")
        self._cb_scope       = w.FindName("cb_scope")

        try:
            proj_info = doc.ProjectInformation
            proj_name = proj_info.Name or doc.Title
            if proj_name:
                self._lbl_project.Text = (
                    "Create, edit, reorder revisions and manage which sheets they appear on.    —    "
                    + proj_name)
        except Exception:
            pass

        # Initial load
        self._load_revisions()
        self._load_sheets()
        self._refresh_rev_counts()
        self._apply_rev_view()
        self._apply_sheet_view()
        self._update_focus_caption()
        self._update_status()
        self._sync_scope_combo()

        # Wire events
        self._txt_search.TextChanged    += self._on_search
        self._txt_search.TextChanged    += self._on_search_placeholder
        self._btn_only_with.Checked     += lambda s, e: self._apply_filters()
        self._btn_only_with.Unchecked   += lambda s, e: self._apply_filters()

        self._rev_grid.SelectionChanged        += self._on_rev_selection_changed
        self._rev_grid.CellEditEnding          += self._on_rev_cell_edit_ending
        # Click-to-deselect: record whether the clicked row was already selected
        self._rev_grid.PreviewMouseLeftButtonDown += self._on_rev_preview_mouse_down
        self._rev_grid.PreviewMouseLeftButtonUp   += self._on_rev_preview_mouse_up

        self._btn_new.Click        += self._on_new_rev
        self._btn_delete.Click     += self._on_delete_rev
        self._btn_up.Click         += lambda s, e: self._on_reorder(-1)
        self._btn_down.Click       += lambda s, e: self._on_reorder(+1)
        self._btn_find.Click       += self._on_find_clouds
        self._btn_apply_sel.Click  += lambda s, e: self._bulk_apply_sel(True)
        self._btn_remove_sel.Click += lambda s, e: self._bulk_apply_sel(False)
        self._btn_apply_all.Click  += lambda s, e: self._bulk_apply_all(True)
        self._btn_remove_all.Click += lambda s, e: self._bulk_apply_all(False)
        self._cb_scope.SelectionChanged += self._on_scope_changed

        # Sheet checkbox click handler (per-row + group)
        self._sheet_grid.AddHandler(
            CheckBox.ClickEvent,
            RoutedEventHandler(self._on_sheet_checkbox_click))

        # Update "Apply to Selected" button state when sheet selection changes
        self._sheet_grid.SelectionChanged += self._on_sheet_selection_changed

        w.ShowDialog()

    # ── Data loading ─────────────────────────────────────────

    def _load_revisions(self):
        self._all_revs = []
        for r in get_all_revisions(doc):
            try:
                self._all_revs.append(RevisionItem(r, doc))
            except Exception:
                pass

    def _load_sheets(self):
        self._all_sheets = []
        for s in get_all_sheets(doc):
            try:
                self._all_sheets.append(SheetRevItem(s, doc))
            except Exception:
                pass
        self._filtered = list(self._all_sheets)

    def _refresh_rev_counts(self):
        rev_to_sheet_count = {}
        for s in self._all_sheets:
            try:
                for rid in s.Sheet.GetAllRevisionIds():
                    key = eid_int(rid)
                    rev_to_sheet_count[key] = rev_to_sheet_count.get(key, 0) + 1
            except Exception:
                pass
        rev_to_cloud_count = {}
        for c in get_all_revision_clouds(doc):
            try:
                key = eid_int(c.RevisionId)
                rev_to_cloud_count[key] = rev_to_cloud_count.get(key, 0) + 1
            except Exception:
                pass
        for rev in self._all_revs:
            rev.SheetCount = rev_to_sheet_count.get(eid_int(rev.ElementId), 0)
            rev.CloudCount = rev_to_cloud_count.get(eid_int(rev.ElementId), 0)

    def _apply_rev_view(self):
        col = ObservableCollection[RevisionItem]()
        for r in self._all_revs:
            col.Add(r)
        view = CollectionViewSource.GetDefaultView(col)
        view.SortDescriptions.Clear()
        view.SortDescriptions.Add(SortDescription("SeqNum", ListSortDirection.Ascending))
        view.Refresh()
        self._rev_grid.ItemsSource = view

    def _apply_sheet_view(self):
        col = ObservableCollection[SheetRevItem]()
        for s in self._filtered:
            col.Add(s)
        view = CollectionViewSource.GetDefaultView(col)
        view.GroupDescriptions.Clear()
        view.GroupDescriptions.Add(PropertyGroupDescription("GroupKey"))
        view.SortDescriptions.Clear()
        view.SortDescriptions.Add(SortDescription("SheetNumber", ListSortDirection.Ascending))
        view.Refresh()
        self._sheet_grid.ItemsSource = view
        self._refresh_group_selectors()

    # ── Sheet filtering ──────────────────────────────────────

    def _on_search(self, sender, e):
        self._apply_filters()

    def _on_search_placeholder(self, sender, e):
        self._ph_search.Visibility = (
            Visibility.Collapsed if self._txt_search.Text else Visibility.Visible)

    def _apply_filters(self):
        q = (self._txt_search.Text or "").strip().lower()
        items = list(self._all_sheets)
        if q:
            items = [s for s in items
                     if q in s.SheetNumber.lower() or q in s.SheetName.lower()]
        if self._btn_only_with.IsChecked and self._focused is not None:
            items = [s for s in items if s.HasFocusedRev is not False]
        self._filtered = items
        self._apply_sheet_view()
        self._update_status()

    # ── Revision focus / click-to-deselect ──────────────────

    def _on_rev_preview_mouse_down(self, sender, e):
        """Record if the click lands on the already-focused (selected) row."""
        self._click_on_selected = False
        if self._focused is None:
            return
        try:
            container = self._rev_grid.ItemContainerGenerator.ContainerFromItem(self._focused)
            if container is not None and container.IsMouseOver:
                self._click_on_selected = True
        except Exception:
            pass

    def _on_rev_preview_mouse_up(self, sender, e):
        """If the mouse went down AND up on the already-selected row, deselect it."""
        if not self._click_on_selected or self._focused is None:
            self._click_on_selected = False
            return
        self._click_on_selected = False
        try:
            container = self._rev_grid.ItemContainerGenerator.ContainerFromItem(self._focused)
            if container is not None and container.IsMouseOver:
                self._suppress_sel_change = True
                self._rev_grid.SelectedItem = None
                self._suppress_sel_change = False
                old = self._focused
                self._focused = None
                if old is not None:
                    old.IsFocused = False
                self._recompute_sheet_states()
                self._update_focus_caption()
                self._update_status()
                self._apply_filters()
        except Exception:
            pass

    def _on_rev_selection_changed(self, sender, e):
        if self._suppress_sel_change:
            return
        sel = self._rev_grid.SelectedItem
        if self._focused is not None and self._focused is not sel:
            self._focused.IsFocused = False
        if isinstance(sel, RevisionItem):
            self._focused = sel
            sel.IsFocused = True
        else:
            self._focused = None
        self._recompute_sheet_states()
        self._update_focus_caption()
        self._update_status()
        self._apply_filters()

    def _recompute_sheet_states(self):
        if self._focused is None:
            for s in self._all_sheets:
                s.HasFocusedRev = False
            return
        rid_key = eid_int(self._focused.ElementId)
        for s in self._all_sheets:
            try:
                addl    = list(s.Sheet.GetAdditionalRevisionIds())
                all_ids = list(s.Sheet.GetAllRevisionIds())
                in_addl = any(eid_int(r) == rid_key for r in addl)
                in_all  = any(eid_int(r) == rid_key for r in all_ids)
                if in_addl:
                    s.HasFocusedRev = True
                elif in_all:
                    s.HasFocusedRev = None   # cloud-pinned, read-only
                else:
                    s.HasFocusedRev = False
            except Exception:
                s.HasFocusedRev = False
        self._refresh_group_selectors()

    def _update_focus_caption(self):
        has = self._focused is not None
        self._btn_delete.IsEnabled     = has
        self._btn_up.IsEnabled         = has
        self._btn_down.IsEnabled       = has
        self._btn_find.IsEnabled       = has and (self._focused.CloudCount > 0)
        self._btn_apply_all.IsEnabled  = has
        self._btn_remove_all.IsEnabled = has
        self._btn_only_with.IsEnabled  = has
        # Apply-to-selected depends on having a focus AND selected sheets
        self._refresh_sel_buttons()

        if not has:
            self._lbl_focus_cap.Text = "No revision selected — click a revision on the left."
            self._lbl_focus_cap.Foreground = SolidColorBrush(Color.FromRgb(0x71, 0x80, 0x96))
            return
        f = self._focused
        self._lbl_focus_cap.Text = (
            "Selected revision: Rev {0}  •  {1}  •  {2}  ({3} sheet(s), {4} cloud(s))".format(
                f.SeqNum,
                f.RevDate or "(no date)",
                f.Description or "(no description)",
                f.SheetCount, f.CloudCount))
        self._lbl_focus_cap.Foreground = SolidColorBrush(Color.FromRgb(0x2B, 0x6C, 0xB0))

    def _on_sheet_selection_changed(self, sender, e):
        self._refresh_sel_buttons()

    def _refresh_sel_buttons(self):
        """Enable/disable Apply/Remove to Selected based on sheet selection count."""
        has_focus = self._focused is not None
        sel_count = 0
        try:
            sel_count = sum(1 for item in self._sheet_grid.SelectedItems
                            if isinstance(item, SheetRevItem))
        except Exception:
            pass
        enabled = has_focus and sel_count > 0
        self._btn_apply_sel.IsEnabled  = enabled
        self._btn_remove_sel.IsEnabled = enabled

    # ── Status bar ───────────────────────────────────────────

    def _update_status(self):
        rev_count = len(self._all_revs)
        shown     = len(self._filtered)
        total     = len(self._all_sheets)
        self._lbl_status.Text = (
            "{0} revision(s)    |    {1} sheet(s) shown  /  {2} total".format(
                rev_count, shown, total))

    # ── Bulk sheet operations ─────────────────────────────────

    def _bulk_apply_sel(self, want_on):
        """Apply / remove focused revision to the highlighted sheet rows."""
        if self._focused is None:
            return
        sel = [item for item in self._sheet_grid.SelectedItems
               if isinstance(item, SheetRevItem)]
        if not sel:
            return
        self._toggle_sheets(sel, want_on)

    def _bulk_apply_all(self, want_on):
        """Apply / remove focused revision to every visible sheet."""
        if self._focused is None:
            return
        self._toggle_sheets(list(self._filtered), want_on)

    # ── Full reload (preserves focused revision) ─────────────

    def _reload_all(self, focus_rev_id=None):
        """Reload revisions + sheets, keeping the given revision focused."""
        target_id = (eid_int(focus_rev_id) if focus_rev_id is not None
                     else (eid_int(self._focused.ElementId)
                           if self._focused else None))
        self._suppress_sel_change = True
        try:
            self._load_revisions()
            self._load_sheets()
            self._refresh_rev_counts()
            self._apply_rev_view()
            if target_id is not None:
                for item in self._all_revs:
                    if eid_int(item.ElementId) == target_id:
                        item.IsFocused  = True
                        self._focused   = item
                        self._rev_grid.SelectedItem = item
                        self._rev_grid.ScrollIntoView(item)
                        break
            self._recompute_sheet_states()
            self._apply_sheet_view()
        finally:
            self._suppress_sel_change = False
        self._update_focus_caption()
        self._update_status()

    # ── Inline edit ──────────────────────────────────────────

    def _on_rev_cell_edit_ending(self, sender, e):
        if e.EditAction == DataGridEditAction.Commit:
            self._w.Dispatcher.BeginInvoke(
                DispatcherPriority.Background,
                Action(self._after_rev_edit))

    def _after_rev_edit(self):
        self._update_focus_caption()

    # ── New / Delete ─────────────────────────────────────────

    def _on_new_rev(self, sender, e):
        try:
            with Transaction(doc, "Revisions: Create New Revision") as t:
                t.Start()
                new_rev = Revision.Create(doc)
                try: new_rev.Description = "New Revision"
                except Exception: pass
                t.Commit()
        except Exception as ex:
            TaskDialog.Show("New Revision", str(ex))
            return
        self._reload_all(focus_rev_id=new_rev.Id)

    def _on_delete_rev(self, sender, e):
        if self._focused is None:
            return
        f = self._focused
        warn = ""
        if f.SheetCount or f.CloudCount:
            warn = "\n\nThis revision is on {0} sheet(s) and has {1} cloud(s).".format(
                f.SheetCount, f.CloudCount)
            warn += "\nDeleting will remove its assignment from sheets and delete its clouds."
        result = TaskDialog.Show(
            "Delete Revision",
            "Delete revision {0} — '{1}'?{2}".format(
                f.SeqNum, f.Description or "(no description)", warn),
            TaskDialogCommonButtons.Yes | TaskDialogCommonButtons.No)
        if result != TaskDialogResult.Yes:
            return
        try:
            with Transaction(doc, "Revisions: Delete Revision") as t:
                t.Start()
                doc.Delete(f.ElementId)
                t.Commit()
        except Exception as ex:
            TaskDialog.Show("Delete Revision", str(ex))
            return
        self._focused = None
        self._reload_all()

    # ── Reorder ──────────────────────────────────────────────

    def _on_reorder(self, delta):
        if self._focused is None:
            return
        focused_id = self._focused.ElementId
        ordered = sorted(self._all_revs, key=lambda r: r.SeqNum)
        ids = [r.ElementId for r in ordered]
        focused_key = eid_int(focused_id)
        try:
            idx = next(i for i, eid in enumerate(ids) if eid_int(eid) == focused_key)
        except StopIteration:
            return
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(ids):
            return
        ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
        try:
            with Transaction(doc, "Revisions: Reorder") as t:
                t.Start()
                Revision.ReorderRevisionSequence(doc, List[ElementId](ids))
                t.Commit()
        except Exception as ex:
            TaskDialog.Show("Reorder", str(ex))
            return
        # Reload and keep selection on the same revision
        self._reload_all(focus_rev_id=focused_id)

    # ── Find Clouds ───────────────────────────────────────────

    def _on_find_clouds(self, sender, e):
        if self._focused is None:
            return
        FindCloudsDialog(self._w, self._focused, doc)

    # ── Numbering scope (Per Project / Per Sheet) ────────────

    def _sync_scope_combo(self):
        """Set the scope ComboBox to match the project's current setting,
        without firing a change event back at us."""
        try:
            current = get_numbering_scope(doc)
        except Exception:
            current = "Per Project"
        idx = 1 if current == "Per Sheet" else 0
        # Suppress the SelectionChanged side-effect during this initial sync.
        self._scope_syncing = True
        try:
            self._cb_scope.SelectedIndex = idx
        finally:
            self._scope_syncing = False

    def _on_scope_changed(self, sender, e):
        if getattr(self, "_scope_syncing", False):
            return
        item = self._cb_scope.SelectedItem
        if item is None:
            return
        new_scope = str(item.Content) if hasattr(item, "Content") else str(item)
        try:
            with Transaction(doc, "Revisions: Set Numbering Scope") as t:
                t.Start()
                set_numbering_scope(doc, new_scope)
                t.Commit()
            # Renumbering may have changed Rev # on every revision — reload.
            self._reload_all()
        except Exception as ex:
            TaskDialog.Show("Numbering Scope",
                "Could not set numbering scope:\n{0}".format(str(ex)))
            self._sync_scope_combo()

    # ── Sheet checkbox / group selector ──────────────────────

    def _on_sheet_checkbox_click(self, sender, e):
        src = e.OriginalSource
        if not isinstance(src, CheckBox):
            return
        tag = str(src.Tag) if src.Tag is not None else ""

        # 1. Group-header tri-state selector
        if tag == "GroupSelector":
            grp = src.DataContext
            if self._focused is None:
                src.IsChecked = False
                e.Handled = True
                return
            new_state = bool(src.IsChecked) if src.IsChecked is not None else False
            if grp is not None and hasattr(grp, "Items"):
                items_in_group = [it for it in grp.Items if isinstance(it, SheetRevItem)]
                self._toggle_sheets(items_in_group, new_state)
            e.Handled = True
            return

        # 2. Per-row sheet checkbox
        if tag == "SheetRevCheck":
            sheet_item = src.DataContext
            if not isinstance(sheet_item, SheetRevItem):
                return
            if self._focused is None:
                sheet_item.HasFocusedRev = False
                src.IsChecked = False
                TaskDialog.Show("Revisions Manager",
                    "Click a revision on the left first to choose which one to apply.")
                e.Handled = True
                return

            new_state = src.IsChecked

            # Three-state checkbox cycle:  False → True → None → False → …
            #   new_state = True  : was False  → user wants to ADD revision
            #   new_state = None  : was True   → user wants to REMOVE revision
            #   new_state = False : was None   → was cloud-pinned (read-only) → revert
            if new_state is False:
                # Clicked a cloud-pinned (indeterminate) row — can't change, put it back
                self._recompute_one_sheet(sheet_item)
                e.Handled = True
                return

            # True  → add;  None (was checked) → remove
            want_on = (new_state is True)

            # When multiple sheets are selected, apply the same intent to all of them.
            try:
                selected = [it for it in self._sheet_grid.SelectedItems
                            if isinstance(it, SheetRevItem)]
            except Exception:
                selected = []

            if sheet_item in selected and len(selected) > 1:
                self._toggle_sheets(selected, want_on)
            else:
                self._toggle_sheets([sheet_item], want_on)
            e.Handled = True

    def _refresh_group_selectors(self):
        def _do():
            try:
                self._walk_group_checkboxes(self._sheet_grid)
            except Exception:
                pass
        self._w.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(_do))

    def _walk_group_checkboxes(self, root):
        from System.Windows.Media import VisualTreeHelper
        if root is None:
            return
        n = VisualTreeHelper.GetChildrenCount(root)
        for i in range(n):
            child = VisualTreeHelper.GetChild(root, i)
            if isinstance(child, CheckBox) and str(child.Tag) == "GroupSelector":
                grp = child.DataContext
                if grp is not None and hasattr(grp, "Items"):
                    states = [it.HasFocusedRev for it in grp.Items
                              if isinstance(it, SheetRevItem)]
                    if not states:
                        child.IsChecked = False
                    elif all(s is True for s in states):
                        child.IsChecked = True
                    elif all(s is False for s in states):
                        child.IsChecked = False
                    else:
                        child.IsChecked = None
            self._walk_group_checkboxes(child)

    def _recompute_one_sheet(self, sheet_item):
        if self._focused is None:
            sheet_item.HasFocusedRev = False
            return
        rid_key = eid_int(self._focused.ElementId)
        try:
            addl    = list(sheet_item.Sheet.GetAdditionalRevisionIds())
            all_ids = list(sheet_item.Sheet.GetAllRevisionIds())
            in_addl = any(eid_int(r) == rid_key for r in addl)
            in_all  = any(eid_int(r) == rid_key for r in all_ids)
            if in_addl:
                sheet_item.HasFocusedRev = True
            elif in_all:
                sheet_item.HasFocusedRev = None
            else:
                sheet_item.HasFocusedRev = False
        except Exception:
            sheet_item.HasFocusedRev = False

    def _toggle_sheets(self, sheet_items, want_on):
        if self._focused is None or not sheet_items:
            return
        rid     = self._focused.ElementId
        rid_key = eid_int(rid)
        cloud_pinned = []
        try:
            with Transaction(doc, "Revisions: Update Sheet Assignments") as t:
                t.Start()
                for s in sheet_items:
                    try:
                        addl    = list(s.Sheet.GetAdditionalRevisionIds())
                        all_ids = list(s.Sheet.GetAllRevisionIds())
                        in_addl = any(eid_int(r) == rid_key for r in addl)
                        in_all  = any(eid_int(r) == rid_key for r in all_ids)
                        on_via_cloud_only = in_all and not in_addl
                        if want_on:
                            if not in_all:
                                addl.append(rid)
                                s.Sheet.SetAdditionalRevisionIds(List[ElementId](addl))
                        else:
                            if in_addl:
                                addl = [r for r in addl if eid_int(r) != rid_key]
                                s.Sheet.SetAdditionalRevisionIds(List[ElementId](addl))
                            if on_via_cloud_only:
                                cloud_pinned.append(
                                    "{0}  •  {1}".format(s.SheetNumber, s.SheetName))
                    except Exception as inner:
                        TaskDialog.Show("Revisions Manager",
                            "Sheet {0}: {1}".format(s.SheetNumber, str(inner)))
                t.Commit()
        except Exception as ex:
            TaskDialog.Show("Revisions Manager", str(ex))
            return

        for s in sheet_items:
            self._recompute_one_sheet(s)
            s.refresh_summary()
        try:
            self._focused.SheetCount = len([
                s for s in self._all_sheets if s.HasFocusedRev is not False])
        except Exception:
            pass
        self._refresh_group_selectors()
        self._update_focus_caption()
        self._update_status()

        if cloud_pinned:
            preview = "\n".join(cloud_pinned[:10])
            extra = "" if len(cloud_pinned) <= 10 else "\n…and {0} more.".format(
                len(cloud_pinned) - 10)
            TaskDialog.Show(
                "Revisions — could not remove",
                "The following sheet(s) still show this revision because a revision cloud "
                "is placed directly on them.\nRemove the cloud(s) first, then try again.\n\n"
                + preview + extra)


# ═══════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════
RevisionsManagerWindow()
