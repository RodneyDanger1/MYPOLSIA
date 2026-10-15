"""Automation presets — the "start an automation" frequency contract (wave V12 / G2 engine lane).

Why this module exists (original-vs-ours, engine side — see ``wave-v12/g2-engine/GAP-LEDGER.md``
rows E8/E9/E19):

* The ORIGINAL's start-an-automation surfaces are FREQUENCY PICKERS.  Cold Outreach offers
  ONCE / DAILY / WEEKDAYS / WEEKLY with the printed monthly counts ``30/22/4 TASKS/MO`` and the
  footnote ``Uses 1 credit per run.`` (``agent2-code/cap-07.md``); Support offers ONCE / DAILY
  (``agent2-code/cap-08.md``); Meta Ads setup "creates a **Meta Ads - Daily** task"
  (<https://help.polsia.com/how-to-run-meta-ads>, fetched 2026-10-15).
* OURS already has the durable half: ``task_schedules`` (cadence once/daily/weekdays/weekly,
  ``app/models/schedule.py``), the DST-safe ``schedule_service.compute_next_run``, and the
  15-minute cadence tick (``celery_app/beat_schedule.py`` "task-cadence").  What was missing is
  the NAMED CONTRACT between a start-modal's frequency choice and that store: one validated
  spec builder, refusal by name for an unknown automation or a frequency it must not offer, and
  a monthly-run number that is COUNTED from a calendar window — never the original's (or
  anyone's) hand-typed estimates.

This module is PURE: no DB, no I/O, no clock of its own.  ``build_schedule_spec`` requires the
caller's ``now`` + company ``tz``; persistence stays with ``schedule_service.create_schedule``
(HANDOFF G2-H3 wires the route / UI adapter — see the lane RECEIPT).

Never a silent default: an unknown automation or an unoffered frequency refuses BY NAME
(``E_VALIDATION`` + ``how_to_fix``), and the store's own refusals (bad hour, weekly without a
weekday) propagate untouched — the store stays the single enforcement point for its rules.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.schedule import ScheduleCadence
from app.services.schedule_service import compute_next_run

# ── the preset table (evidence-cited; see module docstring) ───────────────────

#: Automation -> the frequencies its start-modal may offer.  Every value is a
#: `ScheduleCadence` member value, so the table cannot drift from what the store persists.
PRESETS: dict[str, tuple[str, ...]] = {
    "cold_outreach": ("once", "daily", "weekdays", "weekly"),
    "support": ("once", "daily"),
    "meta_ads_daily": ("daily",),
}

#: Default schedule name + prompt per automation.  Callers may override both; the defaults are
#: the captured modals' own descriptions, minus the credit claim our dollar unit replaces
#: (MATCH-VS-BEAT B4 / JC-1 — we state dollars, not credits).
DEFAULTS: dict[str, tuple[str, str]] = {
    "cold_outreach": (
        "Cold outreach",
        "Cold outreach run: find leads and email them; replies land in the inbox.",
    ),
    "support": (
        "Support",
        "Auto-support run: reply to unanswered emails in the inbox.",
    ),
    "meta_ads_daily": (
        "Meta Ads - Daily",
        "Meta ads daily run: review the running campaign and keep it optimizing.",
    ),
}

#: Plain frequency labels for the UI adapter (numbers ride on the computed estimate only).
_LABELS: dict[str, str] = {
    "once": "Once",
    "daily": "Daily",
    "weekdays": "Weekdays (Mon-Fri)",
    "weekly": "Weekly",
}

#: The fixed counting window for every monthly estimate.  2024-01-01 was a Monday, so the
#: window's weekday mix is deterministic: 4 full weeks + 2 extra days (Mon/Tue) = 22 weekdays.
WINDOW_DAYS = 30
_WINDOW_START = date(2024, 1, 1)

_CADENCE_VALUES = tuple(member.value for member in ScheduleCadence)


def _refuse(field: str, value: object, expected: str, *, how_to_fix: str = "") -> NoReturn:
    raise PolsiaError(
        E_VALIDATION,
        f"automation preset {field} {value!r} is invalid: {expected}",
        how_to_fix=how_to_fix or ERROR_HINTS[E_VALIDATION],
        context={"field": field, "value": value, "expected": expected},
    )


def _refuse_weekday(weekday: object) -> None:
    if weekday is None:
        return
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
        _refuse("weekday", weekday, "an integer 0-6 (Monday=0) or null")


# ── the counted estimate ──────────────────────────────────────────────────────


def estimate_runs_per_month(cadence: str, *, weekday: int | None = None) -> dict[str, Any]:
    """Runs per "month" (a fixed 30-day window), COUNTED from the calendar — never typed.

    ``weekday`` only changes the ``weekly`` answer (an exact count for that weekday); for
    ``weekly`` without one, the min/max across all seven weekdays is reported (4-5 in a 30-day
    window).  ``once`` answers 1 ("fires at most once" — the store clears ``next_run_at``).
    """
    if cadence not in _CADENCE_VALUES:
        _refuse("cadence", cadence, f"one of {', '.join(_CADENCE_VALUES)}")
    _refuse_weekday(weekday)
    window = [_WINDOW_START + timedelta(days=offset) for offset in range(WINDOW_DAYS)]
    if cadence == "once":
        count_min = count_max = 1
    elif cadence == "daily":
        count_min = count_max = len(window)
    elif cadence == "weekdays":
        count_min = count_max = sum(1 for day in window if day.weekday() < 5)
    else:  # weekly
        by_weekday = [
            sum(1 for day in window if day.weekday() == target) for target in range(7)
        ]
        if weekday is None:
            count_min, count_max = min(by_weekday), max(by_weekday)
        else:
            count_min = count_max = by_weekday[weekday]
    return {
        "runs_per_month_min": count_min,
        "runs_per_month_max": count_max,
        "basis": (
            f"counted over a fixed {WINDOW_DAYS}-day window starting "
            f"{_WINDOW_START.isoformat()} (a Monday); 'month' here is {WINDOW_DAYS} days"
        ),
    }


# ── the spec builder ──────────────────────────────────────────────────────────


def build_schedule_spec(
    automation: str,
    frequency: str,
    *,
    hour: int,
    weekday: int | None = None,
    now: datetime,
    tz: ZoneInfo,
    name: str | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Validate a start-modal choice and build the schedule spec for it.

    ``next_run_at`` is DELEGATED to ``schedule_service.compute_next_run`` (the DST-safe,
    company-tz authority) — the store's refusals (out-of-range hour, weekly without a weekday)
    propagate untouched, so there is exactly one place that owns those rules.

    Returns::

        {
          "automation": ...,
          "frequency": ...,                # the modal's word; == the store cadence value
          "schedule_create": {             # exactly schedule_service.create_schedule's kwargs
              "name", "prompt", "cadence", "hour", "weekday"
          },
          "next_run_at": isoformat str,    # UTC instant, strictly after `now`
          "estimate": {"runs_per_month_min", "runs_per_month_max", "basis"},
        }
    """
    if automation not in PRESETS:
        _refuse(
            "automation",
            automation,
            f"one of {', '.join(sorted(PRESETS))}",
            how_to_fix="use an automation name from list_presets()",
        )
    offered = PRESETS[automation]
    if frequency not in offered:
        _refuse(
            "frequency",
            frequency,
            f"one of {', '.join(offered)} for {automation}",
            how_to_fix=(
                f"{automation} does not offer {frequency!r}; its start modal offers: "
                f"{', '.join(offered)}"
            ),
        )
    next_run = compute_next_run(frequency, hour, weekday, now=now, tz=tz)
    default_name, default_prompt = DEFAULTS[automation]
    return {
        "automation": automation,
        "frequency": frequency,
        "schedule_create": {
            "name": default_name if name is None else name,
            "prompt": default_prompt if prompt is None else prompt,
            "cadence": frequency,
            "hour": hour,
            "weekday": weekday,
        },
        "next_run_at": next_run.isoformat(),
        "estimate": estimate_runs_per_month(
            frequency, weekday=weekday if frequency == "weekly" else None
        ),
    }


def list_presets() -> list[dict[str, Any]]:
    """The preset catalog for routes / the UI adapter: automations, their frequencies, defaults.

    Every frequency carries its COUNTED estimate (numbers exist only here, computed — the
    original's modal printed hand-typed counts; ours cannot drift from the calendar).
    """
    rows: list[dict[str, Any]] = []
    for automation, frequencies in PRESETS.items():
        default_name, default_prompt = DEFAULTS[automation]
        rows.append(
            {
                "automation": automation,
                "name": default_name,
                "prompt": default_prompt,
                "frequencies": [
                    {
                        "value": frequency,
                        "label": _LABELS[frequency],
                        "estimate": estimate_runs_per_month(frequency),
                    }
                    for frequency in frequencies
                ],
            }
        )
    return rows


__all__ = [
    "DEFAULTS",
    "PRESETS",
    "WINDOW_DAYS",
    "build_schedule_spec",
    "estimate_runs_per_month",
    "list_presets",
]
