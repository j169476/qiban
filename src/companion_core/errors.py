class AssistantError(Exception):
    """Expected, safe-to-display application error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def as_dict(self):
        return {"error": self.code, "message": str(self)}
