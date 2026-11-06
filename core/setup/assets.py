"""Files ``arize-harness setup`` writes from the installed package, and the manifest that records them.

Every file comes from package data (``importlib.resources``), never from a
source-tree path, so a wheel installed with ``uv tool install`` / ``pipx``, a
Nuitka binary and a dev checkout all behave the same.

Each written file is recorded in ``~/.arize/harness/manifest.json`` with the
package version and how to make it again. ``refresh_stale()`` rewrites files
recorded under another version; ``uninstall_files()`` removes a harness's
files and nothing else.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import core
import core.setup as _setup
from core.setup.runner import arize_harness_path, may_write_setup, ts_argv

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1
CLAUDE_PLUGIN_DIR_NAME = "claude_plugin"

# Package data each harness ships, relative to its ``tracing.<sub>`` package.
# Must match [tool.setuptools.package-data] in pyproject.toml (a test checks).
PACKAGE_FILES: Dict[str, List[str]] = {
    "claude_code": [".claude-plugin/plugin.json", "hooks/hooks.json", "skills/manage-claude-code-tracing/SKILL.md"],
    "codex": ["skills/manage-codex-tracing/SKILL.md"],
    "cursor": ["skills/manage-cursor-tracing/SKILL.md"],
    "kiro": ["skills/manage-kiro-tracing/SKILL.md"],
    "omp": ["plugin/arize-tracing.ts", "skills/manage-omp-tracing/SKILL.md"],
    "opencode": ["plugin/arize-tracing.ts", "skills/manage-opencode-tracing/SKILL.md"],
}

# Harnesses whose installer takes --with-skills and ships skills.
SKILL_HARNESSES = ("claude_code", "codex", "cursor", "kiro", "omp", "opencode")


class AssetError(SystemExit):
    """A package file is missing or a target can't be written. Exits 1; the message names the path."""

    def __init__(self, message: str) -> None:
        super().__init__(1)
        self.message = message

    def __str__(self) -> str:
        return self.message


def _fail(message: str) -> AssetError:
    _setup.err(message)
    return AssetError(message)


def _sub(harness: str) -> str:
    """``claude-code`` -> ``claude_code``: the ``tracing.<sub>`` package name."""
    return harness.replace("-", "_")


def _key(harness: str) -> str:
    """``claude_code`` -> ``claude-code``: the config.json / manifest key."""
    return harness.replace("_", "-")


# ---------------------------------------------------------------------------
# Reading package data
# ---------------------------------------------------------------------------


def read_package_file(harness: str, rel: str) -> bytes:
    """Bytes of ``tracing/<sub>/<rel>`` from the installed package; exits 1 naming it if missing."""
    sub = _sub(harness)
    if rel not in PACKAGE_FILES.get(sub, []):
        raise _fail(f"tracing/{sub}/{rel} is not package data")
    try:
        node = resources.files(f"tracing.{sub}").joinpath(*rel.split("/"))
        return node.read_bytes()
    except (OSError, ModuleNotFoundError) as e:
        raise _fail(f"package file tracing/{sub}/{rel} is missing from the installed package: {e}")


def skill_files(harness: str) -> List[Tuple[str, str]]:
    """``(skill name, package-relative path)`` for every skill file a harness ships."""
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
    """The marketplace ``hooks/hooks.json`` with each ``run-hook`` command replaced by ``hook_command()``.

    Only the command strings change; the rest of the file is kept byte for byte.
    """
    from core.setup.runner import hook_command

    def repl(m: re.Match) -> str:
        return json.dumps(hook_command("claude", m.group(1)))

    text, count = _RUN_HOOK.subn(repl, source)
    if count == 0:
        raise _fail("tracing/claude_code/hooks/hooks.json has no run-hook commands to replace")
    json.loads(text)  # still valid JSON
    return text


_TS_BINARY_PATH = re.compile(r"function binaryPath\(\): string \{\n.*?\n\}\n", re.DOTALL)
_TS_SPAWN = re.compile(r"spawn\(binaryPath\(\), \[\],")


def plugin_ts(harness: str, source: str) -> str:
    """The packaged ``arize-tracing.ts`` with today's venv path replaced by the ``arize-harness hook`` command.

    Keeps the source's line endings: a Windows checkout without a
    ``.gitattributes`` rule ships the file with CRLF.
    """
    file_literal, args_literal = ts_argv(harness)
    newline = "\r\n" if "\r\n" in source else "\n"
    lf = source.replace("\r\n", "\n")
    old = _TS_BINARY_PATH.search(lf)
    semi = ";" if old and ";\n" in old.group(0) else ""  # omp's style uses semicolons, opencode's doesn't
    body = f"function binaryPath(): string {{\n  return {file_literal}{semi}\n}}\n"
    text, n1 = _TS_BINARY_PATH.subn(lambda _m: body, lf, count=1)
    text, n2 = _TS_SPAWN.subn(lambda _m: f"spawn(binaryPath(), {args_literal},", text, count=1)
    if n1 != 1 or n2 != 1:
        raise _fail(f"tracing/{_sub(harness)}/plugin/arize-tracing.ts has no venv path to replace")
    return text.replace("\n", newline) if newline != "\n" else text


# kind -> (harness, package rel, source bytes) -> output bytes
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
    """Write the manifest atomically, through a temp file of this process's own."""
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


# flock / locking errors that mean "this filesystem can't lock" (NFS without lockd, some FUSE/SMB mounts).
_NO_LOCKING = {errno.ENOLCK, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP), errno.ENOSYS}
LOCK_TIMEOUT_SECONDS = 120.0


def _acquire(fd: int, lock: Path) -> bool:
    """Take an exclusive lock on ``fd``; False when the filesystem has no locking."""
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
            # Windows: held by another process (EACCES / EDEADLK); keep trying until the deadline.
            if sys.platform != "win32" or time.monotonic() >= deadline:
                raise _fail(f"cannot lock {lock}: {e}")
            time.sleep(0.05)


@contextmanager
def _manifest_lock() -> Iterator[None]:
    """An exclusive lock on ``manifest.json.lock``, released when the process exits at the latest.

    Blocks on POSIX; on Windows retries for up to ``LOCK_TIMEOUT_SECONDS``. On
    a filesystem without locks it warns and proceeds unlocked rather than fail.
    """
    lock = manifest_path().with_name(MANIFEST_NAME + ".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise _fail(f"cannot open {lock}: {e}")
    locked = False
    try:
        locked = _acquire(fd, lock)
        yield
    finally:
        if sys.platform == "win32":
            if locked:
                try:
                    import msvcrt

                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        os.close(fd)  # also drops a flock


@contextmanager
def manifest_transaction() -> Iterator[dict]:
    """Load the manifest under the lock, let the caller change it, then save it.

    Concurrent setup / status / uninstall runs never lose each other's entries.
    """
    with _manifest_lock():
        data = load_manifest()
        yield data
        save_manifest(data)


def recorded(harness: str) -> Dict[str, dict]:
    """``{path: entry}`` the manifest lists for a harness."""
    files = load_manifest()["harnesses"].get(_key(harness), {})
    return files if isinstance(files, dict) else {}


def _iter_entries(data: dict) -> Iterator[Tuple[str, str, dict]]:
    for harness, files in data["harnesses"].items():
        if isinstance(files, dict):
            for path, entry in files.items():
                if isinstance(entry, dict):
                    yield harness, path, entry


def _harness_files(data: dict, key: str) -> dict:
    """``data["harnesses"][key]`` as a dict, replacing anything malformed there."""
    files = data["harnesses"].get(key)
    if not isinstance(files, dict):
        files = data["harnesses"][key] = {}
    return files


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

# (package rel, target, kind, root)
FileSpec = Tuple[str, Path, str, Optional[Path]]


def _write_bytes(target: Path, data: bytes) -> None:
    try:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            # An old symlink (or a link left where a file goes): replace, never write through it.
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
        # The arize-harness baked into the file; refresh leaves the file to that install.
        entry["command"] = str(arize_harness_path())
    return entry


def write_files(harness: str, specs: List[FileSpec], unlink_first: Sequence[Path] = ()) -> None:
    """Write package files (``copy``) or generated files and record them in one manifest update.

    Every file is rendered first, so a missing package file fails before
    anything is written. ``root`` is the directory we own that a target sits
    in (a skill folder, the Claude plugin folder); uninstall removes it once
    empty. ``unlink_first`` (our old symlinks) are removed only after every
    file rendered. Files written before a failure are still recorded, and the
    write failure is what propagates. Honors ``ARIZE_DRY_RUN``; overwrites
    without prompting.
    """
    rendered = [(rel, target, kind, root, render(harness, rel, kind)) for rel, target, kind, root in specs]
    if _setup.dry_run():
        for _rel, target, _kind, _root, _data in rendered:
            _setup.info(f"would write {target}")
        return
    for link in unlink_first:
        try:
            link.unlink()  # our old symlink: replaced by a copy, never written through
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
                # Keep the write error as the one reported; this one is secondary.
                if not isinstance(e, AssetError):
                    _setup.err(f"could not record {len(written)} written file(s) in {manifest_path()}: {e}")


def write_file(harness: str, rel: str, target: Path, kind: str = "copy", root: Optional[Path] = None) -> None:
    """Write and record one file; see ``write_files``."""
    write_files(harness, [(rel, target, kind, root)])


def _source_rel(harness: str, entry: dict) -> Optional[str]:
    prefix = f"tracing/{_sub(harness)}/"
    source = entry.get("source", "")
    return source[len(prefix) :] if isinstance(source, str) and source.startswith(prefix) else None


_GENERATED_KINDS = {"hooks/hooks.json": "claude_hooks", "plugin/arize-tracing.ts": "plugin_ts"}


def _setup_location(harness: str, path: str, entry: dict) -> Optional[Tuple[str, str, Optional[Path]]]:
    """``(rel, kind, root)`` when a manifest entry names a file setup could have written there, else None.

    Refresh and uninstall act only on these, so a corrupted or planted
    manifest can't make them overwrite or delete anything else: the source
    must be package data of that harness with its kind, and the path must be
    where that file goes (the Claude plugin folder, an ``arize-tracing.ts``,
    or ``.agents/skills/<skill>/...``).
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


def stale_entries(harness: Optional[str] = None) -> List[Tuple[str, str, dict]]:
    """Recorded ``(harness, path, entry)`` whose version is not the running package's."""
    current = core.version()
    return [
        (h, p, e)
        for h, p, e in _iter_entries(load_manifest())
        if (harness is None or h == _key(harness)) and e.get("version") != current
    ]


def refresh_stale() -> List[str]:
    """Rewrite every recorded file whose version differs from the running package's.

    Called by ``setup``, ``status`` and ``uninstall`` (never ``hook`` or
    ``update``). Writes nothing unless ``may_write_setup()``, or under
    ``ARIZE_DRY_RUN``. A recorded file that no longer exists is not recreated
    (the user removed it, or the project holding a skill is gone); it is
    dropped from the manifest instead. Never raises: a file that can't be
    rewritten is reported and left as it is, so ``status`` still runs.
    Returns the rewritten paths.
    """
    try:
        stale = stale_entries()
        if not stale or not may_write_setup():
            return []
    except Exception as e:  # a malformed manifest must not take status down
        _setup.err(f"could not check {manifest_path()} for stale files: {e}")
        return []
    rewritten: List[Tuple[str, str]] = []
    gone: List[Tuple[str, str]] = []
    current_command = str(arize_harness_path())
    for harness, path, entry in stale:
        try:
            location = _setup_location(harness, path, entry)
            if location is None:
                _setup.err(f"ignoring manifest entry {path}: not a file setup writes")
                continue
            rel, kind, _root = location
            command = entry.get("command")
            if kind != "copy" and isinstance(command, str) and command != current_command:
                if Path(command).exists():
                    # Another install that still exists set this up; don't take its registration over.
                    _setup.info(f"leaving {path}: it runs {command}, not this {current_command}")
                    continue
                # The install that set this up is gone (e.g. pipx uninstalled, uv tool installed):
                # its hooks can't run, so take the file over.
            target = Path(path)
            if not target.is_file():
                gone.append((harness, path))
                continue
            if _setup.dry_run():
                _setup.info(f"would rewrite {target} ({entry.get('version')} -> {core.version()})")
                continue
            _write_bytes(target, render(harness, rel, kind))
            rewritten.append((harness, path))
        except (Exception, SystemExit) as e:  # AssetError is a SystemExit; reported already or here
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
    """Remove ``start`` and its parents while empty, up to and including ``root``."""
    try:
        start.relative_to(root)
    except ValueError:
        return
    current = start
    while True:
        try:
            current.rmdir()
        except OSError:
            return  # not empty, or gone
        if current == root:
            return
        current = current.parent


def uninstall_files(harness: str) -> bool:
    """Remove every file the manifest lists for ``harness``, and forget the ones that are gone.

    Only those files: a user's file in the same folder survives, and a folder
    we own is removed only once it is empty. A file that can't be removed
    stays in the manifest, so the next uninstall tries again. Honors
    ``ARIZE_DRY_RUN``. Returns False if any file could not be removed.
    """
    key = _key(harness)
    files = load_manifest()["harnesses"].get(key)
    if not isinstance(files, dict) or not files:
        return True
    if _setup.dry_run():
        for path in files:
            _setup.info(f"would remove {path}")
        return True
    removed = set()
    for path, entry in files.items():
        location = _setup_location(harness, path, entry) if isinstance(entry, dict) else None
        if location is None:
            # Not a file setup writes: forget the entry, never delete the path.
            _setup.err(f"ignoring manifest entry {path}: not a file setup writes")
            removed.add(path)
            continue
        target = Path(path)
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
        except OSError as e:
            _setup.err(f"could not remove {target}: {e}")
            continue
        removed.add(path)
        root = location[2]
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
    """``refresh_stale()``, printing one line per rewritten file."""
    for path in refresh_stale():
        _setup.info(f"Refreshed {path} for version {core.version()}")


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def _skills_dest(target_dir: Optional[Path]) -> Path:
    return (target_dir if target_dir is not None else Path.cwd()) / ".agents" / "skills"


def _old_skills_root(harness: str) -> Path:
    """Where pre-manifest installs symlinked skills from: ``~/.arize/harness/tracing/<sub>/skills``."""
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
    """Copy a harness's skills into ``<target_dir>/.agents/skills/`` (default: cwd).

    Replaces our old symlinks there; each copied file is recorded in the
    manifest. Does nothing for a harness that ships no skills.
    """
    skills = skill_files(harness)
    if not skills:
        return
    dest = _skills_dest(target_dir)
    old_root = _old_skills_root(harness)
    specs: List[FileSpec] = []
    old_links: List[Path] = []
    for name, rel in skills:
        skill_dir = dest / name
        if skill_dir.is_symlink():
            if not _points_into(skill_dir, old_root):
                _setup.info(f"warning: {skill_dir} is a symlink setup did not make; leaving it, skipping {name}")
                continue
            if skill_dir not in old_links:
                old_links.append(skill_dir)
        specs.append((rel, skill_dir / rel.split("/", 2)[2], "copy", skill_dir))
    if specs:
        write_files(harness, specs, unlink_first=old_links)


def remove_old_skill_links(harness: str, target_dir: Optional[Path] = None) -> None:
    """Remove symlinks in ``.agents/skills/`` that point into ``~/.arize/harness/tracing/<sub>/skills``."""
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
    """The plugin folder setup writes for Claude Code: ``~/.arize/harness/claude_plugin``."""
    return _setup.INSTALL_DIR / CLAUDE_PLUGIN_DIR_NAME


def install_claude_plugin() -> Path:
    """Write the Claude plugin folder (manifest + generated ``hooks/hooks.json``) and return it."""
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
    """Write ``harness``'s generated ``arize-tracing.ts`` to ``target`` (opencode, omp)."""
    write_file(harness, "plugin/arize-tracing.ts", target, "plugin_ts")
