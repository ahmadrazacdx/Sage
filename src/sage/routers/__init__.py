"""Router registry for Sage API."""

from sage.routers.chat import router as chat_router
from sage.routers.documents import router as documents_router
from sage.routers.sessions import router as sessions_router
from sage.routers.system import router as system_router

__all__: list[str] = [
    "chat_router",
    "sessions_router",
    "documents_router",
    "system_router",
]
