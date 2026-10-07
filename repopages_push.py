#!/usr/bin/env python3
"""RepoPages CI client (stdlib-only Python 3). Signs and POSTs Markdown to the RepoPages sync trigger.

Subcommands:
  push      Send the Markdown files changed by one commit (git diff REV~1..REV). Used on every push.
            `push PATH...` sends only the named files, read from REV even if the commit did not
            change them (unchanged pages are skipped unless --force).
  import    Full import: send every Markdown file in the tree at REV, in two passes:
              pass 1  all files (creates/updates pages; unchanged files are skipped by the app),
              pass 2  only files with a relative page link, so links to pages created in later
                      chunks of pass 1 resolve. The app rewrites a page only if a link target now
                      resolves differently, so re-running an import creates no new page versions.
  selftest  Offline checks: HMAC test vector, chunking, exclusion globs, link detection, diagram fences,
            code includes.

Code includes (--includes on, the default): FastAPI-style markers `{* ../../docs_src/app.py ln[1:9] *}`
are replaced by a fenced code block with that file's content from REV, because the app only sees
the Markdown. A file that is not found is logged as `WARN <file>: include not found: <path>` and the
marker is sent as is (the page shows a note). A change to an included file alone does not trigger
a push of the page; use `push --force PATH` or `import --force`.

Both push and import read file content from git at REV (default HEAD), not from the working tree,
split files into chunks (max 15 files and max 4 MB of UTF-8 JSON per call, images included), POST the chunks one
after another, retry once after 5 s on HTTP 429/5xx, and stop at the first other failure.

Diagrams (--render auto, the default): every ```mermaid and ```plantuml block is rendered and sent
as the file's `diagrams`; the app embeds the image and keeps the source in a "Diagram source"
expand. Format: --diagram-format svg (default; sharp at any zoom and in PDF export, labels as plain
SVG <text> so Word export keeps them), png (2x scale) or both (the app embeds the SVG). Background: --diagram-background (default transparent,
so the image works on light and dark Confluence themes; one image cannot suit both with an opaque
fill; a CSS colour name or #hex such as white makes it opaque). Palette: --diagram-theme
(default neutral: mid-grey arrows and outside text, opaque light boxes and edge labels, readable on
light and dark themes; light: the renderers' defaults; dark: Mermaid's dark theme). Renderers:
  Mermaid   `mmdc` on PATH, or MERMAID_CLI (a command, e.g. "npx -y @mermaid-js/mermaid-cli@11").
            When CHROME_BIN or PUPPETEER_EXECUTABLE_PATH is set, mmdc gets a puppeteer config with
            that browser and --no-sandbox (GitHub runners, the repopages-ci Docker image).
  PlantUML  PLANTUML_JAR (run with `java -jar`), or `plantuml` on PATH.
A missing renderer fails the run: when a file has a block whose renderer is not available, the
client prints `ERROR <file>:<line> needs a <lang> renderer ...` (the first such block, with the
tools it looks for) and exits with status 2 before anything is sent. --allow-missing-renderer
sends such blocks as code instead (logged once per language; the app keeps the image a page
already has for an unchanged block). A block that fails to render (or renders larger than 2 MiB)
is logged as `WARN <file>:<line> <lang> render failed: <reason>` and sent as code; a render
failure never fails the run. --render off sends every block as code (no renderer needed).

Environment (flags win):
  REPOPAGES_URL      sync web trigger URL                       (--url)
  REPOPAGES_SECRET   HMAC secret of the repo mapping            (--secret-file)
  DOCS_PREFIX        mapping path prefix, "" = whole repo       (--prefix)
  REPOPAGES_EXCLUDE  comma-separated exclusion globs            (--exclude, repeatable)
  GITHUB_REPOSITORY, GITHUB_REF, GITHUB_SERVER_URL              (set by GitHub Actions)

Exclusion globs: a pattern without "/" matches the file name at any depth ("CLAUDE.md");
a pattern with "/" matches the repo path ("tools/**", "**/node_modules/**"). "*" and "?" stay
inside one path segment, "**" crosses segments.

Partial pushes: HTTP 207 means the app applied the call but some files failed. The remaining
chunks are still sent; at the end the client prints `ERROR <n> chunk(s) were only partially
applied; ...` and exits with 3, so the CI job fails. --allow-partial prints that as a WARN and
exits 0 instead. Which files failed: the RepoPages settings page (Manage > Last run) or the app
logs (`sync.failed_file` events).

Exit codes:
  0  every call returned 200 (or 207 with --allow-partial), or nothing to send
  1  a call failed with another status or a network error, after the retry (the run stops there)
  2  configuration or renderer error, nothing sent: missing URL, secret or repo, a diagram
     renderer missing (see --allow-missing-renderer), bad arguments
  3  every chunk was sent but at least one returned 207 partial (see --allow-partial)
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_FILES = 15
MAX_BYTES = 4 * 1024 * 1024
RETRY_WAIT_S = 5
TIMEOUT_S = 70
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MD_RE = re.compile(r"\.(md|markdown)$", re.I)
DIAGRAM_LANGS = ("mermaid", "plantuml")
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # per PNG and per SVG, enforced by the app too
DIAGRAM_FORMATS = ("svg", "png", "both")
RENDER_TIMEOUT_S = 90

# --diagram-theme palettes. One transparent PNG is shown on both Confluence themes, so "neutral"
# draws everything that sits on the bare background (arrows, edge and message text, stick-actor
# names, cardinalities) in mid-grey and gives every box and edge label an opaque light fill with
# dark text. Copy for reference: mermaid-config.json next to this file (selftest checks they match).
DIAGRAM_THEMES = ("neutral", "light", "dark")
GREY, INK, FILL, BORDER = "#8a8a8a", "#1f1f1f", "#ECECFF", "#9370DB"
MERMAID_CONFIG = {
    "neutral": {
        "theme": "base",
        "themeVariables": {
            "primaryColor": FILL, "primaryTextColor": INK, "primaryBorderColor": BORDER,
            "secondaryColor": "#fff4dd", "tertiaryColor": "#f4f4f8",
            "lineColor": GREY, "edgeLabelBackground": "#e9e9ee",
            "clusterBkg": "#f4f4f8", "clusterBorder": BORDER,
            "actorBkg": FILL, "actorBorder": BORDER, "actorTextColor": INK, "actorLineColor": GREY,
            "signalColor": GREY, "signalTextColor": GREY,
            "labelBoxBkgColor": FILL, "labelBoxBorderColor": BORDER, "labelTextColor": INK, "loopTextColor": GREY,
            "noteBkgColor": "#fff5ad", "noteBorderColor": "#aaaa33", "noteTextColor": INK,
            "activationBkgColor": "#f4f4f8", "activationBorderColor": GREY, "sequenceNumberColor": "#ffffff",
        },
        # textColor would also grey class-diagram labels inside boxes, so only these get it. With SVG text
        # labels (SVG_TEXT_LABELS) cardinalities are <text> (fill, not color) and edge labels sit on a
        # <rect> Mermaid draws at 50% opacity, which lets the line show through.
        "themeCSS": "text.actor.actor-man>tspan{fill:#8a8a8a;} .edgeTerminals .edgeLabel{color:#8a8a8a;} "
                    ".edgeTerminals text{fill:#8a8a8a;} .edgeLabel rect{opacity:1;}",
    },
    "light": None,  # Mermaid's default theme
    "dark": {"theme": "dark"},
}
# SVG labels as plain <text> instead of HTML <foreignObject>: Confluence's Word export rasterises
# SVG and drops foreignObject labels. mmdc 11 needs the top-level key; the flowchart one alone is ignored.
# Trade-off: no Markdown/HTML inside labels and no automatic wrapping of long labels.
SVG_TEXT_LABELS = {"htmlLabels": False, "flowchart": {"htmlLabels": False}}


def mermaid_config(theme: str, fmt: str):
    """The mmdc -c config for a theme and output format ("svg" adds SVG_TEXT_LABELS), or None."""
    base = MERMAID_CONFIG[theme]
    if fmt != "svg":
        return base
    cfg = dict(base or {})
    cfg["htmlLabels"] = False
    cfg["flowchart"] = {**cfg.get("flowchart", {}), "htmlLabels": False}
    return cfg

PLANTUML_SKINPARAMS = {
    "neutral": {
        # Text defaults to grey (message and arrow labels sit on the background); text in boxes is dark.
        "ArrowColor": GREY, "ArrowFontColor": GREY, "BorderColor": BORDER, "DefaultFontColor": GREY,
        "ParticipantBackgroundColor": FILL, "ParticipantFontColor": INK, "ActorBackgroundColor": FILL,
        "ActorBorderColor": BORDER, "DatabaseBackgroundColor": FILL, "SequenceLifeLineBorderColor": GREY,
        "NoteBackgroundColor": "#fff5ad", "NoteBorderColor": "#aaaa33", "NoteFontColor": INK,
        "SequenceGroupBorderColor": BORDER, "SequenceGroupBackgroundColor": FILL,
        "SequenceGroupHeaderFontColor": INK, "SequenceGroupFontColor": GREY,
        "ClassBackgroundColor": FILL, "ClassBorderColor": BORDER, "ClassFontColor": INK, "ClassAttributeFontColor": INK,
        "PackageFontColor": GREY, "PackageBorderColor": BORDER,
    },
    "light": {},
    "dark": {
        "ArrowColor": "#bbbbbb", "ArrowFontColor": "#bbbbbb", "BorderColor": "#bbbbbb", "DefaultFontColor": "#dddddd",
        "ParticipantBackgroundColor": "#2d2d35", "ActorBackgroundColor": "#2d2d35", "ActorBorderColor": "#bbbbbb",
        "DatabaseBackgroundColor": "#2d2d35", "SequenceLifeLineBorderColor": "#bbbbbb",
        "NoteBackgroundColor": "#4a4630", "NoteBorderColor": "#bbbb66", "NoteFontColor": "#dddddd",
        "SequenceGroupBackgroundColor": "#2d2d35", "SequenceGroupBorderColor": "#bbbbbb", "ClassBackgroundColor": "#2d2d35", "ClassFontColor": "#dddddd",
        "ClassAttributeFontColor": "#dddddd",
    },
}

# Shared HMAC test vector (same as test-vectors/push-v1.json here and in the RepoPages app repo).
VECTOR_SIGNATURE = "sha256=a3a520cba8adb092cb92b524a75fe5bb995d276f23234d3540d8fdcb6a63fe0f"


# ---------------------------------------------------------------- signing / HTTP

def sign(secret: str, body: bytes) -> str:
    """Same algorithm as src/payload.ts sign(): HMAC-SHA256 of the raw UTF-8 body, hex."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def encode(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def post(url: str, body: bytes, signature: str):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-RepoPages-Signature": signature})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def send(url: str, secret: str, payload: dict, label: str) -> int:
    """POST one chunk; one retry on 429/5xx. Returns the final HTTP status (0 = network error)."""
    body = encode(payload)
    for attempt in (1, 2):
        t0 = time.monotonic()
        try:
            status, text = post(url, body, sign(secret, body))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"{label}: {len(payload['files'])} files, {len(body)} bytes -> network error: {e}", flush=True)
            return 0
        took = time.monotonic() - t0
        print(f"{label}: {len(payload['files'])} files, {len(body)} bytes -> HTTP {status} {text.strip()} ({took:.1f} s)", flush=True)
        if attempt == 1 and (status == 429 or status >= 500):
            print(f"{label}: retrying once in {RETRY_WAIT_S} s", flush=True)
            time.sleep(RETRY_WAIT_S)
            continue
        return status
    return status


# ---------------------------------------------------------------- pure helpers

def split_patterns(values):
    out = []
    for v in values or []:
        out += [p.strip() for p in v.split(",") if p.strip()]
    return out


def glob_re(pattern: str):
    p = pattern.strip().lstrip("/")
    if p.endswith("/"):
        p += "**"
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            out, i = out + ".*", i + 2
        elif p[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif p[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(p[i]), i + 1
    return re.compile(out + r"\Z")


def is_excluded(path: str, patterns) -> bool:
    for pat in patterns:
        target = path if "/" in pat.strip().lstrip("/") else path.rsplit("/", 1)[-1]
        if glob_re(pat).match(target):
            return True
    return False


def wanted(path: str, prefix: str, excludes) -> bool:
    return path.startswith(prefix) and bool(MD_RE.search(path)) and not is_excluded(path, excludes)


# Inline links/images "](target ...)" and reference definitions "[id]: target".
LINK_RE = re.compile(r"\]\(\s*(<[^>]*>|[^)\s]+)[^)]*\)|^[ ]{0,3}\[[^\]]+\]:[ \t]*(<[^>]*>|\S+)", re.M)


def has_relative_md_link(markdown: str) -> bool:
    """True if the Markdown links to another page of the repo: a relative or site-absolute target
    that is a .md/.markdown/.html file, a folder ("guide/") or extensionless ("./features"), as the
    app's link resolver accepts them. Decides which files pass 2 re-sends."""
    for m in LINK_RE.finditer(markdown):
        target = (m.group(1) or m.group(2) or "").strip("<>").strip()
        if not target or target.startswith(("#", "//")) or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
            continue
        target = urllib.parse.unquote(target.split("#", 1)[0].split("?", 1)[0])
        last = target.rstrip("/").rsplit("/", 1)[-1]
        if MD_RE.search(target) or re.search(r"\.html?$", target, re.I) or target.endswith("/") or (last and "." not in last):
            return True
    return False


def say(msg: str):
    print(msg, flush=True)


def payload_size(envelope: dict, files) -> int:
    return len(encode({**envelope, "files": files}))


def strip_images(f: dict) -> dict:
    return {k: v for k, v in f.items() if k != "diagrams"}


def chunk(files, envelope: dict, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES, log=None):
    """Greedy split into lists of at most max_files files and max_bytes of encoded payload.

    Sizes are exact: base64 SVGs and PNGs are part of each file's encoded JSON. A file that does not fit next
    to others starts a new chunk (and so is sent alone if it is big). A file that does not fit even
    alone loses its images (sent as code, with a WARN); if it still does not fit, ValueError.
    """
    log = log or say
    base = payload_size(envelope, [])  # '{...,"files":[]}'
    chunks, cur, cur_bytes = [], [], base
    for f in files:
        size = len(encode(f))
        if base + size > max_bytes and f.get("diagrams"):
            log(f"WARN {f['path']}: {base + size} bytes with {len(f['diagrams'])} image(s) exceeds {max_bytes} "
                "even alone; sending its diagrams as code")
            f = strip_images(f)
            size = len(encode(f))
        if base + size > max_bytes:
            raise ValueError(f"{f['path']} alone exceeds {max_bytes} bytes of payload; exclude it or split the file")
        add = size + (1 if cur else 0)  # comma between files
        if cur and (len(cur) + 1 > max_files or cur_bytes + add > max_bytes):
            chunks.append(cur)
            cur, cur_bytes, add = [], base, size
        cur.append(f)
        cur_bytes += add
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- code includes

# FastAPI-style include marker: {* ../../docs_src/app.py ln[1:9,29] hl[3] title["main.py"] *}
INCLUDE_RE = re.compile(r"^(\s*)\{\*\s*(\S+)(.*?)\*\}\s*$")
INCLUDE_LANGS = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript", ".tsx": "tsx",
                 ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".sh": "bash", ".html": "html",
                 ".css": "css", ".md": "markdown", ".jinja": "jinja", ".j2": "jinja", ".sql": "sql", ".go": "go",
                 ".java": "java", ".rs": "rust", ".ini": "ini", ".cfg": "ini", ".xml": "xml"}


def line_ranges(spec: str, n: int):
    """'1:9,29,38:41' -> list of (start, end) 1-based inclusive ranges within 1..n."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition(":")
        try:
            lo, hi = int(a or 1), int(b) if b else (int(a) if not _ else n)
        except ValueError:
            continue
        lo, hi = max(1, lo), min(n, hi)
        if lo <= hi:
            out.append((lo, hi))
    return out


def expand_includes(markdown: str, path: str, read, exists, log=None):
    """Replace FastAPI include markers ({* file ln[..] *}) with a fenced code block of that file.

    The app cannot see other files of the repo, so the CI inlines them. A marker's path is tried
    relative to the Markdown file's directory and each of its parents (FastAPI writes them relative
    to the MkDocs folder), deepest first. `ln[1:9,29]` keeps those lines ("..." between gaps), the
    file name or `title["..."]` becomes the code block title, `hl[...]` is ignored. Markers inside
    fenced code are left alone; a file that is not found leaves the marker (the app shows a note).
    Returns (markdown, expanded count).
    """
    log = log or say
    lines, out, fence, n = markdown.split("\n"), [], None, 0
    parts = path.split("/")[:-1]
    dirs = ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]
    for line in lines:
        fm = FENCE_RE.match(line.lstrip(" "))
        if fence:
            if fm and fm.group(1)[0] == fence[0] and len(fm.group(1)) >= len(fence) and not fm.group(2).strip():
                fence = None
            out.append(line)
            continue
        if fm and not (fm.group(1)[0] == "`" and "`" in fm.group(2)):
            fence = fm.group(1)
            out.append(line)
            continue
        m = INCLUDE_RE.match(line)
        if not m:
            out.append(line)
            continue
        indent, rel, opts = m.group(1), m.group(2), m.group(3)
        target = None
        for d in dirs:
            cand = os.path.normpath(os.path.join(d, rel)).replace(os.sep, "/")
            if not cand.startswith("..") and exists(cand):
                target = cand
                break
        if not target:
            log(f"WARN {path}: include not found: {rel}")
            out.append(line)
            continue
        src = read(target).rstrip("\n").split("\n")
        lm = re.search(r"\bln\[([^\]]*)\]", opts)
        if lm:
            picked, last = [], 0
            for lo, hi in line_ranges(lm.group(1), len(src)):
                if picked and lo > last + 1:
                    picked.append("...")
                picked += src[lo - 1:hi]
                last = hi
            src = picked or src
        tm = re.search(r"\btitle\[\s*[\"']?([^\]\"']*)[\"']?\s*\]", opts)
        title = (tm.group(1) if tm else target.rsplit("/", 1)[-1]).replace('"', "'")
        lang = INCLUDE_LANGS.get(os.path.splitext(target)[1].lower(), "text")
        ticks = "`" * max(3, max((len(r) for r in re.findall(r"`+", "\n".join(src))), default=0) + 1)
        out.append(f'{indent}{ticks}{lang} title="{title}"')
        out += [indent + s if s else s for s in src]
        out.append(indent + ticks)
        n += 1
    return "\n".join(out), n


def add_includes(files, git, mode: str = "on"):
    """Inline include markers in every file's content (in place)."""
    if mode == "off":
        return
    tree = None
    total = 0
    for f in files:
        if "content" in f and "{*" in f["content"]:
            if tree is None:
                tree = set(git.tree_files())
            f["content"], n = expand_includes(f["content"], f["path"], git.read, tree.__contains__)
            total += n
    if total:
        print(f"includes: {total} code include(s) inlined", flush=True)


# ---------------------------------------------------------------- diagrams

FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
QUOTE_RE = re.compile(r"^ {0,3}> ?")
LIST_RE = re.compile(r"^( {0,3})([-+*]|\d{1,9}[.)])( {1,4}(?! )|[ ]*$)")
# MkDocs admonition / collapsible / content tab: the body is indented by 4 (the app dedents it).
INDENTED_BLOCK_RE = re.compile(r"^( *)(!!!|\?\?\?\+?|===\+?)[ \t]+\S")


def split_quote(line: str):
    """("> > " prefix, rest) for blockquote lines."""
    prefix, m = "", QUOTE_RE.match(line)
    while m:
        prefix += m.group(0)
        line = line[m.end():]
        m = QUOTE_RE.match(line)
    return prefix, line


def find_diagram_fences(markdown: str):
    """Fenced ```mermaid / ```plantuml blocks in document order: [{index, lang, line, source}].

    Mirrors how the app's Markdown parser (marked) delivers code block text: the lines between the
    fences, with up to the opening fence's indentation removed from each, joined with "\n". Handles
    blockquotes, list-item indentation and MkDocs `!!!` / `???` / `===` bodies (indented by 4); other fences (any language) are skipped as a whole, so a
    ```mermaid line inside a ```markdown block is not a diagram. `line` is 1-based in the file.
    The app also checks each block's hash, so a rare mismatch only means "sent as code".
    """
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = 0  # the app drops YAML front matter before parsing
    if lines and lines[0].lstrip("\ufeff") == "---":
        start = next((j + 1 for j in range(1, len(lines)) if lines[j] == "---"), 0)
    out, i, list_indent = [], start, 0
    while i < len(lines):
        quote, raw = split_quote(lines[i])
        lm = LIST_RE.match(raw)
        if lm:  # replace the list marker by spaces; content starts at the marker's width
            list_indent = lm.end() if lm.group(3).strip() == "" and lm.group(3) else len(lm.group(1)) + len(lm.group(2)) + 1
            raw = " " * lm.end() + raw[lm.end():]
        lead = len(raw) - len(raw.lstrip(" "))
        if raw.strip() and lead < list_indent and not lm:
            list_indent = 0  # dedented text ends the list
        am = None if lm else INDENTED_BLOCK_RE.match(raw)
        if am and lead - list_indent <= 3:  # "!!! note": its body works like a list item's, 4 deeper
            list_indent = lead + 4
            i += 1
            continue
        fm = FENCE_RE.match(raw[lead:])
        if not fm or lead - list_indent > 3 or (fm.group(1)[0] == "`" and "`" in fm.group(2)):
            i += 1
            continue
        fence, info = fm.group(1), fm.group(2).strip()
        lang = info.split()[0].lower() if info else ""
        close_re = re.compile(r"^ *" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$")
        content, j = [], i + 1
        while j < len(lines):
            q, ln = split_quote(lines[j])
            if quote and not q:
                break  # the blockquote ended, so did the fence
            if close_re.match(ln) and len(ln) - len(ln.lstrip(" ")) - list_indent <= 3:
                break
            k = 0
            while k < lead and k < len(ln) and ln[k] == " ":
                k += 1
            content.append(ln[k:])
            j += 1
        if lang in DIAGRAM_LANGS:
            out.append({"index": len(out), "lang": lang, "line": i + 1, "source": "\n".join(content)})
        i = j + 1
    return out


def png_size(data: bytes):
    """(width, height) from a PNG's IHDR chunk, or None if this is not a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


SVG_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.S)
SCRIPT_RE = re.compile(r"<script\b.*?</script\s*>|<script\b[^>]*/>", re.I | re.S)


def _svg_attr(tag: str, name: str):
    m = re.search(r'\s' + name + r'\s*=\s*(["\'])(.*?)\1', tag, re.S)
    return m.group(2) if m else None


SVG_DECLARED_SCALE = 2


def normalize_svg(data: bytes):
    """Prepare a rendered SVG for Confluence: (bytes, natural (width, height)), or raises RuntimeError.

    Leading BOM/whitespace and every <script> element are removed (<style> is kept). The natural
    size is the viewBox size rounded up (or the root's own px width/height when there is no
    viewBox, which is then added). The root declares 2x that size as integer width/height
    (Mermaid writes width="100%", PlantUML "118px") and keeps the viewBox, so the drawing scales:
    Confluence's Word and PDF exporters size and rasterise the image from the declared size, and
    2x keeps those copies sharp. The page shows the image at the natural width (the payload's
    `width`; the app reads it from the viewBox), so the declared size does not change the page.
    """
    try:
        text = data.decode("utf-8").lstrip("\ufeff \t\r\n")
    except UnicodeDecodeError:
        raise RuntimeError("SVG is not UTF-8")
    text = SCRIPT_RE.sub("", text)
    m = SVG_ROOT_RE.search(text)
    if not m:
        raise RuntimeError("renderer output is not an SVG")
    tag = m.group(0)
    w = h = None
    vb = _svg_attr(tag, "viewBox")
    nums = re.findall(r"-?[\d.]+(?:e-?\d+)?", vb or "")
    add_viewbox = len(nums) != 4
    if not add_viewbox:
        w, h = float(nums[2]), float(nums[3])
    else:
        aw, ah = (re.fullmatch(r"\s*([\d.]+)\s*(px)?\s*", _svg_attr(tag, k) or "") for k in ("width", "height"))
        if aw and ah:
            w, h = float(aw.group(1)), float(ah.group(1))
    if not w or not h or w <= 0 or h <= 0:
        raise RuntimeError("SVG has no usable viewBox or width/height")
    size = (max(1, int(-(-w // 1))), max(1, int(-(-h // 1))))  # ceil
    new = re.sub(r'\s(width|height|viewBox)\s*=\s*(["\']).*?\2' if add_viewbox else r'\s(width|height)\s*=\s*(["\']).*?\2', "", tag, flags=re.S)
    declared = f'<svg width="{size[0] * SVG_DECLARED_SCALE}" height="{size[1] * SVG_DECLARED_SCALE}"'
    if add_viewbox:
        declared += f' viewBox="0 0 {w:g} {h:g}"'
    new = declared + new[4:]
    text = text[:m.start()] + new + text[m.end():]
    out = text.encode("utf-8")
    check_svg(out)
    return out, size


def check_svg(data: bytes):
    """The app's SVG rules: starts with <svg or <?xml, no <script, at most MAX_IMAGE_BYTES."""
    if not (data.startswith(b"<svg") or data.startswith(b"<?xml")):
        raise RuntimeError("SVG must start with <svg or <?xml")
    if b"<script" in data.lower():
        raise RuntimeError("SVG contains <script")
    if len(data) > MAX_IMAGE_BYTES:
        raise RuntimeError(f"SVG is {len(data)} bytes, over the {MAX_IMAGE_BYTES} byte limit")


RENDERER_HINTS = {"mermaid": "mmdc / MERMAID_CLI", "plantuml": "PLANTUML_JAR + java / plantuml"}


class MissingRenderer(Exception):
    """A file has a diagram block whose renderer is not available (--render auto without --allow-missing-renderer)."""

    def __init__(self, lang: str, path: str, line: int):
        self.lang, self.path, self.line = lang, path, line
        super().__init__(
            f"ERROR {path}:{line} needs a {lang} renderer, none found ({RENDERER_HINTS[lang]}). Install it, or pass "
            f"--allow-missing-renderer to send such blocks as code (pages keep the images they already have for "
            f"unchanged blocks), or --render off. Nothing was sent.")


class Renderer:
    """Renders diagram sources to SVG and/or PNG with locally available tools. Results are cached by source hash."""

    def __init__(self, mode: str = "auto", env=None, log=None, background: str = "transparent", theme: str = "neutral",
                 fmt: str = "svg", allow_missing: bool = False):
        if theme not in DIAGRAM_THEMES:
            raise ValueError(f"unknown diagram theme {theme!r}")
        if fmt not in DIAGRAM_FORMATS:
            raise ValueError(f"unknown diagram format {fmt!r}")
        self.fmt = fmt
        self.formats = ("svg", "png") if fmt == "both" else (fmt,)
        self.env = os.environ if env is None else env
        self.background = background
        self.theme = theme
        self.log = log or say
        self.mode = mode
        self.allow_missing = allow_missing
        self.cache = {}
        self.missing_logged = set()
        self.tmp = None
        self.mermaid = self._mermaid_cmd() if mode == "auto" else None
        self.plantuml = self._plantuml_cmd() if mode == "auto" else None

    def _mermaid_cmd(self):
        cli = self.env.get("MERMAID_CLI")
        mmdc = shutil.which("mmdc", path=self.env.get("PATH"))
        cmd = shlex.split(cli) if cli else ([mmdc] if mmdc else None)
        if not cmd:
            return None
        browser = self.env.get("PUPPETEER_EXECUTABLE_PATH") or self.env.get("CHROME_BIN")
        if browser:
            cfg = os.path.join(self._tmpdir(), "puppeteer.json")
            with open(cfg, "w") as fh:
                json.dump({"executablePath": browser, "args": ["--no-sandbox"]}, fh)
            cmd += ["-p", cfg]
        return cmd

    def mermaid_cmd(self, fmt: str):
        """The mmdc command for one output format (each format has its own -c config file)."""
        config = mermaid_config(self.theme, fmt)
        if not config:
            return list(self.mermaid)
        cfg = os.path.join(self._tmpdir(), f"mermaid-config-{fmt}.json")
        if not os.path.isfile(cfg):
            with open(cfg, "w") as fh:
                json.dump(config, fh)
        return self.mermaid + ["-c", cfg]

    def _plantuml_cmd(self):
        jar = self.env.get("PLANTUML_JAR")
        if jar and os.path.isfile(jar):
            java = shutil.which("java", path=self.env.get("PATH"))
            return [java, "-Djava.awt.headless=true", "-jar", jar] if java else None
        exe = shutil.which("plantuml", path=self.env.get("PATH"))
        return [exe] if exe else None

    def _tmpdir(self):
        if not self.tmp:
            self.tmp = tempfile.mkdtemp(prefix="repopages-render-")
        return self.tmp

    def available(self, lang: str) -> bool:
        return bool(self.mermaid if lang == "mermaid" else self.plantuml)

    def render(self, lang: str, source: str, fmt: str = "png"):
        """Raw SVG or PNG bytes, or raises RuntimeError(reason)."""
        if lang == "mermaid":
            d = self._tmpdir()
            src, out = os.path.join(d, "in.mmd"), os.path.join(d, f"out.{fmt}")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(source + "\n")
            if os.path.exists(out):
                os.remove(out)
            scale = ["-s", "2"] if fmt == "png" else []
            r = subprocess.run(self.mermaid_cmd(fmt) + ["-q", "-i", src, "-o", out] + scale + ["-b", self.background],
                               capture_output=True, timeout=RENDER_TIMEOUT_S)
            if r.returncode != 0 or not os.path.isfile(out):
                raise RuntimeError(error_reason(r.stderr) or f"mmdc exit {r.returncode}")
            with open(out, "rb") as fh:
                return fh.read()
        text = source if "@start" in source else f"@startuml\n{source}\n@enduml"
        skin = [f"-S{k}={v}" for k, v in PLANTUML_SKINPARAMS[self.theme].items()]
        # SVG at the default 96 dpi: -Sdpi would scale the SVG's own size too.
        kind = ["-tsvg"] if fmt == "svg" else ["-tpng", "-Sdpi=192"]
        r = subprocess.run(self.plantuml + kind + ["-pipe", "-failfast2", f"-SbackgroundColor={self.background}"] + skin,
                           input=text.encode("utf-8"), capture_output=True, timeout=RENDER_TIMEOUT_S)
        if r.returncode != 0:
            raise RuntimeError((error_reason(r.stderr) or "") + f" (plantuml exit {r.returncode})")
        return r.stdout

    def check_available(self, files):
        """Raise MissingRenderer for the first block (file order) without a renderer, unless that is allowed."""
        if self.mode == "off" or self.allow_missing:
            return
        for f in files:
            if "content" in f:
                for b in find_diagram_fences(f["content"]):
                    if not self.available(b["lang"]):
                        raise MissingRenderer(b["lang"], f["path"], b["line"])

    def diagrams_for(self, path: str, markdown: str):
        """The `diagrams` list for one file (blocks that failed are left out). A block without a renderer
        raises MissingRenderer, or with allow_missing is left out (logged once per language)."""
        out = []
        for b in find_diagram_fences(markdown):
            lang, h = b["lang"], hashlib.sha256(b["source"].encode("utf-8")).hexdigest()
            if self.mode == "off":
                continue
            if not self.available(lang):
                if not self.allow_missing:
                    raise MissingRenderer(lang, path, b["line"])
                if lang not in self.missing_logged:
                    self.missing_logged.add(lang)
                    self.log(f"WARN {lang} renderer not found ({RENDERER_HINTS[lang]}), diagrams will be sent as code"
                             " (--allow-missing-renderer)")
                continue
            key = (lang, h)
            if key not in self.cache:
                try:
                    self.cache[key] = self.render_block(lang, b["source"])
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                    reason = "timed out" if isinstance(e, subprocess.TimeoutExpired) else str(e)
                    self.log(f"WARN {path}:{b['line']} {lang} render failed: {reason}")
                    self.cache[key] = None
            hit = self.cache[key]
            if hit:
                images, (w, hgt) = hit
                entry = {"index": b["index"], "lang": lang, "hash": h}
                for fmt in ("svg", "png"):
                    if fmt in images:
                        entry[fmt] = base64.b64encode(images[fmt]).decode("ascii")
                out.append({**entry, "width": w, "height": hgt})
        return out

    def render_block(self, lang: str, source: str):
        """({format: bytes}, (width, height)) for the configured formats; the size is the SVG's
        (CSS px) when an SVG is rendered, else the PNG's pixels (2x). Raises RuntimeError."""
        images, size = {}, None
        for fmt in self.formats:
            data = self.render(lang, source, fmt)
            if fmt == "svg":
                data, size = normalize_svg(data)
            else:
                psize = png_size(data)
                if not psize:
                    raise RuntimeError("renderer output is not a PNG")
                if len(data) > MAX_IMAGE_BYTES:
                    raise RuntimeError(f"PNG is {len(data)} bytes, over the {MAX_IMAGE_BYTES} byte limit")
                size = size or psize
            images[fmt] = data
        return images, size

    def close(self):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)


def error_reason(b: bytes) -> str:
    """The most useful line of a renderer's stderr: the first "...Error..." line, else the last line."""
    lines = [ln.strip() for ln in b.decode("utf-8", "replace").splitlines() if ln.strip()]
    hit = next((ln for ln in lines if "Error" in ln and not ln.startswith("at ")), None)
    return (hit or (lines[-1] if lines else ""))[:300]


def add_diagrams(files, renderer: Renderer):
    """Attach `diagrams` to the files (in place); returns the image count. Raises MissingRenderer before
    rendering anything when a block has no renderer (see Renderer.check_available)."""
    renderer.check_available(files)
    n = 0
    for f in files:
        if "content" in f:
            d = renderer.diagrams_for(f["path"], f["content"])
            if d:
                f["diagrams"] = d
                n += len(d)
    return n


def parse_name_status(z: str, prefix: str, excludes, read):
    """Parse `git diff --name-status -z -M` output into payload file entries."""
    parts, files, i = z.split("\0"), [], 0
    ok = lambda p: wanted(p, prefix, excludes)
    while i < len(parts) and parts[i]:
        status = parts[i][0]
        if status in "RC":
            old, new = parts[i + 1], parts[i + 2]
            i += 3
            if status == "R" and ok(new):
                files.append({"path": new, "from": old, "action": "renamed", "content": read(new)})
            elif status == "R" and ok(old):
                files.append({"path": old, "action": "removed"})
            elif status == "C" and ok(new):
                files.append({"path": new, "action": "added", "content": read(new)})
            continue
        path = parts[i + 1]
        i += 2
        if not ok(path):
            continue
        if status == "D":
            files.append({"path": path, "action": "removed"})
        elif status in "AMT":
            files.append({"path": path, "action": "added" if status == "A" else "modified", "content": read(path)})
    return files


# ---------------------------------------------------------------- git

class Git:
    def __init__(self, repo_dir: str, rev: str):
        self.dir = repo_dir
        self.sha = self.run("rev-parse", "--verify", rev + "^{commit}").strip()

    def run(self, *args) -> str:
        return subprocess.run(["git", "-C", self.dir, *args], check=True, capture_output=True).stdout.decode("utf-8", "replace")

    def read(self, path: str) -> str:
        return self.run("show", f"{self.sha}:{path}")

    def tree_files(self):
        return [p for p in self.run("ls-tree", "-r", "-z", "--name-only", self.sha).split("\0") if p]

    def parent(self) -> str:
        try:
            return self.run("rev-parse", "--verify", "--quiet", self.sha + "~1").strip()
        except subprocess.CalledProcessError:
            return EMPTY_TREE

    def commit_time(self) -> str:
        return self.run("show", "-s", "--format=%cI", self.sha).strip()

    def branch_ref(self) -> str:
        try:
            return self.run("symbolic-ref", "-q", "HEAD").strip()
        except subprocess.CalledProcessError:
            return "refs/heads/main"


# ---------------------------------------------------------------- commands

def config_error(msg: str):
    print(f"ERROR {msg}", file=sys.stderr, flush=True)
    sys.exit(2)


def setup(a):
    url = a.url or os.environ.get("REPOPAGES_URL")
    if a.secret_file:
        secret = Path(a.secret_file).expanduser().read_text("utf-8").strip()
    else:
        secret = os.environ.get("REPOPAGES_SECRET", "")
    if not a.dry_run and (not url or not secret):
        config_error("REPOPAGES_URL and REPOPAGES_SECRET (or --url / --secret-file) are required")
    if url:
        url = url.strip()
        if not url.startswith("https://"):
            config_error("REPOPAGES_URL must be the sync URL from the RepoPages settings page (it starts with https://); "
                         f"got a value of {len(url)} characters that does not")
    git = Git(a.repo_dir, a.rev)
    repo = a.repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        config_error("--repo (or GITHUB_REPOSITORY) is required")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    envelope = {
        "repo": repo,
        "ref": a.ref or os.environ.get("GITHUB_REF") or git.branch_ref(),
        "sha": git.sha,
        "shortSha": git.sha[:7],
        "commitUrl": f"{server}/{repo}/commit/{git.sha}",
        "pushedAt": git.commit_time(),
    }
    prefix = a.prefix if a.prefix is not None else os.environ.get("DOCS_PREFIX", "")
    excludes = split_patterns(a.exclude) or split_patterns([os.environ.get("REPOPAGES_EXCLUDE", "")])
    return url, secret, git, envelope, prefix, excludes


def run_pass(name, files, envelope, url, secret, dry_run, max_files=MAX_FILES, max_bytes=MAX_BYTES):
    """Send one pass, every chunk even after a 207. Returns (rc, partial): rc 0 sent, 1 failed (stopped);
    partial = how many chunks got HTTP 207."""
    try:
        chunks = chunk(files, envelope, max_files=max_files, max_bytes=max_bytes)
    except ValueError as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 1, 0
    print(f"{name}: {len(files)} file(s) in {len(chunks)} chunk(s)", flush=True)
    partial = 0
    for n, c in enumerate(chunks, 1):
        label = f"{name} chunk {n}/{len(chunks)}"
        if dry_run:
            print(f"{label}: {len(c)} files, {payload_size(envelope, c)} bytes (dry run): "
                  + ", ".join(f["path"] + (f" [{len(f['diagrams'])} image(s)]" if f.get("diagrams") else "") for f in c))
            continue
        status = send(url, secret, {**envelope, "files": c}, label)
        if status == 207:
            partial += 1
        elif not 200 <= status < 300:
            print(f"{label}: stopping. HTTP {status or 'network error'}; see the RepoPages settings page or `forge logs`.",
                  file=sys.stderr)
            return 1, partial
    return 0, partial


def partial_result(partial: int, allow_partial: bool, hint: str = "") -> int:
    """Exit code once every chunk was sent: 3 when some chunk got 207 (0 with --allow-partial)."""
    if not partial:
        return 0
    msg = (f"{partial} chunk(s) were only partially applied; see the RepoPages settings screen (Manage → Last run) "
           "or the app logs for the failed files")
    if allow_partial:
        print(f"WARN {msg} (--allow-partial){hint}", file=sys.stderr, flush=True)
        return 0
    print(f"ERROR {msg}{hint}", file=sys.stderr, flush=True)
    return 3


BACKGROUND_RE = re.compile(r"^(transparent|[A-Za-z]{3,30}|#[0-9A-Fa-f]{3}|#[0-9A-Fa-f]{6}|#[0-9A-Fa-f]{8})$")


def background_arg(v: str) -> str:
    if not BACKGROUND_RE.match(v):
        raise argparse.ArgumentTypeError("use 'transparent', a colour name (white) or #rgb / #rrggbb / #rrggbbaa")
    return v


def render_all(files, mode: str, background: str = "transparent", theme: str = "neutral", fmt: str = "svg",
               allow_missing: bool = False):
    """Render every diagram block of the files (in place). A block that fails to render is sent as code;
    raises MissingRenderer (before rendering) when a block has no renderer and that is not allowed."""
    r = Renderer(mode, background=background, theme=theme, fmt=fmt, allow_missing=allow_missing)
    try:
        t0 = time.monotonic()
        n = add_diagrams(files, r)
        if n or r.cache:
            failed = sum(1 for v in r.cache.values() if v is None)
            print(f"render: {n} diagram image(s) attached, {failed} source(s) failed ({time.monotonic() - t0:.1f} s)", flush=True)
    finally:
        r.close()


def render_or_report(files, a) -> bool:
    """render_all with the command's flags; False (error printed, nothing to send) when a renderer is missing."""
    try:
        render_all(files, a.render, a.diagram_background, a.diagram_theme, a.diagram_format, a.allow_missing_renderer)
        return True
    except MissingRenderer as e:
        print(str(e), file=sys.stderr, flush=True)
        return False


def cmd_push(a) -> int:
    url, secret, git, envelope, prefix, excludes = setup(a)
    z = git.run("diff", "--name-status", "-z", "-M", git.parent(), git.sha)
    files = parse_name_status(z, prefix, excludes, git.read)
    if a.paths:
        # Only the named files; ones this commit did not change are read from the tree at REV.
        files = [f for f in files if f["path"] in a.paths]
        have, tree = {f["path"] for f in files}, set(git.tree_files())
        for p in a.paths:
            if p not in have:
                if p in tree and wanted(p, prefix, excludes):
                    files.append({"path": p, "action": "modified", "content": git.read(p)})
                else:
                    print(f"WARN {p}: not a mapped Markdown file at {git.sha[:7]}; skipped", flush=True)
    if a.force:
        for f in files:
            if f["action"] != "removed":
                f["force"] = True
    if not files:
        print(f"No Markdown changes under '{prefix}' in {git.sha[:7]}")
        return 0
    add_includes(files, git, a.includes)
    if not render_or_report(files, a):
        return 2
    rc, partial = run_pass("push", files, envelope, url, secret, a.dry_run, a.max_files, a.max_bytes)
    return rc or partial_result(partial, a.allow_partial)


def strip_force(f: dict) -> dict:
    return {k: v for k, v in f.items() if k != "force"}


def cmd_import(a) -> int:
    url, secret, git, envelope, prefix, excludes = setup(a)
    envelope["mode"] = "import"
    paths = sorted(p for p in git.tree_files() if wanted(p, prefix, excludes))
    print(f"import {envelope['repo']}@{git.sha[:7]}: {len(paths)} Markdown file(s) under '{prefix}'"
          + (f", excluding {excludes}" if excludes else ""), flush=True)
    files = []
    for p in paths:
        f = {"path": p, "action": "modified", "content": git.read(p)}
        if a.force:
            f["force"] = True
        files.append(f)
    add_includes(files, git, a.includes)
    if not render_or_report(files, a):
        return 2
    rc1, partial1 = run_pass("pass 1", files, envelope, url, secret, a.dry_run, a.max_files, a.max_bytes)
    if rc1:
        return rc1
    # Pass 2 re-sends the same diagrams: the app's unchanged check includes them.
    linked = [strip_force(f) for f in files if has_relative_md_link(f["content"])]
    rc2, partial2 = (run_pass("pass 2 (links)", linked, envelope, url, secret, a.dry_run, a.max_files, a.max_bytes)
                     if linked and not a.no_links_pass else (0, 0))
    if rc2:
        return rc2
    return partial_result(partial1 + partial2, a.allow_partial, ". Re-run the import; unchanged files are skipped.")


def cmd_selftest(_a) -> int:
    # 1. HMAC vector (embedded, and the shared file when run inside the RepoPages repo).
    here = Path(__file__).resolve()
    candidates = [here.parent / "test-vectors" / "push-v1.json", here.parent.parent.parent / "test-vectors" / "push-v1.json"]
    vec_path = next((c for c in candidates if c.is_file()), candidates[0])
    if vec_path.is_file():
        v = json.loads(vec_path.read_text("utf-8"))
        assert sign(v["secret"], v["body"].encode("utf-8")) == v["signature"], "test vector signature mismatch"
        assert v["signature"] == VECTOR_SIGNATURE, "embedded vector signature is stale"
        print(f"vector ok ({vec_path})")
    else:
        print("vector file not found; checked embedded signature only")
    secret = "repopages-test-vector-secret-0123456789abcdef"
    payload = {"repo": "owner/name", "ref": "refs/heads/main", "sha": "0123456789abcdef0123456789abcdef01234567",
               "shortSha": "0123456", "commitUrl": "https://github.com/owner/name/commit/0123456789abcdef0123456789abcdef01234567",
               "pushedAt": "2026-10-06T10:00:00+00:00", "mode": "import",
               "files": [{"path": "docs/intro.md", "action": "added", "content": "# Intro — Grüße ✓\n\nSee [setup](setup.md).\n", "force": True},
                         {"path": "docs/gone.md", "action": "removed"}]}
    assert sign(secret, encode(payload)) == VECTOR_SIGNATURE, "embedded vector: encode()+sign() mismatch"

    # 2. Chunking by count and by bytes.
    env = {"repo": "o/r", "files": []}
    small = [{"path": f"f{i}.md", "action": "modified", "content": "x"} for i in range(40)]
    assert [len(c) for c in chunk(small, env)] == [15, 15, 10]
    big = [{"path": f"b{i}.md", "action": "modified", "content": "é" * 2000} for i in range(5)]  # ~4 KB each in UTF-8
    sizes = [len(c) for c in chunk(big, env, max_bytes=10_000)]
    assert sizes == [2, 2, 1], sizes
    assert all(payload_size(env, c) <= 10_000 for c in chunk(big, env, max_bytes=10_000))
    try:
        chunk([{"path": "huge.md", "action": "modified", "content": "x" * 20_000}], env, max_bytes=10_000)
        raise AssertionError("oversize file not rejected")
    except ValueError:
        pass
    assert chunk([], env) == []

    # 3. Exclusion globs.
    ex = split_patterns(["CLAUDE.md,tools/**", "**/node_modules/**"])
    assert ex == ["CLAUDE.md", "tools/**", "**/node_modules/**"]
    for p in ["CLAUDE.md", "AI/CLAUDE.md", "tools/x.md", "tools/a/b.md", "node_modules/x.md", "a/node_modules/b/c.md"]:
        assert is_excluded(p, ex), p
    for p in ["AI/claude-notes.md", "AI/CLAUDE.md.bak", "xtools/a.md", "docs/tools/a.md", "README.md"]:
        assert not is_excluded(p, ex), p
    assert is_excluded("docs/draft-1.md", ["draft-*.md"]) and not is_excluded("docs/draft.md", ["draft-?.md"])
    assert is_excluded("Dev/x.md", ["Dev/"]) and not is_excluded("Dev/x.md", ["Dev/*.txt"])
    assert wanted("docs/a.md", "docs/", []) and not wanted("a.md", "docs/", []) and not wanted("docs/a.png", "docs/", [])

    # 4. Relative page link detection (decides pass 2).
    yes = ["[a](b.md)", "see [a](../x/b.md#h) ok", "[a](<b c.md>)", "[a](b%20c.md)", "[a](./b.markdown \"t\")",
           "[a]: ../b.md", "  [ref]: <b.md>", "[a](./features#hmr)", "[a](/guide/)", "[a](api.html#x)", "[a](./)"]
    no = ["[a](https://e.com/b.md)", "[a](#b)", "[a](b.png)", "[a](//e.com/b.md)", "no links", "b.md alone", "[a](mailto:x@y.md)",
          "![i](img/x.svg)"]
    for s in yes:
        assert has_relative_md_link(s), s
    for s in no:
        assert not has_relative_md_link(s), s

    # 5. git diff --name-status -z parsing.
    z = "M\0docs/a.md\0A\0docs/new.md\0D\0docs/old.md\0R100\0docs/r1.md\0docs/r2.md\0R090\0docs/x.md\0other/x.md\0M\0docs/a.png\0M\0docs/CLAUDE.md\0"
    got = parse_name_status(z, "docs/", ["CLAUDE.md"], lambda p: "c:" + p)
    assert got == [
        {"path": "docs/a.md", "action": "modified", "content": "c:docs/a.md"},
        {"path": "docs/new.md", "action": "added", "content": "c:docs/new.md"},
        {"path": "docs/old.md", "action": "removed"},
        {"path": "docs/r2.md", "from": "docs/r1.md", "action": "renamed", "content": "c:docs/r2.md"},
        {"path": "docs/x.md", "action": "removed"},
    ], got

    # 6. Diagram fences: ordinal, language, opening line and source hash, as the app sees them.
    vec2 = next((c.with_name("diagrams-v1.json") for c in candidates if c.with_name("diagrams-v1.json").is_file()), candidates[0].with_name("diagrams-v1.json"))
    if vec2.is_file():
        dv = json.loads(vec2.read_text("utf-8"))
        got = [{"index": b["index"], "lang": b["lang"], "line": b["line"],
                "hash": hashlib.sha256(b["source"].encode("utf-8")).hexdigest()} for b in find_diagram_fences(dv["markdown"])]
        assert got == dv["blocks"], got
        print(f"diagram vector ok ({vec2})")
    md = "# T\n\n```mermaid\ngraph LR\n  A-->B\n```\n\n````md\n```mermaid\nno\n```\n````\n\n- x\n\n  ~~~PlantUML\n  a -> b\n  ~~~\n"
    fences = find_diagram_fences(md)
    assert [(b["index"], b["lang"], b["line"], b["source"]) for b in fences] == [
        (0, "mermaid", 3, "graph LR\n  A-->B"), (1, "plantuml", 16, "a -> b")], fences
    assert find_diagram_fences("---\nx: 1\n---\n```mermaid\nA\n```")[0]["line"] == 4

    # 6b. Code includes.
    repo = {"docs_src/app.py": "a = 1\nb = 2\nc = 3\nd = 4\n", "docs/en/docs/x/data.json": '{"k": "```"}\n'}
    md = ("# T\n\n{* ../../docs_src/app.py hl[2] *}\n\n- item\n\n    {* ../../docs_src/app.py ln[1,3:4] title[\"main.py\"] *}\n\n"
          "{* data.json *}\n\n```md\n{* ../../docs_src/app.py *}\n```\n\n{* missing.py *}\n")
    logs = []
    got, n = expand_includes(md, "docs/en/docs/x/page.md", repo.__getitem__, repo.__contains__, logs.append)
    assert n == 3, got
    assert '```python title="app.py"\na = 1\nb = 2\nc = 3\nd = 4\n```' in got, got
    assert '    ```python title="main.py"\n    a = 1\n    ...\n    c = 3\n    d = 4\n    ```' in got, got
    assert '````json title="data.json"\n{"k": "```"}\n````' in got, got
    assert "```md\n{* ../../docs_src/app.py *}\n```" in got and "{* missing.py *}" in got, got
    assert logs == ["WARN docs/en/docs/x/page.md: include not found: missing.py"], logs
    assert line_ranges("1:9,29,38:", 40) == [(1, 9), (29, 29), (38, 40)]

    # 7. PNG header parsing.
    tiny = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    assert png_size(tiny) == (1, 1) and png_size(b"GIF89a" + b"0" * 30) is None

    # 8. Renderer missing / off / failing / working (no real renderer needed).
    logs = []
    two = "```mermaid\nA\n```\n\n```mermaid\nB\n```\n\n```plantuml\nC\n```\n"
    # Missing (default): an error naming the first block that needs it, raised before anything renders.
    r = Renderer("auto", env={"PATH": ""}, log=logs.append)
    try:
        r.diagrams_for("x.md", two)
        raise AssertionError("missing renderer accepted")
    except MissingRenderer as e:
        assert (e.lang, e.path, e.line) == ("mermaid", "x.md", 1), (e.lang, e.path, e.line)
        assert "mmdc / MERMAID_CLI" in str(e) and "--allow-missing-renderer" in str(e), str(e)
    plant_only = [{"path": "a.md", "content": "no diagrams"}, {"path": "b.md", "content": "x\n\n```plantuml\nC\n```\n"}]
    try:
        add_diagrams(plant_only, Renderer("auto", env={"PATH": ""}, log=logs.append))
        raise AssertionError("missing renderer accepted")
    except MissingRenderer as e:
        assert (e.lang, e.path, e.line) == ("plantuml", "b.md", 3) and "PLANTUML_JAR + java / plantuml" in str(e), str(e)
    assert "diagrams" not in plant_only[1] and logs == [], logs
    # --allow-missing-renderer: WARN once per language, blocks sent as code.
    r = Renderer("auto", env={"PATH": ""}, log=logs.append, allow_missing=True)
    assert r.diagrams_for("x.md", two) == [] and r.diagrams_for("y.md", two) == []
    assert logs == ["WARN mermaid renderer not found (mmdc / MERMAID_CLI), diagrams will be sent as code (--allow-missing-renderer)",
                    "WARN plantuml renderer not found (PLANTUML_JAR + java / plantuml), diagrams will be sent as code (--allow-missing-renderer)"], logs
    logs.clear()
    assert add_diagrams([{"path": "x.md", "content": two}], Renderer("auto", env={"PATH": ""}, log=logs.append, allow_missing=True)) == 0
    logs.clear()
    # The flag parses for both commands and defaults to off.
    assert build_parser().parse_args(["push"]).allow_missing_renderer is False
    assert build_parser().parse_args(["import", "--allow-missing-renderer"]).allow_missing_renderer is True
    assert Renderer("off", env={"PATH": ""}, log=logs.append).diagrams_for("x.md", two) == [] and logs == []

    raw_svg = (b'<svg id="m" width="100%" xmlns="http://www.w3.org/2000/svg" style="max-width: 520.5px;" '
               b'viewBox="0 0 520.515625 113.2"><style>#m{fill:#333}</style><script>alert(1)</script>'
               b'<text x="1" y="1">A</text></svg>')

    class Fake(Renderer):
        def available(self, lang):
            return True

        def render(self, lang, source, fmt="png"):
            if source == "B":
                raise RuntimeError("Parse error on line 1")
            return raw_svg if fmt == "svg" else tiny

    for fmt, keys, width in (("svg", {"svg"}, 521), ("png", {"png"}, 1), ("both", {"svg", "png"}, 521)):
        logs.clear()
        fake = Fake("auto", env={"PATH": ""}, log=logs.append, fmt=fmt)
        d = fake.diagrams_for("docs/x.md", two)
        assert [(x["index"], x["lang"], x["width"], x["hash"]) for x in d] == [
            (0, "mermaid", width, hashlib.sha256(b"A").hexdigest()), (2, "plantuml", width, hashlib.sha256(b"C").hexdigest())], d
        assert logs == ["WARN docs/x.md:5 mermaid render failed: Parse error on line 1"], logs
        assert {k for k in ("svg", "png") if k in d[0]} == keys, d[0].keys()
        if "png" in keys:
            assert d[0]["png"] == base64.b64encode(tiny).decode()
    sent = base64.b64decode(d[0]["svg"])
    # Declared 2x, viewBox kept; the payload carries the natural size (the page's display width).
    assert sent.startswith(b'<svg width="1042" height="228" id="m" xmlns=') and b'viewBox="0 0 520.515625 113.2"' in sent, sent
    assert b"<script" not in sent and b"<style>" in sent, sent
    assert d[0]["width"] == 521 and d[0]["height"] == 114

    # 8b. SVG root rewrite and validation (the app enforces the same rules).
    pu = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" style="width:118px;height:131px;" width="118px" height="131px" viewBox="0 0 118 131"><text>x</text></svg>'
    out, size = normalize_svg(pu)
    assert size == (118, 131) and b'<svg width="236" height="262" xmlns=' in out and b'viewBox="0 0 118 131"' in out and out.startswith(b"<?xml"), out
    # No viewBox: one is added from the px size, so the 2x declared size scales the drawing.
    out, size = normalize_svg(b'\xef\xbb\xbf  <svg width="40px" height="20.5"><g/></svg>')
    assert size == (40, 21) and out == b'<svg width="80" height="42" viewBox="0 0 40 20.5"><g/></svg>', out
    out, size = normalize_svg(b'<svg viewBox="0,0,10.2,3"><SCRIPT type="x">a</SCRIPT><script href="x"/></svg>')
    assert size == (11, 3) and out == b'<svg width="22" height="6" viewBox="0,0,10.2,3"></svg>', out
    for bad in (b"<html/>", b'<svg width="100%"><g/></svg>', b"\xff\xfe<svg"):
        try:
            normalize_svg(bad)
            raise AssertionError(f"accepted {bad!r}")
        except RuntimeError:
            pass
    for bad in (b"<!-- x --><svg/>", b"<svg><scRipt>x</svg>", b"<svg>" + b"x" * MAX_IMAGE_BYTES + b"</svg>"):
        try:
            check_svg(bad)
            raise AssertionError(f"accepted {bad[:40]!r}")
        except RuntimeError:
            pass
    for ok in ("transparent", "white", "#fff", "#1e1e1e", "#ffffff80"):
        assert background_arg(ok) == ok
    for bad in ("", "#ff", "red;rm", "url(x)", "#12345"):
        try:
            background_arg(bad)
            raise AssertionError(f"accepted {bad!r}")
        except argparse.ArgumentTypeError:
            pass
    assert Renderer("auto", env={"PATH": ""}).background == "transparent"
    assert Renderer("auto", env={"PATH": ""}).theme == "neutral"
    try:
        Renderer("auto", env={"PATH": ""}, theme="sepia")
        raise AssertionError("accepted theme 'sepia'")
    except ValueError:
        pass
    assert set(MERMAID_CONFIG) == set(PLANTUML_SKINPARAMS) == set(DIAGRAM_THEMES)
    for t in DIAGRAM_THEMES:  # the -c config file per theme and format, and the -S flags per theme
        fake_mmdc = Renderer("auto", env={"PATH": "", "MERMAID_CLI": "mmdc"}, theme=t)
        assert fake_mmdc.mermaid == ["mmdc"] and fake_mmdc.formats == ("svg",)
        cmd = fake_mmdc.mermaid_cmd("png")
        if MERMAID_CONFIG[t]:
            assert cmd[-2] == "-c" and json.loads(Path(cmd[-1]).read_text()) == MERMAID_CONFIG[t], cmd
        else:
            assert cmd == ["mmdc"], cmd
        cmd = fake_mmdc.mermaid_cmd("svg")
        cfg = json.loads(Path(cmd[-1]).read_text())
        # Text labels need the TOP-LEVEL htmlLabels (mmdc 11 ignores flowchart.htmlLabels alone).
        assert cmd[-2] == "-c" and cfg["htmlLabels"] is False and cfg["flowchart"]["htmlLabels"] is False, cfg
        assert {k: v for k, v in cfg.items() if k not in SVG_TEXT_LABELS} == {k: v for k, v in (MERMAID_CONFIG[t] or {}).items() if k != "flowchart"}
        fake_mmdc.close()
        assert all(re.match(r"^[A-Za-z]+$", k) and BACKGROUND_RE.match(v) for k, v in PLANTUML_SKINPARAMS[t].items())
    ref = Path(__file__).resolve().parent / "mermaid-config.json"
    if ref.is_file():
        assert json.loads(ref.read_text("utf-8")) == mermaid_config("neutral", "svg"), f"{ref} differs from mermaid_config('neutral', 'svg')"
        print(f"mermaid config ok ({ref})")
    ap = build_parser()
    assert ap.parse_args(["push"]).diagram_theme == "neutral"
    assert ap.parse_args(["push"]).diagram_format == "svg" and ap.parse_args(["import", "--diagram-format", "both"]).diagram_format == "both"
    try:
        Renderer("auto", env={"PATH": ""}, fmt="jpg")
        raise AssertionError("accepted format 'jpg'")
    except ValueError:
        pass
    assert ap.parse_args(["import", "--diagram-theme", "dark"]).diagram_theme == "dark"
    for bad in ("sepia", "Neutral", ""):
        try:
            with open(os.devnull, "w") as null:
                err, sys.stderr = sys.stderr, null
                try:
                    ap.parse_args(["push", "--diagram-theme", bad])
                finally:
                    sys.stderr = err
            raise AssertionError(f"accepted --diagram-theme {bad!r}")
        except SystemExit:
            pass
    assert error_reason(b"UnknownDiagramError: No diagram type\n\n    at x (y.js:1)\n") == "UnknownDiagramError: No diagram type"
    assert error_reason(b"ERROR\n1\nSyntax Error? (Assumed diagram type: sequence)\n") == "Syntax Error? (Assumed diagram type: sequence)"

    # 9. Byte accounting with images: exact sizes, big files alone, images dropped only as a last resort.
    img = lambda n: [{"index": 0, "lang": "mermaid", "hash": "0" * 64, "png": "A" * n, "width": 2, "height": 2}]
    fs = [{"path": f"i{i}.md", "action": "modified", "content": "x", "diagrams": img(3000)} for i in range(5)]
    fs.insert(2, {"path": "big.md", "action": "modified", "content": "y", "diagrams": img(9000)})
    cs = chunk(fs, env, max_bytes=10_000)
    assert [[f["path"] for f in c] for c in cs] == [["i0.md", "i1.md"], ["big.md"], ["i2.md", "i3.md", "i4.md"]], cs
    assert all(payload_size(env, c) <= 10_000 for c in cs)
    assert payload_size(env, cs[2]) > 9_000  # images are counted
    logs.clear()
    cs = chunk([{"path": "huge.md", "action": "modified", "content": "z", "diagrams": img(20_000)}], env, max_bytes=10_000, log=logs.append)
    assert cs == [[{"path": "huge.md", "action": "modified", "content": "z"}]] and logs[0].startswith("WARN huge.md:"), (cs, logs)
    # SVG bytes count the same way (and an svg+png pair counts both).
    svg_img = lambda n, png=0: [{"index": 0, "lang": "mermaid", "hash": "0" * 64, "svg": "P" * n, **({"png": "A" * png} if png else {}),
                                 "width": 2, "height": 2}]
    cs = chunk([{"path": f"s{i}.md", "action": "modified", "content": "x", "diagrams": svg_img(3000)} for i in range(4)], env, max_bytes=10_000)
    assert [len(c) for c in cs] == [3, 1] and payload_size(env, cs[0]) > 9_000, [len(c) for c in cs]
    cs = chunk([{"path": f"s{i}.md", "action": "modified", "content": "x", "diagrams": svg_img(2000, 2000)} for i in range(3)], env, max_bytes=10_000)
    assert [len(c) for c in cs] == [2, 1], [len(c) for c in cs]
    # 10. HTTP 207 partial: every chunk is still sent, then exit 3 (0 with --allow-partial).
    global post
    real_post, sent_chunks = post, []

    def fake_post(_url, body, _signature):
        files = json.loads(body)["files"]
        sent_chunks.append([f["path"] for f in files])
        if files[0]["path"] == "f15.md":
            return 207, '{"status":"partial"}'
        if files[0]["path"] == "f99.md":
            return 401, '{"status":"unauthorized"}'
        return 200, '{"status":"ok"}'

    post = fake_post
    try:
        with open(os.devnull, "w") as null:
            out, err, sys.stdout, sys.stderr = sys.stdout, sys.stderr, null, null
            try:
                rc, partial = run_pass("t", small, env, "https://x", "s", False)
                assert (rc, partial) == (0, 1) and [len(c) for c in sent_chunks] == [15, 15, 10], (rc, partial, sent_chunks)
                assert partial_result(0, False) == 0 and partial_result(partial, False) == 3 and partial_result(partial, True) == 0
                sent_chunks.clear()
                stop = [{"path": f"f{i}.md", "action": "modified", "content": "x"} for i in (1, 15, 99, 100)]
                assert run_pass("t", stop, env, "https://x", "s", False, max_files=1) == (1, 1) and len(sent_chunks) == 3, sent_chunks
            finally:
                sys.stdout, sys.stderr = out, err
    finally:
        post = real_post
    assert ap.parse_args(["push"]).allow_partial is False and ap.parse_args(["import", "--allow-partial"]).allow_partial is True
    print("selftest ok")
    return 0


EXIT_CODES = ("Exit codes: 0 ok; 1 a call failed (stopped); 2 configuration or renderer error, nothing sent; "
              "3 some chunk returned 207 partial (0 with --allow-partial).")


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXIT_CODES)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("push", "import"):
        p = sub.add_parser(name, epilog=EXIT_CODES)
        p.add_argument("--repo", help="owner/name (default: $GITHUB_REPOSITORY)")
        p.add_argument("--repo-dir", default=".", help="git checkout to read from (default: .)")
        p.add_argument("--rev", default="HEAD", help="commit to send (default: HEAD)")
        p.add_argument("--ref", help="default: $GITHUB_REF, else the checked-out branch")
        p.add_argument("--prefix", help="mapping path prefix (default: $DOCS_PREFIX or '')")
        p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                       help="exclusion glob, repeatable or comma-separated (default: $REPOPAGES_EXCLUDE)")
        p.add_argument("--url", help="sync trigger URL (default: $REPOPAGES_URL)")
        p.add_argument("--secret-file", help="read the secret from this file (default: $REPOPAGES_SECRET)")
        p.add_argument("--dry-run", action="store_true", help="print the chunks, send nothing")
        p.add_argument("--max-files", type=int, default=MAX_FILES, choices=range(1, MAX_FILES + 1), metavar=f"1..{MAX_FILES}",
                       help=f"files per call (default and maximum: {MAX_FILES})")
        p.add_argument("--max-bytes", type=int, default=MAX_BYTES, metavar="N",
                       help=f"encoded payload bytes per call, images included (default and maximum: {MAX_BYTES})")
        p.add_argument("--includes", choices=("on", "off"), default="on",
                       help="on: inline FastAPI-style {* file *} code includes from the checkout (default); off: send markers as is")
        p.add_argument("--render", choices=("auto", "off"), default="auto",
                       help="auto: render mermaid/plantuml blocks (default; a missing renderer is an error, exit 2, "
                            "nothing sent); off: send every block as code")
        p.add_argument("--allow-missing-renderer", action="store_true",
                       help="with --render auto: send blocks whose renderer is missing as code (WARN once) instead of "
                            "failing; pages keep the images they already have for unchanged blocks")
        p.add_argument("--allow-partial", action="store_true",
                       help="exit 0 (with a WARN) when some chunk got HTTP 207 partial; default: exit 3 after sending "
                            "every chunk, so the CI job fails")
        p.add_argument("--diagram-background", type=background_arg, default="transparent", metavar="COLOR",
                       help="image background: transparent (default), a colour name or #hex, e.g. white")
        p.add_argument("--diagram-format", choices=DIAGRAM_FORMATS, default="svg",
                       help="svg: SVG with text labels (default); png: PNG at 2x; both: send both, the page shows the SVG")
        p.add_argument("--diagram-theme", choices=DIAGRAM_THEMES, default="neutral",
                       help="neutral: grey lines, opaque light boxes, readable on light and dark pages (default); "
                            "light: renderer defaults (dark lines); dark: Mermaid dark theme")
        if name == "import":
            p.add_argument("--force", action="store_true", help="pass 1 rewrites every page even if unchanged")
            p.add_argument("--no-links-pass", action="store_true", help="skip pass 2")
        else:
            p.add_argument("--force", action="store_true", help="rewrite the pages even if unchanged")
            p.add_argument("paths", nargs="*", metavar="PATH",
                           help="send only these repo paths (also when the commit did not change them)")
    sub.add_parser("selftest")
    return ap


def main():
    ap = build_parser()
    a = ap.parse_args()
    if hasattr(a, "max_bytes") and not 1024 <= a.max_bytes <= MAX_BYTES:
        ap.error(f"--max-bytes must be between 1024 and {MAX_BYTES}")
    sys.exit({"push": cmd_push, "import": cmd_import, "selftest": cmd_selftest}[a.cmd](a))


if __name__ == "__main__":
    main()
