# View Templates Manager

Read this before making serious changes anywhere under `View Templates Manager.pushbutton/`. Architecture, scope decisions, and Revit API limitations live here.

## Goal

A dbHMS-styled, modern UI for editing Revit view templates that supports **bulk editing across many templates at once**. The visual reference is Revit's own Properties dialog (`Parameter | Value | Include` table) plus its V/G Overrides → category sub-dialogs — but in our look, with bulk-mode "(varies)" markers and one-Transaction commits across every checked template.

## Files in this folder

```
View Templates Manager.pushbutton/
├── script.py                       Main entry. Loads main form; defines all 5 form classes.
├── ViewTemplatesManagerForm.xaml   Main form: dbHMS header, left template list,
│                                   right parameter table with collapsible Expander sections.
├── VgCategoriesDialog.xaml         V/G Overrides editor for Model / Annotation / Analytical
│                                   (one shared dialog, retitled per kind in script.py).
├── VgImportsDialog.xaml            V/G Overrides → Import editor with CAD-link/layer tree.
│                                   Two-level expand: each CAD link expands to per-layer rows.
├── VgFiltersDialog.xaml            V/G Overrides → Filters editor (preview-only — see below).
├── VgLinksDialog.xaml              V/G Overrides → RVT Link Display Settings.
│                                   ByHostView / ByLinkedView mode switching + per-link Halftone.
├── icon.png                        96×96 toolbar icon.
└── README.md                       This file.
```

## UI structure

### Main form right side — parameter table

Five collapsible Expander sections, in this order. The native Revit dialog's parameter list is a flat table; we group it for readability while preserving native order within each group.

| Section | Rows |
|---|---|
| **View** | View Scale, Scale Value 1: (derived), Display Model, Detail Level, Parts Visibility |
| **Visibility / Graphics Overrides** | V/G Overrides Model, Annotation, Analytical Model (hidden by default), Import, Filters, RVT Links |
| **Graphic Display Options** | Model Display, Shadows, Sketchy Lines, Lighting, Photographic Exposure, Background |
| **View Properties** | Phase Filter, Discipline, Show Hidden Lines, Color Scheme Location, Color Scheme, System Color Schemes |
| **Plan-only Properties** | Underlay Orientation, View Range, Orientation, Depth Clipping. Section auto-hides when no plan template is selected. |

Every row has columns: **Parameter | Value | Include**. The **Apply** column was retired — Apply Changes simply writes every row whose Value is not `(varies)`. The Include column is always visible — when unchecked, the template doesn't enforce that parameter on views applied with it.

`row_vg_analytical` is hidden by default; the "Show Analytical" toggle in the right-side header reveals it. MEP firms typically don't use Analytical so it stays out of the way.

### Editable vs informational rows

| Kind | Rows | How it works |
|---|---|---|
| **Wired (combo / value writer)** | View Scale, Display Model, Detail Level, Parts Visibility, Phase Filter, Discipline, Show Hidden Lines, Underlay Orientation, Orientation | Reads from selected template(s) on selection; writes on Apply. Bulk mode shows `(varies)`. |
| **Wired (sub-dialog Edit&hellip;)** | V/G Overrides Model / Annotation / Analytical / Import / RVT Links | Click Edit&hellip; → modal dialog → OK commits inside one Transaction across every checked template. |
| **Informational only** | V/G Overrides RVT Links (per-aspect overrides), Model Display, Shadows, Sketchy Lines, Lighting, Photographic Exposure, Background, View Range, System Color Schemes | These are multi-field sub-dialogs in Revit. Bulk-editing across templates isn't practical, so the row shows an italic gray hint pointing the user to Revit's native View Templates dialog. The Include checkbox at right still toggles whether the aspect is part of the template (via `SetNonControlledTemplateParameterIds`). |
| **Preview-only** | V/G Overrides Filters | Dialog opens and shows the project's filter list, but OK is a no-op. Wiring is documented in the iteration table below. |

### Selection modes

The script reads checked state via `_checked()` and routes:

- **0 checked** — right side is visually disabled (opacity 0.55), title says "Select a template on the left"
- **1 checked** — single-edit mode
- **2+ checked** — bulk mode, yellow banner appears, Value combos get a "(varies)" tooltip, plan section appears if any selected template is a plan

### Multi-select in lists

Every list — the template list and the V/G sub-dialog category / import lists — supports click / shift-click / ctrl-click. Ticking a checkbox in a row that's part of a multi-row highlight propagates to every highlighted row. `RowMultiSelectHelper` in `script.py` is the reusable implementation; see the root `CLAUDE.md` "Multi-select in lists" convention for the broader contract any new list should follow.

### Search bars

V/G Categories and V/G Imports dialogs have a search TextBox above the row list. Typing filters rows live by name (case-insensitive substring). For Imports, when a child layer matches but its parent CAD doesn't, the parent CAD row stays visible and its layer panel auto-expands so the user can see the match. Clearing the search restores all rows.

## Iteration plan

| Iter | Status | Scope |
|---|---|---|
| **1** | ✅ done | UI shell + navigation. All sub-dialogs populated with mock data. Apply button disabled. Real Revit templates load on the left; everything on the right side is preview-only. |
| **2** | ✅ done | Simple-parameter rows wired end-to-end: View Scale, Display Model, Detail Level, Parts Visibility, Phase Filter, Discipline, Show Hidden Lines, Underlay Orientation, Orientation. Reads from selected template(s) on selection, writes back through one `Transaction` on Apply. Scale / Detail Level / Parts Visibility / Discipline use the View class's direct properties (the BIP route is read-only on view templates); the rest go through `Parameter.Set`. Bulk mode shows `(varies)` markers when templates differ; the per-row Include checkbox drives `SetNonControlledTemplateParameterIds` (3-state with indeterminate when bulk templates disagree). The spec list uses `getattr(BuiltInParameter, name)` with a graceful skip so any future BIP that doesn't exist on a given Revit version simply drops out instead of crashing the form. **Color Scheme Location** stayed preview-only — it doesn't have a matching BIP. |
| **3a** | ✅ done | V/G category visibility wired for Model / Annotation / Analytical / Import. The Edit&hellip; sub-dialogs (`VgCategoriesDialog`, `VgImportsDialog`) load real categories from `doc.Settings.Categories`, read the per-template hidden state via `View.GetCategoryHidden`, and on OK apply changes through `View.SetCategoryHidden` inside one `Transaction` across every checked template. CAD imports show as expandable parents with their layer sub-categories; both levels can be hidden/shown. The Show checkboxes are 3-state — indeterminate means templates disagree on this category and the row is left untouched on OK. |
| **3b** | ✅ done | OGS Halftone + Transparency wired in both V/G Categories and V/G Imports dialogs. Click a row, the right panel loads that row's current `OverrideGraphicSettings.Halftone` / `Transparency` from the first template; ticking Halftone or moving the slider marks that field "dirty"; on OK we call `View.SetCategoryOverrides` for every (template × highlighted category) pair, writing only dirty fields. Switching category selection clears dirty (matches Revit native behavior). The Reset Overrides button clears all OGS on the highlighted categories via `SetCategoryOverrides(catId, OverrideGraphicSettings())`. OK keeps the dialog open and flashes a green checkmark in the status bar instead of closing. Empty highlight is treated as "apply to all rows in the dialog". A `IFailuresPreprocessor` swallows the warning Revit raises when `SetCategoryOverrides` is called on a non-overridable category, and we preflight with `IsCategoryOverridable` to avoid poisoning the transaction. |
| **3c** | ✅ done | Cut + Projection line **weight**, **color**, and **pattern** wired in both V/G Categories and V/G Imports dialogs, plus **Detail Level** in Categories. Color uses Windows Forms `ColorDialog` (no built-in WPF picker); patterns enumerate `LinePatternElement` from the project and include a "&lt;no override&gt;" option mapped to `InvalidElementId`. Weight uses 1–16 with a "&lt;no override&gt;" sentinel of `-1`. Reads from each row's OGS into the right panel on selection (first encountered value across the consensus); writes only dirty fields on OK via the same loop pattern as 3a/3b. |
| 3d | deferred | Surface + cut foreground/background fill patterns. Not blocking real work; users who need these still have Revit's native dialog. Wiring path: `FillPatternElement` enumerator (drafting + model split), then read/write `OGS.SurfaceForegroundPatternId`/`Color`, `SurfaceBackgroundPatternId`/`Color`, `CutForegroundPatternId`/`Color`, `CutBackgroundPatternId`/`Color` through the dirty-tracking path. |
| 3e | deferred | Filters dialog (`VgFiltersDialog`) is currently preview-only — it loads the project filter list and shows OGS controls but OK is a no-op. Wiring path: `View.SetFilterOverrides` / `SetFilterVisibility` / `SetIsFilterEnabled` / `AddFilter`. |
| **4a** | ✅ done | V/G Overrides RVT Links: real link picker (one entry per `RevitLinkType` in the project), ByHostView / ByLinkedView mode toggle, and per-link Halftone. Reads consensus across selected templates via `View.GetLinkOverrides(linkId).GetLinkVisibilityType()` (or `LinkVisibility` property fallback). On OK: ByHostView calls `View.RemoveLinkOverrides(linkId)`; ByLinkedView creates or reuses `RevitLinkGraphicsSettings` and calls `View.SetLinkOverrides(linkId, settings)`; Halftone is applied alongside. The whole apply runs in one Transaction with the silent failure preprocessor + green checkmark flash. |
| **4b** | ✅ informational | Per-aspect link dropdowns (View filters, View range, Phase, Phase filter, Detail level, Discipline, Color fill, Object styles, Nested links) auto-disable when the picked link is in Custom mode and a yellow banner explains why. **Reason:** `LinkVisibility.Custom` is read-only via the Revit API — `SetLinkOverrides` silently no-ops on Custom-mode settings. The main form's V/G Overrides RVT Links row was converted to an informational TextBlock pointing to Revit's native dialog, which is the only way to bulk-edit Custom-mode per-aspect overrides. |
| **5** | ✅ informational | Graphic Display rows (Model Display, Shadows, Sketchy Lines, Lighting, Photographic Exposure, Background), View Range, and System Color Schemes are informational TextBlocks instead of Edit&hellip; buttons. **Reason:** these are multi-field sub-dialogs in Revit (e.g., Model Display has Style + Transparency + Silhouettes + Smooth Lines). Bulk-editing across templates isn't practical — users tweak them once per template in Revit's native View Templates dialog. The Include checkbox on each row still controls whether the aspect is part of the template. **For View Range specifically**, the dbHMS View Range Helper tool gives a visual editor that's better than the native dialog. |
| 6 | deferred | Polish — per-view-type parameter filtering (greying rows that don't apply to the selected template's view type), Color Scheme + Color Scheme Location wiring (these need `View.ColorSchemeLocation` and `View.GetColorFillSchemeId` / `SetColorFillSchemeId`, plus a category picker since color schemes are scoped per `ColorFillCategory`). |

After each iter ships and you confirm it, update the table here — keep status accurate.

## Revit API mapping

For when iter-3d/3e/6 wires more rows. All entries assume Revit 2024+.

### Simple-parameter rows (iter 2)

Read with `template.get_Parameter(BIP).AsInteger()`, write with `.Set(value)`. All within one `revit.Transaction` for bulk apply.

| Parameter | BuiltInParameter | Notes |
|---|---|---|
| View Scale | `VIEW_SCALE` | Or `vt.Scale = N`. |
| Display Model | `VIEW_MODEL_DISPLAY_MODE` | |
| Detail Level | `VIEW_DETAIL_LEVEL` | Or `vt.DetailLevel`. |
| Parts Visibility | `VIEW_PARTS_VISIBILITY` | Or `vt.PartsVisibility`. |
| Phase Filter | `VIEW_PHASE_FILTER` | |
| Discipline | `VIEW_DISCIPLINE` | |
| Show Hidden Lines | `VIEW_SHOW_HIDDEN_LINES` | |
| Underlay Orientation | `VIEWER_UNDERLAY_ORIENTATION` | Plan-only. |
| Orientation | `PLAN_VIEW_NORTH` | Plan-only. True/Project North. |

### Dedicated-API rows

| Parameter | API |
|---|---|
| V/G Overrides categories | `view.SetCategoryOverrides(catId, OverrideGraphicSettings)` + `view.SetCategoryHidden(catId, bool)`. Build OGS once, loop templates inside one Transaction. Preflight with `IsCategoryOverridable` and wrap the transaction in a silent `IFailuresPreprocessor`. |
| V/G Overrides filters | `view.AddFilter(fid)`, `view.SetFilterOverrides(fid, ogs)`, `view.SetFilterVisibility(fid, bool)`, `view.SetIsFilterEnabled(fid, bool)`, `view.GetFilters()`. |
| V/G Overrides RVT Links | `view.GetLinkOverrides(linkId)` → `RevitLinkGraphicsSettings`; `view.SetLinkOverrides(linkId, settings)`; `view.RemoveLinkOverrides(linkId)`. |
| Color Scheme | `view.GetColorFillSchemeId(catId)` / `SetColorFillSchemeId(catId, schemeId)`. |
| Color Scheme Location | `view.ColorSchemeLocation` (property). |

### Include column (every iter)

The Include column maps to:

- `view.GetTemplateParameterIds()` — every parameter that *could* be template-controlled
- `view.GetNonControlledTemplateParameterIds()` — those whose Include box is currently **unchecked**
- `view.SetNonControlledTemplateParameterIds(ICollection<ElementId>)` — only writer; pass the inverse of "what you want included"

In IronPython, mutate the .NET collection with `.Add()` / `.Remove()`, not Python list ops. Inputs must be a subset of `GetTemplateParameterIds()` — invalid IDs throw.

## API limitations (must respect)

- **`LinkVisibility.Custom` cannot be set via API.** Documented Autodesk limitation. `SetLinkOverrides` silently no-ops on Custom-mode settings. The per-aspect dropdowns in `VgLinksDialog.xaml` auto-disable when the picked link is in Custom mode; the main-form row points the user to Revit's native dialog.
- **Per-tab category checklists in the Custom RVT-link dialog** (Model / Annotation / Analytical / Import categories tabs in Revit native) are not bulk-editable through any API path. Out of scope.
- **Schedules.** Most template fields (Fields / Sorting / Grouping / Formatting) need `ViewSchedule.Definition` APIs, not `Parameter.Set`. Out of scope.
- **`SetCategoryOverrides` raises a Revit warning dialog** for non-overridable categories. Suppress with `IFailuresPreprocessor` and preflight with `IsCategoryOverridable`. See `make_silent_failure_options` in `script.py`.
- **Scale / Detail Level / Parts Visibility / Discipline** can't be written via `Parameter.Set` on view templates — those parameter routes silently fail. Use the View class's direct properties (`vt.Scale = N`, `vt.DetailLevel = ViewDetailLevel.Coarse`, etc.).

## UI conventions in use

The XAMLs follow the repo-wide dbHMS design system documented in the root `CLAUDE.md`. The radio-button `ControlTemplate` in `ViewTemplatesManagerForm.xaml` and `VgLinksDialog.xaml` is the firm-standard 14 px outer / 7 px inner template — copy it intact if you add another XAML in this folder, don't shrink or restyle it.

## Bulk-edit semantics

When 2+ templates are checked:

1. Yellow `bnr_bulk` banner appears with a count message.
2. Each Value combo's `ToolTip` is set to a "(varies)" hint when templates disagree on that row.
3. Footer's Apply Changes button iterates checked templates and for each row whose Value is **not `(varies)`**, writes that value into the template inside one `revit.Transaction("Apply view template properties")`. Per-template writers handle their own exceptions and just return False on failure, so a single bad template can't roll back the rest.
4. The Include checkbox toggles `SetNonControlledTemplateParameterIds` once per template — same loop. 3-state indeterminate means "leave each template's existing include state alone."

Per-template usage counts come from `build_usage_map(doc)` and are summed in bulk mode for the right-header "Total views affected: N" hint.

## Testing

The shared `tests/test_extension_integrity.py` covers this tool:

- Pushbutton folder must exist (asserted in `test_expected_pushbuttons_exist`).
- All `.py` files parse via `ast.parse` (CPython 3 must be able to read; no f-strings, no Python-3-only syntax).
- All `.xaml` files parse as XML.
- No merge-conflict markers anywhere.

Run from repo root: `.\run_tests.ps1`.
