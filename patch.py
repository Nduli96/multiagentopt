#!/usr/bin/env python3
"""Byte-level edits for files shared between agents working in parallel.

- Reads and writes BYTES (latin-1 is a bijection): legacy encodings such as
  cp949 sources or cp1252 locale files are never decoded.
- Keeps the file's line endings (CRLF or LF): write old/new/anchor with \n.
- Per-file lock: two agents never interleave a read-modify-write.
- Atomic write (temporary file + os.replace).

API (from a script in the scratchpad):
    import sys; sys.path.insert(0, r"<folder of this file>")
    from patch import replace, insert_after, insert_before, append, new_file
    replace(path, old, new, count=1, enc="latin-1")
    insert_after(path, anchor, text, enc="latin-1")    # anchor must be unique
    insert_before(path, anchor, text, enc="latin-1")
    append(path, text, enc="latin-1")
    new_file(path, text, enc="latin-1", crlf=True)     # fails if it exists

`enc` is the encoding that turns old/new/text into bytes: "latin-1" rejects
any character outside latin-1 (ASCII text is always fine); for a file in
another encoding (utf-8, cp1252, cp1250...) pass the encoding of THAT file.

CLI:
    python patch.py spec.json          # list of operations, see _run_spec
    python patch.py --verify [file..]  # from inside the repo: U+FFFD and line endings against HEAD
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

FFFD = b"\xef\xbf\xbd"
LOCK_DIR = os.path.join(tempfile.gettempdir(), "claude_multiagentopt_locks")
LOCK_TIMEOUT = 120.0
STALE_AFTER = 60.0


class PatchError(Exception):
    pass


class _Lock:
    def __init__(self, path):
        os.makedirs(LOCK_DIR, exist_ok=True)
        key = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()
        self.lock = os.path.join(LOCK_DIR, key + ".lock")

    def __enter__(self):
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except (FileExistsError, PermissionError):
                # on Windows a lock being deleted raises PermissionError
                try:
                    if time.time() - os.path.getmtime(self.lock) > STALE_AFTER:
                        os.remove(self.lock)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise PatchError("lock held for too long: " + self.lock)
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.remove(self.lock)
        except OSError:
            pass


def _is_crlf(data):
    lf = data.count(b"\n")
    return lf > 0 and data.count(b"\r\n") * 2 >= lf


def _to_bytes(s, enc, crlf):
    if isinstance(s, str):
        b = s.encode(enc)
    else:
        b = bytes(s)
    b = b.replace(b"\r\n", b"\n")
    if crlf:
        b = b.replace(b"\n", b"\r\n")
    return b


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _disk_name(path):
    # os.replace takes its casing from the path it is given: read the name from disk
    d, name = os.path.split(os.path.abspath(path))
    try:
        for n in os.listdir(d):
            if n.lower() == name.lower():
                return os.path.join(d, n)
    except OSError:
        pass
    return path


def _write_atomic(path, data):
    path = _disk_name(path)
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".patch-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # on Windows a target opened by another process refuses for a moment
        for attempt in range(100):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(0.05)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _check(path, before, after, inserted):
    if FFFD in inserted:
        raise PatchError(path + ": the inserted text contains U+FFFD")
    if after.count(FFFD) > before.count(FFFD):
        raise PatchError(path + ": more U+FFFD than before, write cancelled")
    if _is_crlf(before) and after.count(b"\r\n") != after.count(b"\n") \
            and before.count(b"\r\n") == before.count(b"\n"):
        raise PatchError(path + ": mixed line endings after the change")


def _apply(path, fn):
    with _Lock(path):
        before = _read(path)
        after, inserted = fn(before, _is_crlf(before))
        if after == before:
            return False
        _check(path, before, after, inserted)
        _write_atomic(path, after)
        return True


def replace(path, old, new, count=1, enc="latin-1"):
    """Replaces `old` with `new`. count=N: there must be EXACTLY N
    occurrences; count=0: all of them, at least one."""
    def fn(data, crlf):
        o = _to_bytes(old, enc, crlf)
        n = _to_bytes(new, enc, crlf)
        found = data.count(o)
        if found == 0:
            raise PatchError("%s: text to replace not found:\n%r" % (path, old[:200]))
        if count and found != count:
            raise PatchError("%s: expected %d occurrences, found %d:\n%r" % (path, count, found, old[:200]))
        return data.replace(o, n), n
    return _apply(path, fn)


def _insert(path, anchor, text, enc, after_anchor):
    def fn(data, crlf):
        a = _to_bytes(anchor, enc, crlf)
        t = _to_bytes(text, enc, crlf)
        found = data.count(a)
        if found != 1:
            raise PatchError("%s: the anchor must be unique, found %d times:\n%r" % (path, found, anchor[:200]))
        i = data.index(a)
        if after_anchor:
            i += len(a)
        return data[:i] + t + data[i:], t
    return _apply(path, fn)


def insert_after(path, anchor, text, enc="latin-1"):
    return _insert(path, anchor, text, enc, True)


def insert_before(path, anchor, text, enc="latin-1"):
    return _insert(path, anchor, text, enc, False)


def append(path, text, enc="latin-1"):
    def fn(data, crlf):
        t = _to_bytes(text, enc, crlf)
        sep = b""
        if data and not data.endswith(b"\n"):
            sep = b"\r\n" if crlf else b"\n"
        return data + sep + t, t
    return _apply(path, fn)


def new_file(path, text, enc="latin-1", crlf=True):
    if os.path.exists(path):
        raise PatchError(path + ": already exists")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = _to_bytes(text, enc, crlf)
    if FFFD in data:
        raise PatchError(path + ": the text contains U+FFFD")
    _write_atomic(path, data)
    return True


def _repo_root():
    # The repo being worked on, not this file's location: the skill can live outside it.
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True)
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else None


def verify(paths=None):
    """Compares with HEAD: U+FFFD must not increase, line endings not mixed."""
    root = _repo_root()
    if not root:
        print("verify: not inside a git repository, nothing was checked")
        return False
    explicit = bool(paths)
    if not paths:
        out = subprocess.run(["git", "-C", root, "diff", "--name-only"],
                             capture_output=True).stdout.decode("utf-8", "replace")
        paths = [p for p in out.splitlines() if p.strip()]
    bad = 0
    for p in paths:
        full = os.path.abspath(p) if explicit else os.path.join(root, p)
        if not os.path.isfile(full):
            if explicit:
                bad += 1
                print("%s: file not found" % p)
            continue
        rel = os.path.relpath(full, root).replace("\\", "/")
        cur = _read(full)
        head = subprocess.run(["git", "-C", root, "show", "HEAD:" + rel],
                              capture_output=True).stdout
        msgs = []
        if cur.count(FFFD) > head.count(FFFD):
            msgs.append("U+FFFD %d -> %d" % (head.count(FFFD), cur.count(FFFD)))
        crlf, lf = cur.count(b"\r\n"), cur.count(b"\n")
        if crlf and crlf != lf:
            msgs.append("mixed line endings (CRLF %d of %d lines)" % (crlf, lf))
        if msgs:
            bad += 1
            print("%s: %s" % (rel, "; ".join(msgs)))
    print("verify: %d files with problems out of %d" % (bad, len(paths)))
    return bad == 0


def _run_spec(spec_path):
    """spec.json = [{"op": "replace"|"insert_after"|"insert_before"|"append",
    "path": ..., "old"/"anchor"/"text"/"new": ..., "count": 1, "enc": "latin-1"}]"""
    with open(spec_path, "r", encoding="utf-8") as f:
        ops = json.load(f)
    for op in ops:
        kind, path, enc = op["op"], op["path"], op.get("enc", "latin-1")
        if kind == "replace":
            changed = replace(path, op["old"], op["new"], op.get("count", 1), enc)
        elif kind == "insert_after":
            changed = insert_after(path, op["anchor"], op["text"], enc)
        elif kind == "insert_before":
            changed = insert_before(path, op["anchor"], op["text"], enc)
        elif kind == "append":
            changed = append(path, op["text"], enc)
        else:
            raise PatchError("unknown operation: " + kind)
        print("%-13s %s %s" % (kind, "ok " if changed else "==", path))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--verify":
        sys.exit(0 if verify(sys.argv[2:]) else 1)
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    try:
        _run_spec(sys.argv[1])
    except PatchError as e:
        print("ERROR: %s" % e)
        sys.exit(1)
