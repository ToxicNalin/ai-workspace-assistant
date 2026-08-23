import uuid

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins import Timestamped, UUIDPrimaryKey


class User(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "users"

    # Normalised to lowercase in app/services/auth_service.py before every
    # read or write — this is a plain unique index, not citext.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Refresh-token rotation state: the jti a valid refresh token must carry.
    # A refresh request bearing a stale jti indicates reuse of an already
    # rotated-out token and is refused.
    current_refresh_jti: Mapped[uuid.UUID | None] = mapped_column(default=None)
