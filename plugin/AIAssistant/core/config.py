"""Plugin settings stored in the slicer preferences (steslicer.cfg, section ai_assistant)."""
import collections
import os

from UM.Resources import Resources

PREFIX = "ai_assistant/"

DEFAULTS = collections.OrderedDict([
    ("api_base_url", ""),
    ("api_key", ""),
    ("model", ""),
    ("temperature", 0.2),
    ("vision", True),
    ("verify_ssl", True),
    ("request_timeout", 180),
    ("max_steps", 30),
    ("port", 8765),
    ("output_dir", ""),
    ("open_browser_on_start", False),
])

# Keys the SPA may change through /api/settings.
EDITABLE = ("api_base_url", "api_key", "model", "temperature", "vision", "verify_ssl",
            "request_timeout", "max_steps", "output_dir", "open_browser_on_start")


def _to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _coerce(value, default):
    """Preferences come back from steslicer.cfg as strings: restore the default's type."""
    try:
        if isinstance(default, bool):
            return _to_bool(value)
        if isinstance(default, int):
            return int(float(value))
        if isinstance(default, float):
            return float(value)
    except (TypeError, ValueError):
        return default
    return "" if value is None else str(value)


class Config(object):
    def __init__(self, preferences):
        self._preferences = preferences
        for key, default in DEFAULTS.items():
            preferences.addPreference(PREFIX + key, default)

    def get(self, key):
        return _coerce(self._preferences.getValue(PREFIX + key), DEFAULTS[key])

    def set(self, key, value):
        if key not in DEFAULTS:
            raise KeyError(key)
        self._preferences.setValue(PREFIX + key, _coerce(value, DEFAULTS[key]))

    def update(self, values):
        changed = []
        for key, value in values.items():
            if key not in EDITABLE:
                continue
            if key == "api_key" and (value is None or value == self.maskedKey()):
                continue  # the SPA sends the mask back when the key was not edited
            self.set(key, value)
            changed.append(key)
        return changed

    def maskedKey(self):
        key = self.get("api_key")
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return key[:3] + "*" * 6 + key[-4:]

    def publicDict(self):
        """Settings for the UI: the API key is never sent back in clear text."""
        result = collections.OrderedDict()
        for key in DEFAULTS:
            result[key] = self.get(key)
        result["api_key"] = self.maskedKey()
        result["has_api_key"] = bool(self.get("api_key"))
        result["output_dir"] = self.outputDir()
        return result

    def isLlmConfigured(self):
        return bool(self.get("api_base_url")) and bool(self.get("model"))

    def outputDir(self):
        path = self.get("output_dir")
        if not path:
            path = os.path.join(os.path.expanduser("~"), "Documents", "STE AI Assistant")
        return path


def data_dir():
    """%APPDATA%/steslicer/<version>/ai_assistant - history, uploads, CA bundle."""
    path = os.path.join(Resources.getDataStoragePath(), "ai_assistant")
    os.makedirs(path, exist_ok=True)
    return path
