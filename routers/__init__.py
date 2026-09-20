
from routers.auth import router as auth_router
from routers.messages import router as messages_router
from routers.channels import router as channels_router
from routers.users import router as users_router
from routers.upload import router as upload_router

__all__ = [
    "auth_router",
    "messages_router",
    "channels_router",
    "users_router",
    "upload_router",
]
