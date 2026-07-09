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

**Documented exceptions to "no shared modules":**

1. **`lib/clash_*/`** — the **`Clash Detection.panel/`** (inside
   `dbHMS Tools.tab/`) uses an extension-level `lib/` folder
   (`dbHMS Extensions.extension/lib/clash_*/`) shared across its
   pushbuttons (currently two: `Clash Detection.pushbutton` and
   `3D Viewer.pushbutton`; the legacy WPF suite was removed 2026-07).
   Clash detection is a single coherent system — the buttons read and
   write the same JSON database, share the same data model, and use the
   same detection algorithms; duplicating thousands of lines across them
   would be unworkable. pyRevit
   auto-adds extension-level `lib/` to `sys.path`. Architecture, data
   model, and storage layout are documented in
   `src/extensions/dbHMS Extensions.extension/dbHMS Tools.tab/Clash Detection.panel/README.md`
   — read that before making serious changes anywhere under
   `Clash Detection.panel/` or `lib/clash_*/`.

2. **`lib/dbhms_ui/`** — shared UI helpers used by every tool in the
   repo. Currently exports `dbhms_ui.info(message, title=...)`, the
   friendly dbHMS-branded replacement for `forms.alert(...)` on
   informational popups (see UI conventions section below). Scoped so
   other tools can simply `import dbhms_ui` and not worry about the
   internals. Exists because every tool needs the same look on popups
   and duplicating the dialog markup would make any change a 10+ file
   edit.

3. **`lib/dbhms_telemetry/`** — usage tracking. Every pushbutton
   `script.py` records one `session_started` + one `session_ended`
   JSON-Lines event per invocation (start/stop time, duration, user,
   host, Revit version, doc title, error+traceback on failure) so
   firm usage can be analyzed. Storage is
   `H:\TOOLS\REVIT\dbHMS Custom Extensions\Data Traceback\<YYYY>\<MM>\<YYYY-MM-DD>_<USER>.jsonl`
   with a local fallback at `%LOCALAPPDATA%\dbhms_telemetry\...` so
   events are never lost when H: is unmapped. Telemetry I/O is fully
   try/excepted — a failing write never breaks a tool. Exists because
   EVERY tool needs identical recording + path logic; duplicating
   would make a "change the storage path" tweak a 12-file edit. See
   the Wiring section below for how each `script.py` plugs in.

4. **`View Range Helper.pushbutton` imports `lib/clash_export`** — a
   **cross-panel** use of the clash_export lib (it lives next to Clash
   Detection but is reused by a `BIM Tools.panel` tool). View Range
   Helper is a web/glTF tool, the same approach as the 3D Viewer: it
   exports the active plan's crop footprint to a small `.glb` via
   `clash_export.custom_export.export_region`, hosts WebView2, and slices
   the model live with GPU clipping planes that map 1:1 onto Revit's four
   view-range planes (Top, Cut, Bottom, View Depth). The whole interactive
   editor (plan + section, draggable planes, validation, Apply) lives in
   `web/` (`viewrange.html` + `vr/app.js`, vendored three.js under
   `web/lib/three/`). The page has a **browser dev-fallback mode**: when
   not hosted in WebView2 it auto-loads the sample fixture under
   `web/sample/` so the entire UI is testable in a plain browser over
   HTTP (no Revit). `script.py` stays a thin host: export, build + post
   `meta` (levels, current view range, template state), and on Apply
   convert plane elevations back to (level, offset) and write them in a
   transaction. Reusing `export_region` rather than copying it keeps one
   export pipeline (it handles linked models via a transform stack). Fixture
   regen + how to swap in a real export: `tools/vr_fixtures/README.md`.

If a future tool wants the same treatment, document the exception here
first.

### Wiring telemetry into a new tool

Every pushbutton's `script.py` must record its invocation. The shape
depends on the entry point:

**Modal tools** (the common case — `if __name__ == '__main__': main()`
or module-level `XForm().ShowDialog()`): wrap the entry point in the
`session()` context manager. It writes `session_started` on enter and
`session_ended` (status `completed` or `failed` + traceback) on exit,
then re-raises so pyRevit's normal error display still kicks in.

```python
import dbhms_telemetry

# ...rest of the script...

if __name__ == '__main__':
    with dbhms_telemetry.session(__title__, script_path=__file__):
        main()
```

or for module-level forms:

```python
with dbhms_telemetry.session(__title__, script_path=__file__):
    SettingsForm().ShowDialog()
```

**Modeless tools** (where `Show()` returns immediately, so the form
outlives the script): the `session()` context manager would close the
session before the user is done. Instead use the lower-level pair: open
the session before constructing the form, attach `dbhms_telemetry.end()`
to the form's `Closed` event, and wrap construction in a try/except that
flips status to `failed` on error. (There is no modeless tool in the repo
right now — the old Walkthrough tool was removed once the web-based 3D
Viewer replaced it — so there's no live reference; follow the steps above
if you add one.)

The `test_every_pushbutton_records_telemetry` integrity test enforces
this: every `script.py` under both panels must `import dbhms_telemetry`
and reference `dbhms_telemetry.session(` or `dbhms_telemetry.start(`.
A new pushbutton without telemetry will fail the suite.

**Tool-level READMEs:** some larger tools have their own `README.md` next to `script.py`. Read these before making serious changes inside that tool's folder; do not auto-load them outside that scope. Currently:
- `src/extensions/dbHMS Extensions.extension/dbHMS Tools.tab/Clash Detection.panel/README.md` — Clash Detection architecture (see paragraph above).
- `src/extensions/dbHMS Extensions.extension/dbHMS Tools.tab/BIM Tools.panel/View Templates Manager.pushbutton/README.md` — multi-XAML structure, iteration plan, Revit API mapping, documented `LinkVisibility.Custom` API limitation. The tool ships 5 XAML files (main + 4 sub-dialogs) and is being built iteration-by-iteration; iter-1 is a UI shell with mock sub-dialog data and disabled Apply.

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

> **THIS SECTION IS MANDATORY, NOT REFERENCE. Read it in full and apply it before you build or edit any form, and re-read it when you finish to confirm every control you touched conforms.** At dbHMS the UI is a first-class deliverable, weighted equally with the function behind a tool. UI is not a finishing coat applied at the end; it is designed deliberately and up front, the same as the logic. Every form MUST match this design system to the letter: the canonical header, the canonical controls (checkbox, dropdown, radio, buttons, cards, chips, ticks), the color tokens, and the layout patterns. "It works" is not "it's done" — a tool that behaves correctly but does not look like it belongs to the same firm as every other tool is not finished. Do not hand-roll a control that already has a canonical style, and do not fall back to a bare WPF default. The canonical controls all live in `AlignViews.pushbutton/AlignViewsForm.xaml` (checkbox, dropdown, buttons, chips, ticks, cards) plus `Sheet Manager.pushbutton/script.py` (radio); copy them verbatim. If a request would break a convention here, say so and reconcile it before shipping rather than quietly diverging. Treat drift from these rules as a bug.

Every tool with a form follows the same dbHMS-branded WPF design system. New tools must match — the styles below are **copied into each XAML's `<Window.Resources>`** rather than imported from a shared file (intentional, so each pushbutton stays standalone-deployable).

> **2026-07 visual refresh — read this first.** The header and accent palette were redesigned: a deep-blue dotted header carrying the real dbHMS logo and a solid orange baseline, plus a cyan/blue/orange accent system in the body (from the clash web app's light-blue palette). **`AlignViews.pushbutton` is the reference implementation of the current look** — copy its `AlignViewsForm.xaml` `<Window.Resources>` and header `Border`, plus the `_load_logo` and `_make_row_content` methods in its `script.py`. Older tools still on the flat `#2D3748` header are being migrated; do **not** copy their header. `View Range Helper.pushbutton/ViewRangeHelperForm.xaml` is still a good reference for dual-canvas layout, but treat its header as legacy until migrated.

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

Every primary form has a top `Border` `DockPanel.Dock="Top"` with a **solid orange baseline** (`BorderBrush="#EE8A34"`, `BorderThickness="0,0,0,3"`) wrapping a `Grid` whose layers are, back to front:

1. **Deep-blue ground** — `Grid.Background` = a subtle diagonal `LinearGradientBrush` (`StartPoint="0,0" EndPoint="1,1"`) with stops `#143257` (0) → `#0F2748` (0.55) → `#0B1D34` (1). A *chosen* navy (≈`#102A4C`), richer and bluer than the old pyRevit slate so it never reads as the default.
2. **Faint dot texture** — a `Rectangle` filled with a tiled `DrawingBrush` (16×16 absolute `Viewport` + `Viewbox`, an `EllipseGeometry` `Center="8,8" RadiusX="1.1"` in brush `#26D6ECFF`), with an `OpacityMask` `LinearGradientBrush` (top→bottom, opaque fading to `#00FFFFFF`) so the dots fade out before the orange line.
3. **Content `Grid`** (`Margin="24,16"`):
   - **Left**: tool title `Foreground="#F2F7FC"`, `FontSize="20"`, `FontWeight="Bold"`; below it a one-line description `Foreground="#A9C2D8"`, `FontSize="12"`.
   - **Right**: the real dbHMS logo as `<Image x:Name="img_logo" Height="38" Stretch="Uniform" RenderOptions.BitmapScalingMode="HighQuality"/>` — **not** hand-set `TextBlock`s. Source set in code-behind (see *Brand logo asset*).

Copy the header verbatim from `AlignViews.pushbutton/AlignViewsForm.xaml`.

Sub-dialogs (e.g. `PlanTypeSettingsForm`, `PreviewForm`) may keep a simpler flat header and drop the logo, using a smaller title (`FontSize="16–18"`).

**Brand logo asset.** Ship `dbhms_logo.png` in the pushbutton folder (master transparent PNG: `tools/icon_work/out/dbHMS Logo (PNG) Transparent.png`, 5950×1404). Load it in `script.py` so the huge master never sits in memory at full size and the file is never locked:

```python
def _load_logo(self):
    try:
        from System import Uri, UriKind
        from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
        path = os.path.join(SCRIPT_DIR, 'dbhms_logo.png')
        if not os.path.exists(path):
            return
        bmp = BitmapImage()
        bmp.BeginInit()
        bmp.CacheOption = BitmapCacheOption.OnLoad
        bmp.UriSource = Uri(path, UriKind.Absolute)
        bmp.DecodePixelHeight = 96
        bmp.EndInit()
        self.img_logo.Source = bmp
    except Exception:
        pass
```

Call `self._load_logo()` at the end of the form's `__init__`. The try/except means a missing or bad logo file can never break the tool. The brand cyan is `#00B4EF` (the old `#00BFFF` wordmark tint is deprecated).

### Footer

`Border DockPanel.Dock="Bottom"`, `Background="White"`, `BorderBrush="#E2E8F0"`, `BorderThickness="0,1,0,0"`, `Padding="20,12"`. Layout: optional left-aligned status `TextBlock` (`Foreground="#4A5568"`, `FontStyle="Italic"`) plus right-aligned action buttons in order **Cancel → Primary**. Use `SecondaryButton` for Cancel/Close/Revert and `PrimaryButton` for the destructive/commit action (`Apply`, `Run`, `Create Everything`, etc.).

### Color tokens

| Token | Hex | Use |
| --- | --- | --- |
| App surface | `#F7FAFC` | window background, tool body, inset hint panels |
| Card surface | `White` | every `CardBorder`, footer, canvas backgrounds |
| Card border | `#E2E8F0` | card stroke, footer top stroke, canvas frame |
| Input border | `#CBD5E0` | text/combo strokes, secondary/mini button strokes |
| **Header ground** | `#143257`→`#0F2748`→`#0B1D34` | deep-blue header gradient (the chosen navy, ≈`#102A4C`) |
| **Header dots** | `#26D6ECFF` | faint tiled dot texture in the header (ARGB, ~15% alpha) |
| **Orange accent** | `#EE8A34` | header baseline + the rationed "operation" accent (see *Accent doctrine*) |
| **Orange text** | `#9C4221` | text on the orange `CountPill` |
| **Orange chip** | `#FBEEDD` / border `#E7B183` | orange count-pill surface / stroke |
| **Brand cyan** | `#00B4EF` | the dbHMS logo ONLY (exact brand cyan; old `#00BFFF` deprecated) |
| Primary | `#2B6CB0` | `PrimaryButton`, blue section ticks, checked-checkbox fill |
| Primary text | `White` | text on `PrimaryButton` and header title |
| Secondary | `#EDF2F7` | `SecondaryButton`/`MiniButton` background, table header rows |
| **Section title** | `#1A365D` | `SectionHeader` text + list sheet numbers (deep navy) |
| **Deep blue** | `#185FA5` | `Chip` text, `CountBadge` text |
| **Info blue** | `#0C447C` | `Inset` hint text |
| **Light-blue chip** | `#EBF3FB` / border `#D7E6F6` | master-trait `Chip`, `CountBadge` (from the clash web app) |
| **Selected row** | `#EBF8FF` / border `#9FD8F1` | highlight on a checked list row (`ViewRowCheck`) |
| Body text | `#2D3748` | default text on cards, `SecondaryButton` foreground, legacy header |
| Field label | `#4A5568` | `FieldLabel`, footer status label |
| Helper text | `#718096` | `HelperText` |
| Subtle hint | `#A0AEC0` | inline hints, list-row meta |
| Header subtitle | `#A9C2D8` | description text inside the header |
| Warn surface | `#FFFBEA` / `#FEFCBF` | banner background / `WarnButton` background |
| Warn border | `#D69E2E` | warn banner & button stroke |
| Warn text | `#744210` | warn banner & button foreground |
| Error surface | `#FFF5F5` | validation banner background |
| Error border | `#FC8181` | validation banner stroke |
| Error text | `#742A2A` | validation message foreground |

**View Range Helper plane swatch palette** (use these exact hexes if drawing analogous plane indicators): Top `#38A169` (green), Cut Plane `#E53E3E` (red), Bottom `#3182CE` (blue), View Depth `#805AD5` (purple).

### Reusable styles

Define these in `<Window.Resources>`. Names are part of the convention — keep them so the look stays consistent.

- `SectionHeader` (TextBlock) — `FontSize="14"`, `FontWeight="SemiBold"`, `Foreground="#1A365D"` (deep navy), `VerticalAlignment="Center"`. Precede it with a `Tick` in a horizontal `StackPanel`.
- `FieldLabel` (TextBlock) — `FontSize="11"`, `FontWeight="SemiBold"`, `Foreground="#4A5568"`, `Margin="0,8,0,2"`
- `HelperText` (TextBlock) — `FontSize="11"`, `Foreground="#718096"`, `TextWrapping="Wrap"`
- `MonoText` (TextBlock, optional) — `FontFamily="Consolas"`, `FontSize="11"`, `Foreground="#2D3748"` (use for sheet-number previews, code-like values)
- `Tick` (Border) — a 3×15 rounded accent bar before a `SectionHeader`: `Width="3"`, `Height="15"`, `CornerRadius="2"`, `Background="#2B6CB0"`, `VerticalAlignment="Center"`, `Margin="0,0,8,0"`. `TickOrange` = `BasedOn="{StaticResource Tick}"` with `Background="#EE8A34"` (see *Accent doctrine*).
- `Chip` (Border) — light-blue info chip: `Background="#EBF3FB"`, `BorderBrush="#D7E6F6"`, `CornerRadius="5"`, `Padding="8"`; put `Foreground="#185FA5"` text inside.
- `Inset` (Border) — light-blue hint panel: `Background="#EBF8FF"`, `BorderBrush="#D3EAF7"`, `CornerRadius="4"`, `Padding="8"`; `Foreground="#0C447C"` text inside.
- `CountBadge` (Border) — light-blue count pill (candidates / found): `Background="#EBF3FB"`, `BorderBrush="#D7E6F6"`, `CornerRadius="10"`, `Padding="9,1"`; `Foreground="#185FA5" FontWeight="Bold"` text.
- `CountPill` (Border) — **orange** count pill (the live / selected count): `Background="#FBEEDD"`, `BorderBrush="#E7B183"`, `CornerRadius="10"`, `Padding="9,1"`; `Foreground="#9C4221" FontWeight="Bold"` text.
- **`ComboBox` (dropdowns)** — never use the bare default combo (flat gray box with a square drop arrow). Every dropdown uses the canonical modern template (rounded field, custom thin chevron, hover/focus border, rounded popup). See **Dropdowns (ComboBox)** below and copy the three-style block verbatim from `AlignViews.pushbutton/AlignViewsForm.xaml`.
- Default `TextBox` — `Padding="6,3"`, `Height="27"`, `VerticalContentAlignment="Center"`
- `CardBorder` (Border) — `Background="White"`, `BorderBrush="#E2E8F0"`, `BorderThickness="1"`, `CornerRadius="6"`, `Padding="13"`, `Margin="0,0,0,12"`
- `PrimaryButton` — `Background="#2B6CB0"`, `Foreground="White"`, `FontWeight="SemiBold"`, `Padding="14,6"`, `MinWidth="120"`, `BorderThickness="0"`, `Cursor="Hand"`, `Margin="6,0,0,0"`
- `SecondaryButton` — `Background="#EDF2F7"`, `Foreground="#2D3748"`, `Padding="12,6"`, `MinWidth="120"`, `BorderBrush="#CBD5E0"`, `BorderThickness="1"`, `Cursor="Hand"`, `Margin="6,0,0,0"`
- `MiniButton` — `Background="#EDF2F7"`, `Foreground="#4A5568"`, `BorderBrush="#CBD5E0"`, `BorderThickness="1"`, `Padding="8,2"`, `Cursor="Hand"`, `Margin="4,0,0,0"` (for All/None/refresh in card headers)
- `WarnButton` — `BasedOn="{StaticResource SecondaryButton}"`, `Background="#FEFCBF"`, `BorderBrush="#D69E2E"`, `Foreground="#744210"`, `MinWidth="0"`
- **`CheckBox`** (default) — retemplate to a 16×16 rounded box (`CornerRadius="4"`) that fills brand-blue `#2B6CB0` with a white check when `IsChecked`, `#CBD5E0` border when off (hover border `#2B6CB0`). **The check MUST be centered:** wrap the check `Path` in a `<Viewbox x:Name="chk" Width="10" Height="10" HorizontalAlignment="Center" VerticalAlignment="Center">` (path `Data="M0,5 L3.8,9 L10,0.5"`, `Stroke="White"`, `StrokeThickness="1.6"`, rounded caps). The Viewbox guarantees the check sits dead-center; the old bare `<Path>` check (which drifted up and to the left) is **retired** — do not reintroduce it. Copy the whole `<Style TargetType="CheckBox">` from `AlignViews.pushbutton/AlignViewsForm.xaml`. The plain default WPF checkbox is deprecated for new work. **Three-state exception:** the blue template renders only checked/unchecked, so any tool that uses `IsThreeState="True"` checkboxes (Parameters Management, View Templates Manager + its `Vg*Dialog` sub-dialogs, Sheet Manager, Revisions Manager) must KEEP the plain native `<Style TargetType="CheckBox">` (just `Margin` + `VerticalContentAlignment`) so the indeterminate "mixed" state stays visible. Rule of thumb: **if a form contains any `IsThreeState="True"` checkbox, do not brand any of its checkboxes** — leave them all native (see the tri-state legend section).
- **`ViewRowCheck`** (CheckBox, for selectable list rows) — same blue box, but the row `Border` gains the **selected-row highlight** (`Background="#EBF8FF"`, `BorderBrush="#9FD8F1"`) when checked and a subtle `#F2F8FD` on hover. Its content is a composite row (bold navy sheet number, name, gray meta docked right) built in code — see `_make_row_content` in `AlignViews.pushbutton/script.py`. Copy both from `AlignViews`.
- **`RadioButton`** — WPF's default radio button looks small and off-center next to text. Use the firm-standard custom template with a 14 px outer ellipse + 7 px inner dot, both centered, blue (`#2B6CB0`) when checked or hovered. Reference implementation lives in `Sheet Manager.pushbutton/script.py` (`<Style TargetType="RadioButton">` block); copy the whole `Style` (including `Setter Property="Template"`) into the new form's `<Window.Resources>`. Without this template, the radio dot is offset and undersized — every form with `RadioButton`s must include it.

### Dropdowns (ComboBox) — canonical modern style

The plain WPF ComboBox (flat gray box, square drop arrow) is **retired firm-wide**. Every dropdown in every tool uses the canonical modern template, which mirrors the clash web app `<select>` exactly: a white field, `1px #CBD5E0` border, `CornerRadius="6"`, a thin custom chevron (`#718096`, rounded caps), a hover border (`#A0AEC0`), and an open/focus border (`#2B6CB0`). The popup is a rounded `#CBD5E0` card whose item rows highlight `#EBF3FB` on hover and `#EBF8FF` when selected.

The style is **three resources that travel together** and must be pasted in this order (the ComboBox template references `ComboToggle` by `StaticResource`, so it has to be defined first):

1. `<Style x:Key="ComboToggle" TargetType="ToggleButton">` — draws the field border + chevron and the hover/open border states.
2. `<Style TargetType="ComboBox">` — the full `ControlTemplate` (ToggleButton + selected-item `ContentPresenter` + `PART_EditableTextBox` + rounded `Popup` with the items host).
3. `<Style TargetType="ComboBoxItem">` — the rounded item rows with hover/selected fills.

Copy all three **verbatim** from `AlignViews.pushbutton/AlignViewsForm.xaml`. For the two code-built tools (Sheet Manager, Revisions Manager) the identical block lives inside their resources XAML string (`SHARED_RESOURCES` in Sheet Manager). An intentionally borderless in-cell combo (e.g. Revisions Manager `GridCombo`, applied by `x:Key` to grid cells) is the one exception — leave it alone; it is meant to read as plain text until clicked.

Do not "simplify" this to a few property setters on the default template — that is exactly the dated gray look this replaces.

### Accent doctrine: cyan, blue, and orange

The brand has two accent colors and they are not interchangeable.

- **Brand cyan `#00B4EF`** belongs to the **logo only**. Do not use it for UI accents.
- **Blue `#2B6CB0`, deep navy `#1A365D`, and the light-blue clash family** are the **workhorse UI accent**: section ticks, primary button, checkbox fill, selected-row highlight, chips, count badges, section titles, sheet numbers.
- **Orange `#EE8A34` is rationed.** It appears in the header baseline, then in a **small number of deliberate, spread-out spots that each carry meaning** — never as decoration. In Align Views the three body spots are: the **master / primary input** (orange rail + `TickOrange`, the thing everything else bends to), the **card whose options get written** (orange tick, "these changes will be applied"), and the **live / selected count** in the footer (`CountPill`). Adding orange to a new tool? Ask "does this element drive or represent the operation?" If not, keep it blue.

### Tri-state checkbox legend

Any form that uses `IsThreeState="True"` checkboxes must include a small inline legend explaining what the three states mean — the filled middle state confuses users who haven't seen it before. Put the legend somewhere the user will read before interacting with the rows: the bulk-mode banner, a hint card under the search box, or a sub-line under the list's section header.

**Use real `CheckBox` controls in the legend, not Unicode glyphs.** The Unicode ballot-box characters (☑/☐/▣) render at noticeably different sizes in Segoe UI — the user immediately reads "the empty box is smaller than the checked one" and gets confused. Embedding actual disabled `CheckBox`es makes the legend pixel-identical to the rows it explains. Pattern:

```xml
<StackPanel Orientation="Horizontal">
    <TextBlock Text="Legend: " Style="{StaticResource HelperText}" VerticalAlignment="Center"/>
    <CheckBox IsChecked="True" IsHitTestVisible="False" Focusable="False"
              VerticalAlignment="Center" Margin="0,0,4,0"/>
    <TextBlock Text="on every selected sheet" Style="{StaticResource HelperText}"
               VerticalAlignment="Center" Margin="0,0,14,0"/>
    <CheckBox IsChecked="False" IsHitTestVisible="False" Focusable="False"
              VerticalAlignment="Center" Margin="0,0,4,0"/>
    <TextBlock Text="on none" Style="{StaticResource HelperText}"
               VerticalAlignment="Center" Margin="0,0,14,0"/>
    <CheckBox x:Name="chk_legend_mixed" IsThreeState="True"
              IsHitTestVisible="False" Focusable="False"
              VerticalAlignment="Center" Margin="0,0,4,0"/>
    <TextBlock Text="mixed (some but not all)" Style="{StaticResource HelperText}"
               VerticalAlignment="Center"/>
</StackPanel>
```

`IsThreeState="True"` alone starts a checkbox at False — push `IsChecked = None` from script after the form loads to make the indicator render in indeterminate state. Use the canonical name `chk_legend_mixed` so a single helper can find it. Reference: `_set_legend_mixed(form)` in `View Templates Manager.pushbutton/script.py` is called once at the end of every form's `__init__`. Swap the noun (`sheet`, `template`, `category`, etc.) for whatever the row represents.

### Mixed-font inline labels

When a label and a code-like value need to render on the same line (e.g. `Storage: T:\path\to\file`), do **not** put two `TextBlock`s with different `FontFamily`s in a horizontal `StackPanel` — different fonts have different ascent metrics and a horizontal `StackPanel` aligns by top, so the value visibly floats above or below the label. Instead, use **one** `TextBlock` with `<Run>` elements:

```xml
<TextBlock Style="{StaticResource HelperText}">
    <Run Text="Storage: "/><Run Text="T:\\_clash_data\\..." FontFamily="Consolas" Foreground="#2D3748"/>
</TextBlock>
```

WPF's text engine handles baseline alignment correctly within a single `TextBlock`.

### Layout patterns

- **Two-column main content**: `Grid` with `Margin="20,16,20,16"` and columns `<320|360> / 16 / *`. Left column holds settings cards (filters, options, master pickers); right column is the long list / canvas.
- **Cards**: every logical group sits in a `Border` styled with `CardBorder`. Inside a card, the order is `SectionHeader` → optional `HelperText` → `FieldLabel` + input pairs.
- **Inset hint panel** (the lighter "tip" box inside a card): `Background="#F7FAFC"`, `BorderBrush="#E2E8F0"`, `BorderThickness="1"`, `CornerRadius="3"`, `Padding="8"`, `Margin="0,8,0,0"` with a `HelperText` inside.
- **Validation banner** (hidden by default, shown on error): `Background="#FFF5F5"`, `BorderBrush="#FC8181"`, `BorderThickness="1"`, `CornerRadius="3"`, `Padding="8"`, `Margin="0,10,0,0"`, with text `Foreground="#742A2A"`, `FontSize="11"`, `TextWrapping="Wrap"`.
- **Lock/warn banner across the top of the form**: `Background="#FFFBEA"`, `BorderBrush="#D69E2E"`, `BorderThickness="0,0,0,1"`, `Padding="16,10"`, with `Foreground="#744210"` text and `WarnButton` actions on the right.

### Multi-select in lists

Any list of selectable rows (templates, categories, filters, links, layers, etc.) should support the same **click / shift-click / ctrl-click** pattern users expect from File Explorer and other software:

- Plain click highlights one row.
- Shift-click extends a range from the last clicked row.
- Ctrl-click toggles a single row in/out of the highlight.
- When a row carries a checkbox and the user toggles it on a row that's part of a multi-row highlight, the new check state propagates to the same checkbox on every other highlighted row (one-click bulk-check).

The reference implementation is `RowMultiSelectHelper` in `View Templates Manager.pushbutton/script.py`. Each row is a small dict (`{"row": Border, "name": str, "chk_show": CheckBox, ...}`); the helper wires `MouseLeftButtonDown` and the named checkbox(es) and applies the highlight visuals (`#EBF8FF` background, `#3182CE` border). Reuse it (or the same pattern) for new lists; skip it only when a list is read-only/single-select by nature.

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

User-facing dialogs:

- **Informational popups** use `dbhms_ui.info(message, title=title)` — a
  shared lib helper that renders a friendly dbHMS-branded modal dialog
  (slate header bar, blue "ⓘ" glyph, OK button styled as PrimaryButton).
  This replaces `pyrevit.forms.alert(message, title=title)` for any
  popup that just reports state ("Export complete", "View created OK").
  pyRevit's `forms.alert` paints the
  Windows yellow-warning-triangle icon even on success messages, which
  reads as failure to users. The `dbhms_ui` dialog drops that and
  matches the rest of the firm's UI. Lives at
  `src/extensions/dbHMS Extensions.extension/lib/dbhms_ui/`. Import as
  `import dbhms_ui` (the lib root is on `sys.path` via pyRevit's
  extension-level `lib/`).
- **Yes/no confirmations** still use `forms.alert(msg, title=title,
  yes=True, no=True)` — pyRevit's confirmation dialog has the right
  shape for a question, and the Yes/No buttons are clearly different
  from a plain OK. Don't replace these.
- **Fatal-precondition gates** still use `forms.alert(msg,
  exitscript=True)` — they should look severe, the warning-triangle is
  appropriate when the script is about to terminate.

Pattern: import once at module level after `from pyrevit import ...`:
```python
from pyrevit import forms
import dbhms_ui

dbhms_ui.info("Saved 13 clashes to clashes.json.", title='Run complete')
```

`dbhms_ui` is the second documented `lib/` exception (alongside
`clash_*` for clash detection — see Architecture section). It exists
because EVERY tool puts up popups, and duplicating the WPF dialog
across every pushbutton would make a "make all popups blue" tweak a
30-file edit. Scoped under the `dbhms_ui` namespace so other tools'
scripts can ignore it. If a future tool wants a similar shared
helper, document the exception here first.

All Revit document mutations happen inside a `revit.Transaction("...")` block.

