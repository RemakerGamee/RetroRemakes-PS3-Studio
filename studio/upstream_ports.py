# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Pinned upstream per-title port routes, built on the player's PC from their own game.

Some titles have a maintained public port that already carries the per-title
runtime work this Studio would otherwise have to rediscover (Killzone 2:
Project Pyrrhus, built on ps3recomp v0.18.0 plus its own patch set). For an
input EBOOT whose SHA-256 matches a route, Studio.build delegates here:

1. acquire the pinned port repository and its pinned ps3recomp submodule into
   the Studio workspace (git, core.autocrlf=false: CRLF checkouts break the
   patch set);
2. build the patched toolkit exactly as upstream tools/ps3recomp-patched.sh
   does (copy without .git, `git apply` every port/patches/*.patch in name
   order, each must apply cleanly, then write the .kz2-patchset stamp);
3. fetch upstream's pinned, SHA-256-checked compiler with its own
   tools/release/fetch_toolchain.py into a Studio cache;
4. run upstream's own first-run pipeline (tools/release/firstrun.py: input
   checks, PPU/SPU lift, configure, compile) with the player's disc dump,
   update and decrypted EBOOT, reporting its PROGRESS lines.

This module holds only repository URLs, commits and input hashes. No game
code or data is stored or shipped; everything generated stays in the player's
own attempt folder. A finished build is a candidate: gameplay is unverified.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROUTES = (
    {
        "id": "killzone2-project-pyrrhus",
        "title": "Killzone 2",
        "title_id": "BCUS98116",
        "repository": "https://github.com/undecuple/project-pyrrhus",
        "commit": "4771777d8b2ed299bfc12d7ccffa3811e2d8a1c0",
        "submodule": "third_party/ps3recomp",
        "submodule_url": "https://github.com/sp00nznet/ps3recomp",
        "submodule_commit": "4e8f3ca87a3e1d87bb83b8b7e9348fe2685cd514",
        "patch_dir": "port/patches",
        "toolkit_dir": "work/ps3recomp-patched",
        "stamp": ".kz2-patchset",
        "toolchain": ("llvm-mingw",),
        "toolchain_tree": "llvm-mingw",
        "firstrun": "tools/release/firstrun.py",
        "artifact": "kz2.exe",
        "update_env": "KZ2_UPDATE_DIR",
        "update_markers": ("added_data_ps3.psarc", "patch_data_ps3"),
        "update_version": "1.29",
        # The release pipeline builds for the update's EBOOT only. Its checks
        # refuse the disc's 1.01 EBOOT (firstrun_check E1), so 1.01 is not a
        # route here; that input keeps the Studio's own pipeline.
        "inputs": {"e8b0ddefc0cb1dc116fb4ae9675a5f551a26cc7eb7d74b06f00ec294ceea8c3d": "1.29"},
        "license": "MIT (port); ps3recomp MIT",
    },
)
KNOWN_UNROUTED = {
    "39a28358efd91fd0e914944392a3807b0141e4c5b6ba22e4964640c9c43b9799":
        "Killzone 2 1.01 (disc EBOOT): Project Pyrrhus builds only from the decrypted EBOOT of the 1.29 update. "
        "Select the 1.29 update's decrypted EBOOT to use that route; this input uses the Studio's own pipeline.",
}
PROGRESS_RE = re.compile(r"^PROGRESS step=(\d+)/(\d+) name=(\S+).*?pct=([\d.]+)")
RESULT_RE = re.compile(r"^RESULT ok=(\d)")


class UpstreamPortError(RuntimeError):
    pass


def route_for(input_sha256):
    for route in ROUTES:
        if input_sha256 in route["inputs"]:
            return route
    return None


def unrouted_note(input_sha256):
    return KNOWN_UNROUTED.get(input_sha256)


def _git(runner, label, args, cwd=None):
    return runner.run(label, ["git", "-c", "core.autocrlf=false", "-c", "core.longpaths=true", *args], cwd=cwd)


def _rev(path):
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def acquire(route, root, runner):
    """Pinned checkout of the port and its submodule; refuses local edits."""
    source = Path(root) / route["id"] / route["commit"][:12]
    sub = source / route["submodule"]
    if _rev(source) != route["commit"]:
        if source.exists():
            raise UpstreamPortError(f"{source} exists but is not the pinned checkout; move it aside and retry")
        source.parent.mkdir(parents=True, exist_ok=True)
        _git(runner, "port-clone", ["clone", "--no-checkout", route["repository"], str(source)])
        _git(runner, "port-checkout", ["-C", str(source), "config", "core.autocrlf", "false"])
        _git(runner, "port-checkout", ["-C", str(source), "checkout", "--detach", route["commit"]])
    if _rev(sub) != route["submodule_commit"]:
        _git(runner, "toolkit-clone", ["-C", str(source), "submodule", "update", "--init", "--", route["submodule"]])
        if _rev(sub) != route["submodule_commit"]:
            raise UpstreamPortError(f"{route['submodule']} is not at the pinned commit {route['submodule_commit']}")
    for path in (source, sub):
        dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            raise UpstreamPortError(f"Pinned checkout has local changes ({path}); refusing to build from it")
    return source


def patch_files(route, source):
    return sorted(Path(source, route["patch_dir"]).glob("*.patch"), key=lambda p: p.name)


def patchset_stamp(route, source):
    """Upstream's stamp: sha256 of the patch files concatenated in name order (16 hex) - short submodule rev."""
    digest = hashlib.sha256()
    for patch in patch_files(route, source):
        digest.update(patch.read_bytes())
    return digest.hexdigest()[:16] + "-" + route["submodule_commit"][:7]


def build_toolkit(route, source, runner, emit=print):
    """Faithful Python form of upstream tools/ps3recomp-patched.sh."""
    source = Path(source).resolve()
    sub, target = source / route["submodule"], source / route["toolkit_dir"]
    stamp = patchset_stamp(route, source)
    if (target / route["stamp"]).is_file() and (target / route["stamp"]).read_text().strip() == stamp:
        emit(f"Patched toolkit is current ({stamp}).")
        return target
    new = target.with_name(target.name + ".new")
    if new.exists():
        shutil.rmtree(new)
    shutil.copytree(sub, new, ignore=shutil.ignore_patterns(".git"))
    for patch in patch_files(route, source):
        out = runner.run("toolkit-patch-" + patch.stem[:40],
                         ["git", "apply", "--verbose", "--whitespace=nowarn",
                          "--directory=" + new.relative_to(source).as_posix(), patch.resolve()],
                         cwd=source)
        if not re.search(r"^Applied patch .* cleanly", out, re.M):
            raise UpstreamPortError(f"patch {patch.name} did not apply cleanly")
    target.mkdir(parents=True, exist_ok=True)
    # Sync new -> target like upstream, keeping unchanged files' timestamps for incremental builds.
    for directory, _, files in os.walk(new):
        rel = Path(directory).relative_to(new)
        (target / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            a, b = Path(directory) / name, target / rel / name
            if not b.is_file() or a.read_bytes() != b.read_bytes():
                shutil.copyfile(a, b)
                shutil.copymode(a, b)
    for directory, _, files in os.walk(target):
        rel = Path(directory).relative_to(target)
        for name in files:
            if rel == Path(".") and name == route["stamp"]:
                continue
            if not (new / rel / name).exists():
                (Path(directory) / name).unlink()
    shutil.rmtree(new)
    (target / route["stamp"]).write_text(stamp + "\n", encoding="utf-8")
    return target


def fetch_toolchain(route, source, cache, runner, python=sys.executable):
    script = Path(source) / "tools/release/fetch_toolchain.py"
    runner.run("toolchain-fetch", [python, script, "--cache", cache, "--only", ",".join(route["toolchain"])], cwd=source)
    tree = Path(cache) / "trees" / route["toolchain_tree"]
    if not (tree / "bin").is_dir():
        raise UpstreamPortError(f"Pinned compiler missing after fetch: {tree}")
    return tree


def update_dir(route, p, settings, elf):
    """The update folder: the project's choice, the Studio setting, the EBOOT's own folder when it is the
    update's USRDIR, or RPCS3's installed update (it holds the update-only files)."""
    for value in (p.get("update_dir"), (settings.get("upstream_update_dirs") or {}).get(route["id"])):
        if value:
            path = Path(value).expanduser()
            if not path.is_dir():
                raise UpstreamPortError(f"Update folder not found: {path}")
            return path
    candidates = [Path(elf).parent]
    rpcs3 = settings.get("rpcs3") or ""
    if rpcs3:
        # RPCS3 installs updates under dev_hdd0/game/<title id>/USRDIR (read only here).
        candidates.append(Path(rpcs3).expanduser().parent / "dev_hdd0" / "game" / route["title_id"] / "USRDIR")
    for folder in candidates:
        if any((folder / marker).exists() for marker in route["update_markers"]):
            return folder
    raise UpstreamPortError(
        f"{route['title']} ({route['title_id']}) builds through its pinned upstream port, which needs the "
        f"{route['update_version']} update. Set this project's update folder to that update's USRDIR "
        f"(it holds {' and '.join(route['update_markers'])}), then build again.")


def run(studio, p, report, attempt, runner, emit, cancel=None):
    """Build the route's port for project p. Returns the build manifest fields to merge."""
    route = route_for(report["sha256"])
    elf = Path(report["input"])
    dump = Path(p.get("disc_dir") or studio.vfs_root_for(p, elf))
    if not (dump / "PS3_GAME").is_dir():
        raise UpstreamPortError(f"Disc dump folder must hold PS3_GAME: {dump}")
    update = update_dir(route, p, studio.state.get("settings", {}), elf)
    source = acquire(route, studio.home / "upstream-ports", runner)
    build_toolkit(route, source, runner, emit)
    toolchain = fetch_toolchain(route, source, studio.home / "tools" / "upstream-toolchains", runner)
    build = attempt / "port-build"
    jobs = max(1, min(8, int(studio.state.get("settings", {}).get("jobs", 2))))
    argv = [sys.executable, "-X", "utf8", source / route["firstrun"], "--dump", dump, "--update", update,
            "--eboot", elf, "--root", source, "--build", build, "--toolchain", toolchain, "--jobs", jobs,
            "--log-dir", attempt / "logs", "--status-file", attempt / "port-status.txt"]
    # firstrun writes its LOG/PROGRESS/RESULT lines to stdout, which the Runner logs and forwards.
    status = attempt / "port-status.txt"
    try:
        tail = runner.run("port-firstrun", argv, cwd=source, timeout=6 * 3600)
    except RuntimeError as error:
        reasons = []
        if status.is_file():
            reasons = [line[7:] for line in status.read_text(encoding="utf-8", errors="replace").splitlines()
                       if line.startswith("REASON ")]
        if reasons:
            raise UpstreamPortError(str(error) + " | " + "; ".join(reasons)) from error
        raise
    result = [RESULT_RE.match(line) for line in tail.splitlines()]
    result = next((m for m in reversed(result) if m), None)
    exe = build / route["artifact"]
    if (result and result[1] != "1") or not exe.is_file():
        reasons = [line[7:] for line in tail.splitlines() if line.startswith("REASON ")]
        raise UpstreamPortError("Upstream port build did not finish" + (": " + "; ".join(reasons) if reasons else ""))
    return {"upstream_port": {"route": route["id"], "repository": route["repository"], "commit": route["commit"],
                              "toolkit": route["submodule_commit"], "patchset": patchset_stamp(route, source),
                              "license": route["license"]},
            "native_exe": str(exe),
            "upstream_launch_env": {route["update_env"]: str(update), "PS3_VFS_ROOT": str(dump)}}


def progress_from_line(line):
    """PROGRESS step=N/M name=X ... pct=P -> (name, percent) for the Studio's progress bar."""
    match = PROGRESS_RE.match(line.strip())
    return (match[3], float(match[4])) if match else None
