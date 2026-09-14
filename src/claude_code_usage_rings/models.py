from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .account_usage import UsageWindow


@dataclass(frozen=True)
class UsageCardModel:
    title: str
    percent_remaining: int
    reset_text: str


def build_card_models(
    *,
    five_hour: UsageWindow | None,
    weekly: UsageWindow | None,
) -> tuple[UsageCardModel, UsageCardModel]:
    return (
        UsageCardModel(
            title="5-hour usage limit",
            percent_remaining=_remaining_percent(five_hour),
            reset_text=_reset_text(five_hour, include_date=False),
        ),
        UsageCardModel(
            title="Weekly usage limit",
            percent_remaining=_remaining_percent(weekly),
            reset_text=_reset_text(weekly, include_date=True),
        ),
    )


def _remaining_percent(window: UsageWindow | None) -> int:
    if window is None or window.remaining_percent is None:
        return 0
    return max(0, min(100, int(round(window.remaining_percent))))


def _reset_text(window: UsageWindow | None, *, include_date: bool) -> str:
    if window is None or window.reset_at is None:
        return "Reset unavailable"

    dt = datetime.fromtimestamp(window.reset_at).astimezone()
    if include_date:
        months = (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        )
        return f"Resets {months[dt.month - 1]} {dt.day}, {dt.year} at {dt:%H:%M}"
    return f"Resets {dt:%H:%M}"
