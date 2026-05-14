# -*- coding: utf-8 -*-
"""Bulk PDF Sheets - one sheet per PDF page, page dropped centered on each.

Pick a multi-page PDF, the title block to use, and the first sheet number.
Paste one sheet name per line for the names. Each line becomes a new sheet:
its number is the first-number with its trailing digits incremented (FA-001
-> FA-002 -> ...), and the matching PDF page is imported at 300 DPI and
placed centered on the title block.

Intended as a one-night batch importer. Everything happens inside a single
Revit transaction so a failure rolls the whole thing back.
"""

__title__ = 'Bulk PDF\nSheets'
__author__ = 'Nathaniel'

import os
import re
import traceback

from pyrevit import revit, DB, forms, script

import dbhms_ui
import dbhms_telemetry


SCRIPT_DIR = os.path.dirname(__file__)
FORM_XAML = os.path.join(SCRIPT_DIR, 'BulkPdfSheetsForm.xaml')

doc = revit.doc
output = script.get_output()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_TRAILING_DIGITS = re.compile(r'(\d+)(\D*)$')


def increment_sheet_numbers(first, count):
    """Return a list of `count` sheet numbers based on `first`.

    Trailing run of digits is incremented and zero-padded to the same width.
    If `first` has no digits, every entry is `first` with an index appended.
    """
    m = _TRAILING_DIGITS.search(first or '')
    if not m:
        return ['{}-{}'.format(first or 'SHEET', i + 1) for i in range(count)]
    digits = m.group(1)
    tail = m.group(2)
    width = len(digits)
    start = int(digits)
    prefix = first[:m.start(1)]
    return [
        '{}{:0{}d}{}'.format(prefix, start + i, width, tail)
        for i in range(count)
    ]


def get_title_block_symbols():
    """Return all title block FamilySymbols sorted by family name + type name."""
    symbols = list(
        DB.FilteredElementCollector(doc)
          .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
          .WhereElementIsElementType()
    )

    def _name(sym):
        try:
            return sym.Name or ''
        except Exception:
            return ''

    def key(sym):
        fam = sym.Family.Name if sym.Family else ''
        return (fam.lower(), _name(sym).lower())

    symbols.sort(key=key)
    return symbols


def title_block_label(sym):
    fam = sym.Family.Name if sym.Family else '?'
    try:
        name = sym.Name or '?'
    except Exception:
        name = '?'
    return '{} : {}'.format(fam, name)


def existing_sheet_numbers():
    """Set of sheet numbers already in the project."""
    out = set()
    for sh in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
        try:
            out.add(sh.SheetNumber)
        except Exception:
            pass
    return out


def title_block_center_on(sheet):
    """Center of the title block instance in sheet (paper) coordinates.

    Falls back to XYZ.Zero if nothing is found.
    """
    tbs = list(
        DB.FilteredElementCollector(doc, sheet.Id)
          .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
          .WhereElementIsNotElementType()
    )
    if not tbs:
        return DB.XYZ(0, 0, 0)
    tb = tbs[0]
    bb = tb.get_BoundingBox(sheet)
    if bb is None:
        return DB.XYZ(0, 0, 0)
    return DB.XYZ((bb.Min.X + bb.Max.X) / 2.0,
                  (bb.Min.Y + bb.Max.Y) / 2.0,
                  0.0)


def parse_names(blob):
    """Split a multiline names blob into a list. Empty lines kept as ''."""
    if not blob:
        return []
    # Normalize line endings, then split. Don't strip the list - trailing
    # blank lines mean the user didn't type names for those sheets.
    lines = blob.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    # Trim trailing fully-blank lines to be friendly about copy/paste
    # leaving a trailing newline.
    while lines and lines[-1].strip() == '':
        lines.pop()
    return [ln.strip() for ln in lines]


def count_pdf_pages(pdf_path):
    """Best-effort PDF page count using cheap byte heuristics.

    Returns -1 if we can't determine it (compressed object streams, etc.).
    """
    try:
        with open(pdf_path, 'rb') as f:
            raw = f.read()
        # latin-1 round-trips every byte 0-255 to a single char, so a str
        # regex search works on the result in both IronPython 2.7 and CPython 3.
        data = raw.decode('latin-1')
    except Exception:
        return -1
    # 1) Try '/Type/Page' (not /Pages). Works for uncompressed PDFs.
    matches = re.findall(r'/Type\s*/Page(?![sM])', data)
    if matches:
        return len(matches)
    # 2) Try the '/Count' of the root Pages tree.
    m = re.search(r'/Type\s*/Pages[^>]*?/Count\s+(\d+)', data)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return -1
    return -1


def open_file_dialog():
    """Open a Win32 file picker for PDFs. Returns selected path or None."""
    import clr  # noqa: F401
    clr.AddReference('System.Windows.Forms')
    from System.Windows.Forms import OpenFileDialog, DialogResult
    dlg = OpenFileDialog()
    dlg.Title = 'Select PDF'
    dlg.Filter = 'PDF files (*.pdf)|*.pdf|All files (*.*)|*.*'
    dlg.Multiselect = False
    if dlg.ShowDialog() == DialogResult.OK:
        return dlg.FileName
    return None


# --------------------------------------------------------------------------
# PDF placement (Revit 2024+: ImageType + ImageInstance with PageNumber)
# --------------------------------------------------------------------------

def make_image_type(pdf_path, page_number, resolution_dpi, link=False):
    """Create one ImageType pointing at page `page_number` of the PDF."""
    source = DB.ImageTypeSource.Link if link else DB.ImageTypeSource.Import
    opts = DB.ImageTypeOptions(pdf_path, link, source)
    # PageNumber is 1-indexed.
    try:
        opts.PageNumber = page_number
    except AttributeError:
        # Older Revit API named it differently; bail loudly.
        raise RuntimeError(
            'This Revit version does not expose ImageTypeOptions.PageNumber. '
            'Bulk PDF Sheets requires Revit 2024 or newer.')
    try:
        opts.Resolution = resolution_dpi
    except AttributeError:
        pass
    return DB.ImageType.Create(doc, opts)


def place_image_centered(sheet, image_type, center):
    """Place an ImageInstance of `image_type` centered on `sheet`.

    ImageInstance.Create signature: (Document, View, ElementId typeId,
    ImagePlacementOptions) - the second arg is the View object itself,
    NOT the view's ElementId.
    """
    place_opts = DB.ImagePlacementOptions(center, DB.BoxPlacement.Center)
    return DB.ImageInstance.Create(doc, sheet, image_type.Id, place_opts)


# --------------------------------------------------------------------------
# Form
# --------------------------------------------------------------------------

class BulkPdfSheetsForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)

        self.confirmed = False
        self._pdf_path = None
        self._pdf_pages = -1  # -1 = unknown
        self._title_blocks = get_title_block_symbols()

        # Populate title block combo.
        self.cmb_titleblock.Items.Clear()
        for sym in self._title_blocks:
            self.cmb_titleblock.Items.Add(title_block_label(sym))
        if self._title_blocks:
            self.cmb_titleblock.SelectedIndex = 0

        # Wire events.
        self.btn_browse.Click += self._on_browse
        self.btn_cancel.Click += self._on_cancel
        self.btn_run.Click += self._on_run
        self.txt_first_number.TextChanged += self._on_anything_changed
        self.txt_names.TextChanged += self._on_anything_changed

        self._update_previews()

    # -- handlers --

    def _on_browse(self, sender, e):
        path = open_file_dialog()
        if not path:
            return
        self._pdf_path = path
        self.txt_pdf_path.Text = path
        self._pdf_pages = count_pdf_pages(path)
        self._update_previews()

    def _on_cancel(self, sender, e):
        self.confirmed = False
        self.Close()

    def _on_run(self, sender, e):
        # Validate before closing so the user can fix and retry.
        err = self._validate()
        if err:
            dbhms_ui.info(err, title='Bulk PDF Sheets')
            return
        self.confirmed = True
        self.Close()

    def _on_anything_changed(self, sender, e):
        self._update_previews()

    # -- live preview --

    def _update_previews(self):
        names = parse_names(self.txt_names.Text)
        count = len(names)

        # Names count + warning if PDF page count disagrees.
        if count == 0:
            self.txt_names_count.Text = 'No names typed yet'
        else:
            self.txt_names_count.Text = '{} name{}'.format(
                count, '' if count == 1 else 's')
            if self._pdf_pages > 0 and self._pdf_pages != count:
                self.txt_names_count.Text += '  (PDF has {} page{})'.format(
                    self._pdf_pages,
                    '' if self._pdf_pages == 1 else 's')

        # PDF info line.
        if not self._pdf_path:
            self.txt_pdf_info.Text = 'No PDF selected.'
        elif self._pdf_pages > 0:
            self.txt_pdf_info.Text = '{} page{} detected.'.format(
                self._pdf_pages,
                '' if self._pdf_pages == 1 else 's')
        else:
            self.txt_pdf_info.Text = 'Page count could not be detected automatically.'

        # Numbering preview (first / second / last).
        first = (self.txt_first_number.Text or '').strip()
        if count and first:
            nums = increment_sheet_numbers(first, count)
            if count == 1:
                self.txt_numbering_preview.Text = 'Will create: {}'.format(nums[0])
            elif count == 2:
                self.txt_numbering_preview.Text = 'Will create: {} -> {}'.format(
                    nums[0], nums[1])
            else:
                self.txt_numbering_preview.Text = 'Will create: {} -> {} -> ... -> {}'.format(
                    nums[0], nums[1], nums[-1])
        else:
            self.txt_numbering_preview.Text = ''

        # Status line in footer.
        if count and self._pdf_path and self.cmb_titleblock.SelectedIndex >= 0:
            self.txt_status.Text = 'Ready to create {} sheet{}.'.format(
                count, '' if count == 1 else 's')
        else:
            self.txt_status.Text = ''

    def _validate(self):
        if not self._pdf_path:
            return 'Pick a PDF file first.'
        if not os.path.isfile(self._pdf_path):
            return 'PDF file not found:\n{}'.format(self._pdf_path)
        if self.cmb_titleblock.SelectedIndex < 0 or not self._title_blocks:
            return 'No title block is loaded in this project, or none is selected.'
        if not (self.txt_first_number.Text or '').strip():
            return 'Enter a first sheet number (e.g. FA-001).'
        names = parse_names(self.txt_names.Text)
        if not names:
            return 'Paste at least one sheet name (one per line).'
        return None

    # -- read settings out --

    def get_settings(self):
        idx = self.cmb_titleblock.SelectedIndex
        return {
            'pdf_path': self._pdf_path,
            'pdf_pages': self._pdf_pages,
            'title_block': self._title_blocks[idx] if idx >= 0 else None,
            'first_number': (self.txt_first_number.Text or '').strip(),
            'names': parse_names(self.txt_names.Text),
            'link_pdf': bool(self.chk_link.IsChecked),
        }


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def run(settings):
    pdf_path = settings['pdf_path']
    tb_symbol = settings['title_block']
    names = settings['names']
    first_number = settings['first_number']
    link_pdf = settings['link_pdf']

    numbers = increment_sheet_numbers(first_number, len(names))

    # Pre-flight: existing sheet number collisions get a "-N" suffix at create
    # time, but warn first so the user knows.
    existing = existing_sheet_numbers()
    collisions = [n for n in numbers if n in existing]

    if collisions:
        msg = ('These sheet numbers already exist and will get a "-N" suffix '
               'when created:\n  {}\n\nContinue?'
               .format(', '.join(collisions[:8])
                       + (' ...' if len(collisions) > 8 else '')))
        if not forms.alert(msg, title='Bulk PDF Sheets',
                           yes=True, no=True):
            return

    created = []
    failures = []

    t = DB.Transaction(doc, 'Bulk PDF Sheets ({} sheets)'.format(len(names)))
    try:
        t.Start()

        if not tb_symbol.IsActive:
            tb_symbol.Activate()
            doc.Regenerate()

        for i, (number, name) in enumerate(zip(numbers, names)):
            page_number = i + 1  # PDF pages are 1-indexed
            try:
                sheet = DB.ViewSheet.Create(doc, tb_symbol.Id)

                # Number (with collision suffix fallback)
                try:
                    sheet.SheetNumber = number
                except Exception:
                    try:
                        sheet.SheetNumber = number + '-1'
                    except Exception:
                        pass

                if name:
                    try:
                        sheet.Name = name
                    except Exception:
                        pass

                # Place the PDF page centered on the title block.
                center = title_block_center_on(sheet)
                img_type = make_image_type(
                    pdf_path, page_number, 300, link=link_pdf)
                place_image_centered(sheet, img_type, center)

                created.append((sheet.SheetNumber, sheet.Name))
            except Exception:
                failures.append((number, name, traceback.format_exc()))

        t.Commit()
    except Exception:
        try:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
        except Exception:
            pass
        raise

    # Report
    output.print_md('## Bulk PDF Sheets')
    output.print_md('- Sheets created: **{}**'.format(len(created)))
    if failures:
        output.print_md('- Failures: **{}**'.format(len(failures)))
        for num, name, tb in failures:
            output.print_md('### Failed: {}  {}'.format(num, name))
            output.print_md('```\n{}\n```'.format(tb))

    if created and not failures:
        dbhms_ui.info(
            'Created {} sheet{} and placed PDF pages.'.format(
                len(created), '' if len(created) == 1 else 's'),
            title='Bulk PDF Sheets')
    elif created and failures:
        dbhms_ui.info(
            'Created {} sheet{}, {} failed - see the pyRevit output for '
            'details.'.format(len(created),
                              '' if len(created) == 1 else 's',
                              len(failures)),
            title='Bulk PDF Sheets')
    else:
        dbhms_ui.info(
            'No sheets were created - see the pyRevit output for details.',
            title='Bulk PDF Sheets')


def main():
    # Cheap up-front check: the API we need showed up in Revit 2024.
    if not hasattr(DB, 'ImageTypeOptions'):
        forms.alert(
            'This tool needs Revit 2024 or newer (the ImageTypeOptions API '
            'is not available in this Revit version).',
            exitscript=True)

    if not get_title_block_symbols():
        forms.alert(
            'No title block families are loaded in this project. '
            'Load a title block first, then re-run.',
            exitscript=True)

    form = BulkPdfSheetsForm()
    form.ShowDialog()
    if not form.confirmed:
        return

    settings = form.get_settings()
    try:
        run(settings)
    except Exception:
        output.print_md('### Bulk PDF Sheets failed')
        output.print_md('```\n{}\n```'.format(traceback.format_exc()))
        dbhms_ui.info(
            'Bulk PDF Sheets ran into an error - see the pyRevit output for '
            'the traceback.',
            title='Bulk PDF Sheets')


if __name__ == '__main__':
    with dbhms_telemetry.session(__title__, script_path=__file__):
        main()
