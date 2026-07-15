#!/usr/bin/env python3
"""font-forager — download the fonts a web page loads, and generate a specimen.

Usage: .venv/bin/python font-forager.py <url>

Setup once:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

Fetches the page and its linked stylesheets, extracts every font URL it can find
in @font-face rules and <link rel="preload" as="font"> tags, and downloads each
font into data/<host>/. Byte-identical files (the same font served under several
@font-face family aliases) are deduplicated, and each file is then named from its
own metadata as <family-slug>-<weight>[-italic].<ext> — family from the name
table, weight from OS/2.usWeightClass — so the filename reflects the font's real
weight rather than any remapped @font-face font-weight. Colliding names get a
-2, -3 suffix.

Afterwards two files are written into data/<host>/: a specimen.html specimen
showing for each downloaded font the characters it actually contains (read from
its cmap), and a style.css with a reusable @font-face rule per font — keyed on
the font's real family name and weight — so the folder can be dropped into a
website as-is.

Note: this is a static fetch. Fonts injected at runtime by JavaScript are not
discovered.
"""

import hashlib
import re
import sys
import unicodedata
import urllib.request
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from fontTools.ttLib import TTFont

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


def _decl(block, prop):
    m = re.search(rf"{prop}\s*:\s*([^;}}]+)", block, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def named_fonts(base, text):
    """Yield (resolved_url, filename, unicode_range) for every @font-face font.

    unicode_range is "" when the rule declares none. It is carried through so a
    subsetted family (many files, same family/weight/style, each covering a
    different range) can be reassembled faithfully in the generated style.css.
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


# --------------------------------------------------------------------------- #
# Canonical naming from font metadata
# --------------------------------------------------------------------------- #

def _font_names(font):
    name = font["name"]
    family = name.getDebugName(16) or name.getDebugName(1) or ""
    subfamily = name.getDebugName(17) or name.getDebugName(2) or ""
    return family.strip(), subfamily.strip()


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
    OS/2.usWeightClass, not in the site's @font-face declaration (which may
    remap weights — e.g. bird.com serves "TWK Lausanne 450" as font-weight:500).
    Returns None if the metadata is unreadable or incomplete.
    """
    try:
        font = TTFont(str(path), fontNumber=0, lazy=True)
        family = _font_names(font)[0]
        weight = font["OS/2"].usWeightClass
    except Exception:
        return None
    slug = slugify(family)
    if not slug or not weight:
        return None
    italic = "-italic" if _is_italic(font) else ""
    return f"{slug}-{weight}{italic}{path.suffix.lower()}"


# --------------------------------------------------------------------------- #
# Specimen generation
# --------------------------------------------------------------------------- #


def _visible_codepoints(cmap):
    out = []
    for cp in sorted(cmap):
        if unicodedata.category(chr(cp))[0] not in ("C", "Z"):
            out.append(cp)
    return out


def _render_font(path, index):
    """Return an HTML section for one font, or (None, error)."""
    try:
        font = TTFont(str(path), fontNumber=0, lazy=True)
        cmap = font.getBestCmap()
    except Exception as exc:  # unreadable / no unicode cmap
        return None, f"{path.name}: {exc}"

    family, subfamily = _font_names(font)
    label = " ".join(p for p in (family, subfamily) if p) or path.stem
    visible = _visible_codepoints(cmap)
    face_id = f"ff-{index}"
    fmt = FORMATS.get(path.suffix.lower(), "")
    src = f'url("{escape(path.name)}")' + (f' format("{fmt}")' if fmt else "")

    cells = "".join(
        f'<div class="cell"><span class="glyph">{escape(chr(cp))}</span>'
        f'<span class="cp">{cp:04X}</span></div>'
        for cp in visible
    )
    css = f"@font-face{{font-family:'{face_id}';src:{src};font-display:swap;}}"
    return (
        f"""<section>
  <style>{css}</style>
  <header>
    <h2 style="font-family:'{face_id}',system-ui,sans-serif">{escape(label)}</h2>
    <p class="meta">{escape(path.name)} &middot; {len(cmap)} characters ({len(visible)} shown)</p>
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
  .grid { display:grid; gap:4px;
    grid-template-columns:repeat(auto-fill,minmax(56px,1fr)); }
  .cell { display:flex; flex-direction:column; align-items:center;
    justify-content:center; aspect-ratio:1; background:var(--cell);
    border:1px solid var(--line); border-radius:6px; }
  .glyph { font-size:1.6rem; line-height:1; }
  .cp { font-family:ui-monospace,monospace; font-size:.6rem;
    color:var(--muted); margin-top:.35rem; }
"""


def write_specimen(out_dir, host):
    fonts = sorted(p for p in out_dir.iterdir() if p.suffix.lower() in FONT_EXTS)
    sections = []
    for index, path in enumerate(fonts):
        section, err = _render_font(path, index)
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


def _face_metadata(path):
    """Return (family, weight, italic, fmt) for a font, or None if unreadable.

    Like canonical_name, this reads the truth from the font's own name table and
    OS/2.usWeightClass rather than any @font-face declaration.
    """
    try:
        font = TTFont(str(path), fontNumber=0, lazy=True)
        family = _font_names(font)[0]
        weight = font["OS/2"].usWeightClass
    except Exception:
        return None
    if not family or not weight:
        return None
    return family, weight, _is_italic(font), FORMATS.get(path.suffix.lower(), "")


def write_stylesheet(out_dir, host, hash_ranges=None):
    """Write style.css with one reusable @font-face rule per downloaded font.

    Each rule uses the font's real family name and weight, so a site can select
    a family by name and let the browser pick the matching weight/style. Paths
    are relative, so style.css works wherever the font files sit beside it.
    When a font was served as a unicode-range subset, that range is emitted so
    the split is preserved (looked up in hash_ranges by the file's content).
    """
    hash_ranges = hash_ranges or {}
    faces = []
    for path in sorted(p for p in out_dir.iterdir() if p.suffix.lower() in FONT_EXTS):
        meta = _face_metadata(path)
        if meta is None:
            print(f"  (skipped in style.css) {path.name}: unreadable metadata", file=sys.stderr)
            continue
        urange = hash_ranges.get(hashlib.sha256(path.read_bytes()).hexdigest(), "")
        faces.append((path, *meta, urange))

    if not faces:
        return

    # Group by family, then weight, upright before italic.
    faces.sort(key=lambda f: (f[1].lower(), f[2], f[3]))

    rules = []
    for path, family, weight, italic, fmt, urange in faces:
        css_family = family.replace("\\", "\\\\").replace('"', '\\"')
        src = f'url("{path.name}")' + (f' format("{fmt}")' if fmt else "")
        range_line = f"  unicode-range: {urange};\n" if urange else ""
        rules.append(
            "@font-face {\n"
            f'  font-family: "{css_family}";\n'
            f"  font-style: {'italic' if italic else 'normal'};\n"
            f"  font-weight: {weight};\n"
            "  font-display: swap;\n"
            f"  src: {src};\n"
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
    unicode-range; it is used to preserve subset splits in the style.css.
    """
    print(f"→ Fetching page: {page_url}")
    page_html = fetch_text(page_url)

    links = LinkCollector()
    links.feed(page_html)

    # Sources to scan for @font-face: the page first, then linked stylesheets.
    sources = [(page_url, page_html)]
    for ref in links.stylesheets:
        css_url = urljoin(page_url, ref)
        print(f"  · stylesheet: {css_url}")
        try:
            sources.append((css_url, fetch_text(css_url)))
        except Exception as exc:
            print(f"    (failed) {exc}", file=sys.stderr)

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

    renamed = rename_to_canonical(out_dir)
    if renamed:
        print(f"→ Renamed {renamed} file(s) to match font metadata")

    write_specimen(out_dir, host)
    write_stylesheet(out_dir, host, hash_ranges)
    print(f"Done → {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
