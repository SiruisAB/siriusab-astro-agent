"""Warm up optional task dependencies after the workbench becomes available."""

import importlib
import threading
from time import perf_counter


def _load_runtime():
    for name in ("main", "agent_turn", "workbench_usage", "jsonschema"):
        importlib.import_module(name)


class RuntimeLoader:
    def __init__(self, load=_load_runtime):
        self._load = load
        self._lock = threading.Lock()
        self._phase = "idle"
        self._started = None
        self._elapsed = None
        self._error = ""
        self._thread = None

    def start(self):
        with self._lock:
            if self._phase != "idle":
                return
            self._phase = "loading"
            self._started = perf_counter()
            self._thread = threading.Thread(target=self._run, name="task-engine-warmup", daemon=True)
            self._thread.start()

    def _run(self):
        try:
            self._load()
        except Exception as exc:
            with self._lock:
                self._phase = "error"
                # Dependency errors can contain paths or credentials; publish only the type.
                self._error = type(exc).__name__
        else:
            with self._lock:
                self._phase = "ready"
        finally:
            with self._lock:
                self._elapsed = perf_counter() - self._started

    def snapshot(self):
        with self._lock:
            return {"phase": self._phase, "error": self._error,
                    "seconds": round(self._elapsed, 3) if self._elapsed is not None else None}

    def wait_until_ready(self):
        """Called in a task worker, never on the HTTP/UI event loop."""
        self.start()
        self._thread.join()
        result = self.snapshot()
        if result["phase"] == "error":
            raise RuntimeError(f"任务引擎初始化失败（{result['error']}），请检查 .conda 环境中的项目依赖。")


RUNTIME = RuntimeLoader()
