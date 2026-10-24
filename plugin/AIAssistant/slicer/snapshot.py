"""PNG snapshots of the scene for the chat and for the LLM's visual self-check.  Main thread only."""
from PyQt5.QtCore import QBuffer, QIODevice, Qt

from ..core.errors import ToolError
from . import api


def _to_png(image):
    buffer = QBuffer()
    buffer.open(QIODevice.ReadWrite)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def take(view="model", width=640, height=480):
    width = max(64, min(int(width), 1600))
    height = max(64, min(int(height), 1200))
    if view == "window":
        window = api.app().getMainWindow()
        if window is None:
            raise ToolError("Главное окно слайсера недоступно.")
        image = window.grabWindow()
        if image is not None and not image.isNull() and image.width() > width:
            image = image.scaledToWidth(width, Qt.SmoothTransformation)
    else:
        if not api.scene_objects():
            raise ToolError("В сцене нет моделей для снимка.")
        from steslicer.Snapshot import Snapshot
        image = Snapshot.snapshot(width=width, height=height)
    if image is None or image.isNull():
        raise ToolError("Не удалось получить изображение сцены.")
    return _to_png(image), image.width(), image.height()
