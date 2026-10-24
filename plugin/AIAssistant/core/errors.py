class ToolError(Exception):
    """An expected, user-facing failure of a tool call (bad argument, wrong state...).

    The message is returned to the LLM / MCP client as the tool result so it can
    recover; it is not logged as a crash.
    """

    def __init__(self, message, hint=None):
        Exception.__init__(self, message)
        self.message = message
        self.hint = hint
