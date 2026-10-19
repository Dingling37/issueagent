"""Synchronous runtime persistence; SQL is confined to this package."""


class PersistenceError(ValueError):
    """Fail closed without exposing connection credentials."""
