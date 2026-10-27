class AssistantError(Exception):
    """Expected, safe-to-display application error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def as_dict(self):
        value = {"error": self.code, "message": str(self)}
        if hasattr(self, "diagnostics"):
            value["diagnostics"] = self.diagnostics
        return value
