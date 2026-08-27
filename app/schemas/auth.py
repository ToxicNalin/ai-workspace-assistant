import uuid

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


class RegisterRequest(BaseModel):
    email: EmailStr
    # bcrypt silently ignores bytes beyond 72 -- capped here so a long
    # password never appears to "work" while actually being truncated.
    password: str = Field(min_length=8, max_length=72)
    name: str = Field(min_length=1, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SessionOut(BaseModel):
    """What /auth/login and /auth/refresh hand back.

    No refresh token. It is set as an httpOnly cookie instead (SPEC-v2 D19),
    and returning a copy here would undo the point of that entirely: script
    that can read this body can read it after calling /auth/refresh, which the
    browser will happily authenticate from the cookie.

    `csrf_token` is not a credential on its own -- it is worthless without the
    cookie -- and it has to be readable, because the client must echo it back
    on the one endpoint the cookie authenticates. See app/auth/cookies.py.
    """

    access_token: str
    token_type: str = "bearer"
    csrf_token: str
    # Seconds. Saves the client parsing a JWT it is not supposed to interpret
    # just to know when to stop using the token.
    expires_in: int


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    name: str
    is_active: bool
