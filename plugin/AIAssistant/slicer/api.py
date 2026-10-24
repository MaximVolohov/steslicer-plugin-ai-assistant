"""Shared helpers over the STE Slicer API.  Everything here runs on the Qt main thread.

Coordinates
-----------
The Uranium scene is Y-up with the build plate centre at the origin.  Tools talk
in printer coordinates (as the slicer UI shows them): X right, Y back, Z up, mm,
origin in the centre of the build plate:

    printer X =  scene x
    printer Y = -scene z
    printer Z =  scene y
"""
import weakref

import numpy

from UM.Math.Vector import Vector
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator

from steslicer.SteSlicerApplication import SteSlicerApplication

from ..core.errors import ToolError


def app():
    return SteSlicerApplication.getInstance()


def global_stack():
    stack = app().getGlobalContainerStack()
    if stack is None:
        raise ToolError("В слайсере не выбран принтер (нет активной машины).")
    return stack


def extruder_stacks(stack=None):
    stack = stack or global_stack()
    return [stack.extruders[key] for key in sorted(stack.extruders.keys(), key=lambda k: int(k))]


def extruder_stack(index, stack=None):
    stack = stack or global_stack()
    key = str(int(index))
    if key not in stack.extruders:
        raise ToolError("Экструдер %s не существует. Доступны: %s" % (key, ", ".join(sorted(stack.extruders.keys()))))
    return stack.extruders[key]


def scene():
    return app().getController().getScene()


def to_printer(vector):
    return [round(float(vector.x), 3), round(float(-vector.z), 3), round(float(vector.y), 3)]


def printer_vector_to_scene(x, y, z):
    return Vector(float(x), float(z), -float(y))


def _setting_flag(node, key):
    stack = node.callDecoration("getStack")
    if not stack:
        return False
    try:
        return bool(stack.getProperty(key, "value"))
    except Exception:
        return False


NON_PRINTING_ROLES = ("support_mesh", "anti_overhang_mesh", "cutting_mesh", "infill_mesh")


def node_role(node):
    for key in NON_PRINTING_ROLES:
        if _setting_flag(node, key):
            return key
    return "model"


def scene_objects():
    """Top-level printable objects (groups count as one object)."""
    result = []
    for node in scene().getRoot().getChildren():
        if node.callDecoration("isGroup") or (node.callDecoration("isSliceable") and node.getMeshData() is not None):
            result.append(node)
    return result


def all_mesh_nodes(node):
    """The node itself or, for a group, all mesh-carrying descendants."""
    if node.getMeshData() is not None and not node.callDecoration("isGroup"):
        return [node]
    return [n for n in DepthFirstIterator(node) if n.getMeshData() is not None and n is not node]


def world_mesh(node):
    """Vertices (printer coordinates, float64) and triangle indices of a scene object."""
    vertex_blocks = []
    face_blocks = []
    offset = 0
    for mesh_node in all_mesh_nodes(node):
        mesh = mesh_node.getMeshData()
        verts = mesh.getVertices()
        if verts is None or len(verts) == 0:
            continue
        verts = numpy.asarray(verts, dtype=numpy.float64)
        if mesh.hasIndices():
            faces = numpy.asarray(mesh.getIndices(), dtype=numpy.int64)
        else:
            count = (len(verts) // 3) * 3
            faces = numpy.arange(count, dtype=numpy.int64).reshape(-1, 3)
        matrix = mesh_node.getWorldTransformation().getData()
        verts = verts.dot(matrix[0:3, 0:3].T) + matrix[0:3, 3]
        printer = numpy.column_stack((verts[:, 0], -verts[:, 2], verts[:, 1]))
        vertex_blocks.append(printer)
        face_blocks.append(faces + offset)
        offset += len(printer)
    if not vertex_blocks:
        raise ToolError("У объекта нет геометрии.")
    return numpy.vstack(vertex_blocks), numpy.vstack(face_blocks)


class ObjectRegistry(object):
    """Stable, short ids (obj1, obj2...) for scene nodes, without keeping nodes alive."""

    def __init__(self):
        self._entries = []  # [(id, weakref)]
        self._counter = 0

    def _prune(self):
        self._entries = [(i, ref) for i, ref in self._entries if ref() is not None]

    def idFor(self, node):
        self._prune()
        for object_id, ref in self._entries:
            if ref() is node:
                return object_id
        self._counter += 1
        object_id = "obj%d" % self._counter
        self._entries.append((object_id, weakref.ref(node)))
        return object_id

    def find(self, object_id):
        self._prune()
        live = scene_objects()
        for known_id, ref in self._entries:
            node = ref()
            if known_id == object_id and node is not None and any(n is node for n in live):
                return node
        # Accept the node name as well, the LLM sometimes uses it.
        for node in live:
            if node.getName() == object_id:
                return node
        return None

    def get(self, object_id):
        if object_id in (None, "", "auto"):
            objects = scene_objects()
            if len(objects) == 1:
                return objects[0]
            if not objects:
                raise ToolError("В сцене нет моделей. Сначала загрузите модель (load_model).")
            raise ToolError("В сцене несколько объектов, укажите object_id. Объекты: %s"
                            % ", ".join(self.idFor(n) for n in objects))
        node = self.find(object_id)
        if node is None:
            raise ToolError("Объект '%s' не найден. Актуальный список даёт list_objects." % object_id)
        return node


registry = ObjectRegistry()
