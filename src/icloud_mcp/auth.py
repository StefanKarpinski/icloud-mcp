"""Authentication management for iCloud MCP server."""

from typing import Tuple, Optional
from fastmcp import Context
from fastmcp.server.dependencies import get_http_headers
from .config import config


class AuthenticationError(Exception):
    """Raised when authentication fails."""
    pass


def get_credentials(context: Context) -> Tuple[str, str]:
    """Extract iCloud credentials from HTTP headers, falling back to env vars."""

    headers = get_http_headers()

    email: Optional[str] = headers.get("x-apple-email") or headers.get("X-Apple-Email")
    password: Optional[str] = headers.get("x-apple-app-specific-password") or headers.get("X-Apple-App-Specific-Password")

    if not email:
        email = config.FALLBACK_EMAIL
    if not password:
        password = config.FALLBACK_PASSWORD

    if not email or not password:
        raise AuthenticationError(
            "Authentication required. Provide credentials via headers "
            "(X-Apple-Email, X-Apple-App-Specific-Password) or environment variables "
            "(ICLOUD_EMAIL, ICLOUD_APP_SPECIFIC_PASSWORD)"
        )

    return email, password


def require_auth(context: Context) -> Tuple[str, str]:
    """Decorator-friendly authentication check."""
    return get_credentials(context)
