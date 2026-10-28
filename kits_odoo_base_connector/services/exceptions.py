# -*- coding: utf-8 -*-
"""Connector-specific exceptions used by the HTTP client and sync engine."""


class ConnectorError(Exception):
    """Base exception for connector failures."""

    def __init__(self, message='', *, details=None, status_code=None):
        super().__init__(message)
        self.message = message or str(self)
        self.details = details
        self.status_code = status_code


class ConnectorAuthError(ConnectorError):
    """Raised when authentication or authorization fails."""


class ConnectorAPIError(ConnectorError):
    """Raised when a remote API returns an unexpected or invalid response."""


class ConnectorTimeoutError(ConnectorError):
    """Raised when an HTTP request times out."""


class ConnectorValidationError(ConnectorError):
    """Raised when response validation fails."""
