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
from pathlib import Path
from typing import Optional

import markdown


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


def render_markdown(text: str) -> str:
    _md.reset()
    return _md.convert(text)


def auto_link_urls(html_text: str) -> str:
    """Turn bare HTTP/HTTPS URLs into clickable links.

    Avoids re-linking URLs already inside href="..." or href='...'
    attributes. Also skips URLs inside existing <a> tags.
    """
    return re.sub(
        r'(?<!href=["\'])https?://[^\s<>")]+(?:"|\))?',
        lambda m: m.group(0) if (">" in m.group(0) or "</a" in m.group(0))
                    else f'<a href="{m.group(0).rstrip(").,")}">{m.group(0).rstrip(").,")}</a>',
        html_text,
    )


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
</style>
</head>
<body>
<div class="nav">
  <a href="/">🏠 Dashboard</a>
  <a href="/docs/00-index.md">📖 Docs</a>
  <a href="/MANUAL.md">📋 Manual</a>
  <a href="/README.md">ℹ️ About</a>
  <span class="path">{breadcrumb}</span>
</div>
<div class="container">
{content}
<div class="footer">
  Generated {date} · <a href="{raw_url}">View raw</a> · <a href="/">Back to dashboard</a>
</div>
</div>
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
  <a href="/">🏠 Dashboard</a>
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
    rel = fs_path.relative_to(lovework_root).as_posix()
    try:
        raw = fs_path.read_text(encoding="utf-8")
    except Exception:
        return None

    body = render_markdown(raw)
    body = rewrite_md_links(body, f"/{rel}")
    body = auto_link_urls(body)

    title_m = re.search(r"<h1>(.*?)</h1>", body)
    title = html_mod.unescape(title_m.group(1)) if title_m else fs_path.stem

    return PAGE_HTML.format(
        title=html_mod.escape(title),
        content=body,
        breadcrumb=make_breadcrumb(f"/{rel}"),
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        raw_url=f"/{rel}?raw",
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
        entries = sorted(
            (e for e in fs_path.iterdir() if not is_hidden(e)),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
    except PermissionError:
        entries = []

    rel_path = request_path or "/"
    if rel_path != "/":
        parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        items.append(f'<li><a href="/{parent}">📁 ..</a> <span>(parent)</span></li>')

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

def try_serve_path(path: str, query: str, lovework_root: Path) -> Optional[dict]:
    """Try to serve a path as a doc or directory listing.

    Returns a dict with "data" (bytes), "mime" (str), or None if the
    path is outside the doc-serving scope.
    """
    # Normalise: strip trailing slash to avoid double-slash in hrefs
    path = path.rstrip("/") or "/"
    rel = path.lstrip("/")
    fs_path = lovework_root / rel if rel else lovework_root

    if fs_path.is_dir():
        html = _render_directory_page(fs_path, path, lovework_root)
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
        html = _render_markdown_page(fs_path, path, lovework_root)
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
            elif ctype.startswith("text/"):
                ctype = "text/plain; charset=utf-8"
            return {"data": data, "mime": ctype}
        except Exception:
            return None

    return None
