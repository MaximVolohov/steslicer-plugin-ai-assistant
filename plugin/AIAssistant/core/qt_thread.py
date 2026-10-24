"""Running slicer API calls on the Qt main thread.

The scene, the setting stacks and the backends may only be touched from the
Qt main thread.  Tool calls arrive from HTTP request threads and from the
agent worker thread, so every slicer access goes through run_on_main().
"""
import threading
import traceback

from UM.Application import Application
from UM.Logger import Logger


class MainThreadTimeout(Exception):
    pass


def is_main_thread():
    return threading.current_thread() is threading.main_thread()


def run_on_main(func, *args, **kwargs):
    """Call func(*args, **kwargs) on the Qt main thread and return its result.

    Blocks the calling (worker) thread until the call finishes.  When already on
    the main thread the function is called directly, so this never deadlocks.
    Pass _timeout=<seconds> to change the default 60 s limit.
    """
    timeout = kwargs.pop("_timeout", 60.0)
    if is_main_thread():
        return func(*args, **kwargs)

    done = threading.Event()
    box = {}

    def _call():
        try:
            box["result"] = func(*args, **kwargs)
        except BaseException as e:  # re-raised in the calling thread
            box["error"] = e
            box["trace"] = traceback.format_exc()
        finally:
            done.set()

    Application.getInstance().callLater(_call)
    if not done.wait(timeout):
        raise MainThreadTimeout("Slicer did not respond within %d s" % timeout)
    if "error" in box:
        Logger.log("d", "[AIAssistant] main thread call failed:\n%s", box.get("trace", ""))
        raise box["error"]
    return box.get("result")
