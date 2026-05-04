# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All scripts are PowerShell and assume Windows + Python 3 (the `py` launcher is preferred, falling back to `python`). Run from the repo root.

- Run all tests: `.\run_tests.ps1` (thin wrapper around `scripts\test.ps1`)
- Run tests directly: `.\scripts\test.ps1` — uses `py -3 -m unittest discover -s tests -p "test_*.py" -v`
- Run a single test: `py -3 -m unittest tests.test_extension_integrity.ExtensionIntegrityTests.test_python_scripts_are_valid_syntax -v`
- Build a deployable zip into `artifacts/`: `.\scripts\build.ps1`
- Deploy to a pyRevit extensions root (replaces the target `dbHMS Extensions.extension` folder): `.\scripts\deploy.ps1 -TargetRoot "C:\Path\To\pyRevit\Extensions"`

After deploy, reload pyRevit (or restart Revit) to pick up changes.

## Architecture

This is a **pyRevit extensions monorepo**, not a normal Python package. The runtime is IronPython 2.7 inside Revit; tests are CPython 3 and only validate static structure (parse, XML, JSON shape) — they do not exercise Revit APIs.

### Extension layout (pyRevit conventions)

The shipped product is a single extension package: `src/extensions/dbHMS Extensions.extension/`. pyRevit discovers tools by folder name suffix, so the nesting is load-bearing:

```
<name>.extension / <name>.tab / <name>.panel / <ToolName>.pushbutton /
    script.py        # entry point; pyRevit injects `pyrevit`, `revit`, `DB`, etc.
    icon.png         # toolbar icon
    config.json      # optional tool config (read at runtime)
    *.xaml           # optional WPF form, loaded via pyRevit forms
```

Renaming any `*.extension`, `*.tab`, `*.panel`, or `*.pushbutton` folder changes how the tool appears in Revit — and the integrity tests hard-code the expected pushbutton folder names.

Each `script.py` is self-contained: it sets `__title__` / `__author__` for the toolbar, imports `pyrevit` + .NET WPF types (`System.Windows.*`), reads its sibling `config.json` / `*.xaml`, and runs inside a Revit transaction. Tools do not share helper modules — duplication across pushbuttons is intentional so each one can be deployed standalone.

### Build / deploy model

`build.ps1` zips the entire `dbHMS Extensions.extension` directory verbatim into `artifacts/dbHMS-Extensions-<timestamp>.zip`. `deploy.ps1` copies that same directory tree into a target pyRevit extensions root, deleting any existing `dbHMS Extensions.extension` folder there first. There is no compilation step and no manifest beyond the folder structure itself, so anything committed under `src/extensions/...` ships as-is.

### Test suite as a structural gate

`tests/test_extension_integrity.py` is the only test file and exists to catch deploy-breaking mistakes that pyRevit/Revit would only surface at load time:

- expected `*.pushbutton` folders are present
- every `script.py` parses as valid Python (`ast.parse`)
- no merge-conflict markers in `.py` / `.xaml` / `.json`
- every `.xaml` parses as XML
- `AlignViews/config.json` and `SheetSetup/config.json` have their required keys (`filters`/`match` for AlignViews; `patterns`/`options`/`disciplines[].{code,name,plan_types}` for SheetSetup)

When adding a new pushbutton, update the `expected` set in `test_expected_pushbuttons_exist`. When adding required keys to either tracked `config.json`, extend `test_json_configs_are_valid_and_have_required_keys`.

## UI conventions (read this before building a new tool)

Every tool with a form follows the same dbHMS-branded WPF design system. New tools must match — the styles below are **copied into each XAML's `<Window.Resources>`** rather than imported from a shared file (intentional, so each pushbutton stays standalone-deployable). Reference implementations: `AlignViews.pushbutton/AlignViewsForm.xaml` and `View Range Helper.pushbutton/ViewRangeHelperForm.xaml` are the most complete; copy from one of those.

### Window chrome

- `Background="#F7FAFC"` (off-white app surface)
- `FontFamily="Segoe UI"`, `FontSize="12"`
- `WindowStartupLocation="CenterScreen"` for top-level windows; `CenterOwner` for sub-dialogs (e.g. settings popovers)
- `ResizeMode="CanResizeWithGrip"`
- Default sizes by tool type:
  - Main tool window: `Width="900–1040"`, `Height="760–780"` (View Range Helper is wider at 1600×980 because it has dual canvases)
  - Confirmation/preview window: `Width="780"`, `Height="640"`
  - Modal sub-dialog: `Width="540"`, `Height="600"`

### Header (mandatory dbHMS branding bar)

Every primary form has a top `Border` `DockPanel.Dock="Top"` with `Background="#2D3748"` (dark slate), `Padding="20,14"`. Inside, a two-column `Grid`:

- **Left column**: tool title in `White`, `FontSize="20"`, `FontWeight="Bold"`; underneath, a one-line description in `Foreground="#CBD5E0"`, `FontSize="12"`, `Margin="0,2,0,0"`.
- **Right column** (the wordmark): `StackPanel Orientation="Horizontal"`, `VerticalAlignment="Center"`, `Opacity="0.85"`, made of three `TextBlock`s:
  - `"db"` — `Foreground="#00BFFF"`, `FontSize="32"`, `FontWeight="Bold"`, `FontFamily="Segoe UI"`
  - `" | "` — `Foreground="#7A8FA6"`, `FontSize="32"`, `FontWeight="Light"`
  - `"HMS"` — same as `"db"`

Sub-dialogs (e.g. `PlanTypeSettingsForm`, `PreviewForm`) keep the `#2D3748` bar but drop the wordmark and use a smaller title (`FontSize="16–18"`).

### Footer

`Border DockPanel.Dock="Bottom"`, `Background="White"`, `BorderBrush="#E2E8F0"`, `BorderThickness="0,1,0,0"`, `Padding="20,12"`. Layout: optional left-aligned status `TextBlock` (`Foreground="#4A5568"`, `FontStyle="Italic"`) plus right-aligned action buttons in order **Cancel → Primary**. Use `SecondaryButton` for Cancel/Close/Revert and `PrimaryButton` for the destructive/commit action (`Apply`, `Run`, `Create Everything`, etc.).

### Color tokens

| Token | Hex | Use |
| --- | --- | --- |
| App surface | `#F7FAFC` | window background, inset hint panels |
| Card surface | `White` | every `CardBorder`, footer, canvas backgrounds |
| Header bar | `#2D3748` | dbHMS branding bar, modal headers |
| Card border | `#E2E8F0` | card stroke, footer top stroke, canvas frame |
| Input border | `#CBD5E0` | secondary/mini button strokes, canvas frames |
| Primary | `#2B6CB0` | `PrimaryButton` background, primary actions |
| Primary text | `White` | text on `PrimaryButton` and header |
| Secondary | `#EDF2F7` | `SecondaryButton`/`MiniButton` background, table header rows |
| Section heading text | `#1A202C` | `SectionHeader` |
| Body text | `#2D3748` | default text on cards, `SecondaryButton` foreground |
| Field label | `#4A5568` | `FieldLabel`, footer status text |
| Helper text | `#718096` | `HelperText`, sub-titles in dark header |
| Subtle hint | `#A0AEC0` | inline hints under inputs |
| Header subtitle | `#CBD5E0` | description text inside `#2D3748` bar |
| Wordmark accent | `#00BFFF` | `db` and `HMS` glyphs |
| Wordmark divider | `#7A8FA6` | `" | "` between glyphs |
| Warn surface | `#FFFBEA` / `#FEFCBF` | banner background / `WarnButton` background |
| Warn border | `#D69E2E` | warn banner & button stroke |
| Warn text | `#744210` | warn banner & button foreground |
| Error surface | `#FFF5F5` | validation banner background |
| Error border | `#FC8181` | validation banner stroke |
| Error text | `#742A2A` | validation message foreground |

**View Range Helper plane swatch palette** (use these exact hexes if drawing analogous plane indicators): Top `#38A169` (green), Cut Plane `#E53E3E` (red), Bottom `#3182CE` (blue), View Depth `#805AD5` (purple).

### Reusable styles

Define these in `<Window.Resources>`. Names are part of the convention — keep them so the look stays consistent.

- `SectionHeader` (TextBlock) — `FontSize="14"`, `FontWeight="SemiBold"`, `Foreground="#1A202C"`, `Margin="0,0,0,6"`
- `FieldLabel` (TextBlock) — `FontSize="11"`, `FontWeight="SemiBold"`, `Foreground="#4A5568"`, `Margin="0,8,0,2"`
- `HelperText` (TextBlock) — `FontSize="11"`, `Foreground="#718096"`, `TextWrapping="Wrap"`
- `MonoText` (TextBlock, optional) — `FontFamily="Consolas"`, `FontSize="11"`, `Foreground="#2D3748"` (use for sheet-number previews, code-like values)
- Default `ComboBox` — `Padding="6,4"`, `Height="26"`
- Default `TextBox` — `Padding="6,3"`, `Height="26"`, `VerticalContentAlignment="Center"`
- `CardBorder` (Border) — `Background="White"`, `BorderBrush="#E2E8F0"`, `BorderThickness="1"`, `CornerRadius="4"`, `Padding="12"`, `Margin="0,0,0,10"`
- `PrimaryButton` — `Background="#2B6CB0"`, `Foreground="White"`, `FontWeight="SemiBold"`, `Padding="14,6"`, `MinWidth="120"`, `BorderThickness="0"`, `Cursor="Hand"`, `Margin="6,0,0,0"`
- `SecondaryButton` — `Background="#EDF2F7"`, `Foreground="#2D3748"`, `Padding="12,6"`, `MinWidth="120"`, `BorderBrush="#CBD5E0"`, `BorderThickness="1"`, `Cursor="Hand"`, `Margin="6,0,0,0"`
- `MiniButton` — `Background="#EDF2F7"`, `BorderBrush="#CBD5E0"`, `Padding="8,2"`, `Cursor="Hand"`, `Margin="0,0,4,0"` (for All/None/refresh icons in card headers)
- `WarnButton` — `BasedOn="{StaticResource SecondaryButton}"`, `Background="#FEFCBF"`, `BorderBrush="#D69E2E"`, `Foreground="#744210"`, `MinWidth="0"`
- Default `CheckBox` — `Margin="0,3,0,3"`, `VerticalContentAlignment="Center"`

### Layout patterns

- **Two-column main content**: `Grid` with `Margin="20,16,20,16"` and columns `<320|360> / 16 / *`. Left column holds settings cards (filters, options, master pickers); right column is the long list / canvas.
- **Cards**: every logical group sits in a `Border` styled with `CardBorder`. Inside a card, the order is `SectionHeader` → optional `HelperText` → `FieldLabel` + input pairs.
- **Inset hint panel** (the lighter "tip" box inside a card): `Background="#F7FAFC"`, `BorderBrush="#E2E8F0"`, `BorderThickness="1"`, `CornerRadius="3"`, `Padding="8"`, `Margin="0,8,0,0"` with a `HelperText` inside.
- **Validation banner** (hidden by default, shown on error): `Background="#FFF5F5"`, `BorderBrush="#FC8181"`, `BorderThickness="1"`, `CornerRadius="3"`, `Padding="8"`, `Margin="0,10,0,0"`, with text `Foreground="#742A2A"`, `FontSize="11"`, `TextWrapping="Wrap"`.
- **Lock/warn banner across the top of the form**: `Background="#FFFBEA"`, `BorderBrush="#D69E2E"`, `BorderThickness="0,0,0,1"`, `Padding="16,10"`, with `Foreground="#744210"` text and `WarnButton` actions on the right.

### Toolbar icons

- File: `icon.png` at the pushbutton folder root.
- Spec: **96×96 PNG, square, transparent background.** This is the dominant size; pyRevit will scale, but ship at 96 for crisp rendering. (Two legacy tools ship 32×32 — do not copy that for new tools.)
- Keep filenames lowercase exactly as `icon.png` — pyRevit looks it up by name.

### Python form glue

Forms are loaded via `pyrevit.forms.WPFWindow`. The script.py preamble is identical across tools:

```python
__title__ = 'Tool\nName'   # \n breaks the toolbar caption onto two lines
__author__ = 'Nathaniel'

import os
from pyrevit import revit, DB, forms, script

SCRIPT_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')
FORM_XAML = os.path.join(SCRIPT_DIR, 'ToolNameForm.xaml')

class ToolForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, FORM_XAML)
        # bind events, populate combos, etc.
```

User-facing dialogs use `forms.alert(...)` (with `title=` for non-default titles, and `exitscript=True` for fatal preconditions). All Revit document mutations happen inside a `revit.Transaction("...")` block.

