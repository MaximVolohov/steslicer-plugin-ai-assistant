from . import tools_analysis, tools_scene, tools_settings, tools_slicing
from .registry import ToolRegistry


def build_registry():
    registry = ToolRegistry()
    for module in (tools_settings, tools_scene, tools_slicing, tools_analysis):
        module.register(registry)
    return registry
