"""Plugin-owned Teams domain; importing this package starts no workers or storage."""

from .errors import TeamsError

__all__ = ["TeamsError"]
