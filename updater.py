#!/usr/bin/env python3
"""updater.py: check and update brew, cargo, go and ollama packages.

Usage:
  updater.py [-y] [--dry-run] [--only brew,cargo,go,ollama] [--include-casks] [--rollback]

Flow: check all sources, show one table grouped by source, then ask per source
(y / N / s to select individual packages). Every applied change is appended to
~/.local/state/updater/updates.jsonl. This script never deletes, truncates or
rewrites that file, and it sets the macOS user-append-only flag on it
(chflags uappend), so removing it requires running `chflags nouappend` first.
"""

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

LOG_DIR = Path.home() / ".local" / "state" / "updater"
LOG_FILE = LOG_DIR / "updates.jsonl"
OLLAMA_RELEASE_API = "https://api.github.com/repos/ollama/ollama/releases/latest"
OLLAMA_INSTALL_CMD = "curl -fsSL https://ollama.com/install.sh | sh"
SOURCES = ["brew", "cargo", "go", "ollama"]


@dataclass
class Pkg:
    source: str
    name: str
    old: str
    new: str
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- helpers

def run(cmd, timeout=300, cwd=None):
    """Run a command without a shell. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {shlex.join(cmd)}"


def version_tuple(v):
    nums = [int(x) for x in re.findall(r"\d+", v)][:3]
    return tuple(nums + [0] * (3 - len(nums)))


def last(v):
    if isinstance(v, list):
        return str(v[-1]) if v else ""
    return str(v or "")


def ask(question, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(question + suffix).strip().lower()
    except EOFError:
        print()
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def ask_choice(question):
    """Returns 'y' (all), 'n' (none) or 's' (select individually)."""
    try:
        ans = input(f"{question} [y/N/s(elect)] ").strip().lower()
    except EOFError:
        print()
        return "n"
    if ans in ("y", "yes"):
        return "y"
    if ans in ("s", "select"):
        return "s"
    return "n"


def parse_selection(text, n):
    """'1,3,5-7' -> sorted 0-based indices. Raises ValueError on bad input."""
    chosen = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(part)
        if lo < 1 or hi > n or lo > hi:
            raise ValueError(f"out of range: {part}")
        chosen.update(range(lo - 1, hi))
    return sorted(chosen)


def select_packages(pkgs):
    while True:
        try:
            text = input("Numbers to update (e.g. 1,3,5-7; empty = none): ").strip()
        except EOFError:
            print()
            return []
        if not text:
            return []
        try:
            return [pkgs[i] for i in parse_selection(text, len(pkgs))]
        except ValueError as e:
            print(f"invalid selection: {e}")


def print_table(found):
    for name, (_, pkgs) in found.items():
        print(f"\n== {name}: {len(pkgs)} outdated ==")
        if not pkgs:
            print("  all up to date")
            continue
        w = max(len(p.name) for p in pkgs)
        for i, p in enumerate(pkgs, 1):
            tag = "  (report only)" if p.extra.get("report_only") else ""
            print(f"  {i:>2}. {p.name:<{w}}  {p.old or '?':>14} -> {p.new}{tag}")


# ---------------------------------------------------------------- log

class AppendLog:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run

    def ensure_file(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(LOG_DIR, 0o700)
        if not LOG_FILE.exists():
            fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
        self._protect()

    def _protect(self):
        uf_append = getattr(stat, "UF_APPEND", None)
        if uf_append is None or not hasattr(os, "chflags"):
            print("warning: user-append flag not supported on this platform", file=sys.stderr)
            return
        try:
            st = os.stat(LOG_FILE)
            if not st.st_flags & uf_append:
                os.chflags(LOG_FILE, st.st_flags | uf_append)
        except OSError as e:
            print(f"warning: could not set uappend on {LOG_FILE}: {e}", file=sys.stderr)

    def append(self, entry):
        if self.dry_run:
            return
        self.ensure_file()
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def read_entries(self):
        if not LOG_FILE.exists():
            return []
        out = []
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


# ---------------------------------------------------------------- sources

class Source:
    name = ""
    tool = ""
    report_only = False

    def available(self):
        return shutil.which(self.tool) is not None

    def check(self, ctx):
        """Return list[Pkg] that have a newer version available."""
        raise NotImplementedError

    def update(self, pkg):
        """Return (ok, command_str, output)."""
        raise NotImplementedError

    def installed_version(self, pkg):
        """Return the currently installed version string, or None."""
        return None

    def rollback(self, entry):
        """Return (ok, command_str, output)."""
        return False, "", "rollback not supported for this source"


class Brew(Source):
    name = "brew"
    tool = "brew"

    def check(self, ctx):
        rc, _, err = run(["brew", "update", "--quiet"], timeout=900)
        if rc != 0:
            raise RuntimeError(f"brew update failed: {err.strip()}")
        if ctx.include_casks:
            cmd = ["brew", "outdated", "--json=v2", "--greedy"]
        else:
            cmd = ["brew", "outdated", "--json=v2", "--formula"]
        rc, out, err = run(cmd, timeout=600)
        if rc != 0 and not out.strip():
            raise RuntimeError(f"brew outdated failed: {err.strip()}")
        data = json.loads(out or "{}")
        pkgs = []
        for f in data.get("formulae", []):
            pkgs.append(Pkg("brew", f["name"], last(f.get("installed_versions")),
                            f.get("current_version", ""), {"cask": False}))
        if ctx.include_casks:
            for c in data.get("casks", []):
                pkgs.append(Pkg("brew", c["name"], last(c.get("installed_versions")),
                                c.get("current_version", ""), {"cask": True}))
        return pkgs

    def update(self, pkg):
        cmd = ["brew", "upgrade"] + (["--cask"] if pkg.extra.get("cask") else []) + [pkg.name]
        rc, out, err = run(cmd, timeout=3600)
        return rc == 0, shlex.join(cmd), out + err

    def installed_version(self, pkg):
        cmd = ["brew", "list", "--versions"] + (["--cask"] if pkg.extra.get("cask") else []) + [pkg.name]
        rc, out, _ = run(cmd)
        parts = out.split()
        return parts[-1] if rc == 0 and len(parts) > 1 else None

    def rollback(self, entry):
        name, old = entry["package"], entry["old"]
        if entry.get("meta", {}).get("cask"):
            return False, "", f"casks cannot be rolled back automatically; reinstall {name} {old} manually"
        versioned = f"{name}@{old.split('.')[0]}"
        if run(["brew", "info", versioned])[0] != 0:
            return False, "", (f"no versioned formula {versioned}. Manual option: "
                               f"brew extract --version={old} {name} <your-tap>")
        cmd = ["brew", "install", versioned]
        rc, out, err = run(cmd, timeout=3600)
        return rc == 0, shlex.join(cmd), out + err


class Cargo(Source):
    name = "cargo"
    tool = "cargo"
    LINE = re.compile(r"^(\S+)\s+v?(\S+)\s+v?(\S+)\s+(Yes|No)$")

    def check(self, ctx):
        rc, out, err = run(["cargo", "install-update", "-l"], timeout=900)
        if rc != 0 and "no such command" in err:
            if ctx.dry_run:
                print("note: cargo-update is not installed; cargo skipped (dry run does not install)")
                return []
            if not ask("cargo-update is not installed. Install it now with `cargo install cargo-update`?"):
                print("cargo skipped")
                return []
            cmd = ["cargo", "install", "cargo-update"]
            rc2, out2, err2 = run(cmd, timeout=3600)
            ver = self._installed_plugin_version()
            ctx.log.append(self._entry(ctx, Pkg("cargo", "cargo-update", "", ver or "",
                                                {}), shlex.join(cmd), rc2 == 0,
                                       None if rc2 == 0 else (out2 + err2)[-500:]))
            if rc2 != 0:
                raise RuntimeError(f"install of cargo-update failed: {err2.strip()[-300:]}")
            rc, out, err = run(["cargo", "install-update", "-l"], timeout=900)
        if rc != 0:
            raise RuntimeError(f"cargo install-update -l failed: {err.strip()[-300:]}")
        pkgs = []
        for line in out.splitlines():
            m = self.LINE.match(line.strip())
            if m and m.group(4) == "Yes":
                pkgs.append(Pkg("cargo", m.group(1), m.group(2), m.group(3)))
        return pkgs

    def _installed_plugin_version(self):
        rc, out, _ = run(["cargo", "install", "--list"])
        for line in out.splitlines():
            m = re.match(r"^cargo-update v(\S+):$", line)
            if m:
                return m.group(1)
        return None

    def _entry(self, ctx, pkg, cmd, ok, error):
        return make_entry(ctx, "update", pkg, pkg.old, pkg.new, ok, cmd, error)

    def update(self, pkg):
        cmd = ["cargo", "install-update", pkg.name]
        rc, out, err = run(cmd, timeout=3600)
        return rc == 0, shlex.join(cmd), out + err

    def installed_version(self, pkg):
        rc, out, _ = run(["cargo", "install", "--list"])
        for line in out.splitlines():
            m = re.match(rf"^{re.escape(pkg.name)} v(\S+):$", line)
            if m:
                return m.group(1)
        return None

    def rollback(self, entry):
        cmd = ["cargo", "install", entry["package"], "--version", f"={entry['old']}", "--locked", "--force"]
        rc, out, err = run(cmd, timeout=3600)
        return rc == 0, shlex.join(cmd), out + err


class Go(Source):
    name = "go"
    tool = "go"

    def gobin(self):
        _, out, _ = run(["go", "env", "GOBIN"])
        if out.strip():
            return out.strip()
        _, out, _ = run(["go", "env", "GOPATH"])
        return os.path.join(out.strip() or str(Path.home() / "go"), "bin")

    def check(self, ctx):
        d = self.gobin()
        if not os.path.isdir(d):
            return []
        bins = []
        for entry in sorted(os.scandir(d), key=lambda e: e.name):
            if not entry.is_file():
                continue
            rc, out, _ = run(["go", "version", "-m", entry.path], timeout=60)
            if rc != 0:
                continue
            path = mod = ver = None
            for line in out.splitlines():
                parts = line.split("\t")
                if parts[0] == "path" and len(parts) >= 2:
                    path = parts[1]
                elif parts[0] == "mod" and len(parts) >= 3:
                    mod, ver = parts[1], parts[2]
            if not (path and mod and ver) or ver == "(devel)":
                continue
            bins.append((entry.name, entry.path, path, mod, ver))

        latest_cache = {}
        pkgs = []
        for name, bin_path, pkg_path, mod, ver in bins:
            if mod not in latest_cache:
                rc, out, _ = run(["go", "list", "-m", "-json", f"{mod}@latest"], timeout=120)
                try:
                    latest_cache[mod] = json.loads(out).get("Version") if rc == 0 else None
                except json.JSONDecodeError:
                    latest_cache[mod] = None
            latest = latest_cache[mod]
            if latest and latest != ver:
                pkgs.append(Pkg("go", name, ver, latest,
                                {"path": pkg_path, "module": mod, "bin": bin_path}))
        return pkgs

    def update(self, pkg):
        cmd = ["go", "install", f"{pkg.extra['path']}@latest"]
        rc, out, err = run(cmd, timeout=1800)
        return rc == 0, shlex.join(cmd), out + err

    def installed_version(self, pkg):
        rc, out, _ = run(["go", "version", "-m", pkg.extra["bin"]])
        for line in out.splitlines():
            parts = line.split("\t")
            if parts[0] == "mod" and len(parts) >= 3:
                return parts[2]
        return None

    def rollback(self, entry):
        meta = entry.get("meta", {})
        cmd = ["go", "install", f"{meta['path']}@{entry['old']}"]
        rc, out, err = run(cmd, timeout=1800)
        return rc == 0, shlex.join(cmd), out + err


class Ollama(Source):
    name = "ollama"
    tool = "ollama"
    report_only = True

    def installed(self):
        _, out, err = run(["ollama", "--version"])
        m = re.search(r"(\d+\.\d+\.\d+\S*)", out + err)
        return m.group(1) if m else None

    def latest(self):
        req = urllib.request.Request(OLLAMA_RELEASE_API, headers={
            "User-Agent": "updater-script", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        return str(data.get("tag_name", "")).lstrip("v") or None

    def check(self, ctx):
        cur, latest = self.installed(), self.latest()
        if cur and latest and version_tuple(latest) > version_tuple(cur):
            return [Pkg("ollama", "ollama", cur, latest, {"report_only": True})]
        return []

    def update(self, pkg):
        return False, OLLAMA_INSTALL_CMD, "report only; the script never installs ollama"

    def installed_version(self, pkg):
        return self.installed()


SOURCE_CLASSES = {"brew": Brew, "cargo": Cargo, "go": Go, "ollama": Ollama}


# ---------------------------------------------------------------- apply / log entries

def make_entry(ctx, kind, pkg, old, new, ok, cmd, error=None, verified=True, rolls_back=None):
    entry = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_id": ctx.run_id,
        "kind": kind,
        "source": pkg.source,
        "package": pkg.name,
        "old": old,
        "new": new,
        "status": "ok" if ok else "failed",
        "verified": verified,
        "cmd": cmd,
        "meta": pkg.extra,
    }
    if error:
        entry["error"] = error
    if rolls_back:
        entry["rolls_back_ts"] = rolls_back
    return entry


def apply_one(src, pkg, ctx):
    print(f"-> {pkg.source} {pkg.name} {pkg.old} -> {pkg.new} ...", flush=True)
    ok, cmd, out = src.update(pkg)
    installed = src.installed_version(pkg) if ok else None
    verified = installed is not None
    new = installed or pkg.new if ok else pkg.old
    error = None if ok else out.strip()[-500:]
    ctx.log.append(make_entry(ctx, "update", pkg, pkg.old, new, ok, cmd, error, verified))
    if ok:
        note = "" if verified else " (could not verify installed version)"
        print(f"   ok {new}{note}")
    else:
        print(f"   FAILED: {error[-200:] if error else 'unknown error'}")
    return ok


def do_rollback(ctx):
    entries = [e for e in ctx.log.read_entries()
               if e.get("kind", "update") == "update" and e.get("status") == "ok"
               and e.get("source") in SOURCE_CLASSES]
    if not entries:
        print("no successful updates in the log")
        return 0
    recent = entries[-30:][::-1]
    print("Recent successful updates (newest first):")
    for i, e in enumerate(recent, 1):
        print(f"  {i:>2}. {e['ts']}  {e['source']:<6} {e['package']}  {e['old']} -> {e['new']}")
    try:
        text = input("Roll back which entries? (numbers, empty = none): ").strip()
    except EOFError:
        print()
        return 0
    if not text:
        return 0
    try:
        idx = parse_selection(text, len(recent))
    except ValueError as e:
        print(f"invalid selection: {e}")
        return 1

    sources = {}
    failures = 0
    for i in idx:
        e = recent[i]
        src = sources.setdefault(e["source"], SOURCE_CLASSES[e["source"]]())
        pkg = Pkg(e["source"], e["package"], e["new"], e["old"], e.get("meta", {}))
        if not ask(f"Roll back {e['source']} {e['package']} {e['new']} -> {e['old']}?"):
            print("skipped")
            continue
        ok, cmd, out = src.rollback(e)
        installed = src.installed_version(pkg) if ok else None
        verified = installed is not None
        new = (installed or e["old"]) if ok else e["new"]
        error = None if ok else out.strip()[-500:]
        ctx.log.append(make_entry(ctx, "rollback", pkg, e["new"], new, ok, cmd, error,
                                  verified, rolls_back=e["ts"]))
        if ok:
            print(f"   rolled back to {new}")
        else:
            failures += 1
            print(f"   FAILED: {error[-300:] if error else out.strip()}")
    return 1 if failures else 0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Check and update brew, cargo, go and ollama packages.")
    ap.add_argument("-y", "--yes", action="store_true", help="accept all updates without prompting")
    ap.add_argument("--dry-run", action="store_true", help="only list outdated packages")
    ap.add_argument("--only", default="", help="comma-separated sources: " + ",".join(SOURCES))
    ap.add_argument("--include-casks", action="store_true", help="also check and upgrade brew casks")
    ap.add_argument("--rollback", action="store_true", help="roll back entries from the update log")
    args = ap.parse_args(argv)

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    bad = [s for s in only if s not in SOURCES]
    if bad:
        ap.error(f"unknown source(s): {', '.join(bad)}")
    if args.rollback and args.dry_run:
        ap.error("--rollback cannot be combined with --dry-run")

    ctx = argparse.Namespace(
        yes=args.yes,
        dry_run=args.dry_run,
        include_casks=args.include_casks,
        log=AppendLog(args.dry_run),
        run_id=dt.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6],
    )

    if not args.dry_run:
        ctx.log.ensure_file()

    if args.rollback:
        return do_rollback(ctx)

    selected = [SOURCE_CLASSES[s]() for s in (only or SOURCES)]

    # Detect phase: check every source before asking anything about updates.
    found = {}
    for src in selected:
        if not src.available():
            print(f"skip {src.name}: '{src.tool}' not found")
            continue
        try:
            found[src.name] = (src, src.check(ctx))
        except Exception as e:  # one broken source must not stop the others
            print(f"warning: {src.name} check failed: {e}", file=sys.stderr)

    print_table(found)
    total = sum(len(p) for _, p in found.values())
    if total == 0:
        print("\nnothing to update")
        return 0
    if args.dry_run:
        print("\ndry run: nothing changed")
        return 0

    # Decision + apply phase, one source at a time.
    failures = 0
    for name, (src, pkgs) in found.items():
        if not pkgs:
            continue
        if src.report_only:
            for p in pkgs:
                print(f"\nollama {p.old} -> {p.new} is available. To update, run:\n  {OLLAMA_INSTALL_CMD}")
            continue
        if ctx.yes:
            chosen = pkgs
        else:
            choice = ask_choice(f"\nUpdate these {len(pkgs)} {name} packages?")
            if choice == "y":
                chosen = pkgs
            elif choice == "s":
                chosen = select_packages(pkgs)
            else:
                print(f"{name}: skipped")
                continue
        for p in chosen:
            if not apply_one(src, p, ctx):
                failures += 1

    print(f"\ndone. log: {LOG_FILE}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
