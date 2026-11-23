"""A single running step per workbench, with replayable progress for browser reconnects."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4


class WindowsJob:
    """Group the worker and its workflow descendants for one native cancellation."""

    def __init__(self, process):
        import ctypes
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def terminate(self):
        import ctypes
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        self.kernel.CloseHandle(self.handle)


class LiveStep:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.data = {}

    def snapshot(self):
        with self.lock:
            return {**self.data, "events":list(self.data.get("events", []))}

    def reset(self):
        with self.lock:
            if not self.process:
                self.data = {}

    def run(self, ws, thread):
        with self.lock:
            if self.process:
                raise ValueError("A step is already running.")
            proc = subprocess.Popen([sys.executable, "-u", "-m", "james.step_worker", str(ws.root)],
                                    stdout=subprocess.PIPE, text=True, start_new_session=os.name != "nt")
            try:
                self.job = WindowsJob(proc) if os.name == "nt" else None
            except OSError:
                proc.kill()
                proc.wait()
                proc.stdout.close()
                raise
            self.process = proc
            self.data = dict(id=uuid4().hex, thread=thread, started=time.time(), running=True, events=[])
        result = {"event":"paused", "reason":"Step worker exited unexpectedly."}
        try:
            with proc.stdout:
                for line in proc.stdout:
                    item = json.loads(line)
                    if "message" in item:
                        with self.lock:
                            self.data["events"].append(item["message"])
                    else:
                        result = item["result"]
            proc.wait()
        except (ValueError, OSError) as exc:
            result = {"event":"paused", "reason":f"Step worker interrupted: {exc}"}
        finally:
            with self.lock:
                if self.data.get("stopping") and result["event"] != "completed":
                    result = {"event":"paused", "reason":"Stopped. Inspect the last action before resuming."}
                self.data.update(running=False, ended=time.time(), result=result)
                activity = list(self.data["events"])
                if self.job:
                    self.job.close()
                self.process = None
        # A stop can interrupt an atomic commit. Replay its journal before reporting state.
        with ws.writer():
            pass
        if result["event"] in {"paused","consultation"}:
            result = {**result, "activity":activity}
        return result

    def stop(self, run_id):
        with self.lock:
            proc = self.process
            if not proc or self.data["id"] != run_id or proc.poll() is not None:
                return
            if self.job:
                self.job.terminate()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            self.data["stopping"] = True
