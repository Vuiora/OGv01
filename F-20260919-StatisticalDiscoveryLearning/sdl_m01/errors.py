"""Public failure types for the data-and-evidence protocol."""


class ValidationError(ValueError):
    """Input or declared protocol is not valid."""


class AccessDenied(PermissionError):
    """The authenticated role may not perform this operation."""


class StateError(RuntimeError):
    """The evidence resource cannot make the requested transition."""


class IntegrityError(RuntimeError):
    """Stored content does not match its registered integrity record."""
