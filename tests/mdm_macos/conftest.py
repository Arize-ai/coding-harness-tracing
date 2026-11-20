"""Fakes for scripts/mdm/macos: root, macOS, the system folders, user accounts, uv, the wheel and Iru."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

import pytest

from tests._pyproject import REPO_ROOT
from tests.core.test_system_install import (  # noqa: F401  (re-exported)
    SPAN_LINE,
    _command_paths,
    _set_writable,
    _snapshot,
    _tree,
)
from tests.core.test_wheel_install import _dist_wheel, _run

MDM_DIR = REPO_ROOT / "scripts" / "mdm" / "macos"
INSTALL_SH = MDM_DIR / "install.sh"
UNINSTALL_SH = MDM_DIR / "uninstall.sh"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
TEST_KEY = "mdm-test-key-do-not-print"


def pytest_collection_modifyitems(config, items):
    here = Path(__file__).parent
    skip = pytest.mark.skip(reason="macOS MDM scripts")
    for item in items:
        if sys.platform != "darwin" and here in Path(str(item.fspath)).parents:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Command stubs, first on the script's PATH
# ---------------------------------------------------------------------------

_PY = f"#!{sys.executable}\n"

# ACLs and owners belong to a file, not a path: entries carry (st_dev, st_ino), so a
# file replaced by tmp+mv loses them as on macOS.
_FILE_DB = """import json, os, sys
def load(var):
    path = os.environ[var]
    return json.load(open(path)) if os.path.exists(path) else {}
def save(var, db):
    json.dump(db, open(os.environ[var], "w"))
def ident(path):
    st = os.stat(path)
    return [st.st_dev, st.st_ino]
def get(db, path):
    entry = db.get(os.path.realpath(path))
    if not entry or not os.path.exists(path) or entry["ident"] != ident(path):
        return None
    return entry["value"]
def put(db, path, value):
    db[os.path.realpath(path)] = {"ident": ident(path), "value": value}
"""

_STUBS: Dict[str, str] = {
    # Root and macOS. FAKE_UID / FAKE_UNAME_S / FAKE_UNAME_M override; id -u <name> reads FAKE_USERS.
    "id": _PY
    + """import json, os, sys
args = sys.argv[1:]
uid = os.environ.get("FAKE_UID", "0")
if args == ["-u"]:
    print(uid)
    sys.exit(0)
if args in (["-un"], ["-nu"], ["-u", "-n"], ["-n", "-u"]) and uid == "0":
    print("root")
    sys.exit(0)
if len(args) == 2 and args[0] == "-u":
    match = [u for u in json.load(open(os.environ["FAKE_USERS"])) if u["name"] == args[1]]
    if not match:
        print(f"id: {args[1]}: no such user", file=sys.stderr)
        sys.exit(1)
    print(match[0]["UniqueID"])
    sys.exit(0)
os.execv("/usr/bin/id", ["id", *args])
""",
    "uname": """#!/bin/bash
case "$*" in
  -s) echo "${FAKE_UNAME_S:-Darwin}" ;;
  -m) if [ -n "${FAKE_UNAME_M:-}" ]; then echo "$FAKE_UNAME_M"; else /usr/bin/uname -m; fi ;;
  *) exec /usr/bin/uname "$@" ;;
esac
""",
    # dscl . -list /Users [Key] | dscl . -read /Users/<name> [Key...], from FAKE_USERS (JSON).
    "dscl": _PY
    + """import json, os, sys
users = json.load(open(os.environ["FAKE_USERS"]))
if "-plist" in sys.argv:
    print("dscl stub: -plist unsupported", file=sys.stderr)
    sys.exit(1)
args = [a for a in sys.argv[1:] if a not in (".", "localhost", "-q")]
if args[:2] == ["-list", "/Users"]:
    key = args[2] if len(args) > 2 else None
    for u in users:
        if key is None:
            print(u["name"])
        elif key in u:
            print(f"{u['name']:<23} {u[key]}")
    sys.exit(0)
if len(args) >= 2 and args[0] == "-read" and args[1].startswith("/Users/"):
    name = args[1][len("/Users/"):]
    match = [u for u in users if u["name"] == name]
    if not match:
        print("<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)", file=sys.stderr)
        sys.exit(56)
    keys = sorted(args[2:] or ["RecordName", "UniqueID", "NFSHomeDirectory", "UserShell"])
    missing = [k for k in keys if k not in match[0]]
    for key in keys:
        if key in match[0]:
            print(f"{key}: {match[0][key]}")
    if missing:
        print(f"No such key: {missing[0]}", file=sys.stderr)
    sys.exit(0)
print(f"dscl stub: unsupported {sys.argv[1:]}", file=sys.stderr)
sys.exit(1)
""",
    # sudo [-u user] [-H] [-n] [--] cmd...: like macOS sudo, the env is reset (HOME kept unless
    # -H); anything else must come through argv, e.g. `env VAR=value`.
    "sudo": _PY
    + """import json, os, sys
users = {u["name"]: u for u in json.load(open(os.environ["FAKE_USERS"]))}
args, user, set_home = sys.argv[1:], "root", False
while args and args[0].startswith("-"):
    flag = args.pop(0)
    if flag == "--":
        break
    if flag == "-u":
        user = args.pop(0)
    elif flag.startswith("-u"):
        user = flag[2:]
    elif flag == "-H":
        set_home = True
    elif flag not in ("-n",):
        print(f"sudo stub: unsupported flag {flag}", file=sys.stderr)
        sys.exit(1)
if user not in users:
    print(f"sudo: unknown user {user}", file=sys.stderr)
    sys.exit(1)
with open(os.environ["FAKE_CALL_LOG"], "a") as log:
    log.write(json.dumps({"cmd": "sudo", "user": user, "set_home": set_home, "argv": args}) + "\\n")
keep = ("PATH", "LANG", "TERM", "HOME")
env = {k: v for k, v in os.environ.items() if k in keep or k.startswith(("LC_", "FAKE_"))}
env.update(USER=user, LOGNAME=user, SUDO_USER="root", FAKE_UID=str(users[user]["UniqueID"]))
if set_home:
    env["HOME"] = users[user]["NFSHomeDirectory"]
os.execvpe(args[0], args, env)
""",
    # launchctl asuser <uid> cmd...: enters the user's session only; still root, env unchanged.
    "launchctl": _PY
    + """import json, os, sys
users = {str(u["UniqueID"]): u for u in json.load(open(os.environ["FAKE_USERS"]))}
args = sys.argv[1:]
if args[:1] != ["asuser"] or len(args) < 3 or args[1] not in users:
    print(f"launchctl stub: unsupported {args}", file=sys.stderr)
    sys.exit(1)
with open(os.environ["FAKE_CALL_LOG"], "a") as log:
    log.write(json.dumps({"cmd": "launchctl", "user": users[args[1]]["name"], "argv": args[2:]}) + "\\n")
os.execvp(args[2], args[2:])
""",
    # chmod +a/-a/=a/-N record ACLs in FAKE_ACL_DB; other modes go to /bin/chmod.
    "chmod": _PY
    + _FILE_DB
    + """args = sys.argv[1:]
if args and args[0] in ("+a", "-a", "=a", "-N"):
    db = load("FAKE_ACL_DB")
    op, entry, paths = (args[0], None, args[1:]) if args[0] == "-N" else (args[0], args[1], args[2:])
    for p in paths:
        if not os.path.exists(p):
            print(f"chmod: {p}: No such file or directory", file=sys.stderr)
            sys.exit(1)
        acl = list(get(db, p) or [])
        if op == "-N":
            acl = []
        elif op == "+a" and entry not in acl:
            acl.append(entry)
        elif op == "-a":
            acl = [e for e in acl if e != entry]
        elif op == "=a":
            acl = [entry]
        put(db, p, acl)
    save("FAKE_ACL_DB", db)
    sys.exit(0)
os.execv("/bin/chmod", ["chmod", *args])
""",
    # chown records the owner in FAKE_OWNER_DB, since tests don't run as root.
    "chown": _PY
    + _FILE_DB
    + """args = [a for a in sys.argv[1:] if not a.startswith("-")]
owner, paths = args[0], args[1:]
db = load("FAKE_OWNER_DB")
user, _, group = owner.partition(":")
for p in paths:
    if not os.path.exists(p):
        print(f"chown: {p}: No such file or directory", file=sys.stderr)
        sys.exit(1)
    old = get(db, p) or {}
    put(db, p, {"user": user or old.get("user"), "group": group or old.get("group")})
save("FAKE_OWNER_DB", db)
""",
    # ls -e (as in ls -le, -led) prints recorded ACL lines like macOS after each -l line.
    "ls": _PY
    + _FILE_DB
    + """import subprocess
args = sys.argv[1:]
flags = "".join(a[1:] for a in args if a.startswith("-") and a != "--")
if "e" not in flags:
    os.execv("/bin/ls", ["ls", *args])
db = load("FAKE_ACL_DB")
rc = 0
for p in [a for a in args if not a.startswith("-")]:
    r = subprocess.run(["/bin/ls", *[a for a in args if a.startswith("-")], p], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    rc = rc or r.returncode
    for i, entry in enumerate(get(db, p) or [] if os.path.exists(p) else []):
        who, _, rest = entry.partition(" ")
        print(f" {i}: user:{who} {rest}")
sys.exit(rc)
""",
    # stat -f FMT: %Su / %Sg come from FAKE_OWNER_DB when chown recorded them.
    "stat": _PY
    + _FILE_DB
    + """args = sys.argv[1:]
if len(args) == 3 and args[0] == "-f" and os.path.exists(args[2]):
    owner = get(load("FAKE_OWNER_DB"), args[2])
    if owner:
        fmt = args[1].replace("%Su", owner.get("user") or "%Su").replace("%Sg", owner.get("group") or "%Sg")
        args = ["-f", fmt, args[2]]
os.execv("/usr/bin/stat", ["stat", *args])
""",
    # PlistBuddy -c "Print :Key" <file>, read with plistlib.
    "PlistBuddy": _PY
    + """import plistlib, sys
args = sys.argv[1:]
if len(args) != 3 or args[0] != "-c" or not args[1].startswith("Print :"):
    print(f"PlistBuddy stub: unsupported {args}", file=sys.stderr)
    sys.exit(1)
key, path = args[1][len("Print :"):], args[2]
try:
    data = plistlib.load(open(path, "rb"))
except FileNotFoundError:
    print(f"File Doesn't Exist, Will Create: {path}")
    sys.exit(1)
if key not in data:
    print(f'Print: Entry, ":{key}", Does Not Exist', file=sys.stderr)
    sys.exit(1)
print(data[key])
""",
}


@pytest.fixture(scope="session")
def stubs_dir(tmp_path_factory) -> Path:
    """Folder holding every command stub, made executable."""
    path = tmp_path_factory.mktemp("stubs")
    for name, body in _STUBS.items():
        stub = path / name
        stub.write_text(body)
        stub.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@dataclass
class FakeUser:
    name: str
    uid: int
    home: Path


@dataclass
class Machine:
    """A fake Mac: system root, accounts, Iru plist and the env the MDM scripts run with."""

    root: Path
    work: Path
    stubs: Path
    users: List[FakeUser]
    accounts_file: Path
    call_log: Path
    acl_db: Path
    owner_db: Path
    iru_plist: Path

    @property
    def credentials_dir(self) -> Path:
        return self.root / "Library" / "Application Support" / "Arize" / "harness"

    @property
    def arize_dir(self) -> Path:
        return self.root / "opt" / "arize"

    @property
    def bin_dir(self) -> Path:
        return self.root / "usr" / "local" / "bin"

    @property
    def harness_bin(self) -> Path:
        return self.bin_dir / "arize-harness"

    def user(self, name: str) -> FakeUser:
        return next(u for u in self.users if u.name == name)

    def calls(self) -> List[dict]:
        """Every sudo / launchctl call the stubs saw."""
        if not self.call_log.exists():
            return []
        return [json.loads(line) for line in self.call_log.read_text().splitlines()]

    def acls(self) -> Dict[str, List[str]]:
        return json.loads(self.acl_db.read_text()) if self.acl_db.exists() else {}

    def owners(self) -> Dict[str, dict]:
        return json.loads(self.owner_db.read_text()) if self.owner_db.exists() else {}

    def seal(self) -> None:
        """Make the system install read-only, as root-owned folders are to users (unsealed at teardown)."""
        _set_writable(self.arize_dir, False)

    def write_iru_plist(self, **values: str) -> Path:
        with open(self.iru_plist, "wb") as f:
            plistlib.dump(values, f)
        return self.iru_plist

    def env(self, **overrides: Optional[str]) -> Dict[str, str]:
        """The scripts' environment: stubs first, seams on temp folders; a None override unsets."""
        env = {
            "PATH": f"{self.stubs}:{SYSTEM_PATH}",
            "HOME": str(self.work / "root-home"),
            "TMPDIR": str(self.work / "tmp"),
            "LANG": "en_US.UTF-8",
            "FAKE_USERS": str(self.accounts_file),
            "FAKE_CALL_LOG": str(self.call_log),
            "FAKE_ACL_DB": str(self.acl_db),
            "FAKE_OWNER_DB": str(self.owner_db),
            "ARIZE_MDM_SYSTEM_ROOT": str(self.root),
            "ARIZE_MDM_CREDENTIALS_DIR": str(self.credentials_dir),
            "ARIZE_MDM_IRU_PLIST": str(self.iru_plist),
            "ARIZE_MDM_PLISTBUDDY": str(self.stubs / "PlistBuddy"),
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, script: Path, timeout: int = 600, **overrides: Optional[str]) -> subprocess.CompletedProcess:
        """Run ``script`` with stock macOS bash 3.2."""
        return subprocess.run(
            ["/bin/bash", str(script)],
            env=self.env(**overrides),
            cwd=self.work,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )


def _account(name: str, uid: int, home: Path, shell: str = "/bin/zsh") -> dict:
    return {"name": name, "RecordName": name, "UniqueID": uid, "NFSHomeDirectory": str(home), "UserShell": shell}


@pytest.fixture
def machine(tmp_path, stubs_dir) -> Iterator[Machine]:
    """Two real users with temp homes, plus accounts the scripts must skip."""
    root = tmp_path / "root"
    root.mkdir()
    work = tmp_path / "work"
    for sub in ("root-home", "tmp"):
        (work / sub).mkdir(parents=True)
    homes = tmp_path / "Users"
    users = []
    for name, uid in (("alice", 501), ("bob", 502)):
        home = homes / name
        home.mkdir(parents=True)
        users.append(FakeUser(name, uid, home))
    accounts = [_account(u.name, u.uid, u.home) for u in users]
    accounts += [
        _account("root", 0, tmp_path / "var-root", "/bin/sh"),
        _account("daemon", 1, tmp_path / "var-root", "/usr/bin/false"),
        _account("_spotlight", 89, tmp_path / "var-empty", "/usr/bin/false"),
        _account("_svcadmin", 600, homes / "_svcadmin"),  # _-prefixed, UID >= 501
        _account("ghost", 503, homes / "ghost"),  # no home folder
        _account("svcdaemon", 504, homes / "svcdaemon", "/usr/bin/false"),  # service shell
    ]
    for home in ("var-root", "var-empty", "Users/_svcadmin", "Users/svcdaemon"):
        (tmp_path / home).mkdir(parents=True, exist_ok=True)
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps(accounts))
    m = Machine(
        root=root,
        work=work,
        stubs=stubs_dir,
        users=users,
        accounts_file=accounts_file,
        call_log=tmp_path / "calls.jsonl",
        acl_db=tmp_path / "acl.json",
        owner_db=tmp_path / "owners.json",
        iru_plist=tmp_path / "io.kandji.globalvariables.plist",
    )
    yield m
    if m.arize_dir.exists():
        _set_writable(m.arize_dir, True)


# ---------------------------------------------------------------------------
# uv, Python and the wheel
# ---------------------------------------------------------------------------


@dataclass
class UvRelease:
    tarball: Path
    sha256: str


def _uv_target() -> str:
    arch = subprocess.run(["/usr/bin/uname", "-m"], capture_output=True, text=True, check=True).stdout.strip()
    return {"arm64": "aarch64-apple-darwin", "x86_64": "x86_64-apple-darwin"}.get(arch, f"{arch}-unknown")


@pytest.fixture(scope="session")
def uv_release(tmp_path_factory) -> UvRelease:
    """This machine's uv, packed like a uv release tarball, with its SHA-256."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("needs uv")
    out = tmp_path_factory.mktemp("uv-release")
    target = _uv_target()
    tarball = out / f"uv-{target}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(os.path.realpath(uv), arcname=f"uv-{target}/uv")
    return UvRelease(tarball, hashlib.sha256(tarball.read_bytes()).hexdigest())


@pytest.fixture(scope="session")
def wheelhouse(tmp_path_factory) -> Path:
    """This checkout built into a wheel, next to its runtime dependencies' wheels."""
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("needs uv and git")
    src = tmp_path_factory.mktemp("src") / "repo"
    files = _run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], REPO_ROOT).stdout
    for name in filter(None, files.split("\0")):
        a, b = REPO_ROOT / name, src / name
        if not (a.is_file() or a.is_symlink()):
            continue
        b.parent.mkdir(parents=True, exist_ok=True)
        if a.is_symlink():
            os.symlink(os.readlink(a), b)
        else:
            shutil.copy2(a, b)
    out = tmp_path_factory.mktemp("wheels")
    env = dict(os.environ, SETUPTOOLS_SCM_PRETEND_VERSION="9.9.9")
    _run(["uv", "build", "--quiet", "--wheel", "--out-dir", str(out), str(src)], src, env)
    for project in ("python-dotenv", "json5"):
        _dist_wheel(project, out)
    return out


@pytest.fixture(scope="session")
def wheel(wheelhouse) -> Path:
    (whl,) = wheelhouse.glob("coding_harness_tracing-*.whl")
    return whl


@pytest.fixture(scope="session")
def managed_python(tmp_path_factory) -> Path:
    """A uv-managed CPython 3.12 folder to seed the system Python dir, so installs stay offline."""
    found = subprocess.run(
        ["uv", "python", "find", "--managed-python", "3.12"], capture_output=True, text=True, check=False
    )
    if found.returncode != 0:
        seed = tmp_path_factory.mktemp("python-seed")
        env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(seed))
        _run(["uv", "python", "install", "3.12"], seed, env)
        found = _run(["uv", "python", "find", "--managed-python", "3.12"], seed, env)
    exe = Path(found.stdout.strip()).resolve()
    return next(p for p in exe.parents if p.name.startswith("cpython-"))


@pytest.fixture
def install_env(machine, uv_release, wheel, wheelhouse, managed_python) -> Callable[..., Dict[str, str]]:
    """Overrides for a valid, offline install.sh run; keyword arguments replace or (None) unset them."""
    python_dir = machine.arize_dir / "python"
    python_dir.mkdir(parents=True)
    shutil.copytree(managed_python, python_dir / managed_python.name, symlinks=True)
    cache = machine.work / "uv-cache"

    def build(**overrides: Optional[str]) -> Dict[str, str]:
        env: Dict[str, Optional[str]] = {
            "ARIZE_API_KEY": TEST_KEY,
            "ARIZE_SPACE_ID": "mdm-test-space",
            "ARIZE_MDM_PACKAGE_SOURCE": str(wheel),
            "ARIZE_MDM_UV_SOURCE": str(uv_release.tarball),
            "ARIZE_MDM_TEST_UV_SHA256": uv_release.sha256,
            "ARIZE_MDM_HARNESSES": "claude-code cursor codex",
            "UV_CACHE_DIR": str(cache),
            "UV_OFFLINE": "1",
            "UV_FIND_LINKS": str(wheelhouse),
        }
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    return build


@pytest.fixture
def preflight_env(machine) -> Callable[..., Dict[str, str]]:
    """Overrides for a run that must stop in preflight: inputs valid, uv and wheel never reached."""

    def build(**overrides: Optional[str]) -> Dict[str, str]:
        env: Dict[str, Optional[str]] = {
            "ARIZE_API_KEY": TEST_KEY,
            "ARIZE_SPACE_ID": "mdm-test-space",
            "ARIZE_MDM_PACKAGE_SOURCE": str(machine.work / "missing.whl"),
            "ARIZE_MDM_UV_SOURCE": str(machine.work / "missing-uv.tar.gz"),
            "ARIZE_MDM_HARNESSES": "claude-code cursor codex",
            "UV_OFFLINE": "1",
        }
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    return build
