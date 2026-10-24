"""Scene objects: load, inspect, transform, arrange, delete.  Main thread only.

Positions are printer coordinates (see api.py): x/y of the bounding box centre,
z of the bounding box bottom (0 = standing on the build plate).
"""
import math
import os

import numpy

from PyQt5.QtCore import QUrl

from UM.Math.Quaternion import Quaternion
from UM.Math.Vector import Vector
from UM.Operations.GroupedOperation import GroupedOperation
from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
from UM.Operations.RotateOperation import RotateOperation
from UM.Operations.ScaleOperation import ScaleOperation
from UM.Operations.SetTransformOperation import SetTransformOperation
from UM.Operations.TranslateOperation import TranslateOperation

from ..core.errors import ToolError
from . import api
from .settings import _round

MODEL_EXTENSIONS = (".stl", ".obj", ".3mf", ".amf", ".x3d", ".ply", ".dae", ".gltf", ".glb", ".ctm", ".off")


def _outside(node):
    method = getattr(node, "isOutsideBuildArea", None)
    if callable(method):
        try:
            return bool(method())
        except Exception:
            pass
    return bool(getattr(node, "_outside_buildarea", False))


def mesh_stats(node):
    verts, faces = api.world_mesh(node)
    tri = verts[faces]
    cross = numpy.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    volume = abs(float(numpy.einsum("ij,ij->i", tri[:, 0], cross).sum()) / 6.0)
    area = float(numpy.linalg.norm(cross, axis=1).sum() / 2.0)
    return {"volume_cm3": round(volume / 1000.0, 3), "surface_cm2": round(area / 100.0, 2), "triangles": int(len(faces))}


def object_info(node, detailed=False):
    bbox = node.getBoundingBox()
    scale = node.getScale()
    info = {
        "id": api.registry.idFor(node),
        "name": node.getName(),
        "position": {"x": _round(float(bbox.center.x)), "y": _round(float(-bbox.center.z)), "z": _round(float(bbox.bottom))},
        "size": {"x": _round(float(bbox.width)), "y": _round(float(bbox.depth)), "z": _round(float(bbox.height))},
        "scale": {"x": _round(float(scale.x)), "y": _round(float(scale.z)), "z": _round(float(scale.y))},
        "outside_build_area": _outside(node),
        "role": api.node_role(node),
    }
    extruder = node.callDecoration("getActiveExtruderPosition")
    if extruder is not None:
        info["extruder"] = int(extruder)
    if node.callDecoration("isGroup"):
        info["group_of"] = len(api.all_mesh_nodes(node))
    if detailed:
        info.update(mesh_stats(node))
        mesh = node.getMeshData()
        if mesh is not None and hasattr(mesh, "getFileName") and mesh.getFileName():
            info["file"] = mesh.getFileName()
    return info


def list_objects():
    objects = [object_info(n) for n in api.scene_objects()]
    return {"count": len(objects), "objects": objects,
            "coordinates": "мм; X/Y — центр габарита от центра стола, Z — низ модели (0 = на столе)"}


# ---------------------------------------------------------------------------
# Loading

def object_ids():
    return [id(n) for n in api.scene_objects()]


def start_load(path):
    if not path:
        raise ToolError("Не указан путь к файлу модели.")
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        raise ToolError("Файл не найден: %s" % path)
    extension = os.path.splitext(path)[1].lower()
    supported = MODEL_EXTENSIONS
    try:
        supported = tuple("." + e.lower() for e in api.app().getMeshFileHandler().getSupportedFileTypesRead().keys())
    except Exception:
        pass
    if extension not in supported:
        raise ToolError("Формат %s не поддерживается слайсером." % extension, hint="Поддерживаются: " + ", ".join(sorted(supported)))
    before = object_ids()
    url = QUrl.fromLocalFile(path)
    try:
        api.app().readLocalFile(url, True)  # skip the "open as project?" dialog for .3mf
    except TypeError:
        api.app().readLocalFile(url)
    return before


def new_objects(before):
    known = set(before)
    return [n for n in api.scene_objects() if id(n) not in known]


def describe_new(before):
    return [object_info(n, detailed=True) for n in new_objects(before)]


# ---------------------------------------------------------------------------
# Transformations

def drop_to_plate(node):
    bbox = node.getBoundingBox()
    if abs(float(bbox.bottom)) > 1e-3:
        TranslateOperation(node, Vector(0, -float(bbox.bottom), 0)).push()


def move(node, x=None, y=None, z=None, relative=False):
    bbox = node.getBoundingBox()
    current = (float(bbox.center.x), float(-bbox.center.z), float(bbox.bottom))
    if relative:
        delta = (float(x or 0), float(y or 0), float(z or 0))
    else:
        target = (current[0] if x is None else float(x),
                  current[1] if y is None else float(y),
                  0.0 if z is None else float(z))
        delta = tuple(t - c for t, c in zip(target, current))
    TranslateOperation(node, api.printer_vector_to_scene(*delta)).push()
    return object_info(node)


def center(node):
    move(node, 0.0, 0.0, 0.0)
    return object_info(node)


_AXES = {
    "x": Vector(1, 0, 0),    # printer X  = scene x
    "y": Vector(0, 0, -1),   # printer Y  = -scene z
    "z": Vector(0, 1, 0),    # printer Z  = scene y
}


def rotate(node, axis, angle_deg):
    axis_key = str(axis).strip().lower()
    if axis_key not in _AXES:
        raise ToolError("Ось должна быть x, y или z.")
    rotation = Quaternion.fromAngleAxis(math.radians(float(angle_deg)), _AXES[axis_key])
    RotateOperation(node, rotation, rotate_around_point=node.getBoundingBox().center).push()
    drop_to_plate(node)
    return object_info(node)


def face_down(node, direction):
    """Rotate so that the given direction (printer coordinates, current pose) points down (-Z)."""
    try:
        dx, dy, dz = [float(v) for v in direction]
    except (TypeError, ValueError):
        raise ToolError("direction должен быть списком из трёх чисел [x, y, z].")
    vector = api.printer_vector_to_scene(dx, dy, dz)
    if vector.length() < 1e-9:
        raise ToolError("Нулевой вектор направления.")
    rotation = Quaternion.rotationTo(vector.normalized(), Vector(0.0, -1.0, 0.0))
    RotateOperation(node, rotation, rotate_around_point=node.getBoundingBox().center).push()
    drop_to_plate(node)
    return object_info(node)


def reset_orientation(node):
    SetTransformOperation(node, None, Quaternion(), None).push()
    drop_to_plate(node)
    return object_info(node)


def scale(node, factor=None, size=None, uniform=True):
    current = node.getScale()
    bbox = node.getBoundingBox()
    if factor is not None:
        fx = fy = fz = float(factor)
    elif size:
        dims = {"x": float(bbox.width), "y": float(bbox.depth), "z": float(bbox.height)}
        ratios = {}
        for axis in ("x", "y", "z"):
            if size.get(axis) is not None:
                if dims[axis] <= 0:
                    raise ToolError("Нулевой размер модели по оси %s." % axis)
                ratios[axis] = float(size[axis]) / dims[axis]
        if not ratios:
            raise ToolError("size должен содержать хотя бы одну из осей x, y, z.")
        if uniform:
            common = list(ratios.values())[0]
            fx = fy = fz = common
        else:
            fx, fy, fz = ratios.get("x", 1.0), ratios.get("y", 1.0), ratios.get("z", 1.0)
    else:
        raise ToolError("Укажите factor или size.")
    if min(fx, fy, fz) <= 0:
        raise ToolError("Масштаб должен быть положительным.")
    # printer (x, y, z) -> scene (x, z, y)
    new_scale = Vector(float(current.x) * fx, float(current.y) * fz, float(current.z) * fy)
    ScaleOperation(node, new_scale, set_scale=True).push()
    drop_to_plate(node)
    return object_info(node)


def delete(node):
    object_id = api.registry.idFor(node)
    RemoveSceneNodeOperation(node).push()
    api.scene().sceneChanged.emit(node)
    return {"deleted": object_id}


def clear():
    count = len(api.scene_objects())
    api.app().deleteAll()
    return {"deleted_objects": count}


def arrange_all():
    api.app().arrangeAll()


def positions_signature():
    signature = []
    for node in api.scene_objects():
        position = node.getWorldPosition()
        signature.append((round(float(position.x), 2), round(float(position.y), 2), round(float(position.z), 2)))
    return signature
