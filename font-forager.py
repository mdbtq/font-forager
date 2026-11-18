#!/usr/bin/env python3
"""font-forager — download the fonts a web page loads, and generate a specimen.

Usage: .venv/bin/python font-forager.py <url>

Setup once:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

Fetches the page and its linked stylesheets (following @import), extracts every
font URL it can find in @font-face rules and <link rel="preload" as="font"> tags,
and downloads each font into data/<host>/. Byte-identical files (the same font
served under several @font-face family aliases) are deduplicated, unicode-range
subsets of one face are merged into a single file, and each file is then named
from its own metadata as <family-slug>-<weight>[-italic].<ext> —
family from the name table, weight from OS/2.usWeightClass — so the filename
reflects the font's real weight rather than any remapped @font-face font-weight.
A variable font covers a range of weights, read from its wght axis, and is named
<family-slug>-<low>-<high>[-italic].<ext>. Colliding names get a -2, -3 suffix.
Some foundries (e.g. Monotype's fast.fonts.net) strip the name table, leaving no
family and no italic bit. Webfont kits may also reset the weight and italic bits
while keeping a full name such as "Alliance No.1 Bold Italic"; family and style
are then read from that name. Otherwise the family, weight and italic flag are
recovered from the @font-face-derived filename.

Afterwards two files are written into data/<host>/: a specimen.html specimen
showing for each downloaded face the characters it actually contains (read from
its cmap), with a weight slider for variable fonts, and a style.css with a
reusable @font-face rule per face — keyed on the font's real family name and
weight or weight range — so the folder can be dropped into a website as-is. One
face served in several formats (e.g. woff2 and woff) shares a section and a rule.
Legacy .eot files are downloaded but left out of both, since no current browser
can use them.

Note: this is a static fetch. Fonts injected at runtime by JavaScript are not
discovered.
"""

import hashlib
import heapq
import re
import sys
import tempfile
import unicodedata
import urllib.request
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import urljoin, urlsplit

from fontTools.merge import Merger
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont, newTable
from fontTools.varLib.hvar import add_HVAR
from fontTools.varLib.varStore import NO_VARIATION_INDEX

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
TIMEOUT = 30

# Font file extensions we care about (query/fragment stripped before matching).
FONT_EXT_RE = re.compile(r"\.(woff2|woff|ttf|otf|eot)$", re.IGNORECASE)

# Extension -> CSS @font-face format() hint, and the set we treat as fonts.
FORMATS = {".woff2": "woff2", ".woff": "woff", ".ttf": "truetype", ".otf": "opentype"}
FONT_EXTS = set(FORMATS) | {".eot"}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def fetch_text(url):
    data = fetch_bytes(url)
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #

class LinkCollector(HTMLParser):
    """Collect stylesheet and font-preload hrefs from <link> tags."""

    def __init__(self):
        super().__init__()
        self.stylesheets = []
        self.font_preloads = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        href = a.get("href")
        if not href:
            return
        rel = a.get("rel", "").lower()
        if "stylesheet" in rel:
            self.stylesheets.append(href)
        if a.get("as", "").lower() == "font":
            self.font_preloads.append(href)


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #

def slugify(family):
    family = family.split(",")[0]
    family = family.strip().strip("\"'").lower()
    return re.sub(r"(^-+|-+$)", "", re.sub(r"[^a-z0-9]+", "-", family))


def norm_weight(weight):
    w = weight.strip().strip("\"'").lower()
    if w == "normal":
        return "400"
    if w == "bold":
        return "700"
    if not w:
        return ""
    return re.sub(r"\s+", "-", w)


def ext_of(url):
    path = urlsplit(url).path
    dot = path.rfind(".")
    return path[dot:].lower() if dot != -1 else ""


def base_of(url):
    return urlsplit(url).path.rsplit("/", 1)[-1]


def is_font_url(url):
    return bool(FONT_EXT_RE.search(urlsplit(url).path))


# --------------------------------------------------------------------------- #
# @font-face extraction
# --------------------------------------------------------------------------- #

FONT_FACE_RE = re.compile(r"@font-face\s*{([^}]*)}", re.IGNORECASE)
URL_RE = re.compile(r"url\(\s*([^)]+?)\s*\)", re.IGNORECASE)
IMPORT_RE = re.compile(r"""@import\s*(?:url\(\s*["']?|["'])([^"')\s]+)""", re.IGNORECASE)


def _decl(block, prop):
    m = re.search(rf"{prop}\s*:\s*([^;}}]+)", block, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def named_fonts(base, text):
    """Yield (resolved_url, filename, unicode_range) for every @font-face font.

    unicode_range is "" when the rule declares none. It is carried through so a
    subsetted family (many files, same family/weight/style, each covering a
    different range) can be recognised and merged into one file.
    """
    for block in FONT_FACE_RE.findall(text):
        slug = slugify(_decl(block, "font-family"))
        if not slug:
            continue
        weight = norm_weight(_decl(block, "font-weight"))
        italic = "-italic" if "italic" in _decl(block, "font-style").lower() else ""
        urange = re.sub(r"\s+", " ", _decl(block, "unicode-range")).strip()
        for ref in URL_RE.findall(block):
            ref = ref.strip().strip("\"'")
            if not is_font_url(ref):
                continue
            url = urljoin(base, ref)
            name = slug + (f"-{weight}" if weight else "") + italic + ext_of(url)
            yield url, name, urange


def fallback_fonts(base, text):
    """Yield (resolved_url, basename) for every font url() in a source."""
    for ref in URL_RE.findall(text):
        ref = ref.strip().strip("\"'")
        if not is_font_url(ref):
            continue
        url = urljoin(base, ref)
        yield url, base_of(url)


def imported_stylesheets(base, text):
    """Return the resolved url of every @import in a source."""
    return [urljoin(base, ref) for ref in IMPORT_RE.findall(text)]


# --------------------------------------------------------------------------- #
# Canonical naming from font metadata
# --------------------------------------------------------------------------- #

WEIGHT_NAMES = {
    "thin": 100, "extralight": 200, "ultralight": 200, "light": 300,
    "regular": 400, "book": 400, "medium": 500, "semibold": 600, "demibold": 600,
    "bold": 700, "extrabold": 800, "ultrabold": 800, "heavy": 800, "black": 900,
}
FULL_NAME_RE = re.compile(
    r"^(.*?\S)\s+((?:(?:(?:extra|ultra|semi|demi)[\s-]?)?"
    r"(?:thin|light|regular|book|medium|bold|heavy|black)(?:\s+italic)?)|italic)$",
    re.IGNORECASE,
)


def _full_name_parts(font):
    """Return (family, style) from the full name of a font without a family name.

    Webfont kits can blank the family name and reset the weight and italic bits
    while the full name still reads "Alliance No.1 Bold Italic". Returns None if
    the font has a family name or its full name ends in no known style.
    """
    name = font["name"]
    if (name.getDebugName(16) or name.getDebugName(1) or "").strip():
        return None
    match = FULL_NAME_RE.match((name.getDebugName(4) or "").strip())
    return match.groups() if match else None


def _font_names(font):
    parts = _full_name_parts(font)
    if parts:
        return parts
    name = font["name"]
    family = name.getDebugName(16) or name.getDebugName(1) or ""
    subfamily = name.getDebugName(17) or name.getDebugName(2) or ""
    return family.strip(), subfamily.strip()


def _weight_range(font):
    """Return the (low, high) weights a font covers; equal for a static font.

    For a variable font OS/2.usWeightClass only describes the default instance,
    so the wght axis is the source of truth. A weight named in the full name of a
    font without a family name beats usWeightClass, which is then unreliable.
    """
    if "fvar" in font:
        for axis in font["fvar"].axes:
            if axis.axisTag == "wght":
                return round(axis.minValue), round(axis.maxValue)
    parts = _full_name_parts(font)
    if parts:
        named = WEIGHT_NAMES.get(re.sub(r"italic|[\s-]", "", parts[1].lower()))
        if named:
            return named, named
    weight = font["OS/2"].usWeightClass if "OS/2" in font else 0
    return weight, weight


def _format_weight(weight, separator):
    low, high = weight
    return str(low) if low == high else f"{low}{separator}{high}"


def _is_italic(font):
    """True if the font's own metadata marks it italic."""
    if "OS/2" in font and font["OS/2"].fsSelection & 0x01:
        return True
    if "head" in font and font["head"].macStyle & 0x02:
        return True
    _, subfamily = _font_names(font)
    return "italic" in subfamily.lower()


def canonical_name(path):
    """Name a font from its own metadata: <family-slug>-<weight>[-italic].<ext>.

    The truth about a font's family and weight lives in its name table and
    OS/2.usWeightClass (or wght axis, giving <low>-<high> for a variable font),
    not in the site's @font-face declaration (which may remap weights — e.g.
    bird.com serves "TWK Lausanne 450" as font-weight:500).
    Returns None if the metadata is unreadable or incomplete.
    """
    meta = _face_metadata(path)
    if meta is None:
        return None
    family, weight, is_italic = meta
    slug = slugify(family)
    if not slug:
        return None
    italic = "-italic" if is_italic else ""
    return f"{slug}-{_format_weight(weight, '-')}{italic}{path.suffix.lower()}"


# --------------------------------------------------------------------------- #
# Specimen generation
# --------------------------------------------------------------------------- #


def _visible_codepoints(cmap):
    out = []
    for cp in sorted(cmap):
        if unicodedata.category(chr(cp))[0] not in ("C", "Z"):
            out.append(cp)
    return out


def _font_src(paths, quote=str):
    """Return an @font-face src value listing each file with its format."""
    return ", ".join(
        f'url("{quote(path.name)}") format("{FORMATS[path.suffix.lower()]}")'
        for path in paths
    )


def _render_font(paths, index):
    """Return an HTML section for one face, or (None, error)."""
    path = paths[0]
    try:
        font = TTFont(str(path), fontNumber=0, lazy=True)
        cmap = font.getBestCmap()
        low, high = _weight_range(font)
    except Exception as exc:  # unreadable / no unicode cmap
        return None, f"{path.name}: {exc}"

    family, subfamily = _font_names(font)
    label = " ".join(p for p in (family, subfamily) if p) or path.stem
    visible = _visible_codepoints(cmap)
    face_id = f"ff-{index}"
    src = _font_src(paths, escape)
    file_names = ", ".join(escape(p.name) for p in paths)

    cells = "".join(
        f'<div class="cell"><span class="glyph">{escape(chr(cp))}</span>'
        f'<span class="cp">{cp:04X}</span></div>'
        for cp in visible
    )
    weight_css = weight_meta = slider = ""
    if low != high:
        start = min(max(400, low), high)
        weight_css = f"font-weight:{low} {high};"
        weight_meta = f" &middot; variable weight {low}&ndash;{high}"
        slider = (
            f'\n    <label class="weight">Weight <input type="range" min="{low}" '
            f'max="{high}" value="{start}" oninput="'
            "this.closest('section').querySelector('.grid').style.fontWeight=this.value;"
            f'this.nextElementSibling.value=this.value"><output>{start}</output></label>'
        )
    css = f"@font-face{{font-family:'{face_id}';src:{src};{weight_css}font-display:swap;}}"
    return (
        f"""<section>
  <style>{css}</style>
  <header>
    <h2 style="font-family:'{face_id}',system-ui,sans-serif">{escape(label)}</h2>
    <p class="meta">{file_names} &middot; {len(cmap)} characters ({len(visible)} shown){weight_meta}</p>{slider}
  </header>
  <div class="grid" style="font-family:'{face_id}',system-ui,sans-serif">{cells}</div>
</section>""",
        None,
    )


SPECIMEN_CSS = """
  :root { color-scheme: light dark;
    --bg:#fff; --fg:#111; --muted:#666; --line:#e5e5e5; --cell:#fafafa; }
  @media (prefers-color-scheme: dark) { :root {
    --bg:#16161a; --fg:#eee; --muted:#999; --line:#2a2a30; --cell:#1e1e24; } }
  * { box-sizing: border-box; }
  body { margin:0; padding:2rem; background:var(--bg); color:var(--fg);
    font-family:system-ui,-apple-system,sans-serif; line-height:1.4; }
  h1 { font-size:1.5rem; margin:0 0 .25rem; }
  .lede { color:var(--muted); margin:0 0 2rem; }
  section { border-top:1px solid var(--line); padding:1.5rem 0; }
  header h2 { font-size:1.6rem; margin:0 0 .25rem; font-weight:500; }
  .meta { color:var(--muted); font-size:.85rem; margin:0 0 1rem;
    font-family:ui-monospace,monospace; }
  .weight { display:flex; align-items:center; gap:.5rem; margin:0 0 1rem;
    color:var(--muted); font-size:.85rem; }
  .grid { display:grid; gap:4px;
    grid-template-columns:repeat(auto-fill,minmax(56px,1fr)); }
  .cell { display:flex; flex-direction:column; align-items:center;
    justify-content:center; aspect-ratio:1; background:var(--cell);
    border:1px solid var(--line); border-radius:6px; }
  .glyph { font-size:1.6rem; line-height:1; }
  .cp { font-family:ui-monospace,monospace; font-size:.6rem;
    color:var(--muted); margin-top:.35rem; }
"""


def write_specimen(out_dir, host, faces):
    sections = []
    for index, paths in enumerate(faces):
        section, err = _render_font(paths, index)
        if section:
            sections.append(section)
        elif err:
            print(f"  (skipped in specimen) {err}", file=sys.stderr)

    if not sections:
        return
    plural = "s" if len(sections) != 1 else ""
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Font specimen — {escape(host)}</title>
<style>{SPECIMEN_CSS}</style>
</head>
<body>
<h1>Font specimen — {escape(host)}</h1>
<p class="lede">{len(sections)} font{plural}, each showing the characters it actually contains.</p>
{''.join(sections)}
</body>
</html>"""
    (out_dir / "specimen.html").write_text(page, encoding="utf-8")
    print(f"→ Wrote {out_dir / 'specimen.html'} ({len(sections)} fonts)")


# --------------------------------------------------------------------------- #
# Stylesheet generation
# --------------------------------------------------------------------------- #


FILENAME_RE = re.compile(r"^(.*?)-(\d{2,4})(-italic)?(?:-\d+)?$")


def _family_from_filename(path):
    """Reconstruct (family, weight, italic) from a canonical filename.

    Some foundries (e.g. Monotype's fast.fonts.net) ship webfonts with the name
    table stripped, so the font itself carries no family name. The filename is
    the next best source: it was either derived from the site's @font-face
    font-family or is the served basename.
    """
    m = FILENAME_RE.match(path.stem)
    if m:
        family, weight, italic = m.group(1), int(m.group(2)), bool(m.group(3))
    else:
        family, weight, italic = path.stem, 400, "italic" in path.stem.lower()
    family = " ".join(w.capitalize() for w in family.split("-") if w)
    return (family, weight, italic) if family else None


def _face_metadata(path):
    """Return (family, (low, high), italic) for a font, or None if unusable.

    Like canonical_name, this prefers the truth from the font's own name table
    and weight metadata over any @font-face declaration, falling back to the
    filename for fonts whose metadata was stripped by the foundry.
    """
    try:
        font = TTFont(str(path), fontNumber=0, lazy=True)
    except Exception:
        return None

    family = _font_names(font)[0]
    weight = _weight_range(font)
    italic = _is_italic(font)

    # A stripped font reports no family/weight and no italic bit, so the
    # filename is the only remaining source for all three.
    if not family or not weight[0]:
        fallback = _family_from_filename(path)
        if fallback is None:
            return None
        family = family or fallback[0]
        weight = weight if weight[0] else (fallback[1], fallback[1])
        italic = italic or fallback[2]

    return family, weight, italic


def write_stylesheet(out_dir, host, faces, hash_ranges=None):
    """Write style.css with one reusable @font-face rule per face.

    Each rule uses the font's real family name and weight (a range for a variable
    font), so a site can select a family by name and let the browser pick the
    matching weight/style. Its src lists every format of the face. Paths
    are relative, so style.css works wherever the font files sit beside it.
    A subset that was not merged keeps its unicode-range (looked up in
    hash_ranges by the file's content); a merged file needs none.
    """
    hash_ranges = hash_ranges or {}
    described = []
    for paths in faces:
        meta = _face_metadata(paths[0])
        if meta is None:
            print(f"  (skipped in style.css) {paths[0].name}: unreadable font", file=sys.stderr)
            continue
        urange = hash_ranges.get(hashlib.sha256(paths[0].read_bytes()).hexdigest(), "")
        described.append((paths, *meta, urange))

    if not described:
        return

    # Group by family, then weight, upright before italic.
    described.sort(key=lambda f: (f[1].lower(), f[2], f[3]))

    rules = []
    for paths, family, weight, italic, urange in described:
        css_family = family.replace("\\", "\\\\").replace('"', '\\"')
        range_line = f"  unicode-range: {urange};\n" if urange else ""
        rules.append(
            "@font-face {\n"
            f'  font-family: "{css_family}";\n'
            f"  font-style: {'italic' if italic else 'normal'};\n"
            f"  font-weight: {_format_weight(weight, ' ')};\n"
            "  font-display: swap;\n"
            f"  src: {_font_src(paths)};\n"
            f"{range_line}"
            "}"
        )

    header = f"/* Fonts foraged from {host} — {len(rules)} face(s). */\n\n"
    (out_dir / "style.css").write_text(header + "\n\n".join(rules) + "\n", encoding="utf-8")
    print(f"→ Wrote {out_dir / 'style.css'} ({len(rules)} faces)")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def collect_font_map(page_url):
    """Return ({url: filename}, {url: unicode_range}) for the fonts to download.

    The second dict holds only urls whose @font-face rule declared a
    unicode-range; it identifies the subsets to merge.
    """
    print(f"→ Fetching page: {page_url}")
    page_html = fetch_text(page_url)

    links = LinkCollector()
    links.feed(page_html)

    # Sources to scan for @font-face: the page first, then linked stylesheets
    # and everything they @import.
    sources = [(page_url, page_html)]
    pending = [urljoin(page_url, ref) for ref in links.stylesheets]
    pending += imported_stylesheets(page_url, page_html)
    seen = set()
    while pending:
        css_url = pending.pop(0)
        if css_url in seen:
            continue
        seen.add(css_url)
        print(f"  · stylesheet: {css_url}")
        try:
            css = fetch_text(css_url)
        except Exception as exc:
            print(f"    (failed) {exc}", file=sys.stderr)
            continue
        sources.append((css_url, css))
        pending += imported_stylesheets(css_url, css)

    mapping = {}  # url -> name; first (named) mapping wins
    ranges = {}   # url -> unicode-range; first declaration wins

    # Named @font-face mappings first — they win on dedupe.
    for base, text in sources:
        for url, name, urange in named_fonts(base, text):
            mapping.setdefault(url, name)
            if urange:
                ranges.setdefault(url, urange)

    # Preload links (no family info) -> basename.
    for ref in links.font_preloads:
        url = urljoin(page_url, ref)
        if is_font_url(url):
            mapping.setdefault(url, base_of(url))

    # Fallback basenames for any font url not covered above.
    for base, text in sources:
        for url, name in fallback_fonts(base, text):
            mapping.setdefault(url, name)

    return mapping, ranges


def dedupe_identical(out_dir):
    """Remove byte-identical font files (the same font loaded under several
    @font-face family aliases). Keeps one name per identical set. Returns the
    number of files removed."""
    by_hash = {}
    for path in sorted(out_dir.iterdir()):
        if path.suffix.lower() not in FONT_EXTS:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        by_hash.setdefault(digest, []).append(path)

    removed = 0
    for paths in by_hash.values():
        if len(paths) < 2:
            continue
        keep = min(paths, key=lambda p: p.name)  # deterministic; prefers "twk-lausanne" over "twklausanne"
        for path in paths:
            if path != keep:
                path.unlink()
                print(f"  · deduped {path.name} (identical to {keep.name})")
                removed += 1
    return removed


def _combine_var_stores(fonts):
    """Move each font's GDEF VarStore into one and return it.

    Mark positions in GPOS vary by weight through indices into this store, so
    the indices are shifted to where each font's data ends up.
    """
    combined = None
    for font in fonts:
        gdef = font["GDEF"].table if "GDEF" in font else None
        store = getattr(gdef, "VarStore", None)
        if store is None:
            continue
        gdef.VarStore = None
        if combined is None:
            combined = store
            continue

        outer_offset = len(combined.VarData)
        region_offset = len(combined.VarRegionList.Region)
        combined.VarRegionList.Region.extend(store.VarRegionList.Region)
        combined.VarRegionList.RegionCount = len(combined.VarRegionList.Region)
        for data in store.VarData:
            data.VarRegionIndex = [region_offset + i for i in data.VarRegionIndex]
            combined.VarData.append(data)
        combined.VarDataCount = len(combined.VarData)

        tables = [gdef] + ([font["GPOS"].table] if "GPOS" in font else [])
        indices = set()
        for table in tables:
            table.collect_device_varidxes(indices)
        mapping = {
            index: index if index == NO_VARIATION_INDEX else index + (outer_offset << 16)
            for index in indices
        }
        for table in tables:
            table.remap_device_varidxes(mapping)
    return combined


def _shared_glyph_keys(font):
    """Return {glyph id: [key, ...]} identifying each encoded glyph across subsets.

    A key combines the code point with the glyph's outline and advance, at the
    default location and at both ends of every variation axis. Only TrueType
    outlines are shared; other subsets keep Merger's separate glyph copies.
    """
    if "glyf" not in font:
        return {}
    locations = [{}]
    if "fvar" in font:
        for axis in font["fvar"].axes:
            locations += [{axis.axisTag: axis.minValue}, {axis.axisTag: axis.maxValue}]
    glyph_sets = [font.getGlyphSet(location=location) for location in locations]

    fingerprints, keys = {}, {0: [".notdef"]}
    for codepoint, name in font.getBestCmap().items():
        if name not in fingerprints:
            shapes = []
            for glyph_set in glyph_sets:
                pen = DecomposingRecordingPen(glyph_set)
                glyph_set[name].draw(pen)
                shapes.append((repr(pen.value), glyph_set[name].width))
            fingerprints[name] = tuple(shapes)
        keys.setdefault(font.getGlyphID(name), []).append((codepoint, fingerprints[name]))
    return keys


def _combined_glyph_order(orders):
    """Return one glyph order that preserves the relative order of every input.

    Layout coverage tables list glyphs by ascending glyph id, and browsers reject
    fonts where they do not, so no subset's glyphs may change relative position.
    """
    rank, successors, blockers = {}, {}, {}
    for order in orders:
        for name in order:
            rank.setdefault(name, len(rank))
            successors.setdefault(name, set())
            blockers.setdefault(name, 0)
        for before, after in zip(order, order[1:]):
            if before != after and after not in successors[before]:
                successors[before].add(after)
                blockers[after] += 1

    ready = [(rank[name], name) for name in rank if not blockers[name]]
    heapq.heapify(ready)
    combined = []
    while ready:
        _, name = heapq.heappop(ready)
        combined.append(name)
        for successor in successors[name]:
            blockers[successor] -= 1
            if not blockers[successor]:
                heapq.heappush(ready, (rank[successor], successor))
    if len(combined) != len(rank):
        raise ValueError("subsets order their shared glyphs differently")
    return combined


def _shared_glyph_order(glyph_keys):
    """Build a replacement for Merger's glyph naming step.

    Merger keeps a separate copy of every glyph that occurs in several subsets.
    Each subset's mark positioning then only reaches its own copy, and only one
    copy is encoded, so accents can lose their position. Giving identical glyphs
    one name makes all subsets' layout rules apply to the same glyph.
    """
    def order_glyphs(merger, glyph_orders):
        shared, used_names = {}, set()
        for order, keys in zip(glyph_orders, glyph_keys):
            for glyph_id, name in enumerate(order):
                glyph_keys_here = keys.get(glyph_id, [])
                unique = next((shared[k] for k in glyph_keys_here if k in shared), None)
                if unique is None:
                    unique, suffix = name, 1
                    while unique in used_names:
                        unique, suffix = f"{name}.{suffix}", suffix + 1
                    used_names.add(unique)
                shared.update((key, unique) for key in glyph_keys_here)
                order[glyph_id] = unique
        merger.glyphOrder = _combined_glyph_order(glyph_orders)
        renamed_orders.extend(glyph_orders)

    renamed_orders = []
    return order_glyphs, renamed_orders


def merge_font_files(paths):
    """Merge subset files of one font into a single TTFont.

    fontTools' Merger cannot handle variation data, so for variable fonts that
    data is set aside, the static parts are merged, and it is then restored
    under the merged glyph names.
    """
    fonts = [TTFont(str(path)) for path in paths]
    flavor = fonts[0].flavor
    variable = "fvar" in fonts[0]
    shared_tables, glyph_variations, var_store = {}, [], None
    order_glyphs, glyph_orders = _shared_glyph_order([_shared_glyph_keys(f) for f in fonts])

    if variable:
        axes = [
            [(a.axisTag, a.minValue, a.defaultValue, a.maxValue) for a in font["fvar"].axes]
            if "fvar" in font else None
            for font in fonts
        ]
        if any(a != axes[0] for a in axes):
            raise ValueError("subsets have different variation axes")
        shared_tables = {t: fonts[0][t] for t in ("fvar", "avar", "MVAR", "STAT") if t in fonts[0]}
        glyph_variations = [
            [font["gvar"].variations[g] for g in font.getGlyphOrder()] for font in fonts
        ]
        var_store = _combine_var_stores(fonts)
        for font in fonts:
            for tag in ("fvar", "avar", "gvar", "HVAR", "MVAR", "STAT"):
                if tag in font:
                    del font[tag]

    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for index, font in enumerate(fonts):
            font.flavor = None
            files.append(f"{tmp}/{index}")
            font.save(files[-1])
        with mock.patch("fontTools.merge.computeMegaGlyphOrder", order_glyphs):
            merged = Merger().merge(files)
        merged.flavor = flavor
        merged.recalcTimestamp = False
        if "glyf" in merged:  # Merger sums these, counting shared glyphs repeatedly
            merged["glyf"].glyphOrder = merged.getGlyphOrder()
            merged["maxp"].numGlyphs = len(merged.getGlyphOrder())

        if variable:
            gvar = newTable("gvar")
            gvar.version, gvar.reserved = 1, 0
            gvar.variations = {
                name: variations
                for order, font_variations in zip(glyph_orders, glyph_variations)
                for name, variations in zip(order, font_variations)
            }
            merged["gvar"] = gvar
            for tag, table in shared_tables.items():
                merged[tag] = table
            if var_store is not None:
                merged["GDEF"].table.VarStore = var_store
                merged["GDEF"].table.Version = max(merged["GDEF"].table.Version, 0x00010003)
            add_HVAR(merged)
    return merged


def merge_subsets(out_dir, hash_ranges):
    """Merge unicode-range subsets of the same face into one file.

    Only files served with a unicode-range are candidates, so the same font in
    another format or an unrelated file with matching metadata stays separate.
    A group that cannot be merged keeps its subset files. Returns the number of
    files removed.
    """
    groups = {}
    for path in sorted(out_dir.iterdir()):
        suffix = path.suffix.lower()
        if suffix not in FORMATS:
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() not in hash_ranges:
            continue
        meta = _face_metadata(path)
        if meta is not None:
            family, weight, italic = meta
            groups.setdefault((family, weight, italic, suffix), []).append(path)

    removed = 0
    for (family, *_), paths in groups.items():
        if len(paths) < 2:
            continue
        try:
            merge_font_files(paths).save(str(paths[0]))
        except Exception as exc:
            print(f"  (kept {len(paths)} subsets of {family} separate) {exc}", file=sys.stderr)
            continue
        for path in paths[1:]:
            path.unlink()
        print(f"  · merged {len(paths)} subsets of {family} into {paths[0].name}")
        removed += len(paths) - 1
    return removed


def rename_to_canonical(out_dir):
    """Rename font files to match their own metadata (see canonical_name).
    Distinct fonts that resolve to the same name get a -2, -3 suffix. Renames
    happen via temporary names so files can safely swap names."""
    fonts = sorted(p for p in out_dir.iterdir() if p.suffix.lower() in FONT_EXTS)

    # Resolve a unique final name for each file.
    final, used = {}, set()
    for path in fonts:
        base = canonical_name(path) or path.name
        name = base
        if name in used:
            stem, _, ext = base.rpartition(".")
            n = 2
            while f"{stem}-{n}.{ext}" in used:
                n += 1
            name = f"{stem}-{n}.{ext}"
        used.add(name)
        final[path] = name

    # Apply via temp names to avoid clashes when two files swap names.
    pending = [(p, final[p]) for p in fonts if final[p] != p.name]
    for i, (path, _) in enumerate(pending):
        path.rename(out_dir / f".rename-{i}.tmp")
    for i, (path, name) in enumerate(pending):
        (out_dir / f".rename-{i}.tmp").rename(out_dir / name)
        print(f"  · renamed {path.name} → {name}")
    return len(pending)


def group_face_files(out_dir):
    """Return the usable font files as one list per face, best format first.

    Sites commonly serve one face as both woff2 and woff. Files are the same face
    when their metadata, code points and advance widths match. Drawn outlines are
    not compared: woff2 can store different glyph bounds than woff, which shifts
    them. An unreadable file stays on its own, for the specimen and stylesheet
    writers to report.
    """
    faces = {}
    format_rank = list(FORMATS)
    for path in sorted(p for p in out_dir.iterdir() if p.suffix.lower() in FORMATS):
        try:
            font = TTFont(str(path), fontNumber=0, lazy=True)
            advances = font["hmtx"]
            widths = tuple(
                (codepoint, advances[name][0])
                for codepoint, name in sorted(font.getBestCmap().items())
            )
            face = (_face_metadata(path), widths)
        except Exception:
            face = path
        faces.setdefault(face, []).append(path)
    return [
        sorted(paths, key=lambda p: format_rank.index(p.suffix.lower()))
        for paths in faces.values()
    ]


def download_all(mapping, ranges, out_dir):
    """Download every font. Returns {content_hash: unicode_range} for the files
    whose @font-face rule declared a range — keyed by hash so it survives the
    later dedupe and rename steps."""
    out_dir.mkdir(parents=True, exist_ok=True)
    used = set()
    hash_ranges = {}
    for url, name in mapping.items():
        if name in used:
            stem, dot, ext = name.rpartition(".")
            n = 2
            while f"{stem}-{n}{dot}{ext}" in used:
                n += 1
            name = f"{stem}-{n}{dot}{ext}"
        used.add(name)
        try:
            data = fetch_bytes(url)
            (out_dir / name).write_bytes(data)
            if url in ranges:
                hash_ranges.setdefault(hashlib.sha256(data).hexdigest(), ranges[url])
            print(f"  ✓ {name}")
        except Exception as exc:
            print(f"  ✗ {url} ({exc})", file=sys.stderr)
    return hash_ranges


def main():
    if len(sys.argv) != 2:
        print("Usage: font-forager.py <url>", file=sys.stderr)
        return 1

    page_url = sys.argv[1]
    host = urlsplit(page_url).hostname or "unknown"
    out_dir = Path("data") / host

    mapping, ranges = collect_font_map(page_url)
    if not mapping:
        print("No fonts found.")
        return 0

    print(f"→ Downloading {len(mapping)} font(s) into {out_dir}/")
    hash_ranges = download_all(mapping, ranges, out_dir)

    removed = dedupe_identical(out_dir)
    if removed:
        print(f"→ Removed {removed} duplicate file(s)")

    merged = merge_subsets(out_dir, hash_ranges)
    if merged:
        print(f"→ Merged subsets, removing {merged} file(s)")

    renamed = rename_to_canonical(out_dir)
    if renamed:
        print(f"→ Renamed {renamed} file(s) to match font metadata")

    faces = group_face_files(out_dir)
    write_specimen(out_dir, host, faces)
    write_stylesheet(out_dir, host, faces, hash_ranges)
    print(f"Done → {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
