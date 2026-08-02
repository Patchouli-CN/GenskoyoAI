"""Web/HTTP runtime server package for GensokyoAI."""

from .adapter import WebAdapter
from .http_adapter import create_app

__all__ = ["WebAdapter", "create_app"]
