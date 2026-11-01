# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Fix assistant: diagnose a game's latest build or test run, then fix it
yourself or ask an AI service of your choosing.

Nothing leaves this computer unless the player chooses "Ask AI", enters their
own service address and key, and presses Send. The full request can be
previewed first. Suggested changes are never applied on their own: the player
reviews the patch, and applying it backs up every file it touches so it can be
undone.
"""
from __future__ import annotations
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import time
import urllib.request

from .core import read_json, write_json

MAX_LOG_LINES = 120
MAX_REPORT_CHARS = 6000
SYSTEM_PROMPT = (
    "You help people repair PS3 games that are being statically recompiled to native PC "
    "code with RetroRemakes PS3 Studio (a ps3recomp-based pipeline). You get a build attempt's "
    "report, its newest build log and the newest test log analysis. Explain the most likely "
    "cause in plain language, then give the smallest fix. When a fix changes files, include ONE "
    "unified diff in a ```diff block with paths relative to the build attempt folder "
    "(a/... and b/... prefixes). Never ask for or include game files, keys or firmware.")


def attempts_dir(studio, p):
    return studio.project_dir(p) / "attempts"


def latest_attempt(studio, p):
    """The attempt the project points at, else the newest attempt folder."""
    if p.get("attempt") and Path(p["attempt"]).is_dir():
        return Path(p["attempt"])
    base = attempts_dir(studio, p)
    if not base.is_dir():
        return None
    folders = sorted((a for a in base.iterdir() if a.is_dir()), key=lambda a: a.name)
    return folders[-1] if folders else None


def _newest_log(folder, depth=3):
    if not folder or not Path(folder).is_dir():
        return None
    logs = []
    for path in Path(folder).rglob("*.log"):
        rel = path.relative_to(folder)
        if len(rel.parts) <= depth and path.is_file():
            logs.append(path)
    return max(logs, key=lambda x: x.stat().st_mtime) if logs else None


def _tail(path, lines=MAX_LOG_LINES):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def gather(studio, p):
    """Everything the Studio knows about this game's current problem."""
    info = {"name": p.get("name", ""), "status": p.get("status", ""), "attempt": "",
            "build_report": {}, "build_log": "", "build_log_path": "", "test": {}, "test_log_path": ""}
    attempt = latest_attempt(studio, p)
    if attempt:
        info["attempt"] = str(attempt)
        info["build_report"] = read_json(attempt / "build-report.json", {})
        log = _newest_log(attempt)
        if log:
            info["build_log_path"] = str(log)
            info["build_log"] = _tail(log)
    try:
        test_log = studio.test_log_path(p)
    except (ValueError, OSError):
        test_log = None
    if test_log and Path(test_log).is_file():
        from .batch_queue import analyse_log
        info["test_log_path"] = str(test_log)
        result = analyse_log(test_log)
        info["test"] = {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool, list))}
    return info


def diagnosis_text(info):
    out = [f"Game: {info['name']}", f"Status: {info['status'] or 'unknown'}"]
    out.append(f"Build attempt: {info['attempt'] or 'none yet (build the game first)'}")
    report = info.get("build_report") or {}
    for key in ("status", "stage", "error", "failure", "blocked_reason"):
        if report.get(key):
            out.append(f"Build {key}: {report[key]}")
    test = info.get("test") or {}
    if test:
        out.append(f"Last test: {test.get('classification', 'unknown')} - {test.get('signature', '')}")
    else:
        out.append("Last test: no test log yet")
    if info.get("build_log_path"):
        out.append(f"\nNewest build log: {info['build_log_path']}\n--- last lines ---\n{info['build_log']}")
    return "\n".join(out)


def manual_guide(info):
    attempt = info.get("attempt") or "<build attempt folder>"
    return (
        "Fixing it yourself\n"
        f"1. Open the build attempt folder: {attempt}\n"
        "2. Read the newest build log (button above) and the last test result in the diagnosis.\n"
        "3. Common places to edit:\n"
        "   - loader/*.functions.json: wrong function boundaries or missing functions\n"
        "   - port/main.cpp: start-up, file paths and per-game settings\n"
        "   - generated sources: compiler errors point at the exact file and line\n"
        "   - Game patches (Game queue tab): RPCS3-format patch files you supply\n"
        "4. Save your edits, then press 'Rebuild with my changes'. Your edited files are kept;\n"
        "   new build attempts always get their own folder.\n"
        "5. Test the build. Repeat until it runs.\n")


# ------------------------------------------------------------- AI service

def ai_settings(studio):
    value = studio.state["settings"].get("fix_ai") or {}
    return {"url": value.get("url", ""), "model": value.get("model", ""), "key": value.get("key", "")}


def save_ai_settings(studio, url, model, key):
    studio.state["settings"]["fix_ai"] = {"url": url.strip().rstrip("/"), "model": model.strip(), "key": key.strip()}
    studio.save()


def build_request(info, question=""):
    report = json.dumps(info.get("build_report") or {}, indent=1)[:MAX_REPORT_CHARS]
    body = (f"Game: {info['name']}\nStudio status: {info['status']}\n\n"
            f"Build report:\n{report}\n\nNewest build log (last lines):\n{info.get('build_log') or '(none)'}\n\n"
            f"Test log analysis:\n{json.dumps(info.get('test') or {}, indent=1)}\n")
    if question.strip():
        body += f"\nPlayer's note: {question.strip()}\n"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": body}]


def ask(settings, messages, timeout=300):
    """Send to a chat-completions style endpoint the player configured."""
    url = settings.get("url", "")
    if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise ValueError("Enter the AI service address (https://..., or a local http://localhost server) under AI settings.")
    if not settings.get("model"):
        raise ValueError("Enter the model name your AI service uses.")
    endpoint = url if url.endswith("/chat/completions") else url + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if settings.get("key"):
        headers["Authorization"] = "Bearer " + settings["key"]
    data = json.dumps({"model": settings["model"], "messages": messages, "temperature": 0.2}).encode()
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        reply = json.loads(response.read(8 * 1024 * 1024).decode("utf-8", "replace"))
    try:
        return reply["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("The AI service answered in an unexpected format.")


# ------------------------------------------------------------- patches

_DIFF = re.compile(r"```(?:diff|patch)\s*\n(.*?)```", re.S)


def extract_patch(text):
    match = _DIFF.search(text or "")
    return match.group(1) if match else ""


def patch_files(patch):
    files = []
    for line in patch.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            name = line[4:].split("\t")[0].strip()
            if name == "/dev/null":
                continue
            parts = PurePosixPath(name).parts
            if parts and parts[0] in ("a", "b"):
                parts = parts[1:]
            if not parts or ".." in parts or name.startswith("/") or ":" in parts[0]:
                raise ValueError(f"Patch touches an unsafe path: {name}")
            rel = "/".join(parts)
            if rel not in files:
                files.append(rel)
    if not files:
        raise ValueError("No file changes found in the suggested patch.")
    return files


def apply_patch(attempt, patch):
    """Back up touched files, then apply with git. Returns the fix id."""
    attempt = Path(attempt)
    files = patch_files(patch)
    fix_id = time.strftime("%Y%m%d-%H%M%S")
    store = attempt / "user-fixes" / fix_id
    store.mkdir(parents=True, exist_ok=False)
    (store / "change.patch").write_text(patch if patch.endswith("\n") else patch + "\n", encoding="utf-8")
    check = subprocess.run(["git", "apply", "--check", "--recount", str(store / "change.patch")],
                           cwd=attempt, capture_output=True, text=True)
    if check.returncode:
        shutil.rmtree(store, ignore_errors=True)
        raise ValueError("The patch does not fit these files:\n" + (check.stderr or check.stdout).strip())
    existed = []
    for rel in files:
        source = attempt / rel
        if source.is_file():
            target = store / "backup" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            existed.append(rel)
    write_json(store / "fix.json", {"files": files, "existed": existed, "applied": time.strftime("%Y-%m-%dT%H:%M:%S")})
    done = subprocess.run(["git", "apply", "--recount", str(store / "change.patch")],
                          cwd=attempt, capture_output=True, text=True)
    if done.returncode:
        undo(attempt, fix_id)
        raise ValueError("Applying the patch failed and was rolled back:\n" + done.stderr.strip())
    return fix_id


def list_fixes(attempt):
    base = Path(attempt) / "user-fixes"
    return sorted(p.name for p in base.iterdir() if (p / "fix.json").is_file()) if base.is_dir() else []


def undo(attempt, fix_id):
    attempt = Path(attempt)
    store = attempt / "user-fixes" / fix_id
    record = read_json(store / "fix.json", None)
    if not record:
        raise ValueError("That fix has no backup record.")
    for rel in record["files"]:
        target = attempt / rel
        backup = store / "backup" / rel
        if rel in record["existed"]:
            shutil.copy2(backup, target)
        elif target.is_file():
            target.unlink()
    (store / "fix.json").rename(store / "undone.json")
