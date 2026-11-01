"""The admin About page: links, how to support Harkenn, and the credits, so people see them without visiting the
repositories. The credits are the repositories' own CREDITS.md files, copied into the image (Dockerfile) and turned
into HTML here; just the bits of Markdown they use (headings, paragraphs, lists, tables, links, bold, code)."""
from __future__ import annotations

import html
import re
from pathlib import Path

from markupsafe import Markup

ROOT = Path(__file__).resolve().parent.parent
SERVER_REPO = "https://github.com/harkenn-project/server-docker"
FIRMWARE_REPO = "https://github.com/harkenn-project/speaker-firmware"
SUPPORT_URL = "https://github.com/sponsors/harkenn-project"


def inline(text: str, base: str) -> str:
    """Escape, then links, bold and code. Relative links point into the repository."""
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", out)

    def link(m: re.Match) -> str:
        label, url = m.group(1), html.unescape(m.group(2))
        if not re.match(r"^(https?:|mailto:)", url):
            url = f"{base}/blob/main/{url}"
        return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{label}</a>'
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, out)


def markdown(text: str, base: str) -> Markup:
    """A small Markdown subset to HTML (the level-1 heading is left out: the page has its own)."""
    out: list[str] = []
    para: list[str] = []
    lines = text.splitlines()
    i = 0

    def flush() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para), base)}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            flush()
        elif line.startswith("#"):
            flush()
            level = len(line) - len(line.lstrip("#"))
            if level > 1:
                out.append(f"<h{min(level + 1, 4)}>{inline(line.lstrip('#').strip(), base)}</h{min(level + 1, 4)}>")
        elif line.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], [r for r in rows[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r)]
            out.append('<table class="table"><thead><tr>' + "".join(f"<th>{inline(c, base)}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join(
                           "<tr>" + "".join(f"<td>{inline(c, base)}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table>")
            continue
        elif re.match(r"^\s*[-*] ", line):
            flush()
            items: list[str] = []
            while i < len(lines) and (re.match(r"^\s*[-*] ", lines[i]) or (items and lines[i].startswith("  ")
                                                                            and lines[i].strip())):
                if re.match(r"^\s*[-*] ", lines[i]):
                    items.append(re.sub(r"^\s*[-*] ", "", lines[i]).strip())
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(t, base)}</li>" for t in items) + "</ul>")
            continue
        else:
            para.append(line.strip())
        i += 1
    flush()
    return Markup("\n".join(out))


def documents() -> dict[str, Markup | None]:
    """How support is shared and the server's and the speaker firmware's credits, as HTML (None if a file isn't
    there)."""
    out: dict[str, Markup | None] = {}
    for key, name, base in (("support", "SUPPORT.md", SERVER_REPO), ("server", "CREDITS.md", SERVER_REPO),
                            ("firmware", "FIRMWARE-CREDITS.md", FIRMWARE_REPO)):
        path = ROOT / name
        out[key] = markdown(path.read_text(encoding="utf-8"), base) if path.is_file() else None
    return out
