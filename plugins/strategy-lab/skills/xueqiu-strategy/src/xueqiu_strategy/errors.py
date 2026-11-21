"""Xueqiu broker exceptions."""

class XueqiuAPIError(RuntimeError):
    """Raised when Xueqiu returns an unsuccessful API response."""

class XueqiuTransportError(XueqiuAPIError):
    """The server response was unavailable or could not be interpreted."""
