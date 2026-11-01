# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
"""Manual game sessions are owned independently from build workers."""
from __future__ import annotations

import copy
import os
import signal
import subprocess
import threading
import time


def stop_process_tree(proc):
    """Only called with a live Popen created and retained by this session owner."""
    if proc.poll() is not None:
        return False
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("Could not stop this manual test process tree")
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
    return True


class TestSessions:
    def __init__(self, write_json, read_json):
        self._write = write_json
        self._read = read_json
        self._lock = threading.RLock()
        self._sessions = {}

    def register(self, proc, result, path, latest):
        result = copy.deepcopy(result)
        entry = {"process": proc, "result": result, "path": path, "latest": latest, "stop_requested": False}
        with self._lock:
            self._sessions[result["session_id"]] = entry
            self._publish(entry)
        def watch():
            code = proc.wait()
            with self._lock:
                result.update(exit_code=code, elapsed_s=round(time.time() - result["started"], 3),
                              status="stopped" if entry["stop_requested"] else
                              ("exited normally" if code == 0 else "exited with error"))
                self._publish(entry)
        threading.Thread(target=watch, daemon=True).start()

    def _publish(self, entry):
        self._write(entry["path"], entry["result"])
        latest = self._read(entry["latest"], {})
        if not latest or latest.get("session_id") == entry["result"]["session_id"]:
            self._write(entry["latest"], entry["result"])

    def list(self, project_id=None):
        with self._lock:
            return [copy.deepcopy(entry["result"]) for entry in self._sessions.values()
                    if project_id is None or entry["result"]["project_id"] == project_id]

    def stop(self, session_id):
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                raise ValueError("This manual test is not owned by this Studio session")
            if entry["result"]["status"] not in ("running", "stopping"):
                return False
            if entry["process"].poll() is not None:
                return False
            entry["stop_requested"] = True
        stopped = stop_process_tree(entry["process"])
        with self._lock:
            if stopped and entry["result"]["status"] == "running":
                entry["result"]["status"] = "stopping"
                self._publish(entry)
        return stopped
