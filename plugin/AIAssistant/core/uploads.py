"""Files attached in the chat (models and images), stored under the plugin data folder."""
import json
import mimetypes
import os
import re
import threading
import uuid

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
MODEL_EXTENSIONS = (".stl", ".obj", ".3mf", ".amf", ".x3d", ".ply", ".dae", ".gltf", ".glb", ".ctm", ".off")
MAX_UPLOAD = 300 * 1024 * 1024
# Explicit types: the Windows registry may report e.g. image/pjpeg, which LLM APIs reject.
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif", ".bmp": "image/bmp"}


class UploadStore(object):
    def __init__(self, directory):
        self._directory = directory
        os.makedirs(directory, exist_ok=True)
        self._index_path = os.path.join(directory, "index.json")
        self._lock = threading.Lock()
        self._items = {}
        try:
            with open(self._index_path, "r", encoding="utf-8") as stream:
                self._items = json.load(stream)
        except (OSError, ValueError):
            self._items = {}

    def _save(self):
        try:
            with open(self._index_path, "w", encoding="utf-8") as stream:
                json.dump(self._items, stream, ensure_ascii=False)
        except OSError:
            pass

    @staticmethod
    def kindOf(name):
        extension = os.path.splitext(name)[1].lower()
        if extension in IMAGE_EXTENSIONS:
            return "image"
        if extension in MODEL_EXTENSIONS:
            return "model"
        return "file"

    def save(self, name, stream, length):
        if length > MAX_UPLOAD:
            raise ValueError("Файл слишком большой (максимум %d МБ)." % (MAX_UPLOAD // (1024 * 1024)))
        safe = re.sub(r"[^\w\-. ()]+", "_", os.path.basename(name or "file")).strip() or "file"
        upload_id = uuid.uuid4().hex[:12]
        folder = os.path.join(self._directory, upload_id)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, safe)
        remaining = length
        with open(path, "wb") as target:
            while remaining > 0:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                target.write(chunk)
                remaining -= len(chunk)
        extension = os.path.splitext(safe)[1].lower()
        mime = _IMAGE_MIME.get(extension) or mimetypes.guess_type(safe)[0] or "application/octet-stream"
        item = {"id": upload_id, "name": safe, "kind": self.kindOf(safe), "path": path,
                "size": os.path.getsize(path), "mime": mime}
        with self._lock:
            self._items[upload_id] = item
            self._save()
        return item

    def get(self, upload_id):
        with self._lock:
            item = self._items.get(upload_id)
        if item and os.path.isfile(item["path"]):
            return item
        return None
