"""Token accounting, and the budget that is checked before the model runs.

Two things live here, and the order between them is the whole point. Requests
per minute bounds how often somebody can ask; it does not bound what an answer
costs, because one chat turn can fan out into several model calls (SPEC-v2
D22). The daily token budget is what actually stops a public demo holding a
live API key from running up a bill, and it is only worth anything if it is
consulted *before* the call rather than after -- a ledger that notices the
overspend once it has happened is a receipt, not a limit.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import USAGE_BUDGET_WINDOW_SECONDS, UsageKind, WorkspaceRole
from app.database.models.membership import WorkspaceMember
from app.database.models.usage_event import UsageEvent
from app.database.models.user import User
from app.exceptions import RateLimited


def record(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    kind: UsageKind,
    model: str,
    tokens_in: int,
    tokens_out: int,
    user_id: uuid.UUID | None = None,
    estimated: bool = False,
) -> UsageEvent:
    """Stage a usage row. Does not commit.

    Same contract as audit_service.record: the caller commits it alongside the
    thing it paid for, so there is no window in which an answer exists and the
    tokens it cost do not.
    """
    event = UsageEvent(
        workspace_id=workspace_id,
        user_id=user_id,
        kind=kind,
        model=model,
        tokens_in=max(0, tokens_in),
        tokens_out=max(0, tokens_out),
        estimated=estimated,
    )
    db.add(event)
    return event


def _window_start(seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def _in_window(query: Select[Any], workspace_id: uuid.UUID, seconds: int) -> Select[Any]:
    return query.where(
        UsageEvent.workspace_id == workspace_id,
        UsageEvent.created_at >= _window_start(seconds),
    )


async def tokens_used(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    seconds: int = USAGE_BUDGET_WINDOW_SECONDS,
) -> int:
    """Tokens spent by this workspace over a rolling window.

    Rolling rather than per calendar day on purpose: it needs no decision
    about whose midnight counts, and it cannot be waited out by a caller who
    knows what timezone the server thinks it is in.
    """
    total = await db.scalar(
        _in_window(
            select(func.coalesce(func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out), 0)),
            workspace_id,
            seconds,
        )
    )
    return int(total or 0)


async def enforce_budget(db: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Refuse the call if this workspace has spent its day's allowance.

    Called at the top of every path that reaches a model, before any work is
    done -- not merely before the HTTP call, but before retrieval and before
    anything is written. A caller over budget should cost this deployment a
    single aggregate query and nothing else.
    """
    budget = get_settings().daily_token_budget
    if budget <= 0:
        return

    used = await tokens_used(db, workspace_id)
    if used < budget:
        return

    # When the allowance returns is a real answer, not a guess: the window is
    # rolling, so it frees up as the oldest counted call ages out of it.
    oldest = await db.scalar(
        _in_window(
            select(func.min(UsageEvent.created_at)),
            workspace_id,
            USAGE_BUDGET_WINDOW_SECONDS,
        )
    )
    retry_after = USAGE_BUDGET_WINDOW_SECONDS
    if oldest is not None:
        expires_at = oldest + timedelta(seconds=USAGE_BUDGET_WINDOW_SECONDS)
        retry_after = max(1, int((expires_at - datetime.now(UTC)).total_seconds()))

    raise RateLimited(
        f"This workspace has used its daily allowance of {budget:,} tokens. "
        "It refreshes as earlier requests age out of the last 24 hours.",
        retry_after=retry_after,
    )


def tokens_from_messages(messages: Sequence[Any]) -> tuple[int, int]:
    """Sum usage across the messages one agent run produced.

    LangChain attaches usage metadata to each AI message the provider reported
    it for. A run that called three tools has three of them, and the only
    honest total is their sum -- reading the last message alone would bill a
    multi-step run at the price of its final step.
    """
    tokens_in = 0
    tokens_out = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            continue
        tokens_in += int(usage.get("input_tokens", 0) or 0)
        tokens_out += int(usage.get("output_tokens", 0) or 0)
    return tokens_in, tokens_out


@dataclass
class UsageSummary:
    """What /admin/usage answers."""

    tokens_in: int
    tokens_out: int
    calls: int
    tokens_estimated: int
    daily_budget: int
    tokens_used_today: int
    tokens_remaining_today: int
    by_kind: dict[str, int] = field(default_factory=dict)
    by_day: dict[str, int] = field(default_factory=dict)


@dataclass
class MemberUsage:
    """One member of the workspace, and what they have spent."""

    user_id: uuid.UUID
    name: str
    email: str
    role: WorkspaceRole
    joined_at: datetime
    calls: int
    tokens_in: int
    tokens_out: int


async def summary(db: AsyncSession, workspace_id: uuid.UUID, *, days: int) -> UsageSummary:
    seconds = days * 24 * 60 * 60

    totals = (
        await db.execute(
            _in_window(
                select(
                    func.coalesce(func.sum(UsageEvent.tokens_in), 0),
                    func.coalesce(func.sum(UsageEvent.tokens_out), 0),
                    func.count(UsageEvent.id),
                ),
                workspace_id,
                seconds,
            )
        )
    ).one()

    by_kind = {
        str(kind): int(total or 0)
        for kind, total in (
            await db.execute(
                _in_window(
                    select(
                        UsageEvent.kind,
                        func.coalesce(
                            func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out), 0
                        ),
                    ),
                    workspace_id,
                    seconds,
                ).group_by(UsageEvent.kind)
            )
        ).all()
    }

    day = func.date_trunc("day", UsageEvent.created_at)
    by_day = {
        bucket.date().isoformat(): int(total or 0)
        for bucket, total in (
            await db.execute(
                _in_window(
                    select(
                        day,
                        func.coalesce(
                            func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out), 0
                        ),
                    ),
                    workspace_id,
                    seconds,
                )
                .group_by(day)
                .order_by(day)
            )
        ).all()
    }

    # How much of the total above is arithmetic on a heuristic rather than a
    # number a provider gave us. Reported so the page can say so.
    estimated_total = await db.scalar(
        _in_window(
            select(func.coalesce(func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out), 0)),
            workspace_id,
            seconds,
        ).where(UsageEvent.estimated.is_(True))
    )

    budget = get_settings().daily_token_budget
    used_today = await tokens_used(db, workspace_id)

    return UsageSummary(
        tokens_in=int(totals[0] or 0),
        tokens_out=int(totals[1] or 0),
        calls=int(totals[2] or 0),
        tokens_estimated=int(estimated_total or 0),
        daily_budget=budget,
        tokens_used_today=used_today,
        tokens_remaining_today=max(0, budget - used_today),
        by_kind=by_kind,
        by_day=by_day,
    )


async def member_usage(
    db: AsyncSession, workspace_id: uuid.UUID, *, days: int
) -> list[MemberUsage]:
    """Every member of the workspace, with their spend attached.

    An outer join, so a member who has never asked the assistant anything
    still appears with zeroes. An inner join would quietly answer a different
    question -- "who has spent tokens" rather than "who is in this workspace"
    -- and make a silent member look like a missing one.
    """
    spend = (
        _in_window(
            select(
                UsageEvent.user_id.label("user_id"),
                func.count(UsageEvent.id).label("calls"),
                func.coalesce(func.sum(UsageEvent.tokens_in), 0).label("tokens_in"),
                func.coalesce(func.sum(UsageEvent.tokens_out), 0).label("tokens_out"),
            ),
            workspace_id,
            days * 24 * 60 * 60,
        )
        .where(UsageEvent.user_id.is_not(None))
        .group_by(UsageEvent.user_id)
        .subquery()
    )

    rows = (
        await db.execute(
            select(
                User.id,
                User.name,
                User.email,
                WorkspaceMember.role,
                WorkspaceMember.joined_at,
                func.coalesce(spend.c.calls, 0),
                func.coalesce(spend.c.tokens_in, 0),
                func.coalesce(spend.c.tokens_out, 0),
            )
            .join(User, User.id == WorkspaceMember.user_id)
            .outerjoin(spend, spend.c.user_id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .order_by(WorkspaceMember.joined_at)
        )
    ).all()

    return [
        MemberUsage(
            user_id=row[0],
            name=row[1],
            email=row[2],
            role=WorkspaceRole(row[3]),
            joined_at=row[4],
            calls=int(row[5] or 0),
            tokens_in=int(row[6] or 0),
            tokens_out=int(row[7] or 0),
        )
        for row in rows
    ]
