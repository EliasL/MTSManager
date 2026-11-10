"""Failures that users can resolve from the displayed message."""

EXPECTED_ERROR_EXIT = 78
EXPECTED_ERROR_FORMAT = "MTS_EXPECTED_ERROR_FORMAT"


class ExpectedError(ValueError):
    """An actionable error suitable for display without a traceback."""
