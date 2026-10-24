"""Machine info, settings and printing mode.  Main thread only."""
import collections
import json
import re

from UM.i18n import i18nCatalog

from ..core.errors import ToolError
from . import api

_catalog = i18nCatalog("fdmprinter.def.json")

PRINTING_MODES = collections.OrderedDict([
    ("classic", "3D"),
    ("cylindrical_full", "5D"),
    ("cylindrical", "5D Spiral"),
    ("discrete", "5D Discrete (Alpha)"),
    ("spherical", "5D Spherical (Alpha)"),
    ("spherical_full", "5D Spherical Full (Alpha)"),
])

_MODE_ALIASES = {
    "3d": "classic", "classic": "classic", "3-axis": "classic",
    "5d": "cylindrical_full", "5d full": "cylindrical_full", "cylindrical_full": "cylindrical_full",
    "5d spiral": "cylindrical", "spiral": "cylindrical", "cylindrical": "cylindrical",
    "discrete": "discrete", "5d discrete": "discrete",
    "spherical": "spherical", "5d spherical": "spherical",
    "spherical_full": "spherical_full",
}

# Machine geometry, firmware and similar keys are not the agent's business.
_FORBIDDEN_PREFIXES = ("machine_",)


def _round(value):
    if isinstance(value, float):
        return round(value, 4)
    return value


def _jsonable(value):
    if isinstance(value, (bool, int, float, str)) or value is None:
        return _round(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return dict((str(k), _jsonable(v)) for k, v in value.items())
    return str(value)


def find_definition(key, stack=None):
    stack = stack or api.global_stack()
    found = stack.definition.findDefinitions(key=key)
    return found[0] if found else None


def _labels(definition):
    label = definition.label or definition.key
    return label, _catalog.i18nc(definition.key + " label", label)


def _require_definition(key, stack):
    definition = find_definition(key, stack)
    if definition is None:
        suggestions = [item["key"] for item in search_settings(key.replace("_", " "), limit=5)["results"]]
        raise ToolError("Настройка '%s' не существует." % key,
                        hint="Похожие: " + ", ".join(suggestions) if suggestions else "Используйте search_settings.")
    if definition.type == "category":
        raise ToolError("'%s' — это категория, а не настройка." % key)
    return definition


def _is_per_extruder(definition):
    return bool(getattr(definition, "settable_per_extruder", True))


def _source_of(stack, key):
    """Which container of the stack provides the value (user, quality, material, definition...)."""
    for container in stack.getContainers():
        try:
            if container.getProperty(key, "value") is not None:
                kind = container.getMetaDataEntry("type")
                if not kind:
                    kind = "definition"
                return kind
        except Exception:
            continue
    return "global"


def used_extruders():
    positions = set()
    for node in api.scene_objects():
        for mesh_node in api.all_mesh_nodes(node):
            pos = mesh_node.callDecoration("getActiveExtruderPosition")
            if pos is not None:
                positions.add(int(pos))
    return sorted(positions) or [0]


def _resolve_targets(definition, extruder):
    stack = api.global_stack()
    if not _is_per_extruder(definition):
        return [("global", stack)]
    if extruder is None or extruder == "":
        indices = used_extruders()
    elif str(extruder).lower() == "all":
        indices = [i for i, ext in enumerate(api.extruder_stacks(stack)) if getattr(ext, "isEnabled", True)]
    else:
        indices = [int(extruder)]
    return [("extruder %d" % i, api.extruder_stack(i, stack)) for i in indices]


# ---------------------------------------------------------------------------
# Reading

def describe(key, extruder=None):
    stack = api.global_stack()
    definition = _require_definition(key, stack)
    label, label_ru = _labels(definition)
    result = collections.OrderedDict()
    result["key"] = key
    result["label"] = label_ru
    if label_ru != label:
        result["label_en"] = label
    result["type"] = definition.type
    if definition.unit:
        result["unit"] = definition.unit
    if definition.type == "enum":
        result["options"] = collections.OrderedDict(
            (k, _catalog.i18nc(key + " option " + k, v)) for k, v in definition.options.items())
    per_extruder = _is_per_extruder(definition)
    result["per_extruder"] = per_extruder
    if per_extruder:
        values = collections.OrderedDict()
        sources = collections.OrderedDict()
        if extruder is None or str(extruder).lower() == "all":
            stacks = list(enumerate(api.extruder_stacks(stack)))
        else:
            stacks = [(int(extruder), api.extruder_stack(extruder, stack))]
        for index, ext in stacks:
            values[str(index)] = _jsonable(ext.getProperty(key, "value"))
            sources[str(index)] = _source_of(ext, key)
        result["values_per_extruder"] = values
        result["source_per_extruder"] = sources
        reference = stacks[0][1]
    else:
        result["value"] = _jsonable(stack.getProperty(key, "value"))
        result["source"] = _source_of(stack, key)
        reference = stack
    for prop in ("minimum_value", "maximum_value", "minimum_value_warning", "maximum_value_warning"):
        try:
            limit = reference.getProperty(key, prop)
        except Exception:
            limit = None
        if isinstance(limit, (int, float)) and not isinstance(limit, bool):
            result[prop] = _round(float(limit))
    try:
        result["enabled"] = bool(reference.getProperty(key, "enabled"))
    except Exception:
        pass
    return result


def get_settings(keys, extruder=None):
    if isinstance(keys, str):
        keys = [keys]
    results = []
    for key in keys:
        try:
            results.append(describe(key, extruder))
        except ToolError as e:
            results.append({"key": key, "error": e.message, "hint": e.hint})
    return {"settings": results}


_index_cache = {"definition_id": None, "items": []}


def _settings_index(stack):
    definition_id = stack.definition.getId()
    if _index_cache["definition_id"] != definition_id:
        items = []
        for definition in stack.definition.findDefinitions():
            if definition.type == "category":
                continue
            label, label_ru = _labels(definition)
            desc = definition.description or ""
            desc_ru = _catalog.i18nc(definition.key + " description", desc)
            category = definition
            while category.parent is not None:
                category = category.parent
            items.append((definition, label, label_ru, desc + " " + desc_ru, category.key))
        _index_cache["definition_id"] = definition_id
        _index_cache["items"] = items
    return _index_cache["items"]


def search_settings(query, limit=15, include_disabled=True):
    stack = api.global_stack()
    tokens = [t for t in re.split(r"[^\w]+", query.lower()) if t]
    if not tokens:
        raise ToolError("Пустой запрос поиска.")
    scored = []
    for definition, label, label_ru, description, category in _settings_index(stack):
        key = definition.key.lower()
        key_words = key.replace("_", " ")
        labels = (label + " " + label_ru).lower()
        desc = description.lower()
        score = 0
        if key == query.lower().strip():
            score += 100
        for token in tokens:
            if token in key_words.split():
                score += 6
            elif token in key:
                score += 4
            if token in labels:
                score += 5
            elif token in desc:
                score += 1
        if score:
            scored.append((score, definition, label, label_ru, category))
    scored.sort(key=lambda item: (-item[0], len(item[1].key)))
    results = []
    for score, definition, label, label_ru, category in scored:
        reference = stack
        if _is_per_extruder(definition):
            reference = api.extruder_stack(used_extruders()[0], stack)
        try:
            enabled = bool(reference.getProperty(definition.key, "enabled"))
        except Exception:
            enabled = True
        if not include_disabled and not enabled:
            continue
        item = collections.OrderedDict()
        item["key"] = definition.key
        item["label"] = label_ru
        item["category"] = category
        item["type"] = definition.type
        item["value"] = _jsonable(reference.getProperty(definition.key, "value"))
        if definition.unit:
            item["unit"] = definition.unit
        item["enabled"] = enabled
        item["per_extruder"] = _is_per_extruder(definition)
        results.append(item)
        if len(results) >= limit:
            break
    return {"query": query, "results": results}


def list_user_changes():
    stack = api.global_stack()
    result = collections.OrderedDict()

    def dump(container):
        values = collections.OrderedDict()
        for key in sorted(container.getAllKeys()):
            values[key] = _jsonable(container.getProperty(key, "value"))
        return values

    result["global"] = dump(stack.userChanges)
    for index, ext in enumerate(api.extruder_stacks(stack)):
        result["extruder %d" % index] = dump(ext.userChanges)
    result["quality_changes"] = stack.qualityChanges.getName() if stack.qualityChanges.getId() != "empty_quality_changes" else None
    return result


# ---------------------------------------------------------------------------
# Writing

def coerce_value(definition, value):
    setting_type = definition.type
    key = definition.key
    try:
        if setting_type == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("true", "1", "yes", "on", "да", "вкл"):
                return True
            if text in ("false", "0", "no", "off", "нет", "выкл"):
                return False
            raise ValueError(value)
        if setting_type == "int":
            return int(round(float(value)))
        if setting_type == "float":
            return float(value)
        if setting_type in ("extruder", "optional_extruder"):
            return str(int(value))
        if setting_type == "enum":
            text = str(value).strip()
            options = definition.options
            if text in options:
                return text
            lowered = text.lower()
            for option_key, option_label in options.items():
                translated = _catalog.i18nc(key + " option " + option_key, option_label)
                if lowered in (option_key.lower(), option_label.lower(), translated.lower()):
                    return option_key
            raise ToolError("Недопустимое значение '%s' для %s." % (value, key),
                            hint="Варианты: " + ", ".join(options.keys()))
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return str(value)
    except ToolError:
        raise
    except (TypeError, ValueError):
        raise ToolError("Значение '%s' не подходит для %s (тип %s)." % (value, key, setting_type))


def _check_limits(stack, key, value):
    warnings = []
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return warnings
    checks = (("minimum_value", "<", True), ("maximum_value", ">", True),
              ("minimum_value_warning", "<", False), ("maximum_value_warning", ">", False))
    for prop, op, hard in checks:
        try:
            limit = stack.getProperty(key, prop)
        except Exception:
            continue
        if not isinstance(limit, (int, float)) or isinstance(limit, bool):
            continue
        violated = value < limit if op == "<" else value > limit
        if violated:
            message = "%s=%s %s %s (%s)" % (key, value, op, _round(float(limit)), prop)
            if hard:
                raise ToolError("Значение вне допустимого диапазона: " + message)
            warnings.append("Нетипичное значение: " + message)
    return warnings


def set_settings(values, extruder=None):
    if not isinstance(values, dict) or not values:
        raise ToolError("Передайте settings как объект {ключ: значение}.")
    stack = api.global_stack()
    applied = []
    errors = []
    for key, raw in values.items():
        try:
            if key == "printing_mode":
                applied.append(set_printing_mode(raw))
                continue
            if key.startswith(_FORBIDDEN_PREFIXES):
                raise ToolError("Машинные настройки (%s) менять нельзя — они задают геометрию принтера." % key)
            definition = _require_definition(key, stack)
            value = coerce_value(definition, raw)
            targets = _resolve_targets(definition, extruder)
            warnings = []
            for name, target in targets:
                warnings.extend(_check_limits(target, key, value))
            changes = []
            for name, target in targets:
                old = _jsonable(target.getProperty(key, "value"))
                target.userChanges.setProperty(key, "value", value)
                new = _jsonable(target.getProperty(key, "value"))
                if new != _jsonable(value) and target.userChanges.getProperty(key, "value") is None:
                    target.setProperty(key, "value", value)  # fall back to the stack's own setter
                    new = _jsonable(target.getProperty(key, "value"))
                changes.append({"target": name, "old": old, "new": new})
                try:
                    if not target.getProperty(key, "enabled"):
                        warnings.append("%s сейчас неактивна (enabled=false) для %s — значение сохранено, но может не повлиять на печать." % (key, name))
                except Exception:
                    pass
                for suffix in ("_classic", "_cylindrical"):
                    if target.userChanges.getProperty(key + suffix, "value") is not None:
                        warnings.append("Значение %s%s задано отдельно и перекрывает %s в своём режиме." % (key, suffix, key))
            label = _labels(definition)[1]
            entry = {"key": key, "label": label, "value": _jsonable(value), "changes": changes}
            if warnings:
                entry["warnings"] = sorted(set(warnings))
            applied.append(entry)
        except ToolError as e:
            errors.append({"key": key, "error": e.message, "hint": e.hint})
    result = {"applied": applied}
    if errors:
        result["errors"] = errors
    return result


def reset_settings(keys=None, keep_printing_mode=True):
    stack = api.global_stack()
    stacks = [stack] + api.extruder_stacks(stack)
    if keys:
        if isinstance(keys, str):
            keys = [keys]
        for target in stacks:
            for key in keys:
                if target.userChanges.getInstance(key) is not None:
                    target.userChanges.removeInstance(key)
        return {"reset": keys}
    mode = stack.userChanges.getProperty("printing_mode", "value")
    removed = 0
    for target in stacks:
        for key in list(target.userChanges.getAllKeys()):
            if keep_printing_mode and target is stack and key == "printing_mode":
                continue
            target.userChanges.removeInstance(key)
            removed += 1
    return {"reset": "all", "removed": removed,
            "printing_mode_kept": bool(keep_printing_mode and mode is not None)}


# ---------------------------------------------------------------------------
# Printing mode and machine overview

def normalize_mode(mode):
    text = str(mode).strip().lower().replace("-", " ")
    if text in _MODE_ALIASES:
        return _MODE_ALIASES[text]
    raise ToolError("Неизвестный режим печати '%s'." % mode,
                    hint="Используйте 3D, 5D или одно из: " + ", ".join(PRINTING_MODES.keys()))


def printing_mode_info(stack=None):
    stack = stack or api.global_stack()
    five_axis = bool(stack.getProperty("machine_five_axis", "value"))
    mode = stack.getProperty("printing_mode", "value") if five_axis else "classic"
    return {
        "value": mode,
        "label": PRINTING_MODES.get(mode, mode),
        "five_axis_machine": five_axis,
        "available": list(PRINTING_MODES.keys()) if five_axis else ["classic"],
    }


def build_volume(stack=None):
    stack = stack or api.global_stack()
    return {
        "x": _round(stack.getProperty("machine_width", "value")),
        "y": _round(stack.getProperty("machine_depth", "value")),
        "z": _round(stack.getProperty("machine_height", "value")),
        "shape": stack.getProperty("machine_shape", "value"),
        "heated_bed": bool(stack.getProperty("machine_heated_bed", "value")),
    }


def set_printing_mode(mode):
    stack = api.global_stack()
    target = normalize_mode(mode)
    five_axis = bool(stack.getProperty("machine_five_axis", "value"))
    if target != "classic" and not five_axis:
        raise ToolError("Принтер '%s' трёхосевой: доступен только режим 3D (classic)." % stack.getName())
    old = stack.getProperty("printing_mode", "value")
    stack.userChanges.setProperty("printing_mode", "value", target)
    return {
        "key": "printing_mode",
        "old": old,
        "new": stack.getProperty("printing_mode", "value"),
        "label": PRINTING_MODES.get(target, target),
        "build_volume": build_volume(stack),
        "adhesion_type": stack.getProperty("adhesion_type", "value"),
    }


def material_summary(ext):
    material = ext.material
    if material is None or material.getId() == "empty_material":
        return None
    return {
        "id": material.getMetaDataEntry("base_file") or material.getId(),
        "name": material.getName(),
        "brand": material.getMetaDataEntry("brand"),
        "type": material.getMetaDataEntry("material"),
    }


def machine_info():
    stack = api.global_stack()
    mm = api.app().getMachineManager()
    extruders = []
    for index, ext in enumerate(api.extruder_stacks(stack)):
        extruders.append({
            "index": index,
            "name": ext.getName(),
            "enabled": bool(getattr(ext, "isEnabled", True)),
            "nozzle_size": _round(ext.getProperty("machine_nozzle_size", "value")),
            "material_diameter": _round(ext.getProperty("material_diameter", "value")),
            "fiber_extruder": bool(ext.getProperty("machine_fiber_extruder", "value")),
            "material": material_summary(ext),
            "print_temperature": _round(ext.getProperty("material_print_temperature", "value")),
        })
    quality_changes = stack.qualityChanges
    custom = quality_changes.getName() if quality_changes.getId() != "empty_quality_changes" else None
    return {
        "machine": {
            "name": stack.getName(),
            "definition_id": stack.definition.getId(),
            "definition_name": stack.definition.getName(),
            "five_axis": bool(stack.getProperty("machine_five_axis", "value")),
            "hybrid": bool(stack.getProperty("machine_hybrid", "value")),
            "build_volume": build_volume(stack),
        },
        "printing_mode": printing_mode_info(stack),
        "quality": {
            "quality_type": mm.activeQualityType,
            "name": mm.activeQualityOrQualityChangesName,
            "custom_profile": custom,
            "layer_height": _round(stack.getProperty("layer_height", "value")),
        },
        "extruders": extruders,
        "objects_in_scene": len(api.scene_objects()),
        "user_changes_count": len(stack.userChanges.getAllKeys()) + sum(
            len(e.userChanges.getAllKeys()) for e in api.extruder_stacks(stack)),
    }


def list_machines():
    from UM.Settings.ContainerRegistry import ContainerRegistry
    registry = ContainerRegistry.getInstance()
    active = api.app().getGlobalContainerStack()
    machines = []
    for stack in registry.findContainerStacks(type="machine"):
        machines.append({
            "id": stack.getId(),
            "name": stack.getName(),
            "definition_id": stack.definition.getId() if stack.definition else None,
            "active": active is not None and stack.getId() == active.getId(),
        })
    machines.sort(key=lambda m: m["name"])
    return {"machines": machines}


def set_active_machine(machine_id):
    from UM.Settings.ContainerRegistry import ContainerRegistry
    registry = ContainerRegistry.getInstance()
    stacks = registry.findContainerStacks(id=machine_id, type="machine") or \
        registry.findContainerStacks(name=machine_id, type="machine")
    if not stacks:
        raise ToolError("Принтер '%s' не найден." % machine_id, hint="Список: list_machines")
    api.app().getMachineManager().setActiveMachine(stacks[0].getId())
    return {"active_machine": stacks[0].getName()}
