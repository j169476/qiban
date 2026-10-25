"""WeChat boundary; importing it never imports a desktop SDK or starts a client."""

from .connector import WeChatConnector

__all__ = ["WeChatConnector"]
