class AppError(Exception):
    """利用者に見せる前提のエラー。message は原因、hint は対処方法。"""

    def __init__(self, message, hint="", status=400, code="app_error"):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.status = status
        self.code = code

    def to_dict(self):
        return {"ok": False, "code": self.code, "error": self.message, "hint": self.hint}
