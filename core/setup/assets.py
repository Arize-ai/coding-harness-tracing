"""Install, refresh and remove the files ``arize-harness setup`` writes.

Files are read from the installed package and recorded in
``~/.arize/harness/manifest.json``, which ``status`` and ``uninstall`` rely on.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from importlib import resources
from pathlib import Path
from typing import Callable, ContextManager, Dict, Iterator, List, Optional, Sequence, Tuple

import core
import core.setup as _setup
from core.setup.runner import arize_harness_path, may_write_setup, ts_argv

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1
CLAUDE_PLUGIN_DIR_NAME = "claude_plugin"

# Non-python files to be included in the build.
# pyproject.toml's [tool.setuptools.package-data] lists the same files.
PACKAGE_FILES: Dict[str, List[str]] = {
    "claude_code": [".claude-plugin/plugin.json", "hooks/hooks.json", "skills/manage-claude-code-tracing/SKILL.md"],
    "codex": ["skills/manage-codex-tracing/SKILL.md"],
    "cursor": ["skills/manage-cursor-tracing/SKILL.md"],
    "kiro": ["skills/manage-kiro-tracing/SKILL.md"],
    "omp": ["plugin/arize-tracing.ts", "skills/manage-omp-tracing/SKILL.md"],
    "opencode": ["plugin/arize-tracing.ts", "skills/manage-opencode-tracing/SKILL.md"],
}

SKILL_HARNESSES = ("claude_code", "codex", "cursor", "kiro", "omp", "opencode")


class AssetError(SystemExit):
    """Setup hit a missing package file or an unwritable path. Exits 1."""

    def __init__(self, message: str) -> None:
        super().__init__(1)
        self.message = message

    def __str__(self) -> str:
        return self.message


def _fail(message: str) -> AssetError:
    _setup.err(message)
    return AssetError(message)


def _sub(harness: str) -> str:
    return harness.replace("-", "_")


def _key(harness: str) -> str:
    return harness.replace("_", "-")


# ---------------------------------------------------------------------------
# Reading package data
# ---------------------------------------------------------------------------


def read_package_file(harness: str, rel: str) -> bytes:
    sub = _sub(harness)
    if rel not in PACKAGE_FILES.get(sub, []):
        raise _fail(f"tracing/{sub}/{rel} is not package data")
    try:
        node = resources.files(f"tracing.{sub}").joinpath(*rel.split("/"))
        return node.read_bytes()
    except (OSError, ModuleNotFoundError) as e:
        raise _fail(f"package file tracing/{sub}/{rel} is missing from the installed package: {e}")


def skill_files(harness: str) -> List[Tuple[str, str]]:
    out = []
    for rel in PACKAGE_FILES.get(_sub(harness), []):
        parts = rel.split("/")
        if parts[0] == "skills" and len(parts) >= 3:
            out.append((parts[1], rel))
    return out


# ---------------------------------------------------------------------------
# Generated files
# ---------------------------------------------------------------------------

_RUN_HOOK = re.compile(r'"\\"\$\{CLAUDE_PLUGIN_ROOT\}/scripts/run-hook\\" arize-hook-([a-z-]+)"')


def claude_hooks_json(source: str) -> str:
    from core.setup.runner import hook_command

    def repl(m: re.Match) -> str:
        return json.dumps(hook_command("claude", m.group(1)))

    text, count = _RUN_HOOK.subn(repl, source)
    if count == 0:
        raise _fail("tracing/claude_code/hooks/hooks.json has no run-hook commands to replace")
    json.loads(text)
    return text


_TS_BINARY_PATH = re.compile(r"function binaryPath\(\): string \{\n.*?\n\}\n", re.DOTALL)
_TS_SPAWN = re.compile(r"spawn\(binaryPath\(\), \[\],")


def plugin_ts(harness: str, source: str) -> str:
    """The opencode/omp plugin with its venv path replaced by the ``arize-harness hook`` command.

    Keeps CRLF line endings if the source has them.
    """
    file_literal, args_literal = ts_argv(harness)
    newline = "\r\n" if "\r\n" in source else "\n"
    lf = source.replace("\r\n", "\n")
    old = _TS_BINARY_PATH.search(lf)
    semi = ";" if old and ";\n" in old.group(0) else ""  # match the file's style: omp uses semicolons, opencode doesn't
    body = f"function binaryPath(): string {{\n  return {file_literal}{semi}\n}}\n"
    text, n1 = _TS_BINARY_PATH.subn(lambda _m: body, lf, count=1)
    text, n2 = _TS_SPAWN.subn(lambda _m: f"spawn(binaryPath(), {args_literal},", text, count=1)
    if n1 != 1 or n2 != 1:
        raise _fail(f"tracing/{_sub(harness)}/plugin/arize-tracing.ts has no venv path to replace")
    return text.replace("\n", newline) if newline != "\n" else text


# How each kind of file is produced from its packaged source.
_GENERATORS: Dict[str, Callable[[str, str, bytes], bytes]] = {
    "copy": lambda harness, rel, data: data,
    "claude_hooks": lambda harness, rel, data: claude_hooks_json(data.decode("utf-8")).encode("utf-8"),
    "plugin_ts": lambda harness, rel, data: plugin_ts(harness, data.decode("utf-8")).encode("utf-8"),
}


def render(harness: str, rel: str, kind: str) -> bytes:
    return _GENERATORS[kind](harness, rel, read_package_file(harness, rel))


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def manifest_path() -> Path:
    return _setup.INSTALL_DIR / MANIFEST_NAME


def load_manifest() -> dict:
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": MANIFEST_SCHEMA, "harnesses": {}}
    if not isinstance(data, dict) or not isinstance(data.get("harnesses"), dict):
        return {"schema": MANIFEST_SCHEMA, "harnesses": {}}
    return data


def save_manifest(data: dict) -> None:
    """Write the manifest atomically (temp file, then rename)."""
    path = manifest_path()
    data["schema"] = MANIFEST_SCHEMA
    tmp: Optional[str] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        raise _fail(f"cannot write {path}: {e}")
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# errno values meaning the filesystem doesn't support locks (e.g. NFS without lockd).
_NO_LOCKING = {errno.ENOLCK, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP), errno.ENOSYS}
LOCK_TIMEOUT_SECONDS = 120.0


def _acquire(fd: int, lock: Path) -> bool:
    """Lock ``fd`` exclusively. Returns False if the filesystem can't lock."""
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            return True
        except OSError as e:
            if e.errno in _NO_LOCKING:
                _setup.info(f"warning: {lock.parent} does not support file locks; updating the manifest unlocked")
                return False
            # Windows: another process holds the lock, so retry until the deadline.
            if sys.platform != "win32" or time.monotonic() >= deadline:
                raise _fail(f"cannot lock {lock}: {e}")
            time.sleep(0.05)


# How deeply this thread has nested _manifest_lock(); only the outer call holds the file lock.
_LOCK_STATE = threading.local()


@contextmanager
def _manifest_lock() -> Iterator[None]:
    """Exclusive lock on ``manifest.json.lock``, reentrant within a thread.

    Blocks on POSIX, retries on Windows for up to ``LOCK_TIMEOUT_SECONDS``, and
    runs unlocked with a warning where the filesystem has no locks.
    """
    depth = getattr(_LOCK_STATE, "depth", 0)
    if depth:
        _LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _LOCK_STATE.depth = depth
        return
    lock = manifest_path().with_name(MANIFEST_NAME + ".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise _fail(f"cannot open {lock}: {e}")
    locked = False
    try:
        locked = _acquire(fd, lock)
        _LOCK_STATE.depth = 1
        yield
    finally:
        _LOCK_STATE.depth = 0
        if sys.platform == "win32":
            if locked:
                try:
                    import msvcrt

                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        os.close(fd)  # closing also releases the flock


def _operation_lock() -> ContextManager[None]:
    """Hold the manifest lock for a whole setup, refresh or uninstall. No-op in dry runs."""
    return nullcontext() if _setup.dry_run() else _manifest_lock()


@contextmanager
def manifest_transaction() -> Iterator[dict]:
    """Load the manifest under the lock, let the caller change it, then save it."""
    with _manifest_lock():
        data = load_manifest()
        yield data
        save_manifest(data)


def recorded(harness: str) -> Dict[str, dict]:
    files = load_manifest()["harnesses"].get(_key(harness), {})
    return files if isinstance(files, dict) else {}


def _iter_entries(data: dict) -> Iterator[Tuple[str, str, dict]]:
    for harness, files in data["harnesses"].items():
        if isinstance(files, dict):
            for path, entry in files.items():
                if isinstance(entry, dict):
                    yield harness, path, entry


def _harness_files(data: dict, key: str) -> dict:
    files = data["harnesses"].get(key)
    if not isinstance(files, dict):
        files = data["harnesses"][key] = {}
    return files


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

# (package path, target path, kind, owned folder)
FileSpec = Tuple[str, Path, str, Optional[Path]]


def _write_bytes(target: Path, data: bytes) -> None:
    try:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as e:
        raise _fail(f"cannot write {target}: {e}")


def _entry(harness: str, rel: str, kind: str, root: Optional[Path]) -> dict:
    entry = {"source": f"tracing/{_sub(harness)}/{rel}", "kind": kind, "version": core.version()}
    if root is not None:
        entry["root"] = str(root)
    if kind != "copy":
        # the arize-harness this file calls; refresh skips files another install owns
        entry["command"] = str(arize_harness_path())
    return entry


def write_files(harness: str, specs: List[FileSpec], unlink_first: Sequence[Path] = ()) -> None:
    """Render, write and record a set of files in one manifest update."""
    rendered = [(rel, target, kind, root, render(harness, rel, kind)) for rel, target, kind, root in specs]
    if _setup.dry_run():
        for _rel, target, _kind, _root, _data in rendered:
            _setup.info(f"would write {target}")
        return
    with _manifest_lock():
        _write_and_record(harness, rendered, unlink_first)


def _write_and_record(
    harness: str, rendered: List[Tuple[str, Path, str, Optional[Path], bytes]], unlink_first: Sequence[Path]
) -> None:
    for link in unlink_first:
        try:
            link.unlink()  # an old skill symlink, replaced by a copy
        except OSError as e:
            raise _fail(f"cannot replace {link}: {e}")
    written: List[FileSpec] = []
    failed = False
    try:
        for rel, target, kind, root, data in rendered:
            _write_bytes(target, data)
            written.append((rel, target, kind, root))
    except BaseException:
        failed = True
        raise
    finally:
        if written:
            try:
                with manifest_transaction() as manifest:
                    files = _harness_files(manifest, _key(harness))
                    for rel, target, kind, root in written:
                        files[str(target)] = _entry(harness, rel, kind, root)
            except (Exception, SystemExit) as e:
                if not failed:
                    raise
                # report the write error, not this follow-on one
                if not isinstance(e, AssetError):
                    _setup.err(f"could not record {len(written)} written file(s) in {manifest_path()}: {e}")


def write_file(harness: str, rel: str, target: Path, kind: str = "copy", root: Optional[Path] = None) -> None:
    write_files(harness, [(rel, target, kind, root)])


def _source_rel(harness: str, entry: dict) -> Optional[str]:
    prefix = f"tracing/{_sub(harness)}/"
    source = entry.get("source", "")
    return source[len(prefix) :] if isinstance(source, str) and source.startswith(prefix) else None


_GENERATED_KINDS = {"hooks/hooks.json": "claude_hooks", "plugin/arize-tracing.ts": "plugin_ts"}


def _setup_location(harness: str, path: str, entry: dict) -> Optional[Tuple[str, str, Optional[Path]]]:
    """Check that a manifest entry points where setup writes that file.

    Returns ``(rel, kind, owned folder)``, or None. Refresh and uninstall skip entries that fail.
    """
    rel = _source_rel(harness, entry)
    if rel is None or rel not in PACKAGE_FILES.get(_sub(harness), []):
        return None
    kind = _GENERATED_KINDS.get(rel, "copy")
    if entry.get("kind", "copy") != kind:
        return None
    target = Path(path)
    if not target.is_absolute() or ".." in target.parts:
        return None
    parts = rel.split("/")
    if parts[0] == "skills":
        tail = (".agents", *parts)
        if tuple(target.parts[-len(tail) :]) != tail:
            return None
        return rel, kind, target.parents[len(parts) - 3]
    if _sub(harness) == "claude_code":
        root = claude_plugin_dir()
        return (rel, kind, root) if target == root.joinpath(*parts) else None
    if rel == "plugin/arize-tracing.ts":
        return (rel, kind, None) if target.name == "arize-tracing.ts" else None
    return None


def _foreign_link(target: Path, root: Optional[Path]) -> Optional[Path]:
    """The first symlink between ``target`` and its owned folder, if any.

    A symlink there means the user replaced the folder, so refresh and uninstall leave it alone.
    """
    if root is None:
        return None
    try:
        target.relative_to(root)
    except ValueError:
        return None
    current = target.parent
    while True:
        if current.is_symlink():
            return current
        if current == root or current == current.parent:
            return None
        current = current.parent


def stale_entries(harness: Optional[str] = None) -> List[Tuple[str, str, dict]]:
    current = core.version()
    return [
        (h, p, e)
        for h, p, e in _iter_entries(load_manifest())
        if (harness is None or h == _key(harness)) and e.get("version") != current
    ]


def refresh_stale() -> List[str]:
    """Rewrite files recorded by a different package version. Returns the paths rewritten."""
    try:
        if not stale_entries() or not may_write_setup():
            return []
    except Exception as e:  # a bad manifest shouldn't break status
        _setup.err(f"could not check {manifest_path()} for stale files: {e}")
        return []
    try:
        with _operation_lock():
            return _refresh_locked()
    except (Exception, SystemExit) as e:
        if not isinstance(e, AssetError):
            _setup.err(f"could not refresh files listed in {manifest_path()}: {e}")
        return []


def _refresh_locked() -> List[str]:
    stale = stale_entries()
    rewritten: List[Tuple[str, str]] = []
    gone: List[Tuple[str, str]] = []
    current_command = str(arize_harness_path())
    for harness, path, entry in stale:
        try:
            location = _setup_location(harness, path, entry)
            if location is None:
                _setup.err(f"ignoring manifest entry {path}: not a file setup writes")
                continue
            rel, kind, root = location
            link = _foreign_link(Path(path), root)
            if link is not None:
                # the folder was replaced by a symlink: stop tracking the file
                _setup.info(f"leaving {path}: {link} is a symlink setup did not make")
                gone.append((harness, path))
                continue
            command = entry.get("command")
            if kind != "copy" and isinstance(command, str) and command != current_command:
                if Path(command).exists():
                    _setup.info(f"leaving {path}: it runs {command}, not this {current_command}")
                    continue
                # the install that wrote it is gone, so this one takes the file over
            target = Path(path)
            if not target.is_file():
                gone.append((harness, path))
                continue
            if _setup.dry_run():
                _setup.info(f"would rewrite {target} ({entry.get('version')} -> {core.version()})")
                continue
            _write_bytes(target, render(harness, rel, kind))
            rewritten.append((harness, path))
        except (Exception, SystemExit) as e:  # AssetError already printed its message
            if not isinstance(e, AssetError):
                _setup.err(f"could not refresh {path}: {e}")
    if _setup.dry_run() or not (rewritten or gone):
        return [p for _h, p in rewritten]
    current = core.version()
    try:
        with manifest_transaction() as manifest:
            for harness, path in rewritten:
                recorded_entry = _harness_files(manifest, harness).get(path)
                if isinstance(recorded_entry, dict):
                    recorded_entry["version"] = current
                    if recorded_entry.get("kind", "copy") != "copy":
                        recorded_entry["command"] = current_command
            for harness, path in gone:
                files = _harness_files(manifest, harness)
                files.pop(path, None)
                if not files:
                    del manifest["harnesses"][harness]
    except (Exception, SystemExit) as e:
        if not isinstance(e, AssetError):
            _setup.err(f"could not update {manifest_path()}: {e}")
    return [p for _h, p in rewritten]


# ---------------------------------------------------------------------------
# Removing
# ---------------------------------------------------------------------------


def _prune(start: Path, root: Path) -> None:
    try:
        start.relative_to(root)
    except ValueError:
        return
    current = start
    while True:
        try:
            current.rmdir()
        except OSError:
            return
        if current == root:
            return
        current = current.parent


def uninstall_files(harness: str) -> bool:
    """Remove the files the manifest lists for a harness. Returns False if any couldn't be removed."""
    key = _key(harness)
    files = load_manifest()["harnesses"].get(key)
    if not isinstance(files, dict) or not files:
        return True
    if _setup.dry_run():
        for path in files:
            _setup.info(f"would remove {path}")
        return True
    with _manifest_lock():
        return _uninstall_locked(harness, key)


def _uninstall_locked(harness: str, key: str) -> bool:
    files = load_manifest()["harnesses"].get(key)
    if not isinstance(files, dict) or not files:
        return True
    removed = set()
    for path, entry in files.items():
        location = _setup_location(harness, path, entry) if isinstance(entry, dict) else None
        if location is None:
            # not a path setup writes to: drop the entry, keep the file
            _setup.err(f"ignoring manifest entry {path}: not a file setup writes")
            removed.add(path)
            continue
        target = Path(path)
        root = location[2]
        link = _foreign_link(target, root)
        if link is not None:
            # the folder was replaced by a symlink: drop the entry, keep the user's files
            _setup.info(f"leaving {path}: {link} is a symlink setup did not make")
            removed.add(path)
            continue
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
        except OSError as e:
            _setup.err(f"could not remove {target}: {e}")
            continue
        removed.add(path)
        if root is not None:
            _prune(target.parent, root)
    with manifest_transaction() as manifest:
        remaining = {p: e for p, e in _harness_files(manifest, key).items() if p not in removed}
        if remaining:
            manifest["harnesses"][key] = remaining
        else:
            del manifest["harnesses"][key]
    return len(removed) == len(files)


def refresh_and_report() -> None:
    for path in refresh_stale():
        _setup.info(f"Refreshed {path} for version {core.version()}")


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def _skills_dest(target_dir: Optional[Path]) -> Path:
    return (target_dir if target_dir is not None else Path.cwd()) / ".agents" / "skills"


def _old_skills_root(harness: str) -> Path:
    """Where older installs linked skills from: ``~/.arize/harness/tracing/<harness>/skills``."""
    return _setup.INSTALL_DIR / "tracing" / _sub(harness) / "skills"


def _points_into(link: Path, root: Path) -> bool:
    try:
        target = Path(os.readlink(link))
    except OSError:
        return False
    if not target.is_absolute():
        target = link.parent / target
    target = Path(os.path.normpath(target))
    try:
        target.relative_to(os.path.normpath(root))
    except ValueError:
        return False
    return True


def install_skills(harness: str, target_dir: Optional[Path] = None) -> None:
    """Copy a harness's skills into ``.agents/skills/`` under ``target_dir`` (default: cwd).

    Replaces our old symlinks and skips folders setup didn't create.
    """
    skills = skill_files(harness)
    if not skills:
        return
    with _operation_lock():  # check and write under one lock
        _install_skills_locked(harness, skills, _skills_dest(target_dir))


def _install_skills_locked(harness: str, skills: List[Tuple[str, str]], dest: Path) -> None:
    old_root = _old_skills_root(harness)
    ours = recorded(harness)
    specs: List[FileSpec] = []
    old_links: List[Path] = []
    for name, rel in skills:
        skill_dir = dest / name
        target = skill_dir / rel.split("/", 2)[2]
        if skill_dir.is_symlink():
            if not _points_into(skill_dir, old_root):
                _setup.info(f"warning: {skill_dir} is a symlink setup did not make; leaving it, skipping {name}")
                continue
            if skill_dir not in old_links:
                old_links.append(skill_dir)
        elif skill_dir.exists() and str(target) not in ours:
            _setup.info(f"warning: {skill_dir} is a folder setup did not make; leaving it, skipping {name}")
            continue
        specs.append((rel, target, "copy", skill_dir))
    if specs:
        write_files(harness, specs, unlink_first=old_links)


def remove_old_skill_links(harness: str, target_dir: Optional[Path] = None) -> None:
    """Remove ``.agents/skills/`` symlinks left by older installs."""
    dest = _skills_dest(target_dir)
    if not dest.is_dir():
        return
    root = _old_skills_root(harness)
    for link in sorted(dest.iterdir()):
        if link.is_symlink() and _points_into(link, root):
            if _setup.dry_run():
                _setup.info(f"would unlink {link}")
                continue
            try:
                link.unlink()
            except OSError as e:
                _setup.err(f"could not remove {link}: {e}")


# ---------------------------------------------------------------------------
# Per-harness files
# ---------------------------------------------------------------------------


def claude_plugin_dir() -> Path:
    """The Claude Code plugin folder, ``~/.arize/harness/claude_plugin``.

    The ``settings.json`` plugin entry and ``agent_sdk.py`` point here.
    """
    return _setup.INSTALL_DIR / CLAUDE_PLUGIN_DIR_NAME


def install_claude_plugin() -> Path:
    """Write the Claude plugin folder (``plugin.json`` and ``hooks.json``) and return its path."""
    root = claude_plugin_dir()
    write_files(
        "claude_code",
        [
            (".claude-plugin/plugin.json", root / ".claude-plugin" / "plugin.json", "copy", root),
            ("hooks/hooks.json", root / "hooks" / "hooks.json", "claude_hooks", root),
        ],
    )
    return root


def install_plugin_ts(harness: str, target: Path) -> None:
    write_file(harness, "plugin/arize-tracing.ts", target, "plugin_ts")
