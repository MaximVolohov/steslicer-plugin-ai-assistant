"""Slicing state tracking, slice results and file export."""
import os
import re
import threading
import time

from UM.Backend.Backend import BackendState
from UM.Logger import Logger
from UM.Mesh.MeshWriter import MeshWriter
from UM.Qt.Duration import DurationFormat
from UM.Workspace.WorkspaceWriter import WorkspaceWriter

from ..core.errors import ToolError
from ..core.qt_thread import is_main_thread, run_on_main
from . import api
from .settings import _round

GLICER_STALL_SECONDS = 90
GLICER_STALL_MESSAGE = ("5D-движок glicer_console запущен, но не выполняет расчёт (процесс простаивает). "
                        "Обычно это означает, что 5D-модуль STE Slicer не активирован (проверка лицензии ATSS). "
                        "Активируйте дистрибутив по инструкции или используйте режим 3D.")


def _process_cpu_seconds(process):
    """CPU time of a subprocess.Popen on Windows (kernel + user), or None."""
    handle = getattr(process, "_handle", None)
    if handle is None or os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        creation, exit_time, kernel, user = (wintypes.FILETIME(), wintypes.FILETIME(),
                                             wintypes.FILETIME(), wintypes.FILETIME())
        ok = ctypes.windll.kernel32.GetProcessTimes(wintypes.HANDLE(int(handle)), ctypes.byref(creation),
                                                    ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        total = 0
        for value in (kernel, user):
            total += (value.dwHighDateTime << 32) + value.dwLowDateTime
        return total / 1e7
    except Exception:
        return None


_STATE_NAMES = {
    BackendState.NotStarted: "not_started",
    BackendState.Processing: "processing",
    BackendState.Done: "done",
    BackendState.Error: "error",
    BackendState.Disabled: "disabled",
}


class SliceMonitor(object):
    """Follows the active backend (it changes with printing_mode) and mirrors its state.

    Signal handlers run on the main thread; wait() is called from worker threads.
    """

    def __init__(self, bus):
        self._bus = bus
        self._backend = None
        self._finished = threading.Event()
        self._last_progress_emit = 0.0
        self._seen_processing = False
        self.state = "idle"
        self.progress = 0.0
        self.error = None
        self.started_at = None
        self.finished_at = None

    # -- main thread -------------------------------------------------------
    def setup(self):
        manager = api.app().getBackendManager()
        signal = getattr(manager, "currendBackendChanged", None) or getattr(manager, "currentBackendChanged", None)
        if signal is not None:
            signal.connect(self._onBackendChanged)
        self.attach()

    def _onBackendChanged(self, *args):
        self.attach()

    def attach(self):
        backend = api.app().getBackend()
        if backend is None or backend is self._backend:
            return backend
        self._detach()
        backend.backendStateChange.connect(self._onStateChange)
        backend.processingProgress.connect(self._onProgress)
        error_signal = getattr(backend, "backendError", None)
        if error_signal is not None:
            error_signal.connect(self._onBackendError)
        self._backend = backend
        Logger.log("d", "[AIAssistant] tracking backend %s", type(backend).__name__)
        return backend

    def _detach(self):
        backend = self._backend
        if backend is None:
            return
        for name, handler in (("backendStateChange", self._onStateChange),
                              ("processingProgress", self._onProgress),
                              ("backendError", self._onBackendError)):
            signal = getattr(backend, name, None)
            if signal is not None:
                try:
                    signal.disconnect(handler)
                except Exception:
                    pass
        self._backend = None

    def _errorText(self):
        message = getattr(self._backend, "_error_message", None)
        if message is not None:
            try:
                text = message.getText()
            except Exception:
                text = getattr(message, "_text", "")
            if text:
                return re.sub(r"<[^>]+>", "", text)
        return "Слайсер сообщил об ошибке (подробности — в окне сообщений слайсера)."

    def _onStateChange(self, state):
        name = _STATE_NAMES.get(state, str(state))
        if name == "processing":
            self._seen_processing = True
            self.state = "slicing"
        elif name == "done":
            self.state = "done"
            self.progress = 1.0
            self.finished_at = time.time()
            self._finished.set()
        elif name == "error":
            self.state = "error"
            self.error = self._errorText()
            self.finished_at = time.time()
            self._finished.set()
        elif name == "disabled":
            self.state = "disabled"
            self._finished.set()
        elif name == "not_started":
            if self._seen_processing and self.state == "slicing":
                self.state = "interrupted"
            elif self.state == "done":
                self.state = "stale"  # the scene or settings changed after slicing: G-code is outdated
        self._publish("slice_state")

    def _onProgress(self, amount):
        try:
            self.progress = max(0.0, min(1.0, float(amount)))
        except (TypeError, ValueError):
            return
        if 0.0 < self.progress < 1.0:
            # The 5D pipeline (CylindricalBackend) reports progress from its sub-backends.
            self._seen_processing = True
        now = time.time()
        if now - self._last_progress_emit > 0.3 or self.progress >= 1.0:
            self._last_progress_emit = now
            self._publish("slice_progress")

    def _onBackendError(self, *args):
        self.error = self._errorText()

    def _publish(self, event_type):
        self._bus.publish(event_type, self.status())

    def status(self):
        result = {"state": self.state, "progress": round(self.progress, 3)}
        if self.error and self.state == "error":
            result["error"] = self.error
        if self.started_at:
            end = self.finished_at if self.finished_at and self.state in ("done", "error") else time.time()
            result["elapsed_s"] = round(end - self.started_at, 1)
        if self._backend is not None:
            result["backend"] = type(self._backend).__name__
        return result

    def start(self):
        if not api.scene_objects():
            raise ToolError("Нечего нарезать: в сцене нет моделей.")
        backend = self.attach()
        if backend is None:
            raise ToolError("Бэкенд слайсинга недоступен.")
        self._finished.clear()
        self._seen_processing = False
        self.state = "slicing"
        self.progress = 0.0
        self.error = None
        self.started_at = time.time()
        self.finished_at = None
        self._publish("slice_state")
        if hasattr(backend, "forceSlice"):
            backend.forceSlice()
        else:
            backend.slice()

    def stop(self):
        if self._backend is not None:
            self._backend.stopSlicing()
        self.state = "cancelled"
        self._finished.set()
        self._publish("slice_state")

    # -- worker threads ----------------------------------------------------
    def _glicerSample(self):
        """(alive, cpu_seconds) of the 5D engine process, if the active backend runs one."""
        process = getattr(self._backend, "_glicer_process", None)
        if process is None or process.poll() is not None:
            return False, None
        return True, _process_cpu_seconds(process)

    def wait(self, timeout=900.0, cancel_event=None):
        deadline = time.time() + timeout
        start = time.time()
        retried = False
        glicer_since = None  # (time, cpu) when the 5D engine was first seen running
        while True:
            if self._finished.wait(0.5):
                return self.state
            alive, cpu = run_on_main(self._glicerSample)
            if alive and cpu is not None:
                if glicer_since is None or cpu - glicer_since[1] > 0.5:
                    glicer_since = (time.time(), cpu)
                elif time.time() - glicer_since[0] > GLICER_STALL_SECONDS:
                    run_on_main(self.stop)
                    self.state = "error"
                    self.error = GLICER_STALL_MESSAGE
                    self._publish("slice_state")
                    return "error"
            else:
                glicer_since = None
            if cancel_event is not None and cancel_event.is_set():
                run_on_main(self.stop)
                return "cancelled"
            now = time.time()
            if not self._seen_processing and now - start > 12.0:
                if run_on_main(self._backendBusy):
                    self._seen_processing = True
                    continue
                if retried:
                    self.state = "error"
                    self.error = "Слайсинг не запустился (бэкенд не перешёл в состояние обработки)."
                    return "error"
                retried = True
                start = now
                run_on_main(self._kick)
            if self.state == "interrupted" and now - (self.started_at or now) > 2.0:
                # Settings changed while slicing; the backend restarts on its own when auto-slicing is on.
                run_on_main(self._kick)
                self._seen_processing = False
                start = now
            if now > deadline:
                return "timeout"

    def _backendBusy(self):
        backend = self._backend
        if backend is None:
            return False
        return bool(getattr(backend, "_slicing", False) or getattr(backend, "_start_slice_job", None)
                    or getattr(backend, "_process_layers_job", None))

    def _kick(self):
        backend = self.attach()
        self.state = "slicing"
        if backend is not None:
            backend.forceSlice() if hasattr(backend, "forceSlice") else backend.slice()


# ---------------------------------------------------------------------------
# Results and export (main thread)

def _duration_seconds(duration):
    try:
        return int(duration.getDisplayString(DurationFormat.Format.Seconds))
    except Exception:
        try:
            return int(duration)
        except Exception:
            return 0


def human_time(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return "%d ч %02d мин" % (hours, minutes)
    return "%d мин" % max(minutes, 1 if seconds else 0)


def slice_result():
    info = api.app().getPrintInformation()
    seconds = _duration_seconds(info.currentPrintTime)
    lengths = [_round(float(v)) for v in (info.materialLengths or [])]
    weights = [_round(float(v)) for v in (info.materialWeights or [])]
    costs = [_round(float(v)) for v in (info.materialCosts or [])]
    names = [str(v) for v in (info.materialNames or [])]
    features = {}
    try:
        for feature, duration in info.printTimes().items():
            value = _duration_seconds(duration)
            if value:
                features[feature] = value
    except Exception:
        pass
    result = {
        "print_time_seconds": seconds,
        "print_time": human_time(seconds),
        "material_length_m": lengths,
        "material_weight_g": weights,
        "material_names": names,
        "total_weight_g": _round(sum(weights)) if weights else 0,
        "job_name": info.jobName,
    }
    if any(costs):
        result["material_cost"] = costs
    if features:
        result["time_by_feature_s"] = features
    return result


def _gcode_summary(path):
    summary = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index > 200:
                    break
                if line.startswith(";PRINTING_MODE:"):
                    summary["printing_mode"] = line.split(":", 1)[1].strip()
                elif line.startswith(";TIME:"):
                    summary["time_s"] = line.split(":", 1)[1].strip()
                elif line.startswith(";Filament used:"):
                    summary["filament_used"] = line.split(":", 1)[1].strip()
                elif line.startswith(";Layer height:"):
                    summary["layer_height"] = line.split(":", 1)[1].strip()
                elif line.startswith(";FLAVOR:"):
                    summary["flavor"] = line.split(":", 1)[1].strip()
    except OSError:
        pass
    return summary


def _require_worker_thread():
    # STE Slicer's @call_on_qt_thread (used by GCodeWriter's preview and the 3MF workspace writer) has
    # no "already on the main thread" shortcut: it posts to the main thread and waits, so calling the
    # writers from the main thread deadlocks the application.  Like Cura's WriteFileJob, write from a
    # worker thread and let the decorator marshal what must run on the Qt thread.
    if is_main_thread():
        raise ToolError("Экспорт нельзя выполнять в главном потоке слайсера.")


def export_gcode(path):
    """Worker thread only (see _require_worker_thread)."""
    _require_worker_thread()
    writer = api.app().getMeshFileHandler().getWriter("GCodeWriter")
    if writer is None:
        raise ToolError("Плагин GCodeWriter недоступен.")
    ok = False
    try:
        with open(path, "wt", encoding="utf-8") as stream:
            ok = writer.write(stream, None, MeshWriter.OutputMode.TextMode)
    finally:
        if not ok and os.path.exists(path):
            os.remove(path)
    if not ok:
        raise ToolError(writer.getInformation() or "Не удалось записать G-code.",
                        hint="Сначала выполните нарезку (start_slice).")
    result = {"path": path, "size_kb": round(os.path.getsize(path) / 1024.0, 1)}
    result.update(_gcode_summary(path))
    return result


def save_project(path):
    """Worker thread only: ThreeMFWorkspaceWriter.write marshals itself to the Qt thread."""
    _require_worker_thread()
    writer = api.app().getWorkspaceFileHandler().getWriter("3MFWriter")
    if writer is None:
        raise ToolError("Плагин 3MFWriter недоступен.")
    root = run_on_main(lambda: api.scene().getRoot())
    ok = False
    try:
        with open(path, "wb") as stream:
            ok = writer.write(stream, [root], WorkspaceWriter.OutputMode.BinaryMode)
    finally:
        if not ok and os.path.exists(path):
            os.remove(path)
    if not ok:
        raise ToolError("Не удалось сохранить проект 3MF.")
    return {"path": path, "size_kb": round(os.path.getsize(path) / 1024.0, 1)}


def default_basename():
    info = api.app().getPrintInformation()
    name = info.jobName if info is not None else ""
    if not name:
        objects = api.scene_objects()
        name = os.path.splitext(objects[0].getName())[0] if objects else "print"
    return re.sub(r"[^\w\-. ]+", "_", name).strip() or "print"
