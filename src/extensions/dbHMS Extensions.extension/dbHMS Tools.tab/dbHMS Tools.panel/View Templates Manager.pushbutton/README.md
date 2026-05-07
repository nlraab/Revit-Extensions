# View Templates Manager

Read this before making serious changes anywhere under `View Templates Manager.pushbutton/`. Architecture, iteration plan, and Revit API limitations live here.

## Goal

Replace Revit's native View Template editor with a dbHMS-styled, modern UI that supports **bulk editing across many templates at once**. The visual reference is Revit's own Properties dialog (`Parameter | Value | Include` table) plus its V/G Overrides → RVT Link Display Settings sub-dialogs — but in our look, with a "Apply to N templates" column that the native dialog doesn't have.

This is a multi-iteration build. Iter-1 is the navigation shell with mock sub-dialog data; iters 2-6 progressively wire it to the Revit API.

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
├── VgFiltersDialog.xaml            V/G Overrides → Filters editor with OGS controls.
├── VgLinksDialog.xaml              V/G Overrides → RVT Link Display Settings.
│                                   Tabs: Basics + 4 category placeholders (API-limited).
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

Every row has columns: **Parameter | Value | Include | Apply**. The Apply column is hidden in single-edit mode and shown in bulk mode (2+ templates checked). The Include column is always visible — when unchecked, the template doesn't enforce that parameter on views applied with it.

`row_vg_analytical` is hidden by default; the "Show Analytical" toggle in the right-side header reveals it. MEP firms typically don't use Analytical so it stays out of the way.

### Selection modes

The script reads checked state via `_checked()` and routes:

- **0 checked** — right side is visually disabled (opacity 0.55), title says "Select a template on the left"
- **1 checked** — single-edit mode, Apply column hidden, plan section auto-shown/hidden by view type
- **2+ checked** — bulk mode, yellow banner appears, Apply column shown, Value combos get a "(varies)" tooltip, plan section appears if any selected template is a plan

## Iteration plan

| Iter | Status | Scope |
|---|---|---|
| **1** | ✅ done | UI shell + navigation. All sub-dialogs populated with mock data. Apply button disabled. Real Revit templates load on the left; everything on the right side is preview-only. |
| 2 | pending | Wire all simple-parameter rows: Scale, Display Model, Detail Level, Parts Visibility, Phase Filter, Discipline, Show Hidden Lines, Underlay Orientation, Orientation, Depth Clipping. Single + bulk. |
| 3 | pending | V/G Overrides — Model / Annotation / Analytical / Import (with CAD layer toggling) / Filters. Real `OverrideGraphicSettings` editor with color/weight/pattern/halftone/transparency. |
| 4 | pending | RVT Link Display Settings — `View.GetLinkOverrides` / `SetLinkOverrides`. Per-aspect ByHostView ⇄ ByLinkedView toggles. Custom mode is read-only via API; "Open in Revit's native dialog" button posts the native command. |
| 5 | pending | Graphic Display sub-dialogs — Model Display, Shadows, Sketchy Lines, Lighting, Photographic Exposure, Background. Each has its own settings object on `View`. |
| 6 | pending | Polish — per-view-type parameter filtering (greying rows that don't apply to the selected template's view type), Color Scheme wiring, View Range and System Color Schemes editors. |

After each iter ships and you confirm it, update the table here — keep status accurate.

## Revit API mapping

For when iter-2+ wires this up. All entries assume Revit 2024+.

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
| Color Scheme Location | `ROOM_COLOR_SCHEME_LOCATION` | |

### Dedicated-API rows (iter 3-5)

| Parameter | API |
|---|---|
| V/G Overrides categories | `view.SetCategoryOverrides(catId, OverrideGraphicSettings)` + `view.SetCategoryHidden(catId, bool)`. Build OGS once, loop templates inside one Transaction. |
| V/G Overrides filters | `view.AddFilter(fid)`, `view.SetFilterOverrides(fid, ogs)`, `view.SetFilterVisibility(fid, bool)`, `view.SetIsFilterEnabled(fid, bool)`, `view.GetFilters()`. |
| V/G Overrides RVT Links | `view.GetLinkOverrides(linkId)` → `RevitLinkGraphicsSettings`; `view.SetLinkOverrides(linkId, settings)`; `view.RemoveLinkOverrides(linkId)`. |
| View Range | `viewPlan.GetViewRange()` / `SetViewRange(PlanViewRange)`. |
| Depth Clipping | `viewPlan.GetDepthClipping()` / `SetDepthClipping(PlanViewDepthClipping)`. |
| Model Display | `view.GetViewDisplayModel()` / `SetViewDisplayModel`. |
| Shadows | `view.GetShadows()` / `SetShadows`. |
| Sketchy Lines | `view.GetSketchyLines()` / `SetSketchyLines`. |
| Lighting | `view.GetLighting()` / `SetLighting`. |
| Photographic Exposure | `view.GetPhotographicExposure()` / `SetPhotographicExposure`. |
| Background | `view.GetBackground()` / `SetBackground`. |
| Color Scheme | `view.GetColorFillSchemeId(catId)` / `SetColorFillSchemeId`. |

### Include column (every iter)

The Include column maps to:

- `view.GetTemplateParameterIds()` — every parameter that *could* be template-controlled
- `view.GetNonControlledTemplateParameterIds()` — those whose Include box is currently **unchecked**
- `view.SetNonControlledTemplateParameterIds(ICollection<ElementId>)` — only writer; pass the inverse of "what you want included"

In IronPython, mutate the .NET collection with `.Add()` / `.Remove()`, not Python list ops. Inputs must be a subset of `GetTemplateParameterIds()` — invalid IDs throw.

## API limitations (must respect)

- **`LinkVisibility.Custom` cannot be set via API.** Documented Autodesk limitation. The "Open in Revit's native dialog" button in `VgLinksDialog.xaml` is the escape hatch; iter 4 will wire it via `PostCommand` for the active template.
- **Per-tab category checklists in the Custom RVT-link dialog (Model / Annotation / Analytical / Import categories tabs) are UI-only.** The four placeholder tabs in `VgLinksDialog.xaml` show this fact to the user with a yellow notice banner; we never try to populate them with a checklist.
- **Schedules.** Most template fields (Fields / Sorting / Grouping / Formatting) need `ViewSchedule.Definition` APIs, not `Parameter.Set`. Out of scope until a later iteration.

## UI conventions in use

The XAMLs follow the repo-wide dbHMS design system documented in the root `CLAUDE.md`. The radio-button `ControlTemplate` in `ViewTemplatesManagerForm.xaml` and `VgLinksDialog.xaml` is the firm-standard 14 px outer / 7 px inner template — copy it intact if you add another XAML in this folder, don't shrink or restyle it.

## Bulk-edit semantics

When 2+ templates are checked:

1. Yellow `bnr_bulk` banner appears with a count message.
2. Apply column becomes visible on every row (`_set_apply_column_visible(True)`).
3. Each Value combo's `ToolTip` is set to a "(varies)" hint.
4. Footer's Apply button (iter 2+) iterates checked templates and for each row whose **Apply checkbox is true**, writes that value into the template inside one `revit.Transaction("Bulk apply view-template changes")`. If any one template fails, the whole transaction rolls back; iter 2 should add per-template error reporting.
5. The Include checkbox toggles `SetNonControlledTemplateParameterIds` once per template — same loop.

Per-template usage counts come from `build_usage_map(doc)` and are summed in bulk mode for the right-header "Total views affected: N" hint.

## Mock data

For iter-1, the sub-dialogs are fed by lists in `script.py`:

- `MOCK_RVT_LINKS` — 3 fake RVT links
- `MOCK_CAD_IMPORTS` — 2 fake DWG imports with 5-8 layers each
- `MOCK_MODEL_CATEGORIES`, `MOCK_ANNOTATION_CATEGORIES`, `MOCK_ANALYTICAL_CATEGORIES` — common Revit categories
- `MOCK_FILTERS` — 8 typical project filters
- `MOCK_SCALES`, `MOCK_DETAIL_LEVEL`, `MOCK_DISCIPLINE`, etc. — combobox options

Replacing each list with a real query is one of the iter-2 / iter-3 tasks.

## Testing

The shared `tests/test_extension_integrity.py` covers this tool:

- Pushbutton folder must exist (asserted in `test_expected_pushbuttons_exist`).
- All `.py` files parse via `ast.parse` (CPython 3 must be able to read; no f-strings, no Python-3-only syntax).
- All `.xaml` files parse as XML.
- No merge-conflict markers anywhere.

Run from repo root: `.\run_tests.ps1`.
