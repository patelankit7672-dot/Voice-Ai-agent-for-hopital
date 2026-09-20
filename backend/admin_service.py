"""
Staff portal service — sessions, dashboard aggregates and appointment triage.

SECURITY BOUNDARY
-----------------
This is DEMO-GRADE access control and is deliberately labelled as such
throughout the UI. One shared passcode, compared in constant time, buys a
bearer token held in this process's memory. There are no per-staff accounts,
no roles, no audit trail, and the token dies when the server restarts.

That is enough to stop a demo being wide open on a shared network. It is NOT
authentication for real patient data. Before this touches a real patient
record it needs per-user accounts, server-side session storage, an audit log
of every read and write, transport security, and encryption at rest.

Everything here reads and writes the same flat JSON store the voice agent
uses, through appointment_service, so the two views can never disagree.
"""

from __future__ import annotations

import csv
import io
import logging
import secrets
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from .config import settings
from . import appointment_service as appointments
from . import emergency_service as emergency
from . import hospital_service as hospital

logger = logging.getLogger("varanasi.admin")

# Statuses staff can move an appointment into from the portal.
TRIAGE_STATUSES = {"confirmed", "completed", "no_show", "cancelled"}

# Sessions live in memory only: a restart signs everybody out, which is the
# safe direction to fail.
_sessions: Dict[str, datetime] = {}
_lock = threading.Lock()


class AdminError(Exception):
    """Raised for an expected, reportable staff-portal failure."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _purge_expired(now: Optional[datetime] = None) -> None:
    now = now or datetime.now()
    for token, expires in list(_sessions.items()):
        if expires <= now:
            _sessions.pop(token, None)


def login(passcode: str) -> Dict[str, Any]:
    """Exchange the shared passcode for a bearer token."""
    if not settings.admin_enabled:
        raise AdminError(
            "The staff portal is switched off. Set ADMIN_PASSCODE in .env and "
            "restart the server to enable it.",
            status_code=503,
        )

    # Constant time: a timing difference would leak the passcode a character
    # at a time.
    if not secrets.compare_digest(str(passcode or ""), settings.admin_passcode):
        raise AdminError("That passcode is not correct.", status_code=401)

    token = secrets.token_urlsafe(32)
    expires = datetime.now() + timedelta(hours=settings.admin_session_hours)
    with _lock:
        _purge_expired()
        _sessions[token] = expires

    logger.info("Staff portal session opened (expires %s)", expires.isoformat())
    return {"token": token, "expires_at": expires.isoformat()}


def logout(token: Optional[str]) -> None:
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def require_session(token: Optional[str]) -> None:
    """Raise unless `token` is a live session."""
    if not settings.admin_enabled:
        raise AdminError("The staff portal is switched off.", status_code=503)
    if not token:
        raise AdminError("Staff sign-in required.", status_code=401)
    with _lock:
        _purge_expired()
        if token not in _sessions:
            raise AdminError("Your session has expired. Sign in again.", status_code=401)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _today() -> date:
    return hospital.today_ist()


def _all(include_cancelled: bool = True) -> List[Dict[str, Any]]:
    return appointments.list_appointments(include_cancelled=include_cancelled)


def _sort_key(appt: Dict[str, Any]) -> tuple:
    return (appt.get("date") or "", appt.get("time") or "")


def list_for_staff(
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    department: Optional[str] = None,
    doctor_id: Optional[str] = None,
    query: Optional[str] = None,
    scope: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filtered appointment list. Every filter is optional and combines with AND."""
    rows = _all(include_cancelled=True)
    today = _today().isoformat()

    if scope == "today":
        date_from = date_to = today
    elif scope == "upcoming":
        date_from = date_from or today
    elif scope == "past":
        date_to = date_to or today

    if date_from:
        rows = [r for r in rows if (r.get("date") or "") >= date_from]
    if date_to:
        rows = [r for r in rows if (r.get("date") or "") <= date_to]
    if status:
        wanted = {s.strip().lower() for s in status.split(",") if s.strip()}
        rows = [r for r in rows if (r.get("status") or "").lower() in wanted]
    if department:
        rows = [r for r in rows if (r.get("department") or "").lower() == department.lower()]
    if doctor_id:
        rows = [r for r in rows if r.get("doctor_id") == doctor_id]
    if query:
        q = query.strip().lower()
        digits = "".join(ch for ch in q if ch.isdigit())
        def hit(r: Dict[str, Any]) -> bool:
            if q in (r.get("patient_name") or "").lower():
                return True
            if q in (r.get("appointment_id") or "").lower():
                return True
            if q in (r.get("doctor_name") or "").lower():
                return True
            if digits and digits in "".join(
                ch for ch in (r.get("patient_phone") or "") if ch.isdigit()
            ):
                return True
            return False
        rows = [r for r in rows if hit(r)]

    return sorted(rows, key=_sort_key)


def dashboard() -> Dict[str, Any]:
    """Counts and breakdowns for the staff dashboard."""
    rows = _all(include_cancelled=True)
    today = _today()
    today_iso = today.isoformat()
    week_end = (today + timedelta(days=7)).isoformat()

    active = [r for r in rows if r.get("status") in {"confirmed", "rescheduled"}]
    todays = [r for r in rows if r.get("date") == today_iso]

    by_department = Counter(
        r.get("department") or "Unassigned"
        for r in active
        if (r.get("date") or "") >= today_iso
    )
    by_status = Counter((r.get("status") or "unknown") for r in rows)
    by_doctor: Dict[str, Dict[str, Any]] = {}
    for r in active:
        if (r.get("date") or "") < today_iso:
            continue
        key = r.get("doctor_id") or "?"
        entry = by_doctor.setdefault(
            key,
            {
                "doctor_id": key,
                "doctor_name": r.get("doctor_name") or "Unknown",
                "department": r.get("department") or "",
                "upcoming": 0,
                "today": 0,
            },
        )
        entry["upcoming"] += 1
        if r.get("date") == today_iso:
            entry["today"] += 1

    next_up = sorted(
        (
            r
            for r in active
            if (r.get("date") or "") >= today_iso
        ),
        key=_sort_key,
    )[:8]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hospital_date": today_iso,
        "totals": {
            "today": len([r for r in todays if r.get("status") != "cancelled"]),
            "today_completed": len([r for r in todays if r.get("status") == "completed"]),
            "today_remaining": len(
                [r for r in todays if r.get("status") in {"confirmed", "rescheduled"}]
            ),
            "upcoming_7_days": len(
                [r for r in active if today_iso <= (r.get("date") or "") <= week_end]
            ),
            "active_total": len(active),
            "cancelled_total": by_status.get("cancelled", 0),
            "no_show_total": by_status.get("no_show", 0),
            "all_time": len(rows),
        },
        "by_status": dict(by_status),
        "by_department": [
            {"department": name, "count": count}
            for name, count in by_department.most_common()
        ],
        "by_doctor": sorted(
            by_doctor.values(), key=lambda d: (-d["upcoming"], d["doctor_name"])
        ),
        "next_up": next_up,
        "escalations": {
            "emergencies": len(emergency.recent_emergencies(limit=100)),
            "handoffs_queued": len(
                [h for h in emergency.recent_handoffs(limit=100) if h.get("status") == "queued"]
            ),
        },
    }


def doctor_board() -> List[Dict[str, Any]]:
    """Every doctor with their current appointment load."""
    today_iso = _today().isoformat()
    rows = [
        r
        for r in _all(include_cancelled=False)
        if (r.get("date") or "") >= today_iso
    ]
    load: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        load.setdefault(r.get("doctor_id") or "?", []).append(r)

    board = []
    for doc in hospital.list_doctors():
        mine = sorted(load.get(doc.get("id"), []), key=_sort_key)
        board.append(
            {
                **doc,
                "upcoming_count": len(mine),
                "today_count": len([m for m in mine if m.get("date") == today_iso]),
                "next_appointment": mine[0] if mine else None,
            }
        )
    board.sort(key=lambda d: (-d["today_count"], -d["upcoming_count"], d.get("name") or ""))
    return board


def escalations() -> Dict[str, Any]:
    """Emergency detections and staff handoff requests from this server run."""
    return {
        "emergencies": list(reversed(emergency.recent_emergencies(limit=50))),
        "handoffs": list(reversed(emergency.recent_handoffs(limit=50))),
        "in_memory_notice": (
            "These are held in memory for this server run only and are cleared "
            "on restart. They are not a medical record."
        ),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def set_status(appointment_id: str, status: str, note: Optional[str] = None) -> Dict[str, Any]:
    """Move an appointment into a new triage status."""
    status = (status or "").strip().lower()
    if status not in TRIAGE_STATUSES:
        raise AdminError(
            f"Unknown status '{status}'. Allowed: {', '.join(sorted(TRIAGE_STATUSES))}."
        )

    updated = appointments.staff_set_status(
        appointment_id=appointment_id, status=status, note=note
    )
    if not updated:
        raise AdminError(f"No appointment found with ID {appointment_id}.", status_code=404)

    logger.info("Staff moved %s to %s", appointment_id, status)
    return updated


def export_csv(rows: List[Dict[str, Any]]) -> str:
    """Appointments as CSV for handover to another system."""
    columns = [
        "appointment_id",
        "date",
        "time",
        "status",
        "patient_name",
        "patient_phone",
        "doctor_name",
        "department",
        "reason",
        "created_at",
        "updated_at",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return buffer.getvalue()


__all__ = [
    "AdminError",
    "TRIAGE_STATUSES",
    "login",
    "logout",
    "require_session",
    "list_for_staff",
    "dashboard",
    "doctor_board",
    "escalations",
    "set_status",
    "export_csv",
]
