"""Quality profiles, custom profiles (quality_changes) and materials.  Main thread only."""
import collections

from ..core.errors import ToolError
from . import api
from .settings import _jsonable, _round


def _quality_manager():
    return api.app().getQualityManager()


def _layer_height_of(group):
    try:
        node = group.node_for_global
        if node is not None:
            container = node.getContainer()
            if container is not None:
                value = container.getProperty("layer_height", "value")
                if value is not None:
                    return _round(float(value))
    except Exception:
        pass
    return None


def list_quality_profiles():
    stack = api.global_stack()
    qm = _quality_manager()
    mm = api.app().getMachineManager()
    default_layer = stack.definition.getProperty("layer_height", "default_value")
    groups = qm.getQualityGroups(stack)
    profiles = []
    for quality_type, group in groups.items():
        layer = _layer_height_of(group)
        profiles.append({
            "quality_type": quality_type,
            "name": group.name,
            "available": bool(group.is_available),
            "layer_height": layer if layer is not None else _round(default_layer),
        })
    profiles.sort(key=lambda p: (p["layer_height"] or 0))
    custom = []
    for name, group in qm.getQualityChangesGroups(stack).items():
        custom.append({"name": name, "quality_type": getattr(group, "quality_type", None)})
    quality_changes = stack.qualityChanges
    return {
        "active": {
            "quality_type": mm.activeQualityType,
            "name": mm.activeQualityOrQualityChangesName,
            "custom_profile": quality_changes.getName() if quality_changes.getId() != "empty_quality_changes" else None,
        },
        "quality_profiles": profiles,
        "custom_profiles": custom,
        "note": "Профили качества общие (задают в основном высоту слоя); остальное настраивается через set_settings.",
    }


def set_quality_profile(quality_type=None, custom_profile=None):
    stack = api.global_stack()
    qm = _quality_manager()
    mm = api.app().getMachineManager()
    if custom_profile:
        groups = qm.getQualityChangesGroups(stack)
        group = groups.get(custom_profile)
        if group is None:
            raise ToolError("Пользовательский профиль '%s' не найден." % custom_profile,
                            hint="Доступны: " + ", ".join(groups.keys()) if groups else "Пользовательских профилей нет.")
        mm.setQualityChangesGroup(group, no_dialog=True)
    elif quality_type:
        groups = qm.getQualityGroups(stack)
        group = groups.get(quality_type)
        if group is None:
            for candidate in groups.values():
                if candidate.name.lower() == str(quality_type).lower():
                    group = candidate
                    break
        if group is None:
            raise ToolError("Профиль качества '%s' не найден." % quality_type,
                            hint="Доступны: " + ", ".join(groups.keys()))
        if not group.is_available:
            raise ToolError("Профиль '%s' недоступен для текущего принтера/материала." % quality_type)
        mm.setQualityGroup(group, no_dialog=True)
    else:
        raise ToolError("Укажите quality_type или custom_profile.")
    return {
        "quality_type": mm.activeQualityType,
        "name": mm.activeQualityOrQualityChangesName,
        "layer_height": _round(stack.getProperty("layer_height", "value")),
    }


def begin_create_profile(name):
    """Create a quality_changes profile from the current user changes.

    Returns the unique name; activation happens in activate_custom_profile()
    once the QualityManager has picked up the new containers.
    """
    from steslicer.Settings.ContainerManager import ContainerManager
    if not name or not str(name).strip():
        raise ToolError("Укажите имя профиля.")
    unique = ContainerManager.getInstance().makeUniqueName(str(name).strip())
    _quality_manager().createQualityChanges(unique)
    return unique


def find_custom_profile(name):
    groups = _quality_manager().getQualityChangesGroups(api.global_stack())
    return groups.get(name)


def activate_custom_profile(name):
    group = find_custom_profile(name)
    if group is None:
        return None
    api.app().getMachineManager().setQualityChangesGroup(group, no_dialog=True)
    return {"custom_profile": name, "name": api.app().getMachineManager().activeQualityOrQualityChangesName}


def save_profile():
    from steslicer.Settings.ContainerManager import ContainerManager
    stack = api.global_stack()
    if stack.qualityChanges.getId() == "empty_quality_changes":
        raise ToolError("Активен встроенный профиль — его нельзя перезаписать.",
                        hint="Сначала создайте свой профиль: create_profile(name).")
    ok = ContainerManager.getInstance().updateQualityChanges()
    return {"saved": bool(ok) if ok is not None else True, "custom_profile": stack.qualityChanges.getName()}


# ---------------------------------------------------------------------------
# Materials

def _available_materials(extruder_index):
    stack = api.global_stack()
    ext = api.extruder_stack(extruder_index, stack)
    manager = api.app().getMaterialManager()
    materials = manager.getAvailableMaterialsForMachineExtruder(stack, ext)
    return ext, materials or {}


def _metadata(node):
    metadata = getattr(node, "metadata", None)
    if metadata is None and hasattr(node, "getMetadata"):
        metadata = node.getMetadata()
    return metadata or {}


def list_materials(extruder=0, query=None, details=True):
    ext, materials = _available_materials(extruder)
    current = ext.material.getMetaDataEntry("base_file") if ext.material else None
    query_text = (query or "").strip().lower()
    result = []
    for root_id, node in materials.items():
        meta = _metadata(node)
        item = collections.OrderedDict()
        item["id"] = root_id
        item["name"] = meta.get("name", root_id)
        item["brand"] = meta.get("brand")
        item["type"] = meta.get("material")
        item["diameter"] = meta.get("approximate_diameter")
        haystack = " ".join(str(v) for v in item.values()).lower()
        if query_text and not all(token in haystack for token in query_text.split()):
            continue
        if details:
            try:
                container = node.getContainer()
                item["print_temperature"] = _jsonable(container.getProperty("material_print_temperature", "value"))
                item["bed_temperature"] = _jsonable(container.getProperty("material_bed_temperature", "value"))
            except Exception:
                pass
        item["active"] = root_id == current
        result.append(item)
    result.sort(key=lambda m: (str(m.get("brand")), str(m.get("name"))))
    return {"extruder": int(extruder), "current": current, "count": len(result), "materials": result}


def set_material(material_id, extruder=0):
    ext, materials = _available_materials(extruder)
    node = materials.get(material_id)
    if node is None:
        lowered = str(material_id).lower()
        for root_id, candidate in materials.items():
            meta = _metadata(candidate)
            if lowered in (root_id.lower(), str(meta.get("name", "")).lower()):
                node = candidate
                material_id = root_id
                break
    if node is None:
        raise ToolError("Материал '%s' недоступен для экструдера %s." % (material_id, extruder),
                        hint="Список материалов: list_materials(extruder=%s)" % extruder)
    api.app().getMachineManager().setMaterial(str(int(extruder)), node)
    material = ext.material
    return {
        "extruder": int(extruder),
        "material": material.getMetaDataEntry("base_file") if material else None,
        "name": material.getName() if material else None,
        "print_temperature": _jsonable(ext.getProperty("material_print_temperature", "value")),
        "bed_temperature": _jsonable(ext.getProperty("material_bed_temperature", "value")),
    }
