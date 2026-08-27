from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import RateLimitScope
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey
from app.database.types import str_enum


class RateLimitCounter(Base, UUIDPrimaryKey):
    """One fixed window's request count for one identity.

    SPEC-v2 D15 chose Postgres counters over Redis so the free tier needs one
    fewer service; this is the table that decision implies. It is not in the
    spec's seventeen-table list, which names only `usage_events` for migration
    0008 -- but "Postgres-backed counters" has to be backed by something, and
    inventing a home for them inside `usage_events` would mean a rejected
    request polluting the token ledger that the budget is computed from.

    Deliberately *not* workspace-scoped. The limiter runs before any route has
    resolved a workspace, and the abuse it exists to stop -- someone
    registering accounts in a loop -- has no workspace to be scoped to.
    """

    __tablename__ = "rate_limit_counters"
    __table_args__ = (
        # The upsert's conflict target. Also the only index the limiter needs:
        # every read is an exact match on all three columns.
        UniqueConstraint(
            "bucket", "scope", "window_start", name="uq_rate_limit_counters_window"
        ),
    )

    # "user:<uuid>" or "ip:<address>". A string rather than two nullable
    # columns because the limiter never needs to ask which kind it is -- it
    # only needs the two identities to be unable to collide.
    bucket: Mapped[str] = mapped_column(String(100))
    scope: Mapped[RateLimitScope] = mapped_column(
        str_enum(RateLimitScope, name="rate_limit_scope")
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer, default=0)
