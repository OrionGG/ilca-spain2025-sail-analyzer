"""Errors the use cases report."""


class AnalysisError(ValueError):
    permanent = True                              # the same inputs would fail again


class TransientError(AnalysisError):
    permanent = False                             # e.g. a file locked by antivirus / sync


class NotFound(LookupError):
    """An unknown regatta or race (the message says which)."""
