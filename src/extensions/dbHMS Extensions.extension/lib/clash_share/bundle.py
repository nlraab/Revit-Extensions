# -*- coding: utf-8 -*-
"""Build a single self-contained HTML file from the 3D Viewer's web assets.

Why this exists
---------------
Inside Revit the viewer (``web/viewer3.html``) loads its three.js engine as ES
modules over a WebView2 virtual host (https), so the browser can fetch them.
A project manager who just double-clicks a local ``.html`` is on the ``file://``
origin, where the browser blocks fetching sibling files (the engine modules AND
the ``.glb`` model) for security. So a shareable copy has to inline EVERYTHING:

* every engine module becomes a ``data:`` URL inside the page's import map, with
  each module's own ``import`` specifiers rewritten to point at the other
  inlined modules (relative specifiers can't resolve against a ``data:`` URL, so
  they're flattened to import-map keys);
* the model rides along as base64;
* the clash list + saved viewpoints ride along as JSON.

The result is one file with ZERO external fetches that runs the exact same
viewer in "standalone" mode (``web/viewer3.html`` detects the absence of the
WebView2 host and builds an in-page control overlay instead of waiting for the
WPF panel).

Runtime is IronPython 2.7 inside Revit; this module sticks to the stdlib
(``base64``, ``json``, ``os``, ``re``, ``posixpath``) and Python-2 syntax so it
also imports cleanly under CPython 3 for the structural test suite.
"""

import base64
import json
import os
import posixpath
import re

# Cap (uncompressed model bytes) above which the inlined file gets unwieldy for a
# browser to parse in one go. The caller warns the user before crossing it; this
# constant is the shared definition of "big".
WARN_MODEL_BYTES = 150 * 1024 * 1024

# Matches the module specifier in a real top-level import/export STATEMENT, which
# always BEGINS a line with `import` or `export`. We anchor to that (re.M) and scan
# to either `from '<spec>'` (the `[^;]` run crosses newlines for multi-line import
# clauses but never a statement terminator) or a side-effect `import '<spec>'`. The
# specifier is restricted to module-path characters. Anchoring + the charset keep
# the words `from`/`import` inside strings or comments from being mistaken for
# imports (e.g. three.module.js error text, GLTFLoader's `from "srgb-linear"`
# warning, a `' + x + '` string-concat). three.js r168 has no dynamic `import()`.
_SPEC_CHARS = r"[A-Za-z0-9_./@-]+"
_SPEC_RE = re.compile(
    r"(?m)^(?P<pre>[ \t]*(?:import|export)\b[^;]*?\bfrom[ \t]*)(?P<q>['\"])(?P<spec>"
    + _SPEC_CHARS + r")(?P=q)"
    r"|^(?P<pre2>[ \t]*import[ \t]*)(?P<q2>['\"])(?P<spec2>" + _SPEC_CHARS + r")(?P=q2)")

_IMPORTMAP_RE = re.compile(
    r'(<script\b[^>]*\btype="importmap"[^>]*>)(?P<body>.*?)(</script>)', re.S)

_MODULE_OPEN = '<script type="module">'


# ---------------------------------------------------------------------------
# Module-graph flattening
# ---------------------------------------------------------------------------

def _norm_target(target):
    """A raw import-map target (e.g. ``./lib/three/three.module.js``) -> a clean
    web-root-relative posix path (``lib/three/three.module.js``)."""
    t = target[2:] if target.startswith('./') else target
    return posixpath.normpath(t)


def _parse_importmap(html):
    m = _IMPORTMAP_RE.search(html)
    if not m:
        raise ValueError("viewer3.html has no <script type=\"importmap\"> block")
    data = json.loads(m.group('body'))
    return data.get('imports', {}) or {}


def _resolver(imports):
    """Return resolve(spec, importer_relpath) -> web-root-relative posix path,
    applying the page's original import map (exact keys + trailing-slash prefixes)
    and ordinary relative resolution."""
    # Pre-split trailing-slash prefix mappings, longest first for specificity.
    prefixes = sorted(((k, v) for k, v in imports.items() if k.endswith('/')),
                      key=lambda kv: -len(kv[0]))

    def resolve(spec, importer):
        if spec.startswith('./') or spec.startswith('../'):
            base = posixpath.dirname(importer)
            return posixpath.normpath(posixpath.join(base, spec))
        if spec in imports:
            return _norm_target(imports[spec])
        for k, v in prefixes:
            if spec.startswith(k):
                return _norm_target(v) + '/' + spec[len(k):]
        raise KeyError("cannot resolve import specifier %r (from %r)"
                       % (spec, importer or '<page>'))
    return resolve


def _rewrite_specs(source, importer, resolve):
    """Rewrite every import/export specifier in ``source`` to its resolved
    web-root-relative path, and return (rewritten_source, set_of_resolved_paths).
    Resolved paths double as the flattened import-map keys."""
    found = set()

    def repl(m):
        if m.group('spec') is not None:
            spec, q, pre = m.group('spec'), m.group('q'), m.group('pre')
        else:
            spec, q, pre = m.group('spec2'), m.group('q2'), m.group('pre2')
        target = resolve(spec, importer)
        found.add(target)
        return pre + q + target + q
    return _SPEC_RE.sub(repl, source), found


def _read_bytes(path):
    f = open(path, 'rb')
    try:
        return f.read()
    finally:
        f.close()


def _read_text(path):
    return _read_bytes(path).decode('utf-8')


def _b64_ascii(raw_bytes):
    b64 = base64.b64encode(raw_bytes)
    if isinstance(b64, bytes):
        b64 = b64.decode('ascii')
    return b64


def _data_url(source_text):
    return 'data:text/javascript;base64,' + _b64_ascii(source_text.encode('utf-8'))


def _flatten_modules(web_dir, entry_source, resolve):
    """Walk the module graph from ``entry_source`` (the page's inline module),
    rewriting each module's specifiers and base64-inlining it. Returns
    (rewritten_entry_source, {relpath: data_url, ...})."""
    entry_rewritten, queue = _rewrite_specs(entry_source, '', resolve)
    inlined = {}
    pending = list(queue)
    while pending:
        rel = pending.pop()
        if rel in inlined:
            continue
        fpath = os.path.join(web_dir, *rel.split('/'))
        if not os.path.isfile(fpath):
            raise IOError("inlined module not found on disk: %s" % fpath)
        src = _read_text(fpath)
        rewritten, deps = _rewrite_specs(src, rel, resolve)
        inlined[rel] = _data_url(rewritten)
        for d in deps:
            if d not in inlined:
                pending.append(d)
    return entry_rewritten, inlined


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

def _inject_data_script(html, model_b64, payload):
    """Insert a classic <script> (runs before the deferred module) that exposes
    the embedded model + clash data to the page's standalone path."""
    # base64 is JSON/JS-safe (only [A-Za-z0-9+/=]) so the model goes in by plain
    # concatenation -- avoids round-tripping a 100 MB string through json.dumps.
    data_js = (
        '<script>\n'
        'window.__DBHMS_MODEL_B64 = "' + model_b64 + '";\n'
        'window.__DBHMS_BUNDLE = ' + json.dumps(payload, ensure_ascii=True) + ';\n'
        '</script>\n')
    idx = html.find(_MODULE_OPEN)
    # Fall back to before the importmap if the module tag moved; both run before
    # the module executes.
    if idx < 0:
        m = _IMPORTMAP_RE.search(html)
        idx = m.start() if m else 0
    return html[:idx] + data_js + html[idx:]


def _replace_importmap(html, inlined):
    new_map = json.dumps({"imports": inlined}, ensure_ascii=True)

    def repl(m):
        return m.group(1) + '\n' + new_map + '\n' + m.group(3)
    return _IMPORTMAP_RE.sub(repl, html, count=1)


def _replace_module_body(html, new_body):
    start = html.index(_MODULE_OPEN) + len(_MODULE_OPEN)
    end = html.index('</script>', start)
    return html[:start] + new_body + html[end:]


def build_share_html(web_dir, glb_path, clashes, viewpoints,
                     project_name, generated, out_path):
    """Produce a single self-contained ``out_path`` HTML.

    web_dir       -- the viewer's ``web/`` folder (holds viewer3.html + lib/).
    glb_path      -- the exported model to embed.
    clashes       -- list of row dicts: {label, point:[x,y,z], trade, status,
                     kind, haystack}. Coordinates are host feet (the page's
                     hostToViewer transform converts them, same as in Revit).
    viewpoints    -- list of {name, pos, yaw, pitch} (viewer-space camera).
    project_name  -- display title shown in the browser overlay.
    generated     -- ISO timestamp string (passed in; the runtime forbids
                     Date.now()/clock calls inside workflow scripts, and here we
                     just want a caller-controlled stamp).
    out_path      -- destination .html (written atomically).

    Returns the number of bytes written.
    """
    viewer_path = os.path.join(web_dir, 'viewer3.html')
    html = _read_text(viewer_path)

    imports = _parse_importmap(html)
    resolve = _resolver(imports)

    # Pull out the page's inline module body, flatten the engine graph, rewrite.
    mod_start = html.index(_MODULE_OPEN) + len(_MODULE_OPEN)
    mod_end = html.index('</script>', mod_start)
    entry_source = html[mod_start:mod_end]
    entry_rewritten, inlined = _flatten_modules(web_dir, entry_source, resolve)

    model_b64 = _b64_ascii(_read_bytes(glb_path))

    payload = {
        "project": project_name or "Model",
        "generated": generated or "",
        "clashes": clashes or [],
        "viewpoints": viewpoints or [],
    }

    html = _replace_importmap(html, inlined)
    html = _replace_module_body(html, entry_rewritten)
    html = _inject_data_script(html, model_b64, payload)

    out_bytes = html.encode('utf-8')
    tmp = out_path + '.tmp'
    f = open(tmp, 'wb')
    try:
        f.write(out_bytes)
    finally:
        f.close()
    if os.path.isfile(out_path):
        os.remove(out_path)
    os.rename(tmp, out_path)
    return len(out_bytes)
