class AQLError(Exception):
    """Base exception for all AQL errors."""


class AQLParseError(AQLError):
    """Raised when AQL query cannot be parsed (syntax error)."""

    def __init__(self, message: str, line: int | None = None, column: int | None = None):
        self.line = line
        self.column = column
        super().__init__(message)


class AQLValidationError(AQLError):
    """Raised when AQL query violates semantic constraints (too long, too deep, etc.)."""
