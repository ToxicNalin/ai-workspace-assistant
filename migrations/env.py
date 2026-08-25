import asyncio
from itertools import chain
from logging.config import fileConfig

from alembic import context
from alembic.runtime.environment import NameFilterParentNames, NameFilterType
from sqlalchemy import CheckConstraint, Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Every model module must be imported so it registers its table on
# Base.metadata before autogenerate compares against it -- importing only
# app.database.base leaves the metadata empty and produces no-op migrations.
import app.database.models  # noqa: F401
from app.config import get_settings
from app.database.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def _type_bound_check_constraint_names() -> set[str]:
    """The CHECK constraints SQLAlchemy generates on our behalf from Enum types.

    Derived from the metadata rather than hardcoded, so a new str_enum column
    is covered automatically.
    """
    names: set[str] = set()
    for table in Base.metadata.tables.values():
        candidates = chain(table.constraints, *(column.constraints for column in table.columns))
        for constraint in candidates:
            if (
                isinstance(constraint, CheckConstraint)
                and getattr(constraint, "_type_bound", False)
                and constraint.name
            ):
                names.add(str(constraint.name))
    return names


TYPE_BOUND_CHECK_CONSTRAINTS = _type_bound_check_constraint_names()


# LangGraph owns these, and AsyncPostgresSaver.setup() creates them at boot
# (SPEC-v2 table 17). They are therefore in the database and deliberately not
# in Base.metadata -- which is exactly the shape autogenerate reads as "these
# tables were deleted, drop them". Left unfiltered, the first migration
# generated after the agent has ever run carries four DROP TABLEs that destroy
# every paused approval in the system, and does it inside the boot migration
# on Render where nobody is watching.
#
# Hardcoded rather than derived, because there is no metadata to derive them
# from -- that absence is the entire problem. Verified against
# langgraph-checkpoint-postgres 3.1.2.
LANGGRAPH_OWNED_TABLES = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)


def include_name(
    name: str | None,
    type_: NameFilterType,
    parent_names: NameFilterParentNames,
) -> bool:
    """Two filters, for two different ways autogenerate lies about this schema.

    The first: tables another library owns and creates for itself. See
    LANGGRAPH_OWNED_TABLES above -- excluding them here is what stops a
    migration proposing to drop them, and it covers their indexes too, since
    those are filtered by the table they belong to.

    The second: the CHECK constraints the Enum type creates.

    `str_enum()` uses native_enum=False + create_constraint=True, so SQLAlchemy
    attaches the CHECK constraint itself and marks it `_type_bound`. Alembic's
    own `all_table_check_constraints()` then deliberately excludes type-bound
    constraints from the *model* side of the comparison -- while Postgres of
    course still reflects them back on the *database* side. The two sides can
    never agree, so every autogenerate run proposes dropping all of them.

    That noise had to be deleted by hand from migrations 0003 through 0006,
    which is tedious and, worse, risks one day taking a genuine constraint drop
    with it. Filtering the reflected side to match what alembic already does to
    the model side fixes the cause, and makes `alembic check` usable as a real
    drift detector.

    Note this is include_name, not include_object: the model-side constraints
    are gone before any object filter would see them, and a table that is not
    in the metadata at all never becomes an object either.
    """
    if type_ == "table" and name in LANGGRAPH_OWNED_TABLES:
        return False
    if parent_names.get("table_name") in LANGGRAPH_OWNED_TABLES:
        return False

    return not (type_ == "check_constraint" and name in TYPE_BOUND_CHECK_CONSTRAINTS)


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
# app/lifespan.py runs migrations on boot and hands in its own connection --
# the one already holding the advisory lock. Opening a second engine here
# would migrate outside that lock, which is the whole thing it guards against.
elif (existing_connection := config.attributes.get("connection")) is not None:
    do_run_migrations(existing_connection)
else:
    asyncio.run(run_migrations_online())
