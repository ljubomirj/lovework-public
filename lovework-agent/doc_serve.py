"""
Markdown file serving and directory listing for the LoveWork dashboard.

Imported by dashboard_server.py to serve docs/, profiles/, MANUAL.md, README.md,
and any other .md file in the lovework/ tree as rendered HTML — same process,
same port as the live crawl dashboard.
"""

import html as html_mod
import mimetypes
import re
from datetime import datetime

# Register common text extensions that browsers should display inline.
mimetypes.add_type("text/plain", ".log")
mimetypes.add_type("text/plain", ".yaml")
mimetypes.add_type("text/plain", ".yml")
mimetypes.add_type("text/plain", ".toml")
mimetypes.add_type("text/plain", ".cfg")
mimetypes.add_type("text/plain", ".conf")
import functools
from pathlib import Path
from typing import Optional

import markdown

from notify import human_slug


# ── Config ────────────────────────────────────────────────────────────────

EXTENSIONS = [
    "extra",
    "toc",
    "sane_lists",
    "smarty",
]

EXCLUDE_DIRS = {".git", ".git-private", ".git-public", "node_modules",
                "__pycache__", ".pytest_cache", "venv", "venv-test",
                "cache", "OLD"}

EXCLUDE_PREFIXES = (".", "~", ".README.LJ.sw")


# ── Markdown engine ───────────────────────────────────────────────────────

_md = markdown.Markdown(extensions=EXTENSIONS)


#: What `smarty` turns ASCII dashes into, and what they were.
#:
#: Python-Markdown's `smarty` extension applies to **text**, and a URL is text
#: when it is written out — in a link's label, or bare in a sentence. So a URL
#: containing `--` was *displayed* with an en-dash while its `href` stayed correct:
#:
#:     <a href="…/Scientific-Developer--Chemical-Biology-Resources-_JR4508">
#:        https://…/Scientific-Developer&ndash;Chemical-Biology-Resources-_JR4508</a>
#:
#: Clicking worked; **copying the link did not** — which is exactly how LJ hit it
#: (2026-10-14, the EMBL-EBI advert: *"The link on the page … opening that link
#: leads to …"* — the en-dash he pasted came from the rendered text, and the stored
#: file has the double hyphen in both places). A bare URL in prose was worse: the
#: visible text was corrupted *before* `auto_link_urls` turned it into a link.
#:
#: **A URL is data, not prose.** The typographic pass may keep its dashes in
#: sentences — that is the house style — and must not touch a URL.
_TYPOGRAPHIC_DASHES = (("&ndash;", "--"), ("&mdash;", "---"),
                       ("&#8211;", "--"), ("&#8212;", "---"),
                       ("\u2013", "--"), ("\u2014", "---"))

_URL_IN_HTML_RE = re.compile(r"""https?://[^\s<>"']+""")


def restore_url_dashes(html_text: str) -> str:
    """Put back the ASCII dashes inside every URL the typographic pass rewrote."""
    def _fix(match: "re.Match") -> str:
        token = match.group(0)
        for entity, ascii_form in _TYPOGRAPHIC_DASHES:
            token = token.replace(entity, ascii_form)
        return token

    return _URL_IN_HTML_RE.sub(_fix, html_text)


def render_markdown(text: str) -> str:
    _md.reset()
    return restore_url_dashes(_md.convert(text))


_ANCHOR_SPAN_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)


def auto_link_urls(html_text: str) -> str:
    """Turn bare HTTP/HTTPS URLs into clickable links with readable text.

    The visible text is the same short slug the notification email uses
    (`notify.human_slug`), so a tracking-laden advert URL does not dominate the
    page; the full URL lives only in the anchor's `href`.

    Runs *after* markdown rendering, so the input is already HTML-escaped and is
    passed through verbatim (re-escaping would turn `&amp;` into `&amp;amp;`).
    URLs already inside an `<a>` element, or inside an `href` attribute, are
    left alone.
    """
    spans = [(m.start(), m.end()) for m in _ANCHOR_SPAN_RE.finditer(html_text)]

    def _inside_anchor(pos: int) -> bool:
        return any(start <= pos < end for start, end in spans)

    def _anchor(match: "re.Match[str]") -> str:
        raw = match.group(0)
        if _inside_anchor(match.start()):
            return raw
        url = raw.rstrip('").,')          # trailing punctuation stays outside the link
        return f'<a href="{url}">{human_slug(url)}</a>{raw[len(url):]}'

    return re.sub(r'(?<!href=["\'])https?://[^\s<>")]+(?:"|\))?', _anchor, html_text)


def rewrite_md_links(html_text: str, request_path: str) -> str:
    def _fix(m):
        href = m.group(1)
        if href.startswith(("http://", "https://", "mailto:", "#", "/")):
            return m.group(0)
        if not href.endswith(".md") and "." in href.rsplit("/", 1)[-1]:
            return m.group(0)
        base_dir = request_path.rsplit("/", 1)[0] if "/" in request_path else ""
        resolved = f"{base_dir}/{href}" if base_dir else href
        parts = resolved.split("/")
        clean = []
        for p in parts:
            if p == ".." and clean:
                clean.pop()
            elif p and p != ".":
                clean.append(p)
        resolved = "/" + "/".join(clean)
        return f'href="{resolved}"'
    return re.sub(r'href="([^"]+)"', _fix, html_text)


# ── HTML templates ────────────────────────────────────────────────────────

# Progressive-enhancement collapse for report listings (see
# `add_listing_collapse`). The server renders every listing expanded with a
# `+` marker; this script only *adds* the collapsed state, mirroring the
# `+`/`−` inline toggle on LJ's own site (post-knowing.html) without the
# surrounding site CSS. Injected as a format value, not a literal, so its
# braces need no escaping.
LISTING_TOGGLE_SCRIPT = """\
<script>
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.li-toggle').forEach(function (btn) {
    var body = document.getElementById(btn.getAttribute('aria-controls'));
    if (!body) { return; }
    var score = btn.nextElementSibling;
    btn.addEventListener('click', function () {
      if (!body.hasAttribute('hidden')) {
        body.setAttribute('hidden', '');
        btn.textContent = '\u2212';
        btn.setAttribute('aria-expanded', 'false');
        btn.title = 'Expand listing';
        if (score) { score.hidden = false; }
      } else {
        body.removeAttribute('hidden');
        btn.textContent = '+';
        btn.setAttribute('aria-expanded', 'true');
        btn.title = 'Collapse listing';
        if (score) { score.hidden = true; }
      }
    });
  });
  // Section toggles are the coarse control: they hide the whole section body.
  // Listing state is left alone, so reopening a section restores whatever the
  // reader had collapsed inside it.
  document.querySelectorAll('.sec-toggle').forEach(function (btn) {
    var body = document.getElementById(btn.getAttribute('aria-controls'));
    if (!body) { return; }
    btn.addEventListener('click', function () {
      if (!body.hasAttribute('hidden')) {
        body.setAttribute('hidden', '');
        btn.textContent = '\u2212';
        btn.setAttribute('aria-expanded', 'false');
        btn.title = 'Expand section';
      } else {
        body.removeAttribute('hidden');
        btn.textContent = '+';
        btn.setAttribute('aria-expanded', 'true');
        btn.title = 'Collapse section';
      }
    });
  });
});
</script>
"""

PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — LoveWork Docs</title>
<style>
  :root {{
    --bg: #FAFAF7; --fg: #1A1A1A; --accent: #1D4ED8; --line: #D4D4D0;
    --code-bg: #EEEEEA;
    --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--fg); font-family: var(--sans); font-size: 16px; line-height: 1.6; padding: 0; }}
  .nav {{ background: #fff; border-bottom: 1px solid var(--line); padding: 12px 24px; font-size: 14px; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }}
  .nav a {{ color: var(--accent); text-decoration: none; }}
  .nav a:hover {{ text-decoration: underline; }}
  .container {{ max-width: 880px; margin: 0 auto; padding: 32px 24px 80px; }}
  h1, h2, h3, h4 {{ margin-top: 1.5em; margin-bottom: 0.5em; line-height: 1.3; }}
  h1 {{ font-size: 1.8em; border-bottom: 2px solid var(--line); padding-bottom: 0.3em; }}
  h2 {{ font-size: 1.4em; border-bottom: 1px solid var(--line); padding-bottom: 0.2em; }}
  h3 {{ font-size: 1.15em; }}
  p, ul, ol, blockquote, table {{ margin-bottom: 1em; }}
  ul, ol {{ padding-left: 1.5em; }}
  a {{ color: var(--accent); }}
  code {{ background: var(--code-bg); font-family: var(--mono); font-size: 0.9em; padding: 0.15em 0.35em; border-radius: 3px; }}
  pre {{ background: var(--code-bg); padding: 12px 16px; border-radius: 4px; overflow-x: auto; margin-bottom: 1em; }}
  pre code {{ background: none; padding: 0; font-size: 0.85em; }}
  blockquote {{ border-left: 3px solid var(--accent); padding: 8px 16px; margin-left: 0; color: #444; background: #F0F0EC; border-radius: 0 4px 4px 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid var(--line); padding: 6px 10px; text-align: left; }}
  th {{ background: var(--code-bg); font-weight: 600; }}
  img {{ max-width: 100%; }}
  hr {{ border: none; border-top: 1px solid var(--line); margin: 2em 0; }}
  .footer {{ margin-top: 3em; padding-top: 1em; border-top: 1px solid var(--line); font-size: 13px; color: #888; }}
  .li-toggle {{ font-family: var(--mono); font-size: 0.85em; line-height: 1; padding: 1px 6px; margin-left: 8px; border: 1px solid var(--line); border-radius: 3px; background: #fff; color: var(--fg); cursor: pointer; vertical-align: middle; }}
  .sec-toggle {{ font-family: var(--mono); font-size: 0.8em; line-height: 1; padding: 2px 8px; margin-left: 10px; border: 1px solid var(--line); border-radius: 3px; background: #fff; color: var(--fg); cursor: pointer; vertical-align: middle; }}
  .sec-toggle:hover, .li-toggle:hover {{ background: var(--code-bg); }}
  .li-ordinal {{ font-family: var(--mono); font-size: 0.72em; font-weight: 600; color: #777; margin-right: 8px; vertical-align: middle; }}
  .li-toggle:hover {{ background: var(--code-bg); }}
  .li-body[hidden] {{ display: none; }}
  .li-score {{ font-size: 0.78em; font-weight: 600; color: #666; margin-left: 8px; vertical-align: middle; }}
</style>
</head>
<body>
<div class="nav">
  <a href="/">🏠 LoveWork</a>
  <a href="/docs/00-index.md">📖 Docs</a>
  <a href="/MANUAL.md">📋 Manual</a>
  <a href="/README.md">ℹ️ About</a>
  <span class="path">{breadcrumb}</span>
</div>
<div class="container">
{content}
<div class="footer">
  Generated {date} · <a href="{raw_url}">View raw</a> · <a href="/">Back to LoveWork</a>
</div>
</div>
{listing_script}
</body>
</html>"""

DIR_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — LoveWork Docs</title>
<style>
  :root {{ --bg: #FAFAF7; --fg: #1A1A1A; --accent: #1D4ED8; --line: #D4D4D0; --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--fg); font-family: var(--sans); font-size: 16px; }}
  .nav {{ background: #fff; border-bottom: 1px solid var(--line); padding: 12px 24px; font-size: 14px; display: flex; gap: 16px; }}
  .nav a {{ color: var(--accent); text-decoration: none; }}
  .nav a:hover {{ text-decoration: underline; }}
  .container {{ max-width: 880px; margin: 0 auto; padding: 32px 24px 80px; }}
  h1 {{ font-size: 1.6em; margin-bottom: 0.5em; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 5px 0; }}
  li a {{ color: var(--accent); text-decoration: none; font-size: 15px; }}
  li a:hover {{ text-decoration: underline; }}
  .readme {{ margin-top: 2em; border-top: 1px solid var(--line); padding-top: 1em; }}
</style>
</head>
<body>
<div class="nav">
  <a href="/">🏠 LoveWork</a>
  <a href="/docs/00-index.md">📖 Docs</a>
  <a href="/MANUAL.md">📋 Manual</a>
  <span style="color:#666">{rel_path}</span>
</div>
<div class="container">
<h1>{title}</h1>
<ul>
{items}
</ul>
{readme_html}
</div>
</body>
</html>"""


# ── Helpers ───────────────────────────────────────────────────────────────

# Splitting on every h1/h2/h3 boundary keeps each wrap limited to the block
# between one heading and the next.
_HEADING_SPLIT_RE = re.compile(r"(?=<h[123]\b)", re.IGNORECASE)
# A listing is an h3 that already carries an ordinal (`### 1. Title`), **or any
# h3 inside a findings section**. The generators emit unnumbered `### Org — Role`
# headings, so the section context is what decides; the ordinal is added at
# render time and never written back to the markdown.
_LISTING_HEADING_RE = re.compile(r"<h3\b[^>]*>\s*\d+\..*?</h3>", re.IGNORECASE | re.DOTALL)
_FINDINGS_SECTION_RE = re.compile(r"<h2\b[^>]*>\s*(GO|MAYBE|FLAG|DROP)\b",
                                 re.IGNORECASE)
_H3_TEXT_RE = re.compile(r"(<h3\b[^>]*>)(.*?)(</h3>)", re.IGNORECASE | re.DOTALL)
_COMBINED_RE = re.compile(r"<strong>Combined</strong>:\s*([^<]+)")
_SCORE_RE = re.compile(r"<strong>Score</strong>:\s*([^<]+)")


def add_listing_collapse(html_text: str) -> str:
    """Number each listing within its section and wrap it in a collapsible body.

    Two things happen here, both at render time only:

    * **Ordinals** — each listing heading gets `N/M`, where `M` is how many
      listings its own section carries, so `## GO (36)` reads `1/36 … 36/36`.
      The markdown file is left exactly as written.
    * **A `+` toggle** — the listing's body collapses to its title plus score.

    A full report carries thousands of listings, each a long bullet block; the
    toggle lets the page be skimmed. Interaction mirrors the `+`/`−` inline
    toggle on LJ's own site (post-knowing.html), but inverted for progressive
    enhancement: the server renders every block *expanded* with a `+` marker and
    no `hidden` attribute, and the inline script only adds the collapsed state.
    With JavaScript disabled the report is therefore still fully readable as
    plain HTML.

    Only h3 headings count as listings — numbered ones anywhere, or any h3 inside
    a `GO`/`MAYBE`/`FLAG`/`DROP` section — so the report header, `## Schedule`,
    `## What Runs Where`, `## LoveWork application packs` and the closing
    `## Link re-check` section stay untouched. The h3 (and its `toc` anchor id)
    is preserved, as are all links inside the block.
    """
    segments = _HEADING_SPLIT_RE.split(html_text)

    # Pass 1: decide what is a listing, and count them per section.
    in_findings = False
    section = -1
    counts: list[int] = []
    is_listing: list[bool] = []
    for seg in segments:
        if seg.startswith("<h2"):
            in_findings = bool(_FINDINGS_SECTION_RE.match(seg))
            section += 1
            counts.append(0)
        listing = seg.startswith("<h3") and (
            bool(_LISTING_HEADING_RE.match(seg)) or in_findings)
        is_listing.append(listing)
        if listing:
            counts[section] += 1

    # Pass 2: emit with ordinals and toggles.
    out = [segments[0]]
    in_findings = False
    section = -1
    seen = 0
    for seg, listing in zip(segments[1:], is_listing[1:]):
        if seg.startswith("<h2"):
            in_findings = bool(_FINDINGS_SECTION_RE.match(seg))
            section += 1
            seen = 0
        if not listing:
            out.append(seg)
            continue
        seen += 1
        m = _H3_TEXT_RE.match(seg)
        if not m:
            out.append(seg)
            continue
        heading_open, heading_text, heading_close = m.groups()
        body = seg[m.end():]
        toggle_id = f"listing-body-{section}-{seen}"
        score = _COMBINED_RE.search(body) or _SCORE_RE.search(body)
        score_span = (
            f'<span class="li-score" hidden>{score.group(1).strip()}</span>'
            if score else ""
        )
        ordinal = (f'<span class="li-ordinal">{seen}/{counts[section]}</span> '
                   if counts[section] else "")
        heading = (
            f"{heading_open}{ordinal}{heading_text}"
            f' <button class="li-toggle" type="button"'
            f' aria-controls="{toggle_id}" aria-expanded="true"'
            f' title="Collapse listing">+</button>{score_span}{heading_close}'
        )
        out.append(f'{heading}<div class="li-body" id="{toggle_id}">{body}</div>')
    return "".join(out)


def add_section_collapse(html_text: str) -> str:
    """Give each findings section its own `+`: collapse all of its listings.

    The section toggle is the coarse control and the listing toggle the fine one;
    they are independent, so collapsing a section does not disturb a listing the
    reader had already collapsed (or vice versa) once the section is reopened.
    """
    parts = _HEADING_SPLIT_RE.split(html_text)
    out = [parts[0]]
    open_section = False
    n = 0
    for seg in parts[1:]:
        if seg.startswith("<h2"):
            if open_section:
                out.append("</div>")
                open_section = False
            if _FINDINGS_SECTION_RE.match(seg):
                n += 1
                section_id = f"section-body-{n}"
                seg = _H3_TEXT_RE.sub(
                    lambda m: m.group(0), seg)  # no-op: keep the heading intact
                seg = seg.replace(
                    "</h2>",
                    f' <button class="sec-toggle" type="button"'
                    f' aria-controls="{section_id}" aria-expanded="true"'
                    f' title="Collapse section">+</button></h2>', 1)
                out.append(seg)
                out.append(f'<div class="sec-body" id="{section_id}">')
                open_section = True
                continue
        out.append(seg)
    if open_section:
        out.append("</div>")
    return "".join(out)


def is_hidden(path: Path) -> bool:
    name = path.name
    if name in EXCLUDE_DIRS:
        return True
    if name.startswith(EXCLUDE_PREFIXES):
        return True
    if name.endswith("~"):
        return True
    return False


def make_breadcrumb(request_path: str) -> str:
    if request_path in ("", "/"):
        return "&nbsp;"
    parts = request_path.strip("/").split("/")
    crumbs = []
    acc = ""
    for i, p in enumerate(parts):
        acc += f"/{p}"
        name = p.replace(".md", "").replace("-", " ").replace("_", " ").title()
        if i == len(parts) - 1:
            crumbs.append(f"<span>{name}</span>")
        else:
            crumbs.append(f'<a href="{acc}">{name}</a>')
    return " / ".join(crumbs)


def _render_markdown_page(fs_path: Path, request_path: str, lovework_root: Path) -> Optional[str]:
    try:
        raw = fs_path.read_text(encoding="utf-8")
    except Exception:
        return None

    body = render_markdown(raw)
    body = rewrite_md_links(body, request_path)
    body = auto_link_urls(body)
    body = add_section_collapse(add_listing_collapse(body))

    title_m = re.search(r"<h1>(.*?)</h1>", body)
    title = html_mod.unescape(title_m.group(1)) if title_m else fs_path.stem

    return PAGE_HTML.format(
        title=html_mod.escape(title),
        content=body,
        breadcrumb=make_breadcrumb(request_path),
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        raw_url=f"{request_path}?raw",
        listing_script=LISTING_TOGGLE_SCRIPT,
    )


def _render_directory_page(fs_path: Path, request_path: str, lovework_root: Path) -> Optional[str]:
    readme_html = ""
    for rn in ("README.md", "index.md"):
        rp = fs_path / rn
        if rp.exists():
            try:
                raw = rp.read_text(encoding="utf-8")
                rhtml = render_markdown(raw)
                readme_html = rewrite_md_links(rhtml, f"{request_path}/{rn}")
                readme_html = auto_link_urls(readme_html)
                readme_html = f'<div class="readme">{readme_html}</div>'
            except Exception:
                pass
            break

    items = []
    try:
        # Reports directories: sort by mtime descending (newest first).
        # All other directories: sort alphabetically, directories first.
        is_reports_dir = fs_path.name == "reports"
        if is_reports_dir:
            entries = sorted(
                (e for e in fs_path.iterdir() if not is_hidden(e)),
                key=lambda e: (e.stat().st_mtime, e.name.lower()),
                reverse=True,
            )
        else:
            entries = sorted(
                (e for e in fs_path.iterdir() if not is_hidden(e)),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
    except PermissionError:
        entries = []

    rel_path = request_path or "/"
    if rel_path != "/":
        parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        parent_href = parent or "/"
        items.append(f'<li><a href="{parent_href}">📁 ..</a> <span>(parent)</span></li>')

    for e in entries:
        name = e.name
        href = f"{request_path}/{name}" if request_path else f"/{name}"
        if e.is_dir():
            items.append(f'<li><a href="{href}">📁 {name}/</a></li>')
        elif name.endswith(".md"):
            items.append(f'<li><a href="{href}">📄 {name.replace(".md","")}</a></li>')
        else:
            items.append(f'<li><a href="{href}">{name}</a></li>')

    title = rel_path.strip("/").replace("/", " / ") if rel_path != "/" else "LoveWork"
    return DIR_HTML.format(
        title=f"{html_mod.escape(title)} — LoveWork Docs",
        rel_path=html_mod.escape(rel_path),
        items="\n".join(items),
        readme_html=readme_html,
    )


# ── Public entry point ────────────────────────────────────────────────────

_ORG_PAGE_RE = None  # compiled lazily (module import order)
#: the shape the dashboard hands `try_serve_path`: relative to the wiki root
_WIKI_ORG_REL_RE = None


def _fold(name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", (name or "").lower())


@functools.lru_cache(maxsize=8)
def _load_renames_cached(path_str: str, mtime_ns: int, size: int) -> dict:
    """Parse a principal's org-renames map, keyed by its stamp.

    The map was re-read and re-parsed on every resolved link — the server did
    it per request, the link audit per link (13.7 s per principal, 2026-10-07).
    The stamp in the key keeps it correct when the map is rewritten.
    """
    import json as _json

    try:
        return {k.lower(): v for k, v in
                _json.loads(Path(path_str).read_text(encoding="utf-8")).items()}
    except (ValueError, OSError):
        return {}


def _load_renames(rmap: Path) -> dict:
    if not rmap.is_file():
        return {}
    try:
        st = rmap.stat()
    except OSError:
        return {}
    return _load_renames_cached(str(rmap), st.st_mtime_ns, st.st_size)


def resolve_wiki_org(lovework_root: Path, rel: str) -> Optional[Path]:
    """Serving-layer fallback for org-page links (2026-10-07 class fix).

    Historical reports linked org pages with a folded or kebab slug
    ("isomorphic-labs", "eworgmbh") and the 2026-10-07 migration renamed
    pages ("Adyen_https_www.adyen.com" → "Adyen") — both waves of link rot
    are fixed HERE rather than by rewriting sealed reports: fold-match the
    requested slug against the orgs directory, then consult a
    org-renames.json map if the principal has one. Exact files are never
    redirected; only genuinely missing paths fall through.
    """
    import re as _re
    global _ORG_PAGE_RE, _WIKI_ORG_REL_RE
    # **Both directories**: an organisation page lives in `orgs/` or `agencies/`,
    # depending on the kind the page records (LJ asked for the separation,
    # 2026-10-15). A resolver that knew only `orgs/` would 404 every agency link the
    # moment the pages moved — the same shape of bug this function already carries a
    # scar from (the fallback that never fired in production).
    if _ORG_PAGE_RE is None:
        _ORG_PAGE_RE = _re.compile(
            r"^(principals/(?P<p>[^/]+)/wiki/(?P<area>orgs|agencies)/)"
            r"(?P<slug>[^/]+)\.md$")
    if _WIKI_ORG_REL_RE is None:
        _WIKI_ORG_REL_RE = _re.compile(r"^(?P<area>orgs|agencies)/(?P<slug>[^/]+)\.md$")
    m = _ORG_PAGE_RE.match(rel)
    if m:
        # repo-relative shape ("principals/<p>/wiki/orgs/<slug>.md") — what a
        # caller with the repo root in hand passes
        orgs_dir = (lovework_root / "state" / m.group("p") / "wiki" / m.group("area"))
        slug = m.group("slug")
    else:
        # root-relative shape ("orgs/<slug>.md") — what the dashboard actually
        # passes, because `_serve_principal_public_path` splits the URL into
        # principal + area and hands `try_serve_path` the wiki root with the
        # remainder. The fallback accepted only the first shape, so it never
        # fired in production while the unit test (which uses the first shape)
        # stayed green: 0 of 582 (lj), 886 (vj), 74 (kj) broken org links were
        # actually repaired (2026-10-07, link-integrity audit).
        m2 = _WIKI_ORG_REL_RE.match(rel)
        if not m2:
            return None
        orgs_dir = lovework_root / m2.group("area")
        slug = m2.group("slug")
    if not orgs_dir.is_dir():
        # The page may have moved between the directories since the link was
        # written: look in the sibling before giving up.
        sibling = orgs_dir.parent / ("agencies" if orgs_dir.name == "orgs" else "orgs")
        if sibling.is_dir():
            orgs_dir = sibling
        else:
            return None
    wanted = _fold(slug)
    if not wanted:
        return None
    renames = _load_renames(orgs_dir.parent / "org-renames.json")
    renamed = renames.get(slug.lower())
    if renamed and (orgs_dir / (renamed + ".md")).is_file():
        return orgs_dir / (renamed + ".md")
    # lowest stem wins, so two spellings that fold alike always resolve to the
    # same page — the previous early `break` made the choice glob-order
    # dependent (nondeterministic on the same tree).
    best = None
    for f in orgs_dir.glob("*.md"):
        if _fold(f.stem) == wanted and (best is None or f.stem < best.stem):
            best = f
    return best


def _resolve_scoped_path(root: Path, relative_path: str) -> Optional[Path]:
    """Resolve a request-relative path without allowing an escape from root."""
    try:
        root = root.resolve()
        principal = (root / relative_path).resolve()
        principal.relative_to(root)
    except (OSError, ValueError):
        return None
    return principal


def resolve_served_path(rel: str, lovework_root: Path) -> Optional[Path]:
    """The filesystem path serving would use for `rel` — resolution only.

    Split out of `try_serve_path` so a detector can ask *where a link lands*
    without rendering the page it lands on, and so the two can never disagree:
    the link-integrity audit asks this, the server asks this. `rel` is
    relative to `lovework_root` (the dashboard hands the wiki root and
    `orgs/<slug>.md`), and the link-rot fallback is the same one the reader
    gets.
    """
    rel = (rel or "").lstrip("/")
    fs_path = _resolve_scoped_path(lovework_root, rel)
    if fs_path is None:
        return None
    if fs_path.exists():
        return fs_path
    return resolve_wiki_org(lovework_root, rel)


def try_serve_path(
    path: str,
    query: str,
    lovework_root: Path,
    *,
    request_path: Optional[str] = None,
) -> Optional[dict]:
    """Try to serve a path as a doc or directory listing.

    Returns a dict with "data" (bytes), "mime" (str), or None if the
    path is outside the doc-serving scope.
    """
    # `resolve_served_path` owns resolution (including the link-rot fallback)
    # so that the server and the link detector cannot drift.
    # ``path`` is filesystem-relative to lovework_root. ``request_path`` is
    # the public URL used in generated links, which may intentionally differ
    # for a narrowly published subtree such as /principals/vj/wiki/.
    path = path.rstrip("/") or "/"
    rel = path.lstrip("/")
    fs_path = resolve_served_path(rel, lovework_root)
    if fs_path is None:
        return None
    request_path = (request_path or path).rstrip("/") or "/"

    if fs_path.is_dir():
        html = _render_directory_page(fs_path, request_path, lovework_root)
        if html:
            return {"data": html.encode("utf-8"), "mime": "text/html; charset=utf-8"}
        return None

    if fs_path.suffix == ".md" and fs_path.exists():
        if query == "raw":
            try:
                data = fs_path.read_bytes()
                return {"data": data, "mime": "text/plain; charset=utf-8"}
            except Exception:
                return None
        html = _render_markdown_page(fs_path, request_path, lovework_root)
        if html:
            return {"data": html.encode("utf-8"), "mime": "text/html; charset=utf-8"}
        return None

    if fs_path.exists() and fs_path.is_file():
        try:
            data = fs_path.read_bytes()
            ctype, _ = mimetypes.guess_type(str(fs_path))
            if ctype is None:
                # Files with truly unknown extensions (e.g. no suffix) still
                # need a fallback so browsers render rather than download.
                ctype = "application/octet-stream"
            elif ctype == "text/html":
                # A real HTML page (e.g. docs/html storyboards) must render
                # as a page, not as monospaced text (LJ, 2026-10-06).
                ctype = "text/html; charset=utf-8"
            elif ctype.startswith("text/"):
                ctype = "text/plain; charset=utf-8"
            return {"data": data, "mime": ctype}
        except Exception:
            return None

    return None
