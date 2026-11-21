"""Blast Radius selector: run only the tests that can see a change.

Offline (committed as map.json.gz) we recorded, from the pinned sqlglot commit,
which tests execute which source lines, which data files each test opens, and
which classes each test instantiates. At run time every patch is read against
that map; anything we cannot analyse safely falls back to running every test.
"""

import bisect
import gzip
import io
import json
import os
import re
import sys
import tokenize
import zipfile
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SOURCES = Path(os.environ.get("BR_SOURCES", str(Path(__file__).with_name("sources.zip"))))
MAP = Path(os.environ.get("BR_MAP", str(Path(__file__).with_name("map.json.gz"))))
MAX_WORKERS = 8
WORKER_COST_MS = 2000

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class Unsafe(Exception):
    """The patch is not something we can reason about: run everything."""


# ---------------------------------------------------------------- patch parsing
def parse_patch(text: str) -> list:
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if not m:
                raise Unsafe("diff header")
            cur = {"old": m.group(1), "new": m.group(2), "status": "modify", "hunks": [], "binary": False}
            files.append(cur)
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        if line.startswith("new file mode"):
            cur["status"] = "new"
        elif line.startswith("deleted file mode"):
            cur["status"] = "delete"
        elif line.startswith("rename from "):
            cur["status"] = "rename"
            cur["old"] = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur["new"] = line[len("rename to "):]
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur["binary"] = True
        elif line.startswith("old mode") or line.startswith("new mode"):
            cur["modeonly"] = True
        elif line.startswith("--- ") and cur is not None and not cur["hunks"]:
            pass
        elif line.startswith("+++ ") and not cur["hunks"]:
            pass
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if not m:
                raise Unsafe("hunk header")
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o_seen = n_seen = 0
            while i < n and (o_seen < old_len or n_seen < new_len or (i < n and lines[i].startswith("\\"))):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1]
                if tag == "+":
                    n_seen += 1
                elif tag == "-":
                    o_seen += 1
                elif tag == " " or l == "":
                    o_seen += 1
                    n_seen += 1
                    if l == "":
                        l = " "
                else:
                    raise Unsafe("hunk body")
                body.append((tag if l else " ", l[1:]))
                i += 1
            cur["hunks"].append({"old_start": old_start, "new_start": new_start, "old_len": old_len, "new_len": new_len, "body": body})
            continue
        i += 1
    return files


def apply_hunks(old_lines: list, hunks: list) -> list:
    """Return the new file's lines, checking every context/removed line."""
    out = []
    pos = 0  # index into old_lines
    for h in hunks:
        start = h["old_start"] - 1 if h["old_len"] else h["old_start"]
        if start < pos:
            raise Unsafe("overlapping hunks")
        out.extend(old_lines[pos:start])
        pos = start
        for tag, text in h["body"]:
            if tag == " ":
                if pos >= len(old_lines) or old_lines[pos] != text:
                    raise Unsafe("context mismatch")
                out.append(text)
                pos += 1
            elif tag == "-":
                if pos >= len(old_lines) or old_lines[pos] != text:
                    raise Unsafe("removed mismatch")
                pos += 1
            else:
                out.append(text)
    out.extend(old_lines[pos:])
    return out


def change_groups(hunks: list) -> list:
    """Runs of +/- lines: (position_before, [(old_line, text)...], [added text...])."""
    groups = []
    for h in hunks:
        old_ln = h["old_start"] if h["old_len"] else h["old_start"] + 1
        removed, added, before = [], [], None
        for tag, text in h["body"]:
            if tag == " ":
                if removed or added:
                    groups.append((before, removed, added))
                    removed, added = [], []
                old_ln += 1
                continue
            if not removed and not added:
                before = old_ln - 1
            if tag == "-":
                removed.append((old_ln, text))
                old_ln += 1
            else:
                added.append(text)
        if removed or added:
            groups.append((before, removed, added))
    return groups


def real_lines(src: str) -> set:
    skip = (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER)
    lines = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in skip:
            continue
        lines.update(range(tok.start[0], tok.end[0] + 1))
    return lines


def indent_of(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def looks_inert(text: str) -> bool:
    s = text.strip()
    return s == "" or s.startswith("#")


# ---------------------------------------------------------------- the map
class Map:
    def __init__(self, path: Path):
        with gzip.open(path, "rt") as fh:
            d = json.load(fh)
        self.masks = [int(h, 16) for h in d["masks"]]
        self.files = d["files"]
        self.reads = {k: int(v, 16) for k, v in d["reads"].items()}
        self.collect = {k: int(v, 16) for k, v in d["collect"].items()}
        self.pyreads = {k: int(v, 16) for k, v in d.get("pyreads", {}).items()}
        self.pydirs = {}
        for k, v in self.pyreads.items():
            self.pydirs[k.rsplit("/", 1)[0] if "/" in k else ""] = self.pydirs.get(k.rsplit("/", 1)[0] if "/" in k else "", 0) | v
        self.py_collect = {k: int(v, 16) for k, v in d["py_collect"].items()}
        self.mentioned = set(d["mentioned"])
        self.docs = int(d["docs"], 16)
        self.ntests = d["ntests"]
        self.test_ids = d.get("test_ids")
        self.all = (1 << self.ntests) - 1


_sources = None


def read_source(path: str) -> str:
    """The pinned commit's text of a file (shipped as sources.zip, no network needed)."""
    global _sources
    if _sources is None:
        _sources = zipfile.ZipFile(SOURCES)
    try:
        return _sources.read(path).decode("utf-8")
    except KeyError:
        raise Unsafe("source missing: " + path)


def analyse_python(path: str, patch: dict, m: Map) -> int:
    info = m.files.get(path)
    if info is None:
        # not a file any test ever ran: a new module, a script, a benchmark
        return 0
    if patch["status"] == "delete":
        return m.masks[info["any"]] | m.py_collect.get(path, 0)
    old_src = read_source(path)
    old_lines = old_src.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines.pop()
    new_lines = apply_hunks(old_lines, patch["hunks"])
    new_src = "\n".join(new_lines) + "\n"

    groups = change_groups(patch["hunks"])
    real = None  # lazily a set of old lines that carry code
    old_real = set(info["real"])

    # comment / blank-line only?
    only_inert = True
    for before, removed, added in groups:
        if any(ln in old_real for ln, _ in removed) or any(not looks_inert(t) for t in added):
            only_inert = False
            break
    if only_inert:
        try:
            new_real = real_lines(new_src)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return m.masks[info["any"]] | m.py_collect.get(path, 0)
        # the added lines must really be comments/blank in the new file
        # (not text inside a string): compare code-bearing line counts
        if len(new_real) == len(old_real) and compile_ok(new_src, path):
            return 0
        # fall through to the line analysis, treating inert-looking additions as code
        only_inert = False

    if not compile_ok(new_src, path):
        return m.masks[info["any"]] | m.py_collect.get(path, 0)

    u = info["u"]
    g = info["g"]
    result = 0
    for before, removed, added in groups:
        for ln, _ in removed:
            idx = u.get(str(ln))
            if idx is not None:
                result |= m.masks[idx]
        code_added = [t for t in added if not looks_inert(t)]
        # added lines that look like comments but sit inside a statement are treated as code
        if not added:
            continue
        in_place = bool(removed) and bool(code_added) and indent_of(code_added[0]) == indent_of(removed[0][1])
        if in_place:
            continue
        cands = g.get(str(before), [])
        if not code_added:
            # only comment-looking text added: harmless unless it lies inside a statement
            inside = [c for c in cands if c[0] == -1]
            for _, idx in inside:
                result |= m.masks[idx]
            continue
        ind = indent_of(code_added[0])
        chosen = [c for c in cands if c[0] == ind or c[0] == -1]
        if not chosen:
            chosen = cands
        if not chosen:
            result |= m.masks[info["any"]]
        for _, idx in chosen:
            result |= m.masks[idx]
        # new module/class level definitions with names nobody mentions cannot change anything
    return result


def compile_ok(src: str, path: str) -> bool:
    try:
        compile(src, path, "exec", dont_inherit=True)
        return True
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False


# Files that configure how every test runs: a change there can touch any test.
GLOBAL_FILES = {
    "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py", "MANIFEST.in",
}


def affects_everything(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in GLOBAL_FILES or name.startswith("requirements")


def analyse_commit(text: str, m: Map) -> int:
    files = parse_patch(text)
    if not files:
        raise Unsafe("empty patch")
    result = 0
    for patch in files:
        old, new = patch["old"], patch["new"]
        if affects_everything(old) or affects_everything(new):
            return m.all
        # tests that read Python source text: any touch (even a comment) matters
        for key in {old, new}:
            result |= m.pyreads.get(key, 0)
            if key.endswith(".py") and (patch["status"] != "modify"):
                result |= m.pydirs.get(key.rsplit("/", 1)[0] if "/" in key else "", 0)
        if patch["status"] == "rename":
            result |= m.all if old.endswith(".py") else 0
        if old.endswith(".py"):
            if patch["status"] == "new":
                continue
            result |= analyse_python(old, patch, m)
        else:
            for key in {old, new}:
                result |= m.reads.get(key, 0) | m.collect.get(key, 0)
    return result


# ---------------------------------------------------------------- splitting
def split(chosen: list, durations: list) -> list:
    """Best number of workers (<= 8) for these tests: lowest busiest + 2 s each."""
    if not chosen:
        return []
    order = sorted(chosen, key=lambda t: durations[t], reverse=True)
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        groups = [[] for _ in range(k)]
        for t in order:
            j = loads.index(min(loads))
            groups[j].append(t)
            loads[j] += durations[t]
        cost = max(loads) + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, groups)
    return [g for g in best[1] if g]


def runtime_numbers(mask: int, m: Map, tests: list) -> list:
    """Test numbers to run, in this run's numbering, from a mask in the map's numbering."""
    ids = m.test_ids
    runtime = {t["id"]: t["number"] for t in tests}
    if ids is None or (len(ids) == len(tests) and all(runtime.get(tid) == i for i, tid in enumerate(ids))):
        return [i for i in range(len(tests)) if (mask >> i) & 1]
    # numbering differs from the one the map was built with: go by test id,
    # and run any test the map has never heard of
    known = set(ids)
    chosen = [runtime[ids[i]] for i in range(len(ids)) if (mask >> i) & 1 and ids[i] in runtime]
    chosen += [n for tid, n in runtime.items() if tid not in known]
    return sorted(set(chosen))


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    durations = [0] * len(tests)
    for t in tests:
        durations[t["number"]] = round(t["seconds"] * 1000)
    m = Map(MAP)
    total = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            mask = analyse_commit(patch.read_text(encoding="utf-8", errors="surrogateescape"), m)
        except Exception as error:  # anything unexpected: be safe
            print(f"{patch.stem}: unsafe ({error}); running everything", file=sys.stderr)
            mask = m.all
        chosen = runtime_numbers(mask, m, tests)
        total += len(chosen)
        print(json.dumps({"commit": patch.stem, "workers": split(chosen, durations)}))
    print(f"selected {total} tests in total", file=sys.stderr)


if __name__ == "__main__":
    main()
