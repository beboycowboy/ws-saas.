class NotConfigured(RuntimeError):
    """A real adapter is intentionally unconnected in this local delivery."""


class BusinessError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
