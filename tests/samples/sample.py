"""Token storage."""
import os
from typing import Optional as Opt
from .session import Session, refresh as do_refresh

MAX_RETRY = 6
_cache: dict[str, str] = {}


def refresh_token(session: Session, *, force: bool = False) -> str:
    """Exchange a refresh token for a new access token."""
    return session.post("/oauth/refresh")


class TokenStore(Base):
    """Keeps tokens."""

    ttl = 3600

    def __init__(self, path: str) -> None:
        self.path = path

    @property
    def size(self) -> int:
        return len(_cache)

    @staticmethod
    def make() -> "TokenStore":
        handler = lambda x: x + 1
        return TokenStore(os.getcwd())
