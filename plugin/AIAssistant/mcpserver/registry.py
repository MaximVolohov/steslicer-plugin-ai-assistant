"""Tool registry shared by the MCP server and the LLM agent."""
import collections
import time


class ToolResult(object):
    """Structured tool output plus optional images (PNG bytes) for MCP image content."""

    def __init__(self, data, images=None, summary=None):
        self.data = data
        self.images = images or []  # [(bytes, mime)]
        self.summary = summary


class ToolContext(object):
    """Per-call context: services, a cancel flag and a progress callback."""

    def __init__(self, services, cancel_event=None, progress=None, caller="mcp"):
        self.services = services
        self.cancel_event = cancel_event
        self.progress = progress or (lambda message, fraction=None: None)
        self.caller = caller

    def cancelled(self):
        return self.cancel_event is not None and self.cancel_event.is_set()


class Tool(object):
    def __init__(self, name, title, description, handler, properties=None, required=None, group="misc"):
        self.name = name
        self.title = title
        self.description = description
        self.handler = handler
        self.properties = properties or collections.OrderedDict()
        self.required = list(required or [])
        self.group = group

    @property
    def input_schema(self):
        return {"type": "object", "properties": self.properties, "required": self.required}

    def toMcp(self):
        return {"name": self.name, "title": self.title, "description": self.description,
                "inputSchema": self.input_schema}

    def toOpenAI(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.input_schema}}


class ArgumentError(Exception):
    pass


def _coerce(name, schema, value):
    kind = schema.get("type")
    if isinstance(kind, list):
        return value
    try:
        if kind == "number" and isinstance(value, str):
            return float(value)
        if kind == "integer" and not isinstance(value, bool):
            return int(float(value))
        if kind == "boolean" and isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "да")
        if kind == "array" and not isinstance(value, list):
            return [value]
    except (TypeError, ValueError):
        raise ArgumentError("Аргумент '%s' должен быть типа %s." % (name, kind))
    if "enum" in schema and value not in schema["enum"]:
        raise ArgumentError("Аргумент '%s' должен быть одним из: %s." % (name, ", ".join(map(str, schema["enum"]))))
    return value


class ToolRegistry(object):
    def __init__(self):
        self._tools = collections.OrderedDict()

    def add(self, name, title, description, properties=None, required=None, group="misc"):
        """Decorator: register handler(args, ctx) -> dict | ToolResult."""
        def decorator(handler):
            self._tools[name] = Tool(name, title, description, handler, properties, required, group)
            return handler
        return decorator

    def list(self):
        return list(self._tools.values())

    def get(self, name):
        return self._tools.get(name)

    def validate(self, tool, args):
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ArgumentError("Аргументы должны быть объектом JSON.")
        clean = {}
        for key in tool.required:
            if key not in args or args[key] is None or args[key] == "":
                raise ArgumentError("Не хватает обязательного аргумента '%s'." % key)
        for key, value in args.items():
            schema = tool.properties.get(key)
            if schema is None:
                continue  # ignore unknown arguments instead of failing the call
            if value is None:
                continue
            clean[key] = _coerce(key, schema, value)
        return clean

    def call(self, name, args, ctx):
        tool = self.get(name)
        if tool is None:
            raise ArgumentError("Неизвестный инструмент '%s'." % name)
        clean = self.validate(tool, args)
        started = time.time()
        result = tool.handler(clean, ctx)
        if not isinstance(result, ToolResult):
            result = ToolResult(result)
        result.duration = time.time() - started
        return result


def prop(kind, description, **extra):
    schema = collections.OrderedDict([("type", kind), ("description", description)])
    schema.update(extra)
    return schema


def props(*pairs):
    return collections.OrderedDict(pairs)
