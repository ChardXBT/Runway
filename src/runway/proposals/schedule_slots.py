from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runway.db.models import LineupSlotClaim, Proposal
from runway.domain.enums import ProposalStatus

RESERVING_PROPOSAL_STATUSES = frozenset(
    {
        ProposalStatus.APPROVED.value,
        ProposalStatus.INTERNALLY_SCHEDULED.value,
        ProposalStatus.PUBLISHING.value,
        ProposalStatus.EXTERNALLY_SCHEDULED.value,
        ProposalStatus.PUBLISH_UNVERIFIED.value,
        ProposalStatus.PUBLISHED.value,
        ProposalStatus.PUBLISH_FAILED.value,
    }
)


def planned_channel_datetime(
    target_date: date,
    timezone_name: str,
    default_time: str,
) -> datetime:
    hour, minute = (int(value) for value in default_time.split(":"))
    zone = ZoneInfo(timezone_name)
    wall_time = datetime.combine(target_date, time(hour=hour, minute=minute), tzinfo=zone)
    # A post time inside a spring-forward gap does not exist. The UTC round trip
    # moves it to the real instant (for example 02:30 becomes 03:30) while
    # leaving every valid wall time unchanged.
    return wall_time.astimezone(UTC).astimezone(zone)


def aware_timestamp(value: str, *, field_name: str = "scheduled_publish_at") -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"active Lineup row has an invalid {field_name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"active Lineup row has a naive {field_name}")
    return parsed


def channel_slot_date(value: str | datetime, timezone: ZoneInfo) -> date:
    parsed = aware_timestamp(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Lineup timestamp must include a timezone")
    return parsed.astimezone(timezone).date()


def _active_proposal_slots(
    session: Session,
    channel_id: int,
    timezone: ZoneInfo,
) -> dict[int, tuple[date, str]]:
    proposals = session.scalars(
        select(Proposal).where(
            Proposal.channel_id == channel_id,
            Proposal.scheduled_publish_at.is_not(None),
            Proposal.status.in_(sorted(RESERVING_PROPOSAL_STATUSES)),
        )
    ).all()
    slots: dict[int, tuple[date, str]] = {}
    dates: dict[date, list[int]] = {}
    for proposal in proposals:
        assert proposal.scheduled_publish_at is not None
        parsed = aware_timestamp(proposal.scheduled_publish_at)
        slot_date = parsed.astimezone(timezone).date()
        slots[proposal.id] = (slot_date, proposal.scheduled_publish_at)
        dates.setdefault(slot_date, []).append(proposal.id)
    conflicts = {day: ids for day, ids in dates.items() if len(ids) > 1}
    if conflicts:
        conflict_days = ", ".join(day.isoformat() for day in sorted(conflicts))
        raise ValueError(
            "a Lineup date contains multiple active RunWay posts "
            f"({conflict_days}); resolve the conflicting rows before scheduling"
        )
    return slots


def synchronize_lineup_slot_claims(
    session: Session,
    channel_id: int,
    timezone_name: str,
) -> dict[int, tuple[date, str]]:
    """Repair derived claim rows and fail closed on real active-date conflicts.

    Cancelled/rejected rows, stale rows left by old versions, and rows whose
    timestamps moved are not allowed to block the calendar. Conversely, two
    genuinely active rows on one channel date are corruption: the unique claim
    table cannot choose a winner, so the operation fails instead of guessing.
    """
    timezone = ZoneInfo(timezone_name)
    active_slots = _active_proposal_slots(session, channel_id, timezone)
    claims = session.scalars(
        select(LineupSlotClaim).where(LineupSlotClaim.channel_id == channel_id)
    ).all()
    valid_proposal_ids: set[int] = set()
    for claim in claims:
        expected = active_slots.get(claim.proposal_id)
        if expected is None or claim.slot_date != expected[0]:
            session.delete(claim)
            continue
        if claim.scheduled_publish_at != expected[1]:
            claim.scheduled_publish_at = expected[1]
        valid_proposal_ids.add(claim.proposal_id)
    session.flush()

    for proposal_id, (slot_date, scheduled_publish_at) in active_slots.items():
        if proposal_id in valid_proposal_ids:
            continue
        session.add(
            LineupSlotClaim(
                channel_id=channel_id,
                proposal_id=proposal_id,
                slot_date=slot_date,
                scheduled_publish_at=scheduled_publish_at,
            )
        )
    try:
        session.flush()
    except IntegrityError as exc:
        raise ValueError("Lineup schedule slot claims are inconsistent") from exc
    return active_slots


def claim_next_available_slot(
    session: Session,
    proposal: Proposal,
    *,
    timezone_name: str,
    default_time: str,
    now: datetime | None = None,
) -> datetime:
    """Claim the first unoccupied channel date inside the caller's transaction."""
    timezone = ZoneInfo(timezone_name)
    active_slots = synchronize_lineup_slot_claims(session, proposal.channel_id, timezone_name)
    reserved = {
        slot_date
        for proposal_id, (slot_date, _timestamp) in active_slots.items()
        if proposal_id != proposal.id
    }
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("schedule comparison time must include a timezone")
    current = now.astimezone(timezone) if now is not None else datetime.now(timezone)
    first_date = current.date()
    first_slot = planned_channel_datetime(first_date, timezone_name, default_time)
    if first_slot <= current + timedelta(minutes=5):
        first_date += timedelta(days=1)
    candidate_date = first_date
    while candidate_date in reserved:
        candidate_date += timedelta(days=1)
    scheduled_for = planned_channel_datetime(candidate_date, timezone_name, default_time)

    session.execute(delete(LineupSlotClaim).where(LineupSlotClaim.proposal_id == proposal.id))
    session.flush()
    session.add(
        LineupSlotClaim(
            channel_id=proposal.channel_id,
            proposal_id=proposal.id,
            slot_date=candidate_date,
            scheduled_publish_at=scheduled_for.isoformat(),
        )
    )
    try:
        session.flush()
    except IntegrityError as exc:
        raise ValueError("another active Runway post already occupies that Lineup date") from exc
    return scheduled_for


def replace_lineup_slot_claims(
    session: Session,
    entries: Sequence[tuple[Proposal, str]],
    *,
    timezone_name: str,
) -> None:
    """Atomically replace claims for one Lineup move or swap."""
    proposal_ids = [proposal.id for proposal, _timestamp in entries]
    if not proposal_ids:
        return
    session.execute(delete(LineupSlotClaim).where(LineupSlotClaim.proposal_id.in_(proposal_ids)))
    session.flush()
    timezone = ZoneInfo(timezone_name)
    for proposal, timestamp in entries:
        parsed = aware_timestamp(timestamp)
        session.add(
            LineupSlotClaim(
                channel_id=proposal.channel_id,
                proposal_id=proposal.id,
                slot_date=parsed.astimezone(timezone).date(),
                scheduled_publish_at=timestamp,
            )
        )
    try:
        session.flush()
    except IntegrityError as exc:
        raise ValueError("the target Lineup date is already occupied") from exc


def release_lineup_slot_claim(session: Session, proposal_id: int) -> None:
    session.execute(delete(LineupSlotClaim).where(LineupSlotClaim.proposal_id == proposal_id))
