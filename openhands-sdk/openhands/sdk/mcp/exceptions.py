"""MCP-related exceptions for OpenHands SDK."""


class MCPError(Exception):
    """Base exception for MCP-related errors."""

    pass


class MCPTimeoutError(MCPError):
    """Exception raised when MCP operations timeout."""

    timeout: float
    config: dict | None

    def __init__(self, message: str, timeout: float, config: dict | None = None):
        self.timeout = timeout
        self.config = config
        super().__init__(message)


class MCPAuthorizationRequiredError(MCPError):
    """An OAuth MCP server needs interactive (browser) authorization.

    Raised by non-interactive OAuth clients instead of opening a browser,
    so callers can skip the server and let the user re-authorize through
    an explicit settings/install flow.
    """

    pass
