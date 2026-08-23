from enum import StrEnum

from sqlalchemy import Enum


def str_enum(enum_cls: type[StrEnum], *, name: str, length: int = 20) -> Enum:
    """A CHECK-constrained VARCHAR enum column that stores enum .value, not .name.

    Plain sa.Enum(SomeStrEnum) stores the Python member NAME ('ADMIN'), not its
    value ('admin') — a well-known SQLAlchemy surprise. values_callable fixes
    that so the stored strings match the enum's actual (lowercase) values.
    native_enum=False avoids Postgres native CREATE TYPE, which is extra
    migration ceremony (shared-type drop ordering, ALTER TYPE for new values)
    this project doesn't need — but SQLAlchemy's default for that mode is
    create_constraint=False, so without it here nothing at the DB level would
    stop an invalid value ever reaching this column.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )
