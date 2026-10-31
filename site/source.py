"""The source, as pyrtan.com shows it.

    python3 site/source.py OUT [--branch main] [--max-commits 200]

Writes a static browser of this checkout into OUT (the site's ``source/``):
the files at HEAD, a page per directory and per file, README and Markdown
rendered; the releases, one per version HEAD's own line of history has
had in ``pyproject.toml`` (the commit, often a merge, at which it first
said that version), each with the message of the commit that set the
number as its notes and a tarball of the tree at the release commit; the branches the checkout knows
(``refs/remotes/origin/*``), each with what it holds that HEAD does not;
and the history, a page per commit with its diff. Only git and the
standard library; the Pages workflow checks out with full history so the
branches and releases are all there. Commits show their author's name,
not the address.
"""

from __future__ import annotations

import argparse
import html
import posixpath
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
CLONE = "https://github.com/b-i-t-8/pyrtan.git"
MAX_BLOB = 1 << 20      # a file larger than this is listed, not shown
MAX_PATCH = 200_000     # a commit's diff is cut here

esc = html.escape


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, errors="replace", check=check).stdout


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


# -- pages ---------------------------------------------------------------

CSS = """
  :root { --bg:#fbfaf7; --fg:#1d1d1b; --mute:#6b6a64; --line:#e3e1da; --card:#ffffff; --pyr:#c2410c; --tan:#a8804f; --tan-fg:#fff; --soft:#f1efe9; --add:#e6f4ea; --del:#fbe9e7 }
  @media (prefers-color-scheme: dark) { :root:not([data-theme=light]) { --bg:#141513; --fg:#ecebe6; --mute:#9d9c95; --line:#2b2c29; --card:#1c1d1b; --pyr:#fb8a4c; --tan:#d2b48c; --tan-fg:#101318; --soft:#222420; --add:#16301f; --del:#3a1d19 } }
  :root[data-theme=dark] { --bg:#141513; --fg:#ecebe6; --mute:#9d9c95; --line:#2b2c29; --card:#1c1d1b; --pyr:#fb8a4c; --tan:#d2b48c; --tan-fg:#101318; --soft:#222420; --add:#16301f; --del:#3a1d19 }
  :root { --accent:var(--pyr) }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif }
  main { max-width:980px; margin:0 auto; padding:32px 16px 64px }
  header { display:flex; flex-wrap:wrap; gap:6px 18px; align-items:baseline; margin:0 0 20px; padding-bottom:12px; border-bottom:1px solid var(--line) }
  header b { font-size:18px } header nav { display:flex; flex-wrap:wrap; gap:4px 14px }
  header nav a.on { color:var(--fg); font-weight:600; text-decoration:none }
  h1 { font-size:22px; margin:0 0 6px; word-break:break-word } h2 { font-size:18px; margin:28px 0 8px }
  a { color:var(--accent) } .mute { color:var(--mute) } .small { font-size:14px }
  .pyr { color:var(--pyr) } .tan { color:var(--tan) }
  :focus-visible { outline:2px solid var(--tan); outline-offset:2px }
  ::selection { background:var(--tan); color:var(--tan-fg) }
  code, pre { font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace }
  code { background:var(--soft); padding:1px 5px; border-radius:4px }
  pre { padding:12px 14px; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:auto }
  .box { background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:auto }
  table { width:100%; border-collapse:collapse } td, th { text-align:left; padding:6px 10px; border-top:1px solid var(--line); vertical-align:top }
  .box tr:first-child td { border-top:0 } th { color:var(--mute); font-weight:600; font-size:14px }
  td.when { white-space:nowrap; color:var(--mute); font-size:14px; text-align:right }
  td.msg { color:var(--mute); font-size:14px; max-width:420px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
  table.code td { border:0; padding:0 10px; white-space:pre; font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace }
  table.code td.n { text-align:right; user-select:none; width:1%; color:var(--mute) } table.code td.n a { color:inherit; text-decoration:none }
  table.code tr:target { background:var(--soft) }
  .add { background:var(--add) } .del { background:var(--del) } .hunk { color:var(--mute) }
  .notes { white-space:pre-wrap } .crumbs { font-size:15px; margin:0 0 12px; word-break:break-word }
  .md { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:4px 22px 16px; margin-top:20px }
  .md table { margin:10px 0 } .md td, .md th { border:1px solid var(--line) } .md pre { background:var(--soft) }
  .release { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px 18px; margin:0 0 14px }
  .release h2 { margin:0 0 4px } .tag { display:inline-block; font-size:12px; padding:1px 8px; border-radius:10px; background:var(--soft); color:var(--mute); margin-left:6px }
  details summary { cursor:pointer }
  @media (max-width:640px) { td.msg { display:none } }
"""


class Site:
    def __init__(self, out: Path, branch: str, version: str):
        self.out, self.branch, self.version = out, branch, version

    def link(self, cur: str, target: str) -> str:
        """A relative href from page ``cur`` to page ``target`` (both under source/)."""
        rel = posixpath.relpath(target, posixpath.dirname(cur) or ".")
        return quote(rel, safe="/#.-_~")

    def write(self, page: str, title: str, body: str, on: str = "") -> None:
        nav = "".join(
            f'<a href="{self.link(page, href)}"{" class=on" if key == on else ""}>{label}</a>'
            for key, label, href in [("code", "Code", "index.html"), ("releases", "Releases", "releases.html"),
                                     ("branches", "Branches", "branches.html"), ("history", "History", "commits.html")])
        doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Pyrtan source</title>
<meta name="theme-color" content="#c2410c">
<script src="{self.link(page, '../theme.js')}"></script>
<style>{CSS}</style>
</head>
<body>
<main>
<header><b><a href="{self.link(page, '../index.html')}"><span class="pyr">pyr</span><span class="tan">tan</span></a> / <a href="{self.link(page, 'index.html')}">source</a></b><nav>{nav}</nav><span class="mute small">v{esc(self.version)} · {esc(self.branch)}</span></header>
{body}
</main>
</body>
</html>
"""
        path = self.out / page
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc, encoding="utf-8")


def blob_page(path: str) -> str:
    return f"blob/{path}.html"


def tree_page(path: str) -> str:
    return f"tree/{path}/index.html" if path else "index.html"


def commit_page(sha: str) -> str:
    return f"commit/{sha}.html"


# -- Markdown, enough for this repository's documents ---------------------

def outside(url: str) -> str | None:
    """A Markdown link that is not a path in the repository: the link as written
    for a fragment, http(s) or mailto; ``#`` for any other scheme (``javascript:``,
    ``data:``), which no page here makes a link of; None for a path."""
    if url.startswith("#") or re.match(r"(?i)(https?|mailto):", url):
        return url
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", url):
        return "#"
    return None


def md_inline(text: str, resolve) -> str:
    out, pos = [], 0
    for m in re.finditer(r"`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*", text):
        out.append(esc(text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(f"<code>{esc(m.group(1))}</code>")
        elif m.group(2) is not None:
            out.append(f'<a href="{esc(resolve(m.group(3)), quote=True)}">{md_inline(m.group(2), resolve)}</a>')
        else:
            out.append(f"<b>{md_inline(m.group(4), resolve)}</b>")
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


def markdown(src: str, resolve) -> str:
    lines, out, i = src.splitlines(), [], 0
    para: list[str] = []

    def flush():
        if para:
            out.append(f"<p>{md_inline(' '.join(para), resolve)}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append(f"<pre>{esc(chr(10).join(lines[i + 1:j]))}</pre>")
            i = j + 1
            continue
        if m := re.match(r"(#{1,6})\s+(.*)", line):
            flush()
            n = len(m.group(1))
            out.append(f"<h{n}>{md_inline(m.group(2), resolve)}</h{n}>")
        elif line.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            out.append("<table>" + "".join("<tr>" + "".join(f"<td>{md_inline(c, resolve)}</td>" for c in r) + "</tr>" for r in rows) + "</table>")
            continue
        elif m := re.match(r"\s*([-*]|\d+\.)\s+(.*)", line):
            flush()
            tag = "ol" if m.group(1)[0].isdigit() else "ul"
            items = []
            while i < len(lines) and (m := re.match(r"\s*([-*]|\d+\.)\s+(.*)", lines[i])):
                item = [m.group(2)]
                i += 1
                while i < len(lines) and lines[i].startswith("  ") and not re.match(r"\s*([-*]|\d+\.)\s+", lines[i]):
                    item.append(lines[i].strip())
                    i += 1
                items.append(f"<li>{md_inline(' '.join(item), resolve)}</li>")
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        elif not line.strip():
            flush()
        else:
            para.append(line.strip())
        i += 1
    flush()
    return "".join(out)


# -- what git knows --------------------------------------------------------

def files_at(ref: str) -> list[tuple[str, str, int]]:
    """(path, blob sha, size) for every file at ``ref``."""
    out = []
    for rec in git("ls-tree", "-r", "-l", "-z", ref).split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, kind, sha, size = meta.split()
        if kind == "blob":
            out.append((path, sha, int(size) if size.isdigit() else 0))
    return out


def last_commit(ref: str, path: str) -> tuple[str, str, str]:
    """(sha, subject, date) of the last commit at ``ref`` that touched ``path``."""
    rec = git("log", "-1", "--format=%H%x00%s%x00%as", ref, "--", path or ".").strip()
    return tuple(rec.split("\0")) if rec else ("", "", "")  # type: ignore[return-value]


def commits(*revs: str, limit: int, paths: tuple[str, ...] = ()) -> list[dict]:
    raw = git("log", f"-n{limit}", "--format=%H%x00%P%x00%an%x00%as%x00%s%x00%b%x1e", *revs, "--", *paths, check=False)
    out = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, author, date, subject, body = rec.split("\0", 5)
        out.append({"sha": sha, "parents": parents.split(), "author": author, "date": date, "subject": subject, "body": body.strip()})
    return out


def version_at(sha: str) -> str | None:
    text = git("show", f"{sha}:pyproject.toml", check=False)
    try:
        return tomllib.loads(text)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError):
        return None


def releases(ref: str) -> list[dict]:
    """Each version ``ref`` has had, once, newest first: the commit on its first-parent
    line (often a merge) where ``pyproject.toml`` first said it, with ``notes``, the
    subject and body of the commit that set the number, on the merged branch when it
    was set there. A branch's own commits are never releases by themselves: two
    branches that each bumped to the same number, or a number a merge superseded,
    would list a version twice or one ``ref`` never had."""
    shallow = set(_shallow())
    out, seen = [], set()
    for c in commits("--first-parent", ref, limit=1000, paths=("pyproject.toml",)):
        if c["sha"] in shallow:
            continue    # its parent is not in this checkout; the change is unknowable
        v = version_at(c["sha"])
        before = version_at(c["parents"][0]) if c["parents"] else None
        if not v or v == before:
            continue
        note = c
        if len(c["parents"]) > 1:
            # The newest commit on the merged branch that set this number, merges of main into it included.
            for b in commits(f"{c['parents'][0]}..{c['parents'][1]}", limit=1000, paths=("pyproject.toml",)):
                if b["sha"] not in shallow and version_at(b["sha"]) == v and (not b["parents"] or version_at(b["parents"][0]) != v):
                    note = b
                    break
        out.append({**c, "version": v, "notes": {"sha": note["sha"], "subject": note["subject"], "body": note["body"]}})
    # A version said twice on the line (set, changed, set again) is the oldest's: that is when it was released.
    kept = []
    for r in reversed(out):
        if r["version"] not in seen:
            seen.add(r["version"])
            kept.append(r)
    return kept[::-1]


def _shallow() -> list[str]:
    path = ROOT / git("rev-parse", "--git-path", "shallow").strip()
    return path.read_text().split() if path.is_file() else []


def branches(default: str) -> list[str]:
    """The remote branches, the default first."""
    default = f"origin/{default}"
    refs = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").split()
    return sorted((r for r in refs if r not in ("origin", "origin/HEAD")), key=lambda r: (r != default, r))


# -- building --------------------------------------------------------------

def notes(body: str, trailers: bool = True) -> str:
    """A commit message's body as HTML: wrapped paragraphs joined, lists, indented lines and
    trailers (``Co-Authored-By: ...``) kept line by line; ``trailers=False`` leaves trailers out."""
    out = []
    for para in re.split(r"\n\s*\n", body.strip()):
        lines = para.splitlines()
        if lines and all(re.match(r"[A-Za-z][\w-]*: \S", line) for line in lines):
            if trailers:
                out.append(f'<p class="notes mute small">{esc(para)}</p>')
        elif any(re.match(r"\s|[-*] |\d+\. ", line) for line in lines):
            out.append(f'<p class="notes">{esc(para)}</p>')
        else:
            out.append(f"<p>{esc(' '.join(line.strip() for line in lines))}</p>")
    return "".join(out)


def when(date: str) -> str:
    return f'<td class="when">{esc(date)}</td>'


def build(out: Path, branch: str, max_commits: int) -> int:
    version = version_at("HEAD") or "?"
    site = Site(out, branch, version)
    files = files_at("HEAD")
    head = git("rev-parse", "HEAD").strip()
    shown: dict[str, dict] = {}     # every commit that gets a page

    # history: HEAD, then what each branch holds that HEAD does not
    history = commits("HEAD", limit=max_commits)
    for c in history:
        shown[c["sha"]] = c
    branch_rows = []
    for ref in branches(branch):
        name = ref.removeprefix("origin/")
        behind, ahead = (int(n) for n in git("rev-list", "--left-right", "--count", f"HEAD...{ref}").split())
        tip = commits(ref, limit=1)[0]
        own = commits(f"HEAD..{ref}", limit=50)
        for c in [tip, *own]:
            shown.setdefault(c["sha"], c)
        branch_rows.append((name, tip, ahead, behind, own))
    rels = releases("HEAD")
    for c in rels:
        shown.setdefault(c["sha"], c)

    def commit_link(cur: str, c: dict, text: str | None = None) -> str:
        return f'<a href="{site.link(cur, commit_page(c["sha"]))}">{esc(text or c["subject"])}</a>'

    def resolver(cur: str, base_dir: str):
        known = {p for p, _, _ in files}
        dirs = {posixpath.dirname(p) for p in known}
        dirs |= {d for p in list(dirs) for d in _parents(p)}

        def resolve(url: str) -> str:
            elsewhere = outside(url)
            if elsewhere is not None:
                return elsewhere
            target, _, frag = url.partition("#")
            target = posixpath.normpath(posixpath.join(base_dir, target)) if target else ""
            target = "" if target == "." else target.rstrip("/")
            if target in known:
                return site.link(cur, blob_page(target)) + (f"#{frag}" if frag else "")
            if target in dirs or target == "":
                return site.link(cur, tree_page(target))
            return url
        return resolve

    # Code: a page per directory, a page per file
    tree: dict[str, list[tuple[str, bool]]] = {}
    for path, _, _ in files:
        parts = path.split("/")
        for k in range(len(parts)):
            parent = "/".join(parts[:k])
            entry = ("/".join(parts[:k + 1]), k < len(parts) - 1)
            tree.setdefault(parent, [])
            if entry not in tree[parent]:
                tree[parent].append(entry)
    sizes = {p: (sha, size) for p, sha, size in files}

    for d, entries in tree.items():
        page = tree_page(d)
        rows = []
        if d:
            up = posixpath.dirname(d)
            rows.append(f'<tr><td><a href="{site.link(page, tree_page(up))}">..</a></td><td></td><td></td></tr>')
        for path, is_dir in sorted(entries, key=lambda e: (not e[1], e[0].lower())):
            sha, subject, date = last_commit("HEAD", path)
            name = posixpath.basename(path) + ("/" if is_dir else "")
            href = site.link(page, tree_page(path) if is_dir else blob_page(path))
            msg = f'<a href="{site.link(page, commit_page(sha))}">{esc(subject)}</a>' if sha in shown else esc(subject)
            rows.append(f'<tr><td><a href="{href}">{esc(name)}</a></td><td class="msg">{msg}</td>{when(date)}</tr>')
        top = ""
        if not d:
            latest = rels[0] if rels else None
            top = (f'<p class="mute small">{len(files)} files · {len(history)} commits · {len(branch_rows)} branches'
                   + (f' · latest release <a href="releases.html">v{esc(latest["version"])}</a>' if latest else "")
                   + (f' · last change {commit_link(page, history[0], history[0]["date"])}' if history else "")
                   + f'</p><pre>git clone {esc(CLONE)}</pre>')
        crumbs = _crumbs(site, page, d)
        readme = next((p for p, is_dir in entries if not is_dir and posixpath.basename(p).lower() == "readme.md"), None)
        body = f'{crumbs}{top}<div class="box"><table>{"".join(rows)}</table></div>'
        if readme:
            text = git_bytes("cat-file", "blob", sizes[readme][0]).decode("utf-8", "replace")
            body += f'<div class="md">{markdown(text, resolver(page, d))}</div>'
        site.write(page, d or "Code", body, on="code")

    for path, sha, size in files:
        page = blob_page(path)
        c_sha, subject, date = last_commit("HEAD", path)
        meta = f'<p class="mute small">{size:,} bytes · last changed {date} in ' + (
            f'<a href="{site.link(page, commit_page(c_sha))}">{esc(subject)}</a>' if c_sha in shown else esc(subject)) + "</p>"
        if size > MAX_BLOB:
            content = '<p class="mute">Too large to show here; it is in the clone.</p>'
        else:
            data = git_bytes("cat-file", "blob", sha)
            if b"\0" in data[:8000]:
                content = '<p class="mute">A binary file; it is in the clone.</p>'
            else:
                text = data.decode("utf-8", "replace")
                if path.lower().endswith(".md"):
                    content = f'<div class="md">{markdown(text, resolver(page, posixpath.dirname(path)))}</div>'
                else:
                    rows = "".join(f'<tr id="L{n}"><td class="n"><a href="#L{n}">{n}</a></td><td>{esc(line)}</td></tr>'
                                   for n, line in enumerate(text.splitlines(), 1))
                    content = f'<div class="box"><table class="code">{rows}</table></div>'
        site.write(page, path, _crumbs(site, page, path) + meta + content, on="code")

    # Releases
    page = "releases.html"
    (out / "releases").mkdir(parents=True, exist_ok=True)
    blocks = []
    for k, r in enumerate(rels):
        name = f"pyrtan-{r['version']}"
        subprocess.run(["git", "archive", "--format=tar.gz", f"--prefix={name}/", "-o", str(out / "releases" / f"{name}.tar.gz"), r["sha"]],
                       cwd=ROOT, check=True)
        blocks.append(f'<div class="release" id="v{esc(r["version"])}"><h2>v{esc(r["version"])}{"<span class=tag>latest</span>" if k == 0 else ""}</h2>'
                      f'<p class="mute small">{esc(r["date"])} · {commit_link(page, r, r["sha"][:7])} · '
                      f'<a href="releases/{quote(name)}.tar.gz">{esc(name)}.tar.gz</a></p>'
                      f'<p><b>{esc(r["notes"]["subject"])}</b></p>{notes(r["notes"]["body"], trailers=False)}</div>')
    site.write(page, "Releases", "<h1>Releases</h1>" + ("".join(blocks) or '<p class="mute">None yet.</p>'), on="releases")

    # Branches
    page = "branches.html"
    rows = []
    for name, tip, ahead, behind, own in branch_rows:
        mark = '<span class="tag">default</span>' if name == branch else ""
        detail = ""
        if own:
            items = "".join(f'<tr><td>{commit_link(page, c)}</td><td class="mute small">{esc(c["author"])}</td>{when(c["date"])}</tr>' for c in own)
            detail = f'<details><summary class="small">{len(own)} commit{"s" if len(own) != 1 else ""} not on {esc(branch)}</summary><table>{items}</table></details>'
        rows.append(f'<tr><td><code>{esc(name)}</code>{mark}<div class="small">{commit_link(page, tip)}</div>{detail}</td>'
                    f'<td class="mute small">{ahead} ahead · {behind} behind</td>{when(tip["date"])}</tr>')
    site.write(page, "Branches", f'<h1>Branches</h1><p class="mute small">Ahead and behind are counted against {esc(branch)} at {commit_link(page, history[0], head[:7]) if history else esc(head[:7])}.</p>'
               f'<div class="box"><table>{"".join(rows) or "<tr><td class=mute>None in this checkout.</td></tr>"}</table></div>', on="branches")

    # History, and a page per commit
    page = "commits.html"
    rows = "".join(f'<tr><td>{commit_link(page, c)}<div class="mute small">{esc(c["author"])} · <code>{c["sha"][:7]}</code></div></td>{when(c["date"])}</tr>' for c in history)
    site.write(page, "History", f'<h1>History of {esc(branch)}</h1><div class="box"><table>{rows}</table></div>', on="history")

    for sha, c in shown.items():
        page = commit_page(sha)
        parents = " ".join(f'<a href="{site.link(page, commit_page(p))}">{p[:7]}</a>' if p in shown else f"<code>{p[:7]}</code>" for p in c["parents"])
        first = c["parents"][0] if c["parents"] else None
        stat = git("diff", "--stat=100", first, sha, check=False) if first else git("show", "--stat=100", "--format=", sha)
        patch = git("diff", first, sha, check=False) if first else git("show", "--format=", sha)
        cut = len(patch) > MAX_PATCH
        lines = []
        for line in patch[:MAX_PATCH].splitlines():
            cls = "hunk" if line.startswith("@@") else "add" if line.startswith("+") and not line.startswith("+++") else "del" if line.startswith("-") and not line.startswith("---") else ""
            lines.append(f'<div class="{cls}">{esc(line) or " "}</div>' if cls else esc(line) + "\n")
        body = (f'<h1>{esc(c["subject"])}</h1><p class="mute small">{esc(c["author"])} · {esc(c["date"])} · <code>{sha}</code>'
                + (f" · parents {parents}" if parents else "") + "</p>"
                + notes(c["body"])
                + f"<h2>Changes</h2><pre>{esc(stat)}</pre><pre>{''.join(lines)}</pre>"
                + ('<p class="mute">The diff is cut here; the rest is in the clone.</p>' if cut else ""))
        site.write(page, c["subject"], body, on="history")

    return len(files) + len(tree) + len(shown) + 3


def _parents(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:k]) for k in range(1, len(parts))]


def _crumbs(site: Site, page: str, path: str) -> str:
    parts = path.split("/") if path else []
    links = [f'<a href="{site.link(page, "index.html")}">pyrtan</a>']
    for k, part in enumerate(parts):
        sub = "/".join(parts[:k + 1])
        links.append(esc(part) if k == len(parts) - 1 else f'<a href="{site.link(page, tree_page(sub))}">{esc(part)}</a>')
    return f'<p class="crumbs">{" / ".join(links)}</p>'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, help="where to write the browser (the site's source/)")
    ap.add_argument("--branch", default="main", help="the name of the branch HEAD is (default main)")
    ap.add_argument("--max-commits", type=int, default=200, help="how much of HEAD's history to show")
    args = ap.parse_args(argv)
    n = build(args.out, args.branch, args.max_commits)
    print(f"wrote {args.out}: {n} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
