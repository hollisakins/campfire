#!/usr/bin/env python3
"""CAMPFIRE monorepo installer.

Interactive, idempotent setup for the four ways people use this repo:

  client      Basic CLI usage — campfire sync/download/status, Python client
  reduction   JWST data reduction — campfire-pipeline (cfpipe)
  deploy      Publishing data — campfire[deploy] (implies the pipeline)
  web         Web portal development — npm install in web/

Runs on any Python >= 3.8 (stdlib only); it creates or updates the real
environment itself. Re-running is the update path: after `git pull`, run
`python3 install.py` again — previous answers are remembered in
.campfire-install.json (gitignored) and the script converges the environment
without re-asking. Every command executed is appended to .campfire-install.log.

Non-interactive use mirrors every prompt with a flag, e.g.:

  python3 install.py --profiles reduction,deploy --yes
  python3 install.py --doctor          # verify only, install nothing
  python3 install.py --dry-run         # show the plan, run nothing

The pipeline pins its JWST stack exactly (jwst, stcal, drizzle, snowblind in
pipeline/pyproject.toml), because another version is another calibration. When
the environment carries a different stack than this checkout pins — after a
jwst upgrade, on an old release, or in a second worktree — the installer shows
the difference and either upgrades the env in place or builds a sibling env
(`campfire-jwst3`) for this checkout and leaves the first untouched
(`--stack upgrade|new-env`). `--doctor` fails when the env has drifted from the
pins, and notes when it runs another checkout's pipeline. Answers are saved per checkout, so
each worktree remembers its own env.

The script never uses sudo and never touches git (pulling/stashing is yours).
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
STATE_FILE = REPO_ROOT / ".campfire-install.json"
LOG_FILE = REPO_ROOT / ".campfire-install.log"
STATE_VERSION = 1

DEFAULT_ENV_NAME = "campfire"
DEFAULT_PYTHON = "3.12"  # pipeline floor; numba caps at <3.14

PROFILES = {
    "client": "Basic CLI usage (campfire sync/download, Python client)",
    "reduction": "JWST data reduction (campfire-pipeline / cfpipe)",
    "deploy": "Deployment & publication (campfire[deploy]; implies reduction)",
    "web": "Web portal development (npm install in web/)",
}
PY_PROFILES = ("client", "reduction", "deploy")

MICROMAMBA_API = "https://micro.mamba.pm/api/micromamba/{plat}/latest"


# --------------------------------------------------------------------------
# Output / logging
# --------------------------------------------------------------------------

class Ui:
    verbose = False
    assume_yes = False
    dry_run = False

    @staticmethod
    def say(msg=""):
        print(msg)

    @staticmethod
    def note(msg):
        print(f"  \x1b[2m{msg}\x1b[0m" if sys.stdout.isatty() else f"  {msg}")

    @staticmethod
    def warn(msg):
        print(f"\x1b[33m! {msg}\x1b[0m" if sys.stdout.isatty() else f"! {msg}")

    @staticmethod
    def fail(msg):
        print(f"\x1b[31mError: {msg}\x1b[0m" if sys.stderr.isatty() else f"Error: {msg}",
              file=sys.stderr)


def log_line(text):
    with LOG_FILE.open("a") as fh:
        fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {text}\n")


def run(cmd, *, cwd=None, capture=False, check=True, env=None, probe=False):
    """Run a command, logging it. In --dry-run, print instead of running —
    except read-only probes (probe=True: version checks, env listing), which
    execute anyway so the printed plan reflects reality."""
    display = " ".join(str(c) for c in cmd)
    log_line(f"$ {display}" + (f"  (cwd={cwd})" if cwd else ""))
    if Ui.dry_run and not probe:
        Ui.say(f"  [dry-run] {display}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    if Ui.verbose and not capture:
        Ui.note(f"$ {display}")
    result = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if capture and result.stdout:
        log_line(result.stdout.rstrip())
    if check and result.returncode != 0:
        if capture and result.stdout:
            sys.stderr.write(result.stdout)
        raise InstallError(f"command failed ({result.returncode}): {display}\n"
                           f"See {LOG_FILE.name} for the full log.")
    return result


class InstallError(Exception):
    pass


# --------------------------------------------------------------------------
# Prompts (all mirrored by flags; never hang without a TTY)
# --------------------------------------------------------------------------

def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty() and not Ui.assume_yes


def _input(prompt):
    try:
        return input(prompt)
    except EOFError:
        raise InstallError("input stream closed mid-prompt; re-run with --yes "
                           "and flags for non-interactive use.")


def confirm(prompt, default=True):
    if not interactive():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        ans = _input(f"{prompt} {suffix} ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def text_prompt(prompt, default=None):
    if not interactive():
        return default
    suffix = f" [{default}]" if default else ""
    ans = _input(f"{prompt}{suffix}: ").strip()
    return ans or default


def multiselect(title, options, defaults):
    """Numbered checkbox prompt. options: [(key, label)]; returns [key]."""
    if not interactive():
        return list(defaults)
    Ui.say(f"\n{title}")
    keys = [k for k, _ in options]
    for i, (key, label) in enumerate(options, 1):
        mark = "x" if key in defaults else " "
        Ui.say(f"  [{mark}] {i}. {label}")
    Ui.say("Enter numbers to toggle (e.g. '1 3'), or press Enter to accept.")
    selected = set(defaults)
    while True:
        ans = _input("> ").strip()
        if not ans:
            if selected:
                return [k for k in keys if k in selected]
            Ui.say("Nothing selected — pick at least one component.")
            continue
        picks = re.split(r"[,\s]+", ans)
        if not all(p.isdigit() and 1 <= int(p) <= len(options) for p in picks):
            Ui.say(f"Enter numbers 1-{len(options)} separated by spaces.")
            continue
        for p in picks:
            key = keys[int(p) - 1]
            selected.symmetric_difference_update({key})
        for i, (key, label) in enumerate(options, 1):
            mark = "x" if key in selected else " "
            Ui.say(f"  [{mark}] {i}. {label}")


# --------------------------------------------------------------------------
# State (previous answers) — makes bare re-runs the update path
# --------------------------------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text())
        return state if isinstance(state, dict) else {}
    except (json.JSONDecodeError, OSError):
        Ui.warn(f"{STATE_FILE.name} is unreadable — ignoring it.")
        return {}


def save_state(state):
    if Ui.dry_run:
        return
    state["version"] = STATE_VERSION
    state["saved_at"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# --------------------------------------------------------------------------
# Environment managers
# --------------------------------------------------------------------------

def detect_managers():
    """Available conda-family managers, fastest first: [(name, exe_path)]."""
    found = []
    for name in ("micromamba", "mamba", "conda"):
        exe = shutil.which(name)
        if exe:
            found.append((name, exe))
    conda_exe = os.environ.get("CONDA_EXE")
    if conda_exe and Path(conda_exe).exists() and not any(n == "conda" for n, _ in found):
        found.append(("conda", conda_exe))
    return found


def micromamba_platform():
    system, machine = platform.system(), platform.machine().lower()
    if system == "Linux":
        return {"x86_64": "linux-64", "aarch64": "linux-aarch64",
                "ppc64le": "linux-ppc64le"}.get(machine)
    if system == "Darwin":
        return {"x86_64": "osx-64", "arm64": "osx-arm64"}.get(machine)
    return None


def install_micromamba():
    """User-space micromamba into ~/.local/bin. Only ever called after an
    explicit confirmation — never silently."""
    plat = micromamba_platform()
    if not plat:
        raise InstallError("no micromamba build for this platform; install "
                           "conda/mamba/micromamba yourself and re-run.")
    import io
    import tarfile
    import urllib.request
    url = MICROMAMBA_API.format(plat=plat)
    dest = Path.home() / ".local" / "bin" / "micromamba"
    Ui.say(f"Downloading micromamba ({plat}) -> {dest}")
    log_line(f"downloading {url}")
    if Ui.dry_run:
        return str(dest)
    try:
        with urllib.request.urlopen(url) as resp:
            payload = resp.read()
    except OSError as e:
        raise InstallError(
            f"micromamba download failed ({e}). Check your network/proxy, or "
            "install conda/mamba/micromamba yourself "
            "(https://mamba.readthedocs.io) and re-run.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:bz2") as tar:
        member = tar.extractfile("bin/micromamba")
        if member is None:
            raise InstallError("unexpected micromamba archive layout")
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
            shutil.copyfileobj(member, tmp)
            tmp_path = Path(tmp.name)
    tmp_path.chmod(0o755)
    tmp_path.replace(dest)
    if str(dest.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        Ui.warn(f"{dest.parent} is not on your PATH; the installer will use the "
                "absolute path, but add it to your shell profile for daily use.")
    return str(dest)


class CondaEnv:
    """A named conda-family environment, driven by absolute paths — the
    installer never relies on shell activation (`conda run` quirks included)."""

    def __init__(self, manager_name, exe, env_name):
        self.manager = manager_name
        self.exe = exe
        self.name = env_name

    def _env_list(self):
        result = run([self.exe, "env", "list", "--json"], capture=True,
                     check=False, probe=True)
        if result.returncode != 0 or not result.stdout:
            return []
        try:
            return json.loads(result.stdout).get("envs", [])
        except json.JSONDecodeError:
            return []

    @property
    def prefix(self):
        for path in self._env_list():
            if Path(path).name == self.name:
                return Path(path)
        return None

    def exists(self):
        return self.prefix is not None

    def python(self):
        prefix = self.prefix
        if prefix is None:
            return None
        exe = prefix / ("python.exe" if os.name == "nt" else "bin/python")
        return exe if exe.exists() else None

    def create(self, python_version, conda_pkgs):
        run([self.exe, "create", "-y", "-n", self.name, "-c", "conda-forge",
             f"python={python_version}", "pip"] + conda_pkgs)

    def conda_install(self, pkgs):
        run([self.exe, "install", "-y", "-n", self.name, "-c", "conda-forge"] + pkgs)

    def describe(self):
        return f"{self.manager} env '{self.name}'"


class VenvEnv:
    """Plain venv fallback — legitimate for every profile (jwst installs via
    pip; conda-forge numba is a preference, not a requirement)."""

    def __init__(self, path, base_python):
        self.path = Path(path).resolve()
        self.base_python = base_python

    def exists(self):
        return self.python() is not None

    def python(self):
        exe = self.path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return exe if exe.exists() else None

    def create(self, python_version, conda_pkgs):  # signature parity
        run([self.base_python, "-m", "venv", str(self.path)])

    def conda_install(self, pkgs):
        pass  # everything comes from pip in venv mode

    def describe(self):
        return f"venv at {self.path}"


def python_version_of(python_exe):
    result = run([python_exe, "-c",
                  "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                 capture=True, check=False, probe=True)
    if result.returncode != 0 or not result.stdout:
        return None
    return tuple(int(p) for p in result.stdout.strip().split("."))


def required_python(profiles):
    """(floor, ceiling, why). Reduction/deploy carry the pipeline's window."""
    if "reduction" in profiles or "deploy" in profiles:
        return (3, 12), (3, 14), "campfire-pipeline needs >=3.12; numba caps at <3.14"
    return (3, 11), None, "campfire (client) needs >=3.11"


# --------------------------------------------------------------------------
# Calibration stack: the exact pins in pipeline/pyproject.toml vs the env
# --------------------------------------------------------------------------
#
# The pipeline pins jwst (and the libraries that carry its numerics) exactly:
# a different version is a different calibration. An environment is therefore
# tied to one stack, and a checkout that pins another one (a jwst upgrade, an
# old release, a worktree of either) must not silently repoint an environment
# other checkouts still reduce data with. The installer compares the two and
# either converges the env in place or builds a sibling env for this checkout.

_PIN_RE = re.compile(r'^\s*"([A-Za-z0-9_.\-]+)==([^"\s;]+)"', re.MULTILINE)

_STACK_PROBE = """
import json, sys
from importlib import metadata as m
versions = {}
for name in sys.argv[1:]:
    try:
        versions[name] = m.version(name)
    except m.PackageNotFoundError:
        versions[name] = None
url = None
try:
    text = m.distribution('campfire-pipeline').read_text('direct_url.json')
    if text:
        url = json.loads(text).get('url')
except m.PackageNotFoundError:
    pass
print('STACK ' + json.dumps({'versions': versions, 'pipeline_url': url}))
"""


def pinned_stack():
    """{package: version} for every exact pin in pipeline/pyproject.toml."""
    try:
        text = (REPO_ROOT / "pipeline" / "pyproject.toml").read_text()
    except OSError:
        return {}
    return dict(_PIN_RE.findall(text))


def installed_stack(env, names):
    """(versions, pipeline_source) as the env has them; ({}, None) if unknown.

    ``pipeline_source`` is the checkout an editable campfire-pipeline points
    at, or None for a regular / missing install."""
    py = env.python() if env is not None and env.exists() else None
    if py is None or not names:
        return {}, None
    result = run([py, "-c", _STACK_PROBE] + list(names), capture=True,
                 check=False, probe=True)
    for line in reversed((result.stdout or "").splitlines()):
        if line.startswith("STACK "):
            try:
                data = json.loads(line[len("STACK "):])
            except json.JSONDecodeError:
                break
            url = data.get("pipeline_url") or ""
            source = None
            if url.startswith("file://"):
                from urllib.parse import unquote, urlparse
                source = Path(unquote(urlparse(url).path))
            return data.get("versions", {}), source
    return {}, None


def stack_status(env):
    """Compare the env with this checkout's pins.

    Returns {'drift': [(package, installed, pinned)], 'other_checkout': Path
    or None, 'has_stack': bool}. ``drift`` lists only packages that are
    installed at another version; a package that is simply missing is an
    ordinary first install. ``other_checkout`` is set when the env's editable
    pipeline belongs to a different checkout that still exists."""
    pins = pinned_stack()
    versions, source = installed_stack(env, pins)
    drift = [(name, versions.get(name), want) for name, want in pins.items()
             if versions.get(name) and versions.get(name) != want]
    other = None
    if source is not None:
        here = (REPO_ROOT / "pipeline").resolve()
        try:
            same = source.resolve() == here
        except OSError:
            same = False
        if not same and source.exists():
            other = source
    return {"drift": drift, "other_checkout": other,
            "has_stack": any(versions.values())}


def stack_env_suffix():
    """'-jwst3' for a checkout that pins jwst 3.x; '' when jwst is unpinned."""
    jwst = pinned_stack().get("jwst")
    return f"-jwst{jwst.split('.')[0]}" if jwst else ""


def sibling_env(env, sel):
    """The stack-specific environment next to ``env`` (same manager)."""
    suffix = stack_env_suffix() or "-alt"
    if isinstance(env, CondaEnv):
        name = env.name if env.name.endswith(suffix) else env.name + suffix
        sel["env_name"] = name
        return CondaEnv(env.manager, env.exe, name)
    path = str(env.path) if str(env.path).endswith(suffix) else str(env.path) + suffix
    sel["venv_path"] = path
    return VenvEnv(path, env.base_python)


def resolve_stack(env, sel, args):
    """Decide what to do when the env's JWST stack is not this checkout's.

    Returns the environment to install into (the same one, or a sibling) and
    records a one-line description in ``sel['stack_note']`` for the plan."""
    sel["stack_note"] = None
    if env is None or "reduction" not in sel["profiles"] or not env.exists():
        return env
    status = stack_status(env)
    if not status["has_stack"] or not (status["drift"] or status["other_checkout"]):
        return env
    if args.recreate_env:
        return env  # the env is about to be rebuilt from this checkout's pins

    changes = ", ".join(f"{n} {have} -> {want}" for n, have, want in status["drift"])
    Ui.say("")
    if status["drift"]:
        Ui.warn(f"{env.describe()} carries a different JWST stack than this "
                "checkout pins (a different stack is a different calibration):")
        width = max(len(n) for n, _, _ in status["drift"])
        for name, have, want in status["drift"]:
            Ui.say(f"    {name:<{width}}  {have}  ->  {want}")
    if status["other_checkout"]:
        Ui.warn(f"its campfire-pipeline is installed from another checkout: "
                f"{status['other_checkout']}")

    sibling = sibling_env(env, dict(sel))  # peek at the name without committing
    choice = args.stack
    if choice is None:
        # A second checkout is still using the env: leave it alone by default.
        default = "new-env" if status["other_checkout"] else "upgrade"
        if interactive():
            Ui.say(f"  upgrade — converge {env.describe()} in place"
                   + (" (the other checkout then runs on this stack and this code)"
                      if status["other_checkout"] else ""))
            Ui.say(f"  new-env — leave it untouched; use {sibling.describe()} "
                   "for this checkout")
            Ui.say("  abort   — change nothing")
            while True:
                choice = text_prompt("JWST stack (upgrade/new-env/abort)", default)
                if choice in ("upgrade", "new-env", "abort"):
                    break
        else:
            choice = "upgrade"
            Ui.warn("non-interactive: upgrading in place (pass --stack new-env "
                    "to build a separate environment instead).")

    if choice == "abort":
        raise InstallError("stack change declined — nothing was changed.")
    if choice == "new-env":
        target = sibling_env(env, sel)
        if target.exists():
            again = stack_status(target)["drift"]
            sel["stack_note"] = (
                f"Reuse {target.describe()} for this checkout's JWST stack"
                + (" (converging: " + ", ".join(f"{n} {h} -> {w}" for n, h, w in again) + ")"
                   if again else ""))
        else:
            sel["stack_note"] = (f"Leave {env.describe()} untouched; build "
                                 f"{target.describe()} for this checkout's JWST stack")
        return target
    sel["stack_note"] = (f"Change the JWST stack of {env.describe()} in place"
                         + (f": {changes}" if changes else "")
                         + ("; repoint campfire-pipeline to this checkout"
                            if status["other_checkout"] else ""))
    return env


# --------------------------------------------------------------------------
# Selection resolution: flags > saved state > prompts
# --------------------------------------------------------------------------

def resolve_selections(args, state):
    sel = {}

    if args.profiles:
        profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
        unknown = [p for p in profiles if p not in PROFILES]
        if unknown:
            raise InstallError(f"unknown profile(s) {unknown}; "
                               f"choose from {', '.join(PROFILES)}")
    elif state.get("profiles"):
        profiles = [p for p in state["profiles"] if p in PROFILES]
        if interactive():
            Ui.say(f"Previously selected: {', '.join(profiles)} "
                   f"(from {STATE_FILE.name})")
            profiles = multiselect("Select components to install/update:",
                                   list(PROFILES.items()), profiles)
    elif interactive():
        profiles = multiselect("Select components to install/update:",
                               list(PROFILES.items()), ["client"])
    else:
        raise InstallError("no TTY and no saved selections: pass "
                           "--profiles (e.g. --profiles client,web) with --yes.")

    if "deploy" in profiles and "reduction" not in profiles:
        # campfire[deploy] depends on campfire-pipeline (not on PyPI), so the
        # pipeline must be installed editable alongside it regardless.
        profiles.append("reduction")
        Ui.note("deploy implies reduction (campfire[deploy] carries the pipeline).")
    sel["profiles"] = [p for p in PROFILES if p in profiles]

    if sys.platform == "win32" and ("reduction" in profiles or "deploy" in profiles):
        raise InstallError("the reduction/deploy profiles need the JWST stack, "
                           "which does not support native Windows — use WSL. "
                           "client/web work natively.")

    sel["editable"] = (not args.no_editable if args.no_editable is not None
                       else state.get("editable", True))

    if args.dev is not None:
        sel["dev"] = args.dev
    elif "dev" in state:
        sel["dev"] = state["dev"]
    else:
        sel["dev"] = (interactive()
                      and confirm("Include dev extras (pytest, black, mypy)?", False))

    # Client plotting extras ([all] = plotly + specutils)
    if any(p in sel["profiles"] for p in PY_PROFILES):
        if args.extras:
            sel["extras"] = args.extras
        elif "extras" in state:
            sel["extras"] = state["extras"]
        elif "client" in sel["profiles"]:
            sel["extras"] = "all" if confirm(
                "Include interactive plotting extras (plotly, specutils)?", True) else "none"
        else:
            sel["extras"] = "none"
    else:
        sel["extras"] = "none"

    # fitsgl mode (deploy only)
    if "deploy" in sel["profiles"]:
        sibling = (REPO_ROOT.parent / "fitsgl" / "fitsgl-py" / "pyproject.toml").exists()
        if args.fitsgl:
            sel["fitsgl"] = args.fitsgl
        elif "fitsgl" in state:
            sel["fitsgl"] = state["fitsgl"]
        elif interactive():
            default = "sibling" if sibling else "skip"
            Ui.say("\nFitsGL producer (map/cutout tile deploys):")
            Ui.say(f"  sibling — editable from ../fitsgl/fitsgl-py "
                   f"({'detected' if sibling else 'NOT found'})")
            Ui.say("  git     — pinned git dependency (reproducible fallback)")
            Ui.say("  skip    — not deploying tiles from this machine")
            while True:
                ans = text_prompt("fitsgl mode (sibling/git/skip)", default)
                if ans in ("sibling", "git", "skip"):
                    sel["fitsgl"] = ans
                    break
        else:
            sel["fitsgl"] = "sibling" if sibling else "skip"
        if sel["fitsgl"] == "sibling" and not sibling:
            raise InstallError("fitsgl mode 'sibling' but ../fitsgl/fitsgl-py not "
                               "found — clone https://github.com/hollisakins/fitsgl "
                               "next to this repo, or use --fitsgl git|skip.")
    else:
        sel["fitsgl"] = "skip"

    sel["env_name"] = args.env_name or state.get("env_name", DEFAULT_ENV_NAME)
    sel["python"] = args.python or state.get("python", DEFAULT_PYTHON)
    sel["manager"] = args.manager or state.get("manager")  # resolved later
    sel["venv_path"] = args.venv_path or state.get("venv_path", str(REPO_ROOT / ".venv"))
    return sel


def resolve_env(sel, args):
    """Pick/prepare the environment manager. Returns CondaEnv or VenvEnv."""
    needs_py_env = any(p in sel["profiles"] for p in PY_PROFILES)
    if not needs_py_env:
        return None

    if sel["manager"] == "venv":
        # In venv mode --python may be an interpreter *path* (a venv can't pick
        # a version out of thin air); otherwise the venv is built from the
        # interpreter running this script, validated in prepare_env.
        base = sel["python"] if os.sep in str(sel["python"]) else sys.executable
        return VenvEnv(sel["venv_path"], base)

    managers = detect_managers()
    if sel["manager"] in ("conda", "mamba", "micromamba"):
        match = [m for m in managers if m[0] == sel["manager"]]
        if not match:
            raise InstallError(f"--manager {sel['manager']} requested but not found "
                               "on PATH.")
        name, exe = match[0]
        return CondaEnv(name, exe, sel["env_name"])

    if managers:
        name, exe = managers[0]
        if len(managers) > 1:
            Ui.note(f"Found {', '.join(n for n, _ in managers)}; using {name}.")
        sel["manager"] = name
        return CondaEnv(name, exe, sel["env_name"])

    # No conda-family manager at all.
    Ui.warn("No conda, mamba, or micromamba found on PATH.")
    if interactive() and micromamba_platform() and confirm(
            "Download micromamba (user-space, ~/.local/bin, no sudo)?", False):
        exe = install_micromamba()
        sel["manager"] = "micromamba"
        return CondaEnv("micromamba", exe, sel["env_name"])
    if interactive() and confirm(
            f"Fall back to a plain venv at {sel['venv_path']}?", True):
        sel["manager"] = "venv"
        return VenvEnv(sel["venv_path"], sys.executable)
    raise InstallError(
        "no environment manager available. Either install conda/mamba/micromamba "
        "(https://mamba.readthedocs.io), re-run with --manager venv, or re-run "
        "interactively to be offered a user-space micromamba download.")


def prepare_env(env, sel, args):
    """Create the env if missing; sanity-check Python if present."""
    floor, ceiling, why = required_python(sel["profiles"])
    conda_pkgs = ["numba"] if isinstance(env, CondaEnv) and (
        "reduction" in sel["profiles"]) else []

    if not env.exists():
        if isinstance(env, VenvEnv):
            base_version = python_version_of(env.base_python)
            if base_version is None and not Ui.dry_run:
                raise InstallError(f"cannot run {env.base_python} to build the venv.")
            if base_version and (base_version[:2] < floor or
                                 (ceiling and base_version[:2] >= ceiling)):
                label = ".".join(map(str, base_version))
                raise InstallError(
                    f"venv would use python {label} ({env.base_python}), but {why}. "
                    "Point --python at a suitable interpreter path, or use conda.")
            Ui.say(f"Creating {env.describe()} from {env.base_python}")
            env.create(sel["python"], conda_pkgs)
            return
        want = sel["python"]
        want_tuple = tuple(int(p) for p in want.split(".")[:2])
        if want_tuple < floor or (ceiling and want_tuple >= ceiling):
            raise InstallError(f"--python {want} is outside the supported window "
                               f"({why}).")
        Ui.say(f"Creating {env.describe()} (python={want}"
               + (f", conda-forge {' '.join(conda_pkgs)}" if conda_pkgs else "") + ")")
        env.create(want, conda_pkgs)
        return

    py = env.python()
    version = python_version_of(py) if py else None
    if version is None:
        if Ui.dry_run:
            return
        raise InstallError(f"{env.describe()} exists but has no working python — "
                           "recreate it with --recreate-env.")
    v2 = version[:2]
    ok = v2 >= floor and (ceiling is None or v2 < ceiling)
    label = ".".join(map(str, version))
    if ok:
        Ui.note(f"Reusing {env.describe()} (python {label}).")
        if conda_pkgs:
            probe = run([env.python(), "-c", "import numba"], capture=True, check=False)
            if probe.returncode != 0:
                Ui.note("Adding numba from conda-forge.")
                env.conda_install(conda_pkgs)
        return
    Ui.warn(f"{env.describe()} has python {label}, but {why}.")
    if args.recreate_env or confirm("Recreate the environment? (destroys its "
                                    "current contents)", False):
        if isinstance(env, CondaEnv):
            run([env.exe, "env", "remove", "-y", "-n", env.name])
        else:
            if not Ui.dry_run:
                shutil.rmtree(env.path, ignore_errors=True)
        env.create(sel["python"], conda_pkgs)
    else:
        raise InstallError("cannot proceed with an incompatible python; re-run "
                           "with --recreate-env or a different --env-name.")


def recreate_requested(env, sel, args):
    if not args.recreate_env or not env or not env.exists():
        return
    if not Ui.assume_yes and not confirm(
            f"--recreate-env: destroy and recreate {env.describe()}?", False):
        raise InstallError("recreate declined; drop --recreate-env to update in place.")
    Ui.say(f"Removing {env.describe()}")
    if isinstance(env, CondaEnv):
        run([env.exe, "env", "remove", "-y", "-n", env.name])
    else:
        if not Ui.dry_run:
            shutil.rmtree(env.path, ignore_errors=True)


# --------------------------------------------------------------------------
# Install steps
# --------------------------------------------------------------------------

def pip_install(env, target, editable):
    py = env.python()
    if py is None and Ui.dry_run:
        py = "<env-python>"
    cmd = [py, "-m", "pip", "install"]
    if editable:
        cmd.append("-e")
    cmd.append(target)
    # Targets are repo-relative (./layout, ../fitsgl/...): anchor at the repo.
    run(cmd, cwd=REPO_ROOT)


def build_py_targets(sel):
    """Ordered pip targets. Order is a hard contract: layout first, then the
    pipeline, then (after any sibling fitsgl) the client, so the plain-name
    campfire-layout / campfire-pipeline / fitsgl deps resolve locally instead
    of hitting PyPI/git."""
    profiles = sel["profiles"]
    targets = []
    if any(p in profiles for p in PY_PROFILES):
        targets.append("./layout")
        if sel["dev"]:
            targets[-1] = "./layout[test]"
    if "reduction" in profiles:
        targets.append("./pipeline")
    if sel["fitsgl"] == "sibling":
        targets.append(str(Path("..") / "fitsgl" / "fitsgl-py") + "[deploy]")
    if "client" in profiles or "deploy" in profiles:
        extras = []
        if sel["extras"] == "all":
            extras.append("all")
        if "deploy" in profiles:
            extras.append("deploy")
        if sel["fitsgl"] == "git":
            extras.append("fitsgl")
        if sel["dev"]:
            extras.append("dev")
        suffix = f"[{','.join(extras)}]" if extras else ""
        targets.append(f"./python{suffix}")
    return targets


def check_node():
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise InstallError("the web profile needs Node.js >= 18.18 and npm on "
                           "PATH — install via https://nodejs.org or nvm, then "
                           "re-run (only the web step is affected).")
    out = run([node, "--version"], capture=True, probe=True).stdout.strip().lstrip("v")
    parts = tuple(int(p) for p in out.split(".")[:2])
    if parts < (18, 18):
        raise InstallError(f"Node.js {out} found, but Next.js 15 needs >= 18.18.")
    return out


def preflight(sel):
    problems = []
    if "reduction" in sel["profiles"] or sel["fitsgl"] == "git":
        if not shutil.which("git"):
            problems.append("git is required (bagpipes/fitsgl install from git URLs) "
                            "but is not on PATH.")
    for rel in ("layout", "pipeline", "python"):
        if not (REPO_ROOT / rel / "pyproject.toml").exists():
            problems.append(f"{rel}/pyproject.toml missing — run from a full "
                            "checkout of the campfire repo.")
    if problems:
        raise InstallError("\n".join(problems))


# --------------------------------------------------------------------------
# Doctor — verification, also standalone via --doctor
# --------------------------------------------------------------------------

def doctor(env, sel):
    """Probe the installed components; return True when everything passes."""
    checks = []  # (name, ok, detail)

    def probe_import(name, module, extra_code=""):
        py = env.python() if env else None
        if py is None:
            checks.append((name, False, "environment missing"))
            return
        code = f"import {module}; {extra_code}" if extra_code else f"import {module}"
        result = run([py, "-c", code], capture=True, check=False)
        if result.returncode == 0:
            detail = ""
        else:
            lines = (result.stdout or "").strip().splitlines()
            detail = lines[-1] if lines else "import failed"
        checks.append((name, result.returncode == 0, detail))

    def probe_cli(name, script):
        py = env.python() if env else None
        if py is None:
            checks.append((name, False, "environment missing"))
            return
        exe = Path(py).parent / script
        result = run([exe, "--version"], capture=True, check=False) \
            if exe.exists() else None
        if result is None:
            checks.append((name, False, f"{script} not on the env's bin path"))
        else:
            detail = (result.stdout or "").strip().splitlines()[0] if result.stdout else ""
            checks.append((name, result.returncode == 0, detail))

    profiles = sel["profiles"]
    if any(p in profiles for p in PY_PROFILES):
        probe_import("campfire-layout", "campfire_layout")
    if "client" in profiles or "deploy" in profiles:
        probe_import("campfire (client)", "campfire")
        probe_cli("campfire CLI", "campfire")
    if "reduction" in profiles:
        probe_import("campfire-pipeline", "campfire_pipeline")
        probe_cli("cfpipe CLI", "cfpipe")
        probe_import("jwst stack", "jwst")
        probe_import("numba", "numba")
        # The pins are the calibration: an env that drifted from them reduces
        # data with a stack this checkout was not validated against.
        status = stack_status(env)
        pins = pinned_stack()
        if pins:
            drift = "; ".join(f"{n} {have} installed, {want} pinned"
                              for n, have, want in status["drift"])
            checks.append((f"JWST stack matches the pins "
                           f"({', '.join(f'{n} {v}' for n, v in pins.items())})",
                           status["has_stack"] and not status["drift"],
                           drift or ("" if status["has_stack"] else "not installed")))
        # Sharing one env between worktrees is normal while their pins agree,
        # so this is a hint, not a failure.
        stack_hint = (f"the env's campfire-pipeline is another checkout "
                      f"({status['other_checkout']}): cfpipe runs that code, not "
                      "this one." if status["other_checkout"] else None)
    if "deploy" in profiles:
        probe_import("deploy round-trips (pipeline in env)", "campfire_pipeline")
        probe_import("supabase client", "supabase")
        if sel["fitsgl"] != "skip":
            probe_import("fitsgl producer", "fitsgl.build")
    if "web" in profiles:
        try:
            version = check_node()
            checks.append(("node/npm", True, f"node {version}"))
        except InstallError as e:
            checks.append(("node/npm", False, str(e).split("\n")[0]))
        checks.append(("web/node_modules", (REPO_ROOT / "web" / "node_modules").is_dir(),
                       "run the web profile to npm install"))

    # Environment hints (informational, never fail the doctor)
    hints = []
    if "reduction" in profiles and stack_hint:
        hints.append(stack_hint)
    if "reduction" in profiles or "deploy" in profiles:
        if not os.environ.get("CAMPFIRE_ROOT"):
            hints.append("CAMPFIRE_ROOT is not set — the pipeline and client "
                         "need it (export CAMPFIRE_ROOT=/path/to/data).")
        if not os.environ.get("CRDS_PATH"):
            hints.append("CRDS_PATH not set — the pipeline will default the "
                         "cache into $CAMPFIRE_ROOT (fine, but it is tens of GB; "
                         "point it elsewhere if needed).")
    if "web" in profiles and not (REPO_ROOT / "web" / ".env.local").exists():
        hints.append("web/.env.local missing — copy web/.env.example and fill "
                     "in Supabase + storage credentials.")

    Ui.say("\nVerification:")
    ok_all = True
    for name, ok, detail in checks:
        mark = "ok " if ok else "FAIL"
        ok_all &= ok
        Ui.say(f"  [{mark}] {name}" + (f" — {detail}" if detail and not ok else
                                       (f" ({detail})" if detail else "")))
    for hint in hints:
        Ui.warn(hint)
    return ok_all


# --------------------------------------------------------------------------
# Post-install bootstrap (optional, interactive only)
# --------------------------------------------------------------------------

def bootstrap(env, sel):
    profiles = sel["profiles"]
    if interactive() and ("reduction" in profiles or "deploy" in profiles) \
            and not os.environ.get("CAMPFIRE_ROOT"):
        Ui.say("")
        path = text_prompt("Data directory for $CAMPFIRE_ROOT (raw data, config, "
                           "products; Enter to skip)", None)
        if path:
            root = Path(path).expanduser()
            (root / "config").mkdir(parents=True, exist_ok=True)
            config_toml = root / "config" / "config.toml"
            if not config_toml.exists() and env and env.python():
                cfpipe = Path(env.python()).parent / "cfpipe"
                result = run([cfpipe, "config"], capture=True, check=False)
                if result.returncode == 0 and result.stdout:
                    config_toml.write_text(result.stdout)
                    Ui.note(f"Seeded {config_toml} with the pipeline defaults.")
            Ui.say(f"Add to your shell profile:  export CAMPFIRE_ROOT={root}")

    if "web" in profiles:
        env_local = REPO_ROOT / "web" / ".env.local"
        example = REPO_ROOT / "web" / ".env.example"
        if not env_local.exists() and example.exists() and interactive() \
                and confirm("Create web/.env.local from .env.example (you still "
                            "fill in credentials)?", True):
            shutil.copyfile(example, env_local)
            Ui.note(f"Created {env_local} — edit it before `npm run dev`.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_plan(env, sel):
    plan = []
    needs_py = any(p in sel["profiles"] for p in PY_PROFILES)
    if needs_py:
        if sel.get("stack_note"):
            plan.append(sel["stack_note"])
        if env.exists():
            plan.append(f"Reuse {env.describe()} (update packages in place)")
        elif isinstance(env, VenvEnv):
            plan.append(f"Create {env.describe()} from {env.base_python}")
        else:
            extra = " + conda-forge numba" if "reduction" in sel["profiles"] else ""
            plan.append(f"Create {env.describe()} with python {sel['python']}{extra}")
        mode = "editable (-e)" if sel["editable"] else "regular"
        for target in build_py_targets(sel):
            plan.append(f"pip install {mode}: {target}")
    if "web" in sel["profiles"]:
        plan.append("npm install in web/ (node >= 18.18 required)")
    plan.append("Run verification (doctor)")
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="install.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Profiles: " + "; ".join(f"{k} = {v}" for k, v in PROFILES.items()),
    )
    parser.add_argument("--profiles", "-p",
                        help="comma-separated: client,reduction,deploy,web")
    parser.add_argument("--env-name", help=f"conda env name (default {DEFAULT_ENV_NAME})")
    parser.add_argument("--python", help=f"python for a NEW env (default {DEFAULT_PYTHON})")
    parser.add_argument("--manager", choices=["conda", "mamba", "micromamba", "venv"],
                        help="environment manager (default: auto-detect)")
    parser.add_argument("--venv-path", help="venv location for --manager venv "
                        "(default .venv in the repo)")
    parser.add_argument("--fitsgl", choices=["sibling", "git", "skip"],
                        help="FitsGL producer mode for the deploy profile")
    parser.add_argument("--dev", action="store_true", default=None,
                        help="include dev extras (pytest, black, mypy)")
    parser.add_argument("--extras", choices=["all", "none"],
                        help="client plotting extras ([all] = plotly + specutils)")
    parser.add_argument("--no-editable", action="store_true", default=None,
                        help="regular installs instead of editable (-e)")
    parser.add_argument("--stack", choices=["upgrade", "new-env", "abort"],
                        help="when the env carries another JWST stack than this "
                        "checkout pins (or runs another checkout's pipeline): "
                        "upgrade it in place, or build a sibling env "
                        "(<env>-jwst<major>) and leave it untouched")
    parser.add_argument("--recreate-env", action="store_true",
                        help="destroy and recreate the environment (asks first)")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore saved answers in .campfire-install.json")
    parser.add_argument("--doctor", action="store_true",
                        help="verify the current install and exit (installs nothing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and every command without running them")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="non-interactive: accept defaults/saved answers, no prompts")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    Ui.verbose = args.verbose
    Ui.assume_yes = args.yes
    Ui.dry_run = args.dry_run

    log_line(f"=== install.py {' '.join(argv if argv is not None else sys.argv[1:])} ===")

    try:
        state = {} if args.fresh else load_state()
        sel = resolve_selections(args, state)
        preflight(sel)
        env = resolve_env(sel, args)

        if args.doctor:
            ok = doctor(env, sel)
            return 0 if ok else 1

        env = resolve_stack(env, sel, args)
        plan = build_plan(env, sel)
        Ui.say("\nPlan:")
        for i, step in enumerate(plan, 1):
            Ui.say(f"  {i}. {step}")
        if not Ui.dry_run and not Ui.assume_yes and not confirm("\nProceed?", True):
            Ui.say("Aborted — nothing was changed.")
            return 1

        if any(p in sel["profiles"] for p in PY_PROFILES):
            recreate_requested(env, sel, args)
            prepare_env(env, sel, args)
            for target in build_py_targets(sel):
                Ui.say(f"Installing {target}")
                pip_install(env, target, sel["editable"])

        if "web" in sel["profiles"]:
            check_node()
            Ui.say("Installing web dependencies (npm install)")
            run(["npm", "install"], cwd=REPO_ROOT / "web")

        save_state({k: sel[k] for k in
                    ("profiles", "editable", "dev", "extras", "fitsgl",
                     "env_name", "python", "manager", "venv_path")})

        ok = True
        if not Ui.dry_run:
            ok = doctor(env, sel)
            bootstrap(env, sel)

        Ui.say("")
        if Ui.dry_run:
            Ui.say("Dry run complete — no changes made.")
        elif ok:
            Ui.say("Done. To update later:  git pull && python3 install.py")
            if isinstance(env, CondaEnv):
                Ui.say(f"Activate with:  conda activate {env.name}")
            elif isinstance(env, VenvEnv):
                Ui.say(f"Activate with:  source {env.path}/bin/activate")
        else:
            Ui.warn("Install finished but verification reported failures — see "
                    f"above and {LOG_FILE.name}. Re-running install.py is safe.")
            return 1
        return 0

    except InstallError as e:
        Ui.fail(str(e))
        return 1
    except KeyboardInterrupt:
        Ui.say("\nAborted. Re-running install.py is safe — it converges.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
