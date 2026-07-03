// dbHMS View Range Helper - web app.
//
// Revit's four view-range planes (Top, Cut, Bottom, View Depth) map onto GPU
// clipping planes, so we render the exported model slice two ways in one frame:
//   PLAN    = top-down orthographic camera clipped between Bottom and Top.
//   SECTION = orthographic cut along a draggable section line, with the four
//             planes drawn as draggable horizontal bars over the full height.
// The JS app is the single source of truth for the editable view-range state.
// Python sends the initial state once (meta) and acts again only on apply,
// detach, or edittemplate.
//
// Dev fallback: when not hosted in WebView2, the page auto-loads the sample
// fixture under web/sample/ so the whole UI is exercisable in a plain browser.

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const PLANE_KEYS   = ['top', 'cut', 'bot', 'vd'];
const PLANE_LABELS = { top: 'Top', cut: 'Cut Plane', bot: 'Bottom', vd: 'View Depth' };
const PLANE_COLORS = { top: '#38A169', cut: '#E53E3E', bot: '#3182CE', vd: '#805AD5' };
const GUIDE_COLOR  = '#A0AEC0';
// Special view-range level ids, matching Revit's GetLevelId/SetLevelId codes.
// These are the real ids Revit reads and writes (no JS-only remapping); see the
// matching block in script.py.
const SENTINEL = { UNLIMITED: -1, ABOVE: -2, ASSOCIATED: -3, BELOW: -4 };
const SENTINEL_NAME = { '-1': 'unlimited', '-2': 'above', '-3': 'associated', '-4': 'below' };
const EPS = 1e-6;

// ---------------------------------------------------------------------------
// Host bridge (WebView2). In dev (plain browser) hostAvailable is false.
// ---------------------------------------------------------------------------
const hostAvailable = !!(window.chrome && window.chrome.webview);
const DEV = !hostAvailable;
function post(s) { try { if (hostAvailable) window.chrome.webview.postMessage(s); } catch (e) {} }
function diag(s) { if (hostAvailable) post('diag:' + s); else console.log('[vr]', s); }

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------
const state = {
  meta: null,
  baseline: null,          // deep copy of meta.view_range for Revert
  planes: {},              // key -> { level_id, offset_ft, sentinel, ref_ft }
  modelRoot: null,
  modelBox: null,          // THREE.Box3 of the loaded model
  section: { a: null, b: null, lookSign: 1, depth: 1.5 },   // section line endpoints (glTF world) + flip + far-clip depth (m, ~5 ft default)
  // user zoom/pan per region (applied on top of the auto framing)
  view: { plan: { zoom: 1, panX: 0, panZ: 0 }, section: { zoom: 1, panU: 0, panV: 0 } },
  snap: { enabled: false, dist: 0.5 },
  locked: false,
  templateMode: false,     // editing a view template: level pickers are relative + read-only
  disabled: new Set(),     // disabled plane keys (RCP Bottom)
  applying: false,
  loaded: false,           // model arrived
  metaReady: false,
};

// ---------------------------------------------------------------------------
// three.js setup
// ---------------------------------------------------------------------------
const canvas   = document.getElementById('c');
const ovl      = document.getElementById('ovl');
const stage    = document.getElementById('stage');
const hintEl   = document.getElementById('hint');

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, stencil: true,
  preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.setScissorTest(true);
renderer.localClippingEnabled = true;   // material-level clip planes (for capping)
renderer.autoClear = false;             // we clear each scissor region by hand

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xe9eef3);

// The lit pass uses a flat light override (see litOverride), so lighting mostly
// does not matter; a single hemisphere is kept for any unoverridden path.
scene.add(new THREE.HemisphereLight(0xffffff, 0xc4ccd6, 1.2));

// MEP renders LAST from this separate scene, on top of the shell + poche, so any
// duct/pipe the section cuts through stays visible. Its element nodes are moved
// here from the model root in buildShowHide. A separate scene (rather than a 2nd
// render of `scene`) avoids repainting the clay shell over the poche.
const mepScene = new THREE.Scene();
let mepRoot = null;   // a Group under mepScene that holds the MEP element nodes

// Top-down ortho plan camera. North (+Revit Y -> glTF -Z) points up on screen.
const camPlan = new THREE.OrthographicCamera(-10, 10, 10, -10, -10000, 10000);
camPlan.up.set(0, 0, -1);
// Section camera (configured per section line in updateSectionCamera).
const camSection = new THREE.OrthographicCamera(-10, 10, 10, -10, -10000, 10000);
camSection.up.set(0, 1, 0);

// Per-region clip config. `shaded` clips the lit model; `capPlane` + `bound`
// drive the stencil poche fill so cut walls read solid (not hollow shells).
const clip = {
  plan:    { shaded: [], capPlane: null, bound: [] },
  section: { shaded: [], capPlane: null, bound: [] },
};

// --- Solid cut-fill (poche) via stencil capping ---------------------------
// Standard three.js technique: count model front/back faces into the stencil
// buffer along a cutting plane, then draw a flat quad on that plane wherever the
// stencil says we're inside solid. Turns hollow clipped shells into filled cuts.
const POCHE_COLOR = 0x39414f;   // dark slate, so cut walls pop against light floors
function stencilMat(side, op) {
  const m = new THREE.MeshBasicMaterial();
  m.depthWrite = false; m.depthTest = false; m.colorWrite = false;
  m.stencilWrite = true; m.stencilFunc = THREE.AlwaysStencilFunc;
  m.side = side; m.stencilFail = op; m.stencilZFail = op; m.stencilZPass = op;
  return m;
}
const backStencil  = stencilMat(THREE.BackSide,  THREE.IncrementWrapStencilOp);
const frontStencil = stencilMat(THREE.FrontSide, THREE.DecrementWrapStencilOp);
// The cap must STAMP DEPTH at the cut plane wherever an element is solid, so the
// later MEP pass can't paint that element's own geometry behind the cut (the part
// of a pipe/duct between the cut and the far-clip plane, visible end-on through
// the hollow cut) over the dark fill. Without depth here, a pipe whose body sits
// inside the scope extent bleeds its colour over the open cut and the
// cross-section reads gray instead of poche -- and pushing the view depth past the
// pipe's end is exactly what brings that geometry into range.
//   WebGL gotcha: depthWrite is ignored while depthTest is false (a disabled
// depth test bypasses ALL depth-buffer updates). So we keep the test ENABLED but
// force depthFunc=Always: the cap still draws unconditionally over the lit shell
// (nothing is ever in front of the cut), AND it now writes the cut-plane depth.
// MEP behind the cut then fails the normal LessEqual test and is occluded -> dark.
// The cap only draws/writes where stencil != 0 (the cut cross-section), so MEP
// runs travelling back into the view are untouched: they sit at other pixels.
const capMat = new THREE.MeshBasicMaterial({
  color: POCHE_COLOR, side: THREE.DoubleSide,
  depthTest: true, depthFunc: THREE.AlwaysDepth, depthWrite: true,
  stencilWrite: true, stencilRef: 0, stencilFunc: THREE.NotEqualStencilFunc,
  stencilFail: THREE.ReplaceStencilOp, stencilZFail: THREE.ReplaceStencilOp,
  stencilZPass: THREE.ReplaceStencilOp,
});
const capQuad = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), capMat);
capQuad.frustumCulled = false;
const capScene = new THREE.Scene();
capScene.add(capQuad);

// The lit pass (floor + below-cut context) renders with this flat, very light
// override so the floor reads as a pale plate regardless of the model's real
// material -- making the dark poche cut walls pop. Kept as the neutral fallback.
const litOverride = new THREE.MeshBasicMaterial({ color: 0xdfe4ea, side: THREE.DoubleSide });

// --- Shell (context) colouring -------------------------------------------
// Non-MEP elements used to ALL render in one flat light gray (litOverride), so
// furniture, doors, windows and walls blended into each other and into the
// background -- unreadable in the distance/projection. Give each CATEGORY its own
// muted tone instead: enough variety to tell elements apart, still desaturated so
// the bright flat-colour MEP stays the focus. Deterministic (same category ->
// same shade every launch) and scalable (any category not in the map hashes into
// the muted ramp), so there's nothing to maintain per project and no textures or
// extra export data. All tones are kept clearly darker than the #e9eef3
// background so even the lightest shell separates from empty space.
const SHELL_COLORS = {
  'Walls': 0xd6d1c6, 'Curtain Walls': 0xc9d2d6, 'Floors': 0xccd0d6,
  'Ceilings': 0xd1cec7, 'Roofs': 0xcfc8bc, 'Doors': 0xcbb393,
  'Windows': 0xb6cfdb, 'Curtain Panels': 0xbcd0d8, 'Furniture': 0xc9c3b2,
  'Furniture Systems': 0xc2c7bc, 'Casework': 0xcdb89c, 'Plumbing Fixtures': 0xc7ced0,
  'Columns': 0xc6cabf, 'Structural Columns': 0xc1c6ba, 'Structural Framing': 0xbcc1c8,
  'Structural Foundations': 0xc6c2b8, 'Stairs': 0xc3c7cd, 'Railings': 0xccced1,
  'Generic Models': 0xcecbc5, 'Specialty Equipment': 0xc6ccc1, 'Planting': 0xbfcab6,
  'Site': 0xc9cabd, 'Topography': 0xc7cabb, 'Parking': 0xc4c8cd, 'Mass': 0xcdc8cf,
};
// Muted fallback ramp for any category not named above (low saturation, varied
// hue + lightness, all below background lightness).
const SHELL_RAMP = [
  0xd3cdc2, 0xc7cad0, 0xd0c6bb, 0xc2c8bd, 0xcdc7ce,
  0xc6cac4, 0xd2cbbe, 0xbfc6ca, 0xcac4b6, 0xc4c7be,
];
function _hashStr(s) { var h = 0; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h); }
const _shellMatCache = new Map();
function shellMat(category) {
  const key = category || 'Other';
  if (_shellMatCache.has(key)) return _shellMatCache.get(key);
  let hex = SHELL_COLORS[key];
  if (hex === undefined) hex = SHELL_RAMP[_hashStr(key) % SHELL_RAMP.length];
  const m = new THREE.MeshBasicMaterial({ color: hex, side: THREE.DoubleSide });
  _shellMatCache.set(key, m);
  return m;
}

// The two stencil meshes (back-increment, front-decrement) must render in ONE
// renderer.render() call so their counts accumulate into the stencil buffer.
// They share one merged, world-space, position-only copy of the model.
const stencilScene = new THREE.Scene();
let backMesh = null, frontMesh = null, mergedStencilGeo = null;
// Build the poche stencil from a list of meshes (the VISIBLE elements). The cut
// cross-section of each fills solid so walls AND cut ducts/pipes read as section
// cuts. Rebuilt whenever visibility changes. Meshes may live under modelRoot
// (shell) or mepRoot (MEP), so update both worlds before baking.
function buildStencilFromMeshes(meshes) {
  if (state.modelRoot) state.modelRoot.updateMatrixWorld(true);
  if (mepRoot) mepScene.updateMatrixWorld(true);
  const geos = [];
  for (const o of meshes) {
    if (o.isMesh && o.geometry && o.geometry.getAttribute('position')) {
      const src = o.geometry.index ? o.geometry.toNonIndexed() : o.geometry;
      const ng = new THREE.BufferGeometry();
      ng.setAttribute('position', src.getAttribute('position').clone());
      ng.applyMatrix4(o.matrixWorld);     // bake instance/world transform
      geos.push(ng);
      if (src !== o.geometry) src.dispose();
    }
  }
  if (backMesh) { stencilScene.remove(backMesh); stencilScene.remove(frontMesh); }
  if (mergedStencilGeo) mergedStencilGeo.dispose();
  mergedStencilGeo = geos.length ? mergeGeometries(geos, false) : null;
  geos.forEach(g => g.dispose());
  if (mergedStencilGeo) {
    backMesh = new THREE.Mesh(mergedStencilGeo, backStencil);
    frontMesh = new THREE.Mesh(mergedStencilGeo, frontStencil);
    backMesh.renderOrder = 1; frontMesh.renderOrder = 2;
    backMesh.frustumCulled = false; frontMesh.frustumCulled = false;
    stencilScene.add(backMesh); stencilScene.add(frontMesh);
  } else {
    backMesh = null; frontMesh = null;
  }
}
function placeCap(plane) {
  const n = plane.normal;
  // Center the quad on the MODEL (projected onto the plane), not the glTF origin.
  // If the export offset isn't the exact building center, an origin-centered quad
  // misses the far side and the poche cuts off at a straight line. Size to the
  // full model diagonal so it always covers the footprint.
  const center = state.modelBox
    ? state.modelBox.getCenter(new THREE.Vector3()) : new THREE.Vector3();
  const dist = n.dot(center) + plane.constant;        // signed distance to the plane
  capQuad.position.copy(center).addScaledVector(n, -dist);   // center, projected onto plane
  capQuad.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), n.clone().normalize());
  const size = state.modelBox
    ? state.modelBox.getSize(new THREE.Vector3()).length() * 1.6 + 10 : 100;
  capQuad.scale.set(size, size, 1);
  capQuad.updateMatrixWorld();
}

const loader = new GLTFLoader();

// Per-frame pixel rects (CSS px) for each region, and linear inverse maps.
let split = 0.5;
const regions = {
  plan:    { x: 0, y: 0, w: 1, h: 1 },
  section: { x: 0, y: 0, w: 1, h: 1 },
};

// ---------------------------------------------------------------------------
// Units helpers (depend on meta)
// ---------------------------------------------------------------------------
function ftToM()    { return state.meta ? state.meta.ft_to_m : 0.3048; }
function offsetZ()  { return state.meta ? state.meta.offset_ft[2] : 0; }
function feetToY(E) { return (E - offsetZ()) * ftToM(); }
function yToFeet(Y) { return offsetZ() + Y / ftToM(); }
// All internal positioning is in Revit internal-origin feet (matching the
// exported geometry). For DISPLAY, absolute elevations are shown as height above
// the floor this view is on (associated level = 0'-0"): display_datum_ft is that
// floor's elevation, supplied by the host. We subtract it ONLY when formatting an
// absolute elevation for display, never when positioning a bar (so bars stay
// glued to the model). Offsets are relative to a level and so are datum-
// independent -- never pass an offset here.
function displayDatum() { return state.meta && state.meta.display_datum_ft ? state.meta.display_datum_ft : 0; }
function fmtElev(feet)  { return feet == null ? fmtFeetIn(null) : fmtFeetIn(feet - displayDatum()); }

// ---------------------------------------------------------------------------
// Plane state math
// ---------------------------------------------------------------------------
function levelById(id) {
  if (!state.meta) return null;
  return state.meta.levels.find(l => l.id === id) || null;
}
function assocLevel() {
  return state.meta ? levelById(state.meta.view.associated_level_id) : null;
}
// Reference elevation (feet) for a level_id selection, mirroring Revit's
// absolute_z_for_plane: real level -> its elevation; sentinels resolve against
// the view's associated level. Unlimited -> null (off-canvas).
function resolveRefFt(level_id) {
  if (level_id === SENTINEL.UNLIMITED) return null;
  if (level_id === SENTINEL.ASSOCIATED) {   // template: relative to the view's level
    const base = assocLevel();
    return base ? base.elev_ft : 0;
  }
  const sorted = state.meta.levels;   // ascending by elevation
  if (level_id === SENTINEL.ABOVE || level_id === SENTINEL.BELOW) {
    const base = assocLevel();
    if (!base) return 0;
    const idx = sorted.findIndex(l => l.id === base.id);
    if (idx < 0) return base.elev_ft;
    if (level_id === SENTINEL.ABOVE) return idx + 1 < sorted.length ? sorted[idx + 1].elev_ft : base.elev_ft;
    return idx - 1 >= 0 ? sorted[idx - 1].elev_ft : base.elev_ft;
  }
  const lv = levelById(level_id);
  if (lv) return lv.elev_ft;
  const base = assocLevel();
  return base ? base.elev_ft : 0;
}
function absFt(p) {
  if (!p || p.sentinel === 'unlimited' || p.ref_ft == null) return null;
  return p.ref_ft + p.offset_ft;
}
function sentinelFromId(level_id) {
  if (level_id < 0) return SENTINEL_NAME[String(level_id)] || null;
  return null;
}

// ---------------------------------------------------------------------------
// Feet-inch formatting / parsing / snapping (ported from the old tool)
// ---------------------------------------------------------------------------
function fmtFeetIn(feet) {
  if (feet == null) return '-';
  if (Math.abs(feet) < EPS) feet = 0;   // avoid "-0'-0\"" from tiny negatives
  const sign = feet < 0 ? '-' : '';
  const f = Math.abs(feet);
  let whole = Math.floor(f);
  let inches = (f - whole) * 12.0;
  let wi = Math.round(inches * 16.0) / 16.0;
  if (wi >= 12.0 - EPS) { whole += 1; wi = 0.0; }
  // {:g}-style: trim trailing zeros
  const inStr = (Math.round(wi * 16) / 16).toString();
  return sign + whole + "'-" + inStr + '"';
}
function parseOffset(s) {
  if (s == null) return null;
  let t = String(s).trim();
  if (!t) return null;
  if (/^[-+]?(\d+\.?\d*|\.\d+)$/.test(t)) return parseFloat(t);   // plain decimal feet
  let neg = false;
  if (t[0] === '-') { neg = true; t = t.slice(1).trim(); }
  else if (t[0] === '+') { t = t.slice(1).trim(); }
  let feet = 0, inches = 0, matched = false;
  const mf = t.match(/(\d+(?:\.\d+)?)\s*['’]/);   // feet marker ' or right-quote
  if (mf) { feet = parseFloat(mf[1]); matched = true; t = t.slice(mf.index + mf[0].length); }
  const mi = t.match(/(\d+(?:\.\d+)?)(?:\s+(\d+)\/(\d+))?\s*["”]?/);
  if (mi && mi[1] !== undefined && mi[1] !== '') {
    inches = parseFloat(mi[1]);
    if (mi[2] && mi[3]) inches += parseFloat(mi[2]) / parseFloat(mi[3]);
    matched = true;
  }
  if (!matched) return null;
  const val = feet + inches / 12.0;
  return neg ? -val : val;
}
function snap(feet) {
  const d = state.snap.dist;
  if (state.snap.enabled && d > 0) return Math.round(feet / d) * d;
  return feet;
}

// ---------------------------------------------------------------------------
// Model loading
// ---------------------------------------------------------------------------
function clearModel() {
  const dispose = root => root && root.traverse(o => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) {
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      mats.forEach(m => m && m.dispose());
    }
  });
  if (mepRoot) { mepScene.remove(mepRoot); dispose(mepRoot); mepRoot = null; }
  if (state.modelRoot) {
    scene.remove(state.modelRoot);
    dispose(state.modelRoot);          // MEP nodes were moved to mepRoot (disposed above)
    state.modelRoot = null;
  }
  showHide.built = false;
}
function onLoaded(gltf) {
  clearModel();
  state.modelRoot = gltf.scene || gltf.scenes[0];
  scene.add(state.modelRoot);
  let meshes = 0, tris = 0;
  state.modelRoot.traverse(o => {
    if (o.isMesh) {
      meshes++;
      const g = o.geometry;
      if (g && g.index) tris += g.index.count / 3;
      else if (g && g.attributes.position) tris += g.attributes.position.count / 3;
      if (o.material) {
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        mats.forEach(m => { if (m) { m.side = THREE.DoubleSide; m.clipShadows = true; } });
      }
    }
  });
  state.modelBox = new THREE.Box3().setFromObject(state.modelRoot);
  state.loaded = true;
  diag('loaded: ' + meshes + ' meshes, ' + Math.round(tris) + ' tris');
  // Coloring (clay shell vs flat Revit-colour MEP) + the poche stencil are set
  // up in buildShowHide, which needs the meta's shell_categories. onReadyMaybe
  // fires it once both the model and meta have arrived.
  onReadyMaybe();   // section line is initialized there, once meta (orientation) is known
}
function loadFromUrl(url) {
  hintEl.style.display = 'block'; hintEl.textContent = 'Loading view-range slice…';
  loader.load(url, onLoaded, undefined, err => {
    hintEl.textContent = 'Failed to load the model slice.';
    diag('load error: ' + (err && err.message ? err.message : err));
  });
}
function b64ToBuffer(b64) {
  const bin = atob(b64), len = bin.length, bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}
function loadFromBuffer(buf) {
  hintEl.style.display = 'block';
  loader.parse(buf, '', onLoaded, err => {
    hintEl.textContent = 'Failed to parse the model slice.';
    diag('parse error: ' + (err && err.message ? err.message : err));
  });
}

// ---------------------------------------------------------------------------
// Visibility (model / category / workset)
//
// The .glb carries EVERY model category + workset (host + links). The visibility
// panel mirrors the 3D Viewer: one row per model (a whole-model checkbox + an
// Edit button that opens a dialog of that model's categories + worksets). An
// element is shown only if its model AND its category AND its workset are all on.
// Categories/worksets the source plan hides (meta.hidden_categories /
// hidden_worksets) start OFF but stay present, so any can be switched back on to
// chase the Cut/Top plane up to it. Shell categories (meta.shell_categories)
// render as the clay/poche cut; everything else renders in its flat Revit colour.
// ---------------------------------------------------------------------------
const SEP = '␟';   // unit-separator: safe join of model + name into a key
// MEP trades render in their solid Revit colour so the engineer can read them like
// the plan (e.g. green waste piping). EVERYTHING else -- architecture, structure,
// furniture, generic models, anything with no MEP discipline -- renders as the flat
// gray clay shell with NO texture. Discipline comes from the .glb element extras.
const MEP_DISCIPLINES = new Set(['Mechanical', 'Plumbing', 'Fire Protection', 'Electrical', 'Technology']);
const showHide = {
  built: false,
  elements: [],            // [{ node, model, category, workset, isShell, meshes:[] }]
  models: new Map(),       // model -> { cats:Set, ws:Set }
  hiddenModels: new Set(), // model names
  hiddenCats: new Set(),   // "model␟category"
  hiddenWs: new Set(),     // "model␟workset"
};
function vkey(model, name) { return model + SEP + name; }
function isHostModel(model) {
  return !!(state.meta && state.meta.host_model && model === state.meta.host_model);
}

// Flat, unlit material in the element's solid Revit colour -- NO texture map (the
// tool shows clean flat colours, never wood/concrete/etc. textures). Cached per
// source material so shared Revit materials share one GPU material.
const _flatMatCache = new Map();
function revitColorMat(orig) {
  if (_flatMatCache.has(orig)) return _flatMatCache.get(orig);
  const col = (orig && orig.color) ? orig.color.clone() : new THREE.Color(0xb0b6be);
  const m = new THREE.MeshBasicMaterial({
    color: col, side: THREE.DoubleSide,           // no map: textures are never shown
    transparent: !!(orig && orig.transparent),
    opacity: (orig && orig.opacity != null) ? orig.opacity : 1.0,
    // MEP draws last (from mepScene), AFTER the poche, with normal depth testing
    // so near pipes correctly occlude pipes behind them. At a cut cross-section the
    // poche cap now writes depth, so MEP can't paint the element's own interior
    // back wall over the dark fill -- the cut reads as poche, not gray.
    depthTest: true, depthWrite: true,
  });
  _flatMatCache.set(orig, m);
  return m;
}

function buildShowHide() {
  if (!state.modelRoot || !state.meta) return;
  showHide.elements = []; showHide.models = new Map();
  state.modelRoot.traverse(o => {
    if (!(o.userData && o.userData.element_id !== undefined)) return;
    const model = o.userData.model || 'Model';
    const category = o.userData.category || 'Other';
    const workset = o.userData.workset || '';
    // MEP trades get their flat Revit colour; everything else is the gray shell.
    const isShell = !MEP_DISCIPLINES.has(o.userData.discipline || '');
    const meshes = [];
    o.traverse(m => { if (m.isMesh) meshes.push(m); });
    // Colour: a muted per-category shade for the shell (so context elements are
    // distinguishable, not one flat gray), flat Revit colour for MEP.
    for (const m of meshes) {
      m.material = isShell ? shellMat(category) : revitColorMat(m.material);
      m.material.side = THREE.DoubleSide;
    }
    showHide.elements.push({ node: o, model, category, workset, isShell, meshes });
    if (!showHide.models.has(model)) showHide.models.set(model, { cats: new Set(), ws: new Set() });
    const mi = showHide.models.get(model);
    mi.cats.add(category); if (workset) mi.ws.add(workset);
  });
  // Move MEP element nodes into the dedicated mepScene so they render last (over
  // the poche) without re-rendering the main scene. attach() preserves each node's
  // world transform; shell nodes stay in modelRoot (and feed the poche stencil).
  if (mepRoot) mepScene.remove(mepRoot);
  mepRoot = new THREE.Group();
  mepScene.add(mepRoot);
  state.modelRoot.updateMatrixWorld(true);
  for (const el of showHide.elements) if (!el.isShell) mepRoot.attach(el.node);
  // Default-off: categories/worksets the source plan hides (matched by name).
  const hidCats = new Set(state.meta.hidden_categories || []);
  const hidWs = new Set(state.meta.hidden_worksets || []);
  showHide.hiddenModels = new Set();
  showHide.hiddenCats = new Set();
  showHide.hiddenWs = new Set();
  for (const [model, mi] of showHide.models) {
    for (const c of mi.cats) if (hidCats.has(c)) showHide.hiddenCats.add(vkey(model, c));
    for (const w of mi.ws) if (hidWs.has(w)) showHide.hiddenWs.add(vkey(model, w));
  }
  applyVisibility();
  buildVisibilityPanel();
  showHide.built = true;
  diag('visibility: ' + showHide.models.size + ' model(s)');
}

// An element shows only when its model, category, AND workset are all on.
function elementVisible(el) {
  if (showHide.hiddenModels.has(el.model)) return false;
  if (showHide.hiddenCats.has(vkey(el.model, el.category))) return false;
  if (el.workset && showHide.hiddenWs.has(vkey(el.model, el.workset))) return false;
  return true;
}

// Push the visible set onto the scene graph and rebuild the poche stencil. The
// stencil fills the cut cross-section of every VISIBLE element (shell AND MEP), so
// a duct/pipe the section slices through reads as a solid filled cut (like Revit)
// instead of a see-through hollow tube end-on. The element's real colour is then
// drawn over it by the MEP pass.
function applyVisibility() {
  const cutMeshes = [];
  for (const el of showHide.elements) {
    const vis = elementVisible(el);
    el.node.visible = vis;
    if (vis) for (const m of el.meshes) cutMeshes.push(m);
  }
  buildStencilFromMeshes(cutMeshes);
}

// Per-pass visibility: render shell, then the poche fill, then MEP on top, so
// anything the section cuts through stays visible (the poche, drawn depth-test-off,
// would otherwise paint over it). Always gated by elementVisible so hidden
// categories/worksets stay hidden in every pass.
function setPassVisibility(which) {
  for (const el of showHide.elements) {
    const uv = elementVisible(el);
    el.node.visible = which === 'shell' ? (uv && el.isShell)
                    : which === 'mep'   ? (uv && !el.isShell)
                    : uv;
  }
}

// One row per model: whole-model checkbox + Edit button (opens the dialog).
function buildVisibilityPanel() {
  const host = document.getElementById('shtree');
  if (!host) return;
  host.replaceChildren();
  const models = Array.from(showHide.models.keys())
    .sort((a, b) => (isHostModel(b) ? 1 : 0) - (isHostModel(a) ? 1 : 0) || a.localeCompare(b));
  for (const model of models) {
    const row = document.createElement('div'); row.className = 'visrow';
    const lbl = document.createElement('label'); lbl.className = 'vismodel';
    const chk = document.createElement('input'); chk.type = 'checkbox';
    chk.checked = !showHide.hiddenModels.has(model);
    const nm = document.createElement('span'); nm.className = 'vismname';
    nm.textContent = model + (isHostModel(model) ? '  (this model)' : '');
    lbl.appendChild(chk); lbl.appendChild(nm);
    const edit = document.createElement('button'); edit.className = 'visedit'; edit.textContent = 'Edit…';
    row.appendChild(lbl); row.appendChild(edit);
    host.appendChild(row);
    chk.addEventListener('change', () => {
      if (chk.checked) showHide.hiddenModels.delete(model); else showHide.hiddenModels.add(model);
      applyVisibility();
    });
    edit.addEventListener('click', () => openVisDialog(model));
  }
}

// --- Edit dialog: one model's categories + worksets (Apply/Cancel) ----------
let _visDlgModel = null;
function openVisDialog(model) {
  _visDlgModel = model;
  const mi = showHide.models.get(model) || { cats: new Set(), ws: new Set() };
  document.getElementById('vismodal_title').textContent = model;
  fillVisList('vis_cats', Array.from(mi.cats).sort(),
    c => !showHide.hiddenCats.has(vkey(model, c)), 'No model categories.');
  fillVisList('vis_ws', Array.from(mi.ws).sort(),
    w => !showHide.hiddenWs.has(vkey(model, w)), 'No worksets (model not workshared).');
  document.getElementById('vismodal').classList.add('show');
}
function fillVisList(id, items, isOn, emptyMsg) {
  const host = document.getElementById(id);
  host.replaceChildren();
  if (!items.length) {
    const p = document.createElement('div'); p.className = 'visempty'; p.textContent = emptyMsg;
    host.appendChild(p); return;
  }
  for (const name of items) {
    const row = document.createElement('label'); row.className = 'vischk';
    const chk = document.createElement('input'); chk.type = 'checkbox';
    chk.checked = isOn(name); chk.dataset.name = name;
    const sp = document.createElement('span'); sp.textContent = name;
    row.appendChild(chk); row.appendChild(sp); host.appendChild(row);
  }
}
function visDlgSetAll(id, on) {
  for (const c of document.querySelectorAll('#' + id + ' input')) c.checked = on;
}
function applyVisDialog() {
  const model = _visDlgModel;
  if (model) {
    for (const c of document.querySelectorAll('#vis_cats input')) {
      const key = vkey(model, c.dataset.name);
      if (c.checked) showHide.hiddenCats.delete(key); else showHide.hiddenCats.add(key);
    }
    for (const c of document.querySelectorAll('#vis_ws input')) {
      const key = vkey(model, c.dataset.name);
      if (c.checked) showHide.hiddenWs.delete(key); else showHide.hiddenWs.add(key);
    }
    applyVisibility();
  }
  closeVisDialog();
}
function closeVisDialog() {
  document.getElementById('vismodal').classList.remove('show');
  _visDlgModel = null;
}

// ---------------------------------------------------------------------------
// Meta application
// ---------------------------------------------------------------------------
function applyMeta(meta) {
  state.meta = meta;
  state.baseline = JSON.parse(JSON.stringify(meta.view_range));
  state.disabled = new Set(meta.disabled_planes || []);
  state.locked = !!(meta.template_lock && meta.template_lock.locked);
  state.templateMode = !!meta.template_mode;
  if (meta.snap) { state.snap.enabled = !!meta.snap.enabled; state.snap.dist = meta.snap.distance_ft || 0.5; }
  loadPlanesFrom(meta.view_range);
  state.metaReady = true;
  document.getElementById('viewname').textContent = meta.view ? meta.view.name : '';
  syncSnapUi();
  buildEditor();
  applyBannerState();
  onReadyMaybe();
}
function loadPlanesFrom(vr) {
  state.planes = {};
  for (const k of PLANE_KEYS) {
    const r = vr[k] || {};
    const sentinel = r.sentinel || sentinelFromId(r.level_id);
    let ref_ft;
    if (sentinel === 'unlimited' || r.abs_ft == null) ref_ft = (sentinel === 'unlimited' ? null : resolveRefFt(r.level_id));
    else ref_ft = r.abs_ft - r.offset_ft;
    state.planes[k] = { level_id: r.level_id, offset_ft: r.offset_ft || 0,
      sentinel: sentinel || null, ref_ft, level_name: r.level_name || '' };
  }
}
// Both model and meta present -> reveal the editor and start fresh framing.
function onReadyMaybe() {
  if (!(state.metaReady && state.loaded)) return;
  hintEl.style.display = 'none';
  document.getElementById('editor').style.display = state.locked ? 'none' : '';
  if (!showHide.built) buildShowHide();
  if (!state.section.a) initSectionLine();
  resize();
}

// ---------------------------------------------------------------------------
// Section line defaults + camera
// ---------------------------------------------------------------------------
function initSectionLine() {
  if (!state.modelBox || state.modelBox.isEmpty()) return;
  const b = state.modelBox;
  const center = b.getCenter(new THREE.Vector3());
  const right = new THREE.Vector3().crossVectors(planUp(), new THREE.Vector3(0, 1, 0)).normalize();
  let half = 0;
  for (const c of boxCorners(b)) half = Math.max(half, Math.abs(c.clone().sub(center).dot(right)));
  half += (b.max.x - b.min.x) * 0.04 + 0.2;
  const c0 = new THREE.Vector3(center.x, 0, center.z);
  state.section.a = c0.clone().addScaledVector(right, -half);   // a horizontal cut across the plan
  state.section.b = c0.clone().addScaledVector(right,  half);
}
// Horizontal unit direction of the section line (Y zeroed).
function sectionDir() {
  const d = state.section.b.clone().sub(state.section.a); d.y = 0;
  if (d.lengthSq() < 1e-9) d.set(1, 0, 0);
  return d.normalize();
}
// Horizontal normal of the cut plane.
function sectionNormal() {
  const d = sectionDir();
  return new THREE.Vector3().crossVectors(d, new THREE.Vector3(0, 1, 0)).normalize();
}
function updateClips() {
  if (!state.meta) return;
  const botY = feetToY(absFt(state.planes.bot));
  const cutY = feetToY(absFt(state.planes.cut));
  // PLAN: lit model from Bottom up to the Cut plane; poche fill AT the cut so
  // walls read as solid sections (looking straight down, vertical faces are
  // edge-on and would otherwise vanish). Top/View-Depth shape the section.
  const loY = Math.min(botY, cutY), hiY = Math.max(botY, cutY);
  clip.plan.shaded = [
    new THREE.Plane(new THREE.Vector3(0, -1, 0),  hiY),   // y <= cut
    new THREE.Plane(new THREE.Vector3(0,  1, 0), -loY),   // y >= bottom
  ];
  clip.plan.capPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), cutY); // cap at cut
  // No bottom bound: a closed floor slab then nets zero in the stencil (the ray
  // enters and exits it), so only solids the cut plane actually passes through
  // (walls, columns) fill. Bounding at the floor would clip its underside away
  // and leave a top face that floods the whole footprint with poche.
  clip.plan.bound = [];

  // SECTION: like Revit, the section line's length defines the section's width.
  // Clip to the band between the line's endpoints (along the line direction d) so
  // only the building under the line shows; a thin lit slab at the cut adds depth,
  // and the cut cross-section is poche-filled (floor slabs become horizontal
  // bands, walls/columns verticals). Straight-on ortho -> a flat section.
  const look = sectionNormal().multiplyScalar(state.section.lookSign);
  const c = look.dot(state.section.a);
  const d = sectionDir();
  const ua = state.section.a.dot(d), ub = state.section.b.dot(d);
  const uMin = Math.min(ua, ub), uMax = Math.max(ua, ub);
  const uClips = [
    new THREE.Plane(d.clone(),          -uMin),   // keep p.d >= uMin (line start)
    new THREE.Plane(d.clone().negate(),  uMax),   // keep p.d <= uMax (line end)
  ];
  // Like Revit's far clip: show from just behind the cut out to the section
  // depth in the look direction, so pushing the far-clip line out reveals more
  // of the model behind the cut.
  const hd = 0.05;                      // a hair behind the cut so its face renders
  const depth = state.section.depth;    // far-clip distance, in the look direction
  clip.section.shaded = [
    new THREE.Plane(look.clone(),          -(c - hd)),      // keep look.p >= c-hd (just behind the cut)
    new THREE.Plane(look.clone().negate(),   (c + depth)),  // keep look.p <= c+depth (far clip)
  ].concat(uClips);
  clip.section.capPlane = new THREE.Plane(look.clone(), -c);   // cap the cut cross-section
  clip.section.bound = uClips;                                 // ...within the line extent
}
function updateSectionCamera() {
  if (!state.modelBox || state.modelBox.isEmpty()) return;
  const b = state.modelBox;
  const d = sectionDir();
  const up = new THREE.Vector3(0, 1, 0);
  const n = sectionNormal();
  const lookDir = n.clone().multiplyScalar(state.section.lookSign);
  // Horizontal extent = the section LINE length (Revit-style); vertical extent =
  // the building height (from the model box corners projected onto up).
  const ua = state.section.a.dot(d), ub = state.section.b.dot(d);
  let uMin = Math.min(ua, ub), uMax = Math.max(ua, ub);
  let vMin = Infinity, vMax = -Infinity;
  const corners = [
    [b.min.x, b.min.y, b.min.z], [b.max.x, b.min.y, b.min.z],
    [b.min.x, b.max.y, b.min.z], [b.max.x, b.max.y, b.min.z],
    [b.min.x, b.min.y, b.max.z], [b.max.x, b.min.y, b.max.z],
    [b.min.x, b.max.y, b.max.z], [b.max.x, b.max.y, b.max.z],
  ];
  for (const c of corners) {
    const w = new THREE.Vector3(c[0], c[1], c[2]).dot(up);
    if (w < vMin) vMin = w; if (w > vMax) vMax = w;
  }
  // Also include the full vertical span of the plane bars so an Above/Unlimited
  // plane never frames out of view.
  for (const k of PLANE_KEYS) {
    const a = absFt(state.planes[k]);
    if (a != null) { const y = feetToY(a); if (y < vMin) vMin = y; if (y > vMax) vMax = y; }
  }
  const sv = state.view.section;
  const centerU = (uMin + uMax) / 2 + sv.panU, centerV = (vMin + vMax) / 2 + sv.panV;
  const cPlane = n.dot(state.section.a);
  const center = new THREE.Vector3()
    .addScaledVector(d, centerU).addScaledVector(up, centerV).addScaledVector(n, cPlane);
  const diag3 = Math.max(b.getSize(new THREE.Vector3()).length(), 1);
  const dist = diag3 * 2 + 20;
  camSection.position.copy(center).addScaledVector(lookDir, -dist);
  camSection.up.copy(up);
  camSection.lookAt(center);
  const padU = (uMax - uMin) * 0.06 + 0.3, padV = (vMax - vMin) * 0.12 + 0.4;
  let hw = (uMax - uMin) / 2 + padU, hh = (vMax - vMin) / 2 + padV;
  const r = regions.section;
  const aspect = r.w / Math.max(r.h, 1);
  if (hw / hh < aspect) hw = hh * aspect; else hh = hw / aspect;
  hw /= sv.zoom; hh /= sv.zoom;
  camSection.left = -hw; camSection.right = hw; camSection.top = hh; camSection.bottom = -hh;
  camSection.near = 0.1; camSection.far = dist * 2 + diag3;
  camSection.updateProjectionMatrix();
}
// Screen-up for the plan, in glTF world. Honors the Revit view's UpDirection
// (so a building rotated in world coords still reads square), falling back to
// north-up. Revit XY direction (dx,dy) -> glTF (dx, 0, -dy).
function planUp() {
  const ud = state.meta && state.meta.view && state.meta.view.up_dir;
  if (ud && (ud[0] || ud[1])) {
    const v = new THREE.Vector3(ud[0], 0, -ud[1]);
    if (v.lengthSq() > 1e-9) return v.normalize();
  }
  return new THREE.Vector3(0, 0, -1);
}
function updatePlanCamera() {
  if (!state.modelBox || state.modelBox.isEmpty()) return;
  const b = state.modelBox;
  const up = planUp();
  const right = new THREE.Vector3().crossVectors(up, new THREE.Vector3(0, 1, 0)).normalize();
  const center = b.getCenter(new THREE.Vector3());
  // Extent measured along the view's right/up axes so a rotated plan frames tight.
  let halfW = 0, halfD = 0;
  for (const c of boxCorners(b)) {
    const rel = c.clone().sub(center);
    halfW = Math.max(halfW, Math.abs(rel.dot(right)));
    halfD = Math.max(halfD, Math.abs(rel.dot(up)));
  }
  halfW = Math.max(halfW, 0.5); halfD = Math.max(halfD, 0.5);
  const pad = 1.08;
  const r = regions.plan;
  const aspect = r.w / Math.max(r.h, 1);
  let hw = halfW * pad, hh = halfD * pad;
  if (hw / hh < aspect) hw = hh * aspect; else hh = hw / aspect;
  const v = state.view.plan, z = v.zoom;
  camPlan.left = -hw / z; camPlan.right = hw / z; camPlan.top = hh / z; camPlan.bottom = -hh / z;
  const cx = center.x + v.panX, cz = center.z + v.panZ;
  const size = b.getSize(new THREE.Vector3());
  camPlan.position.set(cx, b.max.y + size.y + 50, cz);
  camPlan.up.copy(up);
  camPlan.lookAt(cx, center.y, cz);      // straight down through the panned target
  camPlan.near = -1; camPlan.far = (size.y + 200) * 2;
  camPlan.updateProjectionMatrix();
}
function boxCorners(b) {
  return [
    new THREE.Vector3(b.min.x, b.min.y, b.min.z), new THREE.Vector3(b.max.x, b.min.y, b.min.z),
    new THREE.Vector3(b.min.x, b.max.y, b.min.z), new THREE.Vector3(b.max.x, b.max.y, b.min.z),
    new THREE.Vector3(b.min.x, b.min.y, b.max.z), new THREE.Vector3(b.max.x, b.min.y, b.max.z),
    new THREE.Vector3(b.min.x, b.max.y, b.max.z), new THREE.Vector3(b.max.x, b.max.y, b.max.z),
  ];
}

// ---------------------------------------------------------------------------
// Projection helpers (world -> region pixel)
// ---------------------------------------------------------------------------
function projectTo(vec, cam, r) {
  const v = vec.clone().project(cam);
  return {
    x: r.x + (v.x * 0.5 + 0.5) * r.w,
    y: r.y + (1 - (v.y * 0.5 + 0.5)) * r.h,
  };
}
// Inverse linear map for the plan region: screen px -> world (X,Z) at Y=0.
let planInv = null;
function buildPlanInverse() {
  const r = regions.plan;
  const p0 = projectTo(new THREE.Vector3(0, 0, 0), camPlan, r);
  const px = projectTo(new THREE.Vector3(1, 0, 0), camPlan, r);
  const pz = projectTo(new THREE.Vector3(0, 0, 1), camPlan, r);
  const a = px.x - p0.x, b = pz.x - p0.x, c = px.y - p0.y, d = pz.y - p0.y;
  const det = a * d - b * c;
  planInv = Math.abs(det) < 1e-9 ? null : { p0, ia: d / det, ib: -b / det, ic: -c / det, id: a / det };
}
function planScreenToWorld(sx, sy) {
  if (!planInv) return new THREE.Vector3();
  const dx = sx - planInv.p0.x, dy = sy - planInv.p0.y;
  return new THREE.Vector3(planInv.ia * dx + planInv.ib * dy, 0, planInv.ic * dx + planInv.id * dy);
}
// Section screen pixel -> world point (its components along d/up are exact; the
// depth along the look direction is arbitrary, which is fine for zoom-to-cursor).
function sectionScreenToWorld(sx, sy) {
  const r = regions.section;
  const ndcX = ((sx - r.x) / r.w) * 2 - 1;
  const ndcY = -(((sy - r.y) / r.h) * 2 - 1);
  return new THREE.Vector3(ndcX, ndcY, 0.5).unproject(camSection);
}
// Inverse for the section region: screen Y px -> world Y (m). Linear since up=Y.
let secInv = null;
function buildSectionInverse() {
  const r = regions.section;
  const center = new THREE.Vector3();
  // sample two world points differing only in Y near the cut plane
  const n = sectionNormal();
  const cPlane = n.dot(state.section.a);
  const d = sectionDir();
  const base = new THREE.Vector3().addScaledVector(n, cPlane).addScaledVector(d, state.section.a.dot(d));
  const pA = projectTo(base.clone().setY(0), camSection, r);
  const pB = projectTo(base.clone().setY(1), camSection, r);
  const dyPix = pB.y - pA.y;
  secInv = Math.abs(dyPix) < 1e-9 ? null : { y0: 0, sy0: pA.y, k: 1 / dyPix };
}
function secScreenYToWorldY(sy) {
  if (!secInv) return 0;
  return secInv.y0 + (sy - secInv.sy0) * secInv.k;
}

// ---------------------------------------------------------------------------
// Resize / region layout
// ---------------------------------------------------------------------------
function resize() {
  const w = stage.clientWidth, h = stage.clientHeight;
  if (w === 0 || h === 0) return;
  renderer.setSize(w, h, false);
  const wL = Math.floor(w * split), wR = w - wL;
  regions.plan = { x: 0, y: 0, w: wL, h };
  regions.section = { x: wL, y: 0, w: wR, h };
  ovl.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  ovl.setAttribute('width', w); ovl.setAttribute('height', h);
  const div = document.getElementById('divider'); div.style.left = wL + 'px';
  document.getElementById('lbl_sec').style.left = (wL + 8) + 'px';
  updatePlanCamera();
  updateSectionCamera();
}
window.addEventListener('resize', resize);
new ResizeObserver(resize).observe(stage);

// ---------------------------------------------------------------------------
// Render loop
// ---------------------------------------------------------------------------
function renderRegion(camera, cfg, region) {
  renderer.setViewport(region.x, 0, region.w, region.h);
  renderer.setScissor(region.x, 0, region.w, region.h);
  renderer.clear(true, true, true);          // color + depth + stencil (within scissor)

  // 1. model, clipped to this region's slice.
  renderer.clippingPlanes = cfg.shaded;
  renderer.render(scene, camera);

  // 2. poche fill at the cut plane
  if (cfg.capPlane && mergedStencilGeo) {
    renderer.clear(false, false, true);      // reset stencil only
    backStencil.clippingPlanes = [cfg.capPlane];
    frontStencil.clippingPlanes = [cfg.capPlane];
    renderer.clippingPlanes = cfg.bound;
    renderer.render(stencilScene, camera);   // back + front accumulate in one pass
    placeCap(cfg.capPlane);
    capMat.clippingPlanes = cfg.bound.length ? cfg.bound : null;
    renderer.render(capScene, camera);       // fills where stencil != 0
  }

  // 3. MEP (flat Revit colour) LAST, from its OWN scene, over the shell, so any
  // duct/pipe the section runs through stays visible. A separate scene is used
  // (not a 2nd render of `scene`) because re-rendering the main scene repaints the
  // clay shell over the poche and corrupts the section. MEP uses normal depth
  // testing; where the cut crosses an element the poche cap has stamped depth, so
  // the cross-section stays dark instead of showing the element's interior wall.
  renderer.clippingPlanes = cfg.shaded;
  renderer.render(mepScene, camera);
}

function drawScene() {
  const h = stage.clientHeight;
  if (!state.loaded || h === 0) return;
  updateClips();
  updatePlanCamera();
  updateSectionCamera();
  renderRegion(camPlan, clip.plan, regions.plan);
  renderRegion(camSection, clip.section, regions.section);
  drawOverlay();
}
function renderFrame() {
  requestAnimationFrame(renderFrame);
  drawScene();
}
// Browsers pause requestAnimationFrame in a hidden/background tab. That never
// happens inside WebView2 (the panel is always visible), but it does in a
// headless browser preview, so keep painting via a timer when hidden.
setInterval(function () { if (document.hidden) drawScene(); }, 100);

// ---------------------------------------------------------------------------
// SVG overlay
// ---------------------------------------------------------------------------
const SVGNS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function drawOverlay() {
  if (!state.metaReady || !state.loaded) { ovl.replaceChildren(); return; }
  buildPlanInverse();
  buildSectionInverse();
  const frag = document.createDocumentFragment();
  drawSectionOverlay(frag);
  drawPlanOverlay(frag);
  ovl.replaceChildren(frag);
}

function drawSectionOverlay(frag) {
  const r = regions.section;
  const x0 = r.x + 6, x1 = r.x + r.w - 6;
  // level guides
  for (const lv of state.meta.levels) {
    const y = projectTo(new THREE.Vector3(0, feetToY(lv.elev_ft), 0), camSection, r).y;
    if (y < r.y - 2 || y > r.y + r.h + 2) continue;
    frag.appendChild(svg('line', { x1: x0, y1: y, x2: x1, y2: y,
      stroke: GUIDE_COLOR, 'stroke-width': 1, 'stroke-dasharray': '2 4', opacity: 0.7 }));
    frag.appendChild(text(x0 + 2, y - 3, lv.name + "  " + fmtElev(lv.elev_ft), GUIDE_COLOR, 10, 'start'));
  }
  // plane bars (draw vd, bot, cut, top so top sits on render order top)
  for (const kkey of ['vd', 'bot', 'cut', 'top']) {
    const p = state.planes[kkey];
    const col = PLANE_COLORS[kkey];
    const disabled = state.disabled.has(kkey);
    const a = absFt(p);
    let y, unlimited = (p.sentinel === 'unlimited' || a == null);
    if (unlimited) { y = r.y + r.h - 14; }      // pin near bottom frustum edge
    else { y = projectTo(new THREE.Vector3(0, feetToY(a), 0), camSection, r).y; }
    y = clamp(y, r.y + 8, r.y + r.h - 8);
    const label = PLANE_LABELS[kkey] + '  ' + (unlimited ? 'Unlimited' : fmtElev(a));
    const ln = svg('line', { x1: x0, y1: y, x2: x1, y2: y, stroke: col,
      'stroke-width': 2.5, opacity: disabled ? 0.4 : 0.95 });
    frag.appendChild(ln);
    // label pill background
    const tw = label.length * 6.0 + 12;
    frag.appendChild(svg('rect', { x: x0 + 4, y: y - 19, width: tw, height: 15, rx: 3,
      fill: col, opacity: disabled ? 0.4 : 0.92 }));
    frag.appendChild(text(x0 + 9, y - 8, label, '#fff', 10.5, 'start', 600));
    // drag handle at right edge (skip for disabled, unlimited, or locked)
    if (!disabled && !unlimited && !state.locked) {
      const hx = x1 - 9;
      const h = svg('circle', { cx: hx, cy: y, r: 7, fill: '#fff', stroke: col,
        'stroke-width': 2.5, class: 'hit', cursor: 'ns-resize' });
      h.dataset.plane = kkey;
      h.addEventListener('pointerdown', startBarDrag);
      frag.appendChild(h);
      // a wider invisible hit strip along the bar for easier grabbing
      const strip = svg('line', { x1: x0, y1: y, x2: x1, y2: y, stroke: 'transparent',
        'stroke-width': 12, class: 'hit', cursor: 'ns-resize' });
      strip.dataset.plane = kkey;
      strip.addEventListener('pointerdown', startBarDrag);
      frag.appendChild(strip);
    }
  }
}

function drawPlanOverlay(frag) {
  const r = regions.plan;
  if (!state.section.a) return;
  const a = projectTo(state.section.a, camPlan, r);
  const b = projectTo(state.section.b, camPlan, r);
  // far-clip line: parallel to the section line, offset in the look direction by
  // the section depth. Drag its handle to push the scope out and reveal more of
  // the model behind the cut (Revit-style far clip). The section line is untouched.
  const lookF = sectionNormal().multiplyScalar(state.section.lookSign);
  const fa = projectTo(state.section.a.clone().addScaledVector(lookF, state.section.depth), camPlan, r);
  const fb = projectTo(state.section.b.clone().addScaledVector(lookF, state.section.depth), camPlan, r);
  frag.appendChild(svg('line', { x1: fa.x, y1: fa.y, x2: fb.x, y2: fb.y,
    stroke: GUIDE_COLOR, 'stroke-width': 1.25, 'stroke-dasharray': '3 4', opacity: 0.85 }));
  // light connectors (section-line ends -> far-clip line) read as a crop box
  for (const [p0, p1] of [[a, fa], [b, fb]]) {
    frag.appendChild(svg('line', { x1: p0.x, y1: p0.y, x2: p1.x, y2: p1.y,
      stroke: GUIDE_COLOR, 'stroke-width': 1, 'stroke-dasharray': '3 4', opacity: 0.5 }));
  }
  // far-clip drag handle (offset from center so it never sits under the flip arrow)
  const fhx = fa.x + (fb.x - fa.x) * 0.68, fhy = fa.y + (fb.y - fa.y) * 0.68;
  const fh = svg('circle', { cx: fhx, cy: fhy, r: 5, fill: '#fff', stroke: GUIDE_COLOR,
    'stroke-width': 2, class: 'hit', cursor: 'move' });
  fh.addEventListener('pointerdown', startFarClipDrag);
  frag.appendChild(fh);
  // section line
  frag.appendChild(svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y,
    stroke: '#2D3748', 'stroke-width': 2, 'stroke-dasharray': '6 4', opacity: 0.85 }));
  // section line is always movable (it only changes what the section shows, not
  // the view range), so it stays interactive even when the view range is locked.
  // body hit strip (translate)
  const body = svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: 'transparent',
    'stroke-width': 16, class: 'hit', cursor: 'move' });
  body.addEventListener('pointerdown', startLineDrag);
  frag.appendChild(body);
  // flip arrow at midpoint (perpendicular, along lookSign*normal)
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  const n = sectionNormal().multiplyScalar(state.section.lookSign);
  const nMid = projectTo(state.section.a.clone().lerp(state.section.b, 0.5).add(n.clone().multiplyScalar(1.5)), camPlan, r);
  let dx = nMid.x - mx, dy = nMid.y - my; const dl = Math.hypot(dx, dy) || 1; dx /= dl; dy /= dl;
  const tip = { x: mx + dx * 22, y: my + dy * 22 };
  const px = -dy, py = dx;
  frag.appendChild(svg('polygon', {
    points: [tip.x + ',' + tip.y, (mx + px * 7) + ',' + (my + py * 7), (mx - px * 7) + ',' + (my - py * 7)].join(' '),
    fill: '#2D3748', opacity: 0.9, class: 'hit', cursor: 'pointer',
  })).addEventListener('pointerdown', flipSection);
  // endpoint handles
  for (const [pt, which] of [[a, 'a'], [b, 'b']]) {
    const h = svg('circle', { cx: pt.x, cy: pt.y, r: 6, fill: '#fff', stroke: '#2D3748',
      'stroke-width': 2, class: 'hit', cursor: 'grab' });
    h.dataset.end = which;
    h.addEventListener('pointerdown', startEndpointDrag);
    frag.appendChild(h);
  }
}

function text(x, y, str, fill, size, anchor, weight) {
  const t = svg('text', { x, y, fill, 'font-size': size, 'text-anchor': anchor || 'start',
    'font-family': 'Segoe UI, sans-serif', 'font-weight': weight || 400 });
  t.textContent = str;
  return t;
}

// ---------------------------------------------------------------------------
// Drag interactions
// ---------------------------------------------------------------------------
function stagePoint(ev) {
  const rect = stage.getBoundingClientRect();
  return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
}
function startBarDrag(ev) {
  if (state.locked) return;
  ev.preventDefault(); ev.stopPropagation();
  const k = ev.currentTarget.dataset.plane;
  const move = e => {
    const pt = stagePoint(e);
    const worldY = secScreenYToWorldY(pt.y);
    let abs = yToFeet(worldY);
    // Snap in the frame the user reads (height above the view's floor), so the
    // displayed elevation lands on clean increments instead of the raw internal
    // value (which is offset by the floor's odd elevation).
    const d = displayDatum();
    abs = snap(abs - d) + d;
    const p = state.planes[k];
    p.offset_ft = abs - p.ref_ft;
    updateEditorRow(k);
    runValidation();
  };
  const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}
function startEndpointDrag(ev) {
  ev.preventDefault(); ev.stopPropagation();
  const which = ev.currentTarget.dataset.end;
  const move = e => {
    const pt = stagePoint(e);
    const w = planScreenToWorld(clamp(pt.x, regions.plan.x, regions.plan.x + regions.plan.w), pt.y);
    state.section[which] = snapSectionEnd(new THREE.Vector3(w.x, 0, w.z), which);
  };
  const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}
// Snap a dragged endpoint so the line locks to the plan's horizontal (right) or
// vertical (up) axis when it gets within ~8 degrees of square.
function snapSectionEnd(np, which) {
  const other = which === 'a' ? state.section.b : state.section.a;
  const right = new THREE.Vector3().crossVectors(planUp(), new THREE.Vector3(0, 1, 0)).normalize();
  const up = planUp();
  const rel = np.clone().sub(other); rel.y = 0;
  const ru = rel.dot(right), rv = rel.dot(up);
  if (Math.hypot(ru, rv) < 1e-6) return np;
  const ang = Math.atan2(Math.abs(rv), Math.abs(ru)) * 180 / Math.PI; // 0=horizontal, 90=vertical
  const THR = 8;
  if (ang < THR) return other.clone().addScaledVector(right, ru).setY(0);
  if (ang > 90 - THR) return other.clone().addScaledVector(up, rv).setY(0);
  return np;
}
function startLineDrag(ev) {
  ev.preventDefault(); ev.stopPropagation();
  const start = stagePoint(ev);
  const w0 = planScreenToWorld(start.x, start.y);
  const a0 = state.section.a.clone(), b0 = state.section.b.clone();
  const move = e => {
    const pt = stagePoint(e);
    const w = planScreenToWorld(pt.x, pt.y);
    const dx = w.x - w0.x, dz = w.z - w0.z;
    state.section.a = a0.clone().add(new THREE.Vector3(dx, 0, dz));
    state.section.b = b0.clone().add(new THREE.Vector3(dx, 0, dz));
  };
  const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}
function flipSection(ev) {
  ev.preventDefault(); ev.stopPropagation();
  state.section.lookSign *= -1;
}
// Drag the far-clip line to set how deep (in the look direction) the section
// sees. Depth is the cursor's distance from the section line along the look
// direction, clamped to a sliver minimum and the model's own size.
function startFarClipDrag(ev) {
  ev.preventDefault(); ev.stopPropagation();
  const move = e => {
    const pt = stagePoint(e);
    const w = planScreenToWorld(clamp(pt.x, regions.plan.x, regions.plan.x + regions.plan.w), pt.y);
    const look = sectionNormal().multiplyScalar(state.section.lookSign);
    let depth = w.clone().sub(state.section.a).dot(look);
    const maxD = state.modelBox ? state.modelBox.getSize(new THREE.Vector3()).length() : 100;
    state.section.depth = Math.max(0.15, Math.min(maxD, depth));
  };
  const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

// --- Zoom / pan -----------------------------------------------------------
function regionAt(pt) { return pt.x < regions.plan.x + regions.plan.w ? 'plan' : 'section'; }
// Map a screen-pixel delta into world-units along two world axes, for the given
// ortho camera + region (so pan tracks the cursor exactly).
function buildAxisInverse(cam, r, axisU, axisV) {
  const p0 = projectTo(new THREE.Vector3(0, 0, 0), cam, r);
  const pu = projectTo(axisU, cam, r);
  const pv = projectTo(axisV, cam, r);
  const a = pu.x - p0.x, b = pv.x - p0.x, c = pu.y - p0.y, d = pv.y - p0.y;
  const det = a * d - b * c;
  if (Math.abs(det) < 1e-9) return null;
  return (dx, dy) => ({ du: (d * dx - b * dy) / det, dv: (-c * dx + a * dy) / det });
}
function onWheel(ev) {
  if (!state.loaded) return;
  ev.preventDefault();
  const pt = stagePoint(ev);
  const reg = regionAt(pt);
  const v = reg === 'plan' ? state.view.plan : state.view.section;
  const factor = Math.pow(1.0016, -ev.deltaY);
  if (reg === 'plan') {
    updatePlanCamera(); buildPlanInverse();            // sync to current state first
    const wBefore = planScreenToWorld(pt.x, pt.y);     // world point under the cursor now
    v.zoom = Math.max(0.2, Math.min(40, v.zoom * factor));
    updatePlanCamera(); buildPlanInverse();            // re-frame at the new zoom
    const wAfter = planScreenToWorld(pt.x, pt.y);
    v.panX += wBefore.x - wAfter.x;                     // pan so the cursor keeps its world point
    v.panZ += wBefore.z - wAfter.z;
    updatePlanCamera(); buildPlanInverse();            // commit (so back-to-back wheels stay exact)
  } else {
    const d = sectionDir(), up = new THREE.Vector3(0, 1, 0);
    updateSectionCamera();
    const wBefore = sectionScreenToWorld(pt.x, pt.y);
    v.zoom = Math.max(0.2, Math.min(40, v.zoom * factor));
    updateSectionCamera();
    const wAfter = sectionScreenToWorld(pt.x, pt.y);
    const delta = wBefore.clone().sub(wAfter);
    v.panU += delta.dot(d); v.panV += delta.dot(up);
    updateSectionCamera();
  }
}
function startPan(ev) {
  if (!state.loaded || ev.button === 2) return;   // ignore right-click (context menu)
  const reg = regionAt(stagePoint(ev));
  let prev = stagePoint(ev);
  const move = e => {
    const pt = stagePoint(e);
    const dx = pt.x - prev.x, dy = pt.y - prev.y;
    prev = pt;
    if (reg === 'plan') {
      const up = planUp();
      const right = new THREE.Vector3().crossVectors(up, new THREE.Vector3(0, 1, 0)).normalize();
      const inv = buildAxisInverse(camPlan, regions.plan, right, up);
      if (!inv) return;
      const { du, dv } = inv(dx, dy);
      const wd = right.clone().multiplyScalar(du).add(up.clone().multiplyScalar(dv));
      state.view.plan.panX -= wd.x; state.view.plan.panZ -= wd.z;   // content follows cursor
    } else {
      const d = sectionDir(), up = new THREE.Vector3(0, 1, 0);
      const inv = buildAxisInverse(camSection, regions.section, d, up);
      if (!inv) return;
      const { du, dv } = inv(dx, dy);
      state.view.section.panU -= du; state.view.section.panV -= dv;
    }
  };
  const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}
function resetView(reg) {
  const v = reg === 'plan' ? state.view.plan : state.view.section;
  v.zoom = 1; if (reg === 'plan') { v.panX = 0; v.panZ = 0; } else { v.panU = 0; v.panV = 0; }
}

// ---------------------------------------------------------------------------
// Editor panel
// ---------------------------------------------------------------------------
function buildEditor() {
  const host = document.getElementById('prows');
  host.replaceChildren();
  for (const k of PLANE_KEYS) {
    const row = document.createElement('div');
    row.className = 'prow' + (state.disabled.has(k) ? ' disabled' : '');
    row.dataset.plane = k;
    const sw = document.createElement('div'); sw.className = 'swatch'; sw.style.background = PLANE_COLORS[k];
    const right = document.createElement('div');
    const name = document.createElement('div'); name.className = 'pname'; name.textContent = PLANE_LABELS[k];
    const ctrls = document.createElement('div'); ctrls.className = 'ctrls';
    const sel = document.createElement('select'); sel.dataset.plane = k;
    const off = document.createElement('input'); off.type = 'text'; off.className = 'off'; off.dataset.plane = k;
    ctrls.appendChild(sel); ctrls.appendChild(off);
    const abs = document.createElement('div'); abs.className = 'abs'; abs.dataset.absFor = k;
    right.appendChild(name); right.appendChild(ctrls); right.appendChild(abs);
    row.appendChild(sw); row.appendChild(right);
    host.appendChild(row);
    populateLevelSelect(sel, k);
    const disabled = state.disabled.has(k);
    // Revit locks the Cut Plane's level to the associated level (offset only);
    // its level picker is greyed in the template dialog. Mirror that here.
    const cutLocked = state.templateMode && k === 'cut';
    sel.disabled = disabled || state.locked || cutLocked;
    off.disabled = disabled || state.locked;
    sel.addEventListener('change', () => onLevelChange(k, parseInt(sel.value, 10)));
    off.addEventListener('change', () => onOffsetChange(k, off.value));
    off.addEventListener('keydown', e => { if (e.key === 'Enter') off.blur(); });
    updateEditorRow(k);
  }
  runValidation();
}
function populateLevelSelect(sel, k) {
  sel.replaceChildren();
  const lvls = state.meta.levels.slice().sort((a, b) => b.elev_ft - a.elev_ft);
  const s = (state.meta.sentinels && state.meta.sentinels[k]) || {};
  if (state.templateMode) {
    // A view template is RELATIVE by design -- its planes follow whatever level a
    // view sits on, so the only valid choices are relative ones. We deliberately
    // do NOT list named levels here: pinning a template plane to an absolute level
    // (ROOF, L2, ...) would break every view that shares the template. The Cut
    // Plane is locked to Associated Level (offset only); the others offer
    // Associated Level / Level Above / Level Below, plus Unlimited on View Depth.
    addSentinelOption(sel, SENTINEL.ASSOCIATED, 'Associated Level');
    if (k !== 'cut') {
      addSentinelOption(sel, SENTINEL.ABOVE, 'Level Above');
      addSentinelOption(sel, SENTINEL.BELOW, 'Level Below');
      if (s.unlimited) addSentinelOption(sel, SENTINEL.UNLIMITED, 'Unlimited');
      // Edge case: if a template plane was already pinned to a real level in Revit
      // (rare, but legal), keep just that one option so it shows and round-trips
      // unchanged -- without offering the full level list.
      const cur = state.planes[k] && state.planes[k].level_id;
      if (cur != null && cur > 0) {
        const lv = lvls.find(l => l.id === cur);
        if (lv) addSentinelOption(sel, lv.id, lv.name);
      }
    }
    return;
  }
  // View mode: real levels, descending elevation so higher levels are at the top.
  for (const lv of lvls) {
    const o = document.createElement('option'); o.value = String(lv.id); o.textContent = lv.name;
    sel.appendChild(o);
  }
  if (s.above)     addSentinelOption(sel, SENTINEL.ABOVE, 'Level Above');
  if (s.below)     addSentinelOption(sel, SENTINEL.BELOW, 'Level Below');
  if (s.unlimited) addSentinelOption(sel, SENTINEL.UNLIMITED, 'Unlimited');
}
function addSentinelOption(sel, id, label) {
  const o = document.createElement('option'); o.value = String(id); o.textContent = label;
  sel.appendChild(o);
}
function onLevelChange(k, level_id) {
  const p = state.planes[k];
  p.level_id = level_id;
  p.sentinel = sentinelFromId(level_id);
  p.ref_ft = resolveRefFt(level_id);
  updateEditorRow(k);
  runValidation();
}
function onOffsetChange(k, raw) {
  const p = state.planes[k];
  let v = parseOffset(raw);
  if (v == null) { updateEditorRow(k); return; }   // revert field to last valid
  v = snap(v);
  p.offset_ft = v;
  updateEditorRow(k);
  runValidation();
}
function updateEditorRow(k) {
  const p = state.planes[k];
  const sel = document.querySelector('select[data-plane="' + k + '"]');
  const off = document.querySelector('input.off[data-plane="' + k + '"]');
  const abs = document.querySelector('[data-abs-for="' + k + '"]');
  if (sel) sel.value = String(p.level_id);
  const unlimited = p.sentinel === 'unlimited';
  if (off) {
    off.value = unlimited ? '' : fmtFeetIn(p.offset_ft);
    off.disabled = unlimited || state.disabled.has(k) || state.locked;
  }
  if (abs) {
    const a = absFt(p);
    abs.innerHTML = 'Elevation: <b>' + (unlimited || a == null ? 'Unlimited' : fmtElev(a)) + '</b>';
  }
}

// ---------------------------------------------------------------------------
// Validation (non-blocking warnings)
// ---------------------------------------------------------------------------
function runValidation() {
  const box = document.getElementById('validation');
  const msgs = validate();
  if (!msgs.length) { box.classList.remove('show'); box.replaceChildren(); return; }
  box.replaceChildren();
  const head = document.createElement('div'); head.textContent = 'Check view-range ordering:';
  head.style.fontWeight = '600';
  const ul = document.createElement('ul');
  for (const m of msgs) { const li = document.createElement('li'); li.textContent = m; ul.appendChild(li); }
  box.appendChild(head); box.appendChild(ul);
  box.classList.add('show');
}
function validate() {
  const out = [];
  const top = absFt(state.planes.top), cut = absFt(state.planes.cut),
        bot = absFt(state.planes.bot), vd = absFt(state.planes.vd);
  const isRcp = state.meta.view.is_ceiling_plan || state.disabled.has('bot');
  if (top != null && cut != null && top < cut - EPS)
    out.push('Top (' + fmtElev(top) + ') is below Cut Plane (' + fmtElev(cut) + ').');
  if (isRcp) {
    if (vd != null && cut != null && vd < cut - EPS)
      out.push('On a Ceiling Plan, View Depth (' + fmtElev(vd) + ') is normally above the Cut Plane (' + fmtElev(cut) + ').');
  } else {
    if (cut != null && bot != null && cut < bot - EPS)
      out.push('Cut Plane (' + fmtElev(cut) + ') is below Bottom (' + fmtElev(bot) + ').');
    if (bot != null && vd != null && bot < vd - EPS)
      out.push('Bottom (' + fmtElev(bot) + ') is below View Depth (' + fmtElev(vd) + ').');
  }
  return out;
}

// ---------------------------------------------------------------------------
// Apply / Revert / template
// ---------------------------------------------------------------------------
function buildApplyPayload() {
  const planes = {};
  for (const k of PLANE_KEYS) {
    const p = state.planes[k];
    planes[k] = { level_id: p.level_id, offset_ft: round4(p.offset_ft), sentinel: p.sentinel };
  }
  return { planes };
}
function round4(x) { return Math.round(x * 10000) / 10000; }
function doApply() {
  if (state.locked || state.applying) return;
  const payload = buildApplyPayload();
  if (DEV) {
    toast('Apply (dev): ' + JSON.stringify(payload));
    diag('apply payload ' + JSON.stringify(payload));
    return;
  }
  state.applying = true;
  setApplyBusy(true);
  post('apply:' + JSON.stringify(payload));
}
function onApplied(result) {
  state.applying = false;
  setApplyBusy(false);
  if (result && result.ok) {
    state.baseline = JSON.parse(JSON.stringify(serializeViewRange()));
    toast('View range applied.');
  } else {
    toast((result && result.err) ? ('Apply failed: ' + result.err) : 'Apply failed.', true);
  }
}
function serializeViewRange() {
  const vr = {};
  for (const k of PLANE_KEYS) {
    const p = state.planes[k];
    vr[k] = { level_id: p.level_id, level_name: '', offset_ft: p.offset_ft, abs_ft: absFt(p), sentinel: p.sentinel };
  }
  return vr;
}
function doRevert() {
  if (!state.baseline) return;
  loadPlanesFrom(state.baseline);
  buildEditor();
  toast('Reverted to the last applied view range.');
}
function setApplyBusy(busy) {
  const btn = document.getElementById('btn_apply');
  btn.disabled = busy || state.locked;
  btn.textContent = busy ? 'Applying…' : 'Apply to Revit';
}
function applyBannerState() {
  const banner = document.getElementById('banner');
  const editor = document.getElementById('editor');
  if (state.locked) {
    const nm = (state.meta.template_lock && state.meta.template_lock.template_name) || 'a view template';
    document.getElementById('banner_msg').textContent =
      "View range is controlled by template '" + nm + "'.";
    banner.classList.add('show');
    if (editor) editor.style.display = 'none';
    document.getElementById('btn_apply').disabled = true;
    document.getElementById('btn_revert').disabled = true;
  } else {
    banner.classList.remove('show');
    if (editor && state.loaded) editor.style.display = '';
    document.getElementById('btn_apply').disabled = false;
    document.getElementById('btn_revert').disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Snap UI
// ---------------------------------------------------------------------------
function syncSnapUi() {
  document.getElementById('snap_on').checked = state.snap.enabled;
  document.getElementById('snap_dist').value = String(state.snap.dist);
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------
let toastTimer = null;
function toast(msg, isErr) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.toggle('err', !!isErr);
  t.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), isErr ? 6000 : 3500);
}

// ---------------------------------------------------------------------------
// Host messages
// ---------------------------------------------------------------------------
function handleMessage(data) {
  if (typeof data !== 'string') return;
  const i = data.indexOf(':');
  const cmd = i < 0 ? data : data.slice(0, i);
  const arg = i < 0 ? '' : data.slice(i + 1);
  if (cmd === 'url') loadFromUrl(arg);
  else if (cmd === 'b64') loadFromBuffer(b64ToBuffer(arg));
  else if (cmd === 'meta') { try { applyMeta(JSON.parse(arg)); } catch (e) { diag('meta parse error ' + e); } }
  else if (cmd === 'applied') { try { onApplied(JSON.parse(arg)); } catch (e) { onApplied(null); } }
}
if (hostAvailable) {
  window.chrome.webview.addEventListener('message', e => handleMessage(e.data));
}

// ---------------------------------------------------------------------------
// Wire UI controls
// ---------------------------------------------------------------------------
document.getElementById('btn_apply').addEventListener('click', doApply);
document.getElementById('btn_revert').addEventListener('click', doRevert);
// flip the section's view direction (same as clicking the small arrow on the
// section line, but an explicit, discoverable button)
document.getElementById('btn_flip').addEventListener('click', () => { state.section.lookSign *= -1; });
// zoom (wheel) + pan (drag on empty canvas); double-click resets a region's view
canvas.addEventListener('wheel', onWheel, { passive: false });
canvas.addEventListener('pointerdown', startPan);
canvas.addEventListener('dblclick', e => resetView(regionAt(stagePoint(e))));
document.getElementById('snap_on').addEventListener('change', e => { state.snap.enabled = e.target.checked; });
document.getElementById('snap_dist').addEventListener('change', e => {
  const v = parseFloat(e.target.value); if (!isNaN(v) && v > 0) state.snap.dist = v; else e.target.value = String(state.snap.dist);
});
// Visibility Edit dialog wiring (All / None / Apply / Cancel).
document.getElementById('vis_cats_all').addEventListener('click', () => visDlgSetAll('vis_cats', true));
document.getElementById('vis_cats_none').addEventListener('click', () => visDlgSetAll('vis_cats', false));
document.getElementById('vis_ws_all').addEventListener('click', () => visDlgSetAll('vis_ws', true));
document.getElementById('vis_ws_none').addEventListener('click', () => visDlgSetAll('vis_ws', false));
document.getElementById('vis_apply').addEventListener('click', applyVisDialog);
document.getElementById('vis_cancel').addEventListener('click', closeVisDialog);
document.getElementById('vismodal').addEventListener('click', e => {
  if (e.target.id === 'vismodal') closeVisDialog();   // click the backdrop to dismiss
});
document.getElementById('btn_detach').addEventListener('click', () => {
  if (DEV) { toast('Detach (dev): would detach this view from its template.'); return; }
  post('detach');
});
document.getElementById('btn_edittpl').addEventListener('click', () => {
  if (DEV) { toast('Edit template (dev): would switch the write target to the template.'); return; }
  post('edittemplate');
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.getElementById('editor').style.display = 'none';
renderFrame();
diag('page ready (host=' + hostAvailable + ')');

if (DEV) {
  // dev fallback: auto-load the sample fixture so the UI is fully testable
  const params = new URLSearchParams(location.search);
  const fixture = params.get('fixture');   // e.g. ?fixture=ceiling | locked
  const metaFile = fixture ? ('./sample/sample_meta_' + fixture + '.json') : './sample/sample_meta.json';
  fetch(metaFile)
    .then(r => r.ok ? r.json() : fetch('./sample/sample_meta.json').then(x => x.json()))
    .then(applyMeta)
    .catch(e => diag('dev meta load failed ' + e));
  loadFromUrl('./sample/sample_building.glb');
} else {
  post('ready');
}

// expose a little debug surface for preview_eval driving
window.__vr = { state, doApply, doRevert, buildApplyPayload, validate, applyMeta, toast,
  showHide, applyVisibility, vkey, openVisDialog, applyVisDialog, closeVisDialog,
  setSnap: (on, d) => { state.snap.enabled = on; if (d) state.snap.dist = d; syncSnapUi(); },
  _dbg: () => ({ regions, camPlan, camSection, clip, split,
    rendererSize: renderer.getSize(new THREE.Vector2()) }) };
