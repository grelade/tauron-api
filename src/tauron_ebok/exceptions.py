"""Exception hierarchy. Everything the SDK raises on purpose derives from TauronError."""

from __future__ import annotations


class TauronError(Exception):
    """Base class for all SDK errors."""


class AuthenticationError(TauronError):
    """Login failed (bad credentials, changed login form, or the session could not be re-established)."""


class ParseError(TauronError):
    """A page or response did not have the expected structure (the site probably changed)."""


class ApiError(TauronError):
    """An AJAX endpoint answered with a non-OK status."""

    def __init__(self, message: str, payload: object = None) -> None:
        super().__init__(message)
        self.payload = payload


class PayerNotFoundError(TauronError):
    """The requested payer is not on this account, or several payers exist and none was chosen."""
