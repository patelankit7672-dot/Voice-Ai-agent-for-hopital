"""
Appointment booking, rescheduling, cancellation and lookup.

Storage is a JSON file (data/appointments.json) guarded by a process-level
lock and written atomically. That is good enough for an MVP demo and is
explicitly NOT production storage - see README, "Production improvements".

Every mutating function returns an OperationResult whose `success` flag is the
only thing the voice agent is allowed to treat as proof that something happened.
"""

from __future__ import annotations

import json
import os
import random
import string
import tempfile
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from .config import DATA_DIR
from .hospital_service import (
    IST,
    doctor_public_view,
    generate_slots,
    get_doctor,
    normalize_department,
    resolve_date,
    search_doctors,
    to_12_hour,
    today_ist,
    weekday_name,
)
from .models import OperationResult

APPOINTMENTS_PATH = DATA_DIR / "appointments.json"

# How far ahead a patient may book.
MAX_BOOKING_HORIZON_DAYS = 60
# Minimum lead time for a same-day booking.
MIN_LEAD_MINUTES = 30

_ACTIVE_STATUSES = {"confirmed", "rescheduled"}

_file_lock = threading.RLock()


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def _load() -> Dict[str, Any]:
    if not APPOINTMENTS_PATH.exists():
        return {"appointments": []}
    try:
        with APPOINTMENTS_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (json.JSONDecodeError, OSError):
        # A corrupt demo store should not take the whole app down.
        return {"appointments": []}
    payload.setdefault("appointments", [])
    return payload


def _save(payload: Dict[str, Any]) -> None:
    """Atomic write: temp file in the same directory, then os.replace."""
    APPOINTMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(APPOINTMENTS_PATH.parent), prefix=".appointments-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, APPOINTMENTS_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _new_appointment_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"VH{datetime.now(IST).strftime('%y%m%d')}{suffix}"


def _digits(phone: Optional[str]) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _phone_matches(stored: str, provided: Optional[str]) -> bool:
    """Match on the last 10 digits so '+91 98…' and '098…' both work."""
    if not provided:
        return False
    a, b = _digits(stored), _digits(provided)
    if not a or not b:
        return False
    return a[-10:] == b[-10:]


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _validate_booking_date(target: date) -> Optional[str]:
    current = today_ist()
    if target < current:
        return f"{target.isoformat()} is in the past."
    if target > current + timedelta(days=MAX_BOOKING_HORIZON_DAYS):
        return (
            f"Appointments can only be booked up to "
            f"{MAX_BOOKING_HORIZON_DAYS} days in advance."
        )
    return None


def _slot_is_in_the_past(target: date, hhmm: str) -> bool:
    if target != today_ist():
        return False
    try:
        hour, minute = (int(p) for p in hhmm.split(":"))
    except ValueError:
        return False
    slot_dt = datetime.now(IST).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return slot_dt <= datetime.now(IST) + timedelta(minutes=MIN_LEAD_MINUTES)


def _booked_times(
    appointments: List[Dict[str, Any]],
    doctor_id: str,
    day_iso: str,
    ignore_id: Optional[str] = None,
) -> set[str]:
    return {
        appt["time"]
        for appt in appointments
        if appt["doctor_id"] == doctor_id
        and appt["date"] == day_iso
        and appt["status"] in _ACTIVE_STATUSES
        and appt["appointment_id"] != ignore_id
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def check_availability(
    date_input: str,
    doctor_id: Optional[str] = None,
    department: Optional[str] = None,
) -> OperationResult:
    """Free slots for a doctor, or across a department, on a given date."""
    target = resolve_date(date_input)
    if not target:
        return OperationResult(
            success=False,
            code="invalid_date",
            message=(
                "I could not understand that date. Please say it as a day and "
                "month, or say today or tomorrow."
            ),
        )

    problem = _validate_booking_date(target)
    if problem:
        return OperationResult(success=False, code="invalid_date", message=problem)

    day_iso = target.isoformat()
    day_name = weekday_name(target)

    if doctor_id:
        doctor = get_doctor(doctor_id)
        if not doctor:
            return OperationResult(
                success=False,
                code="doctor_not_found",
                message=(
                    f"I do not have a doctor with the ID {doctor_id} in the "
                    "hospital records."
                ),
            )
        doctors = [doctor]
    elif department:
        canonical = normalize_department(department)
        if not canonical:
            return OperationResult(
                success=False,
                code="department_not_found",
                message=(
                    f"I do not have a department called {department}. I can "
                    "connect you with hospital staff if you would like."
                ),
            )
        doctors = search_doctors(department=canonical)
    else:
        return OperationResult(
            success=False,
            code="missing_target",
            message="Please tell me the doctor or the department you need.",
        )

    with _file_lock:
        store = _load()
        existing = store["appointments"]

    results: List[Dict[str, Any]] = []
    for doctor in doctors:
        if doctor.get("walk_in_only"):
            results.append({
                "doctor_id": doctor["id"],
                "doctor_name": doctor["name"],
                "department": doctor["department"],
                "walk_in_only": True,
                "available_slots": [],
                "note": (
                    "Emergency care is walk-in and does not need an "
                    "appointment. Please come directly to the emergency wing."
                ),
            })
            continue

        if day_name not in doctor["days"]:
            results.append({
                "doctor_id": doctor["id"],
                "doctor_name": doctor["name"],
                "department": doctor["department"],
                "available_slots": [],
                "note": (
                    f"{doctor['name']} does not consult on {day_name}. "
                    f"Available days: {', '.join(doctor['days'])}."
                ),
            })
            continue

        taken = _booked_times(existing, doctor["id"], day_iso)
        free = [
            slot
            for slot in generate_slots(doctor)
            if slot not in taken and not _slot_is_in_the_past(target, slot)
        ]
        results.append({
            "doctor_id": doctor["id"],
            "doctor_name": doctor["name"],
            "department": doctor["department"],
            "consultation_fee_inr": doctor.get("consultation_fee_inr"),
            "timings": doctor["timings"],
            "available_slots": free,
            "available_slots_spoken": [to_12_hour(s) for s in free[:6]],
            "total_available": len(free),
        })

    any_free = any(item.get("available_slots") for item in results)
    if any_free:
        message = f"Availability for {day_name}, {day_iso}."
    elif results:
        message = (
            f"There are no free slots on {day_name}, {day_iso}. "
            "I can check another date."
        )
    else:
        message = "I could not find any matching doctor for that request."

    return OperationResult(
        success=True,
        code="availability",
        message=message,
        data={"date": day_iso, "day": day_name, "doctors": results, "demo_data": True},
    )


def book_appointment(
    patient_name: str,
    patient_phone: str,
    doctor_id: str,
    date_input: str,
    time_hhmm: str,
    reason: Optional[str] = None,
) -> OperationResult:
    """Book a slot. Returns success=False (never a fake confirmation) on failure."""
    doctor = get_doctor(doctor_id)
    if not doctor:
        return OperationResult(
            success=False,
            code="doctor_not_found",
            message=f"I do not have a doctor with the ID {doctor_id} in the records.",
        )

    if doctor.get("walk_in_only"):
        return OperationResult(
            success=False,
            code="walk_in_only",
            message=(
                "Emergency care is walk-in only and cannot be booked as an "
                "appointment. Please go directly to the emergency wing."
            ),
        )

    target = resolve_date(date_input)
    if not target:
        return OperationResult(
            success=False,
            code="invalid_date",
            message="I could not understand that date. Please say the day and month.",
        )

    problem = _validate_booking_date(target)
    if problem:
        return OperationResult(success=False, code="invalid_date", message=problem)

    day_iso = target.isoformat()
    day_name = weekday_name(target)

    if day_name not in doctor["days"]:
        return OperationResult(
            success=False,
            code="doctor_unavailable_that_day",
            message=(
                f"{doctor['name']} does not consult on {day_name}. "
                f"Available days are {', '.join(doctor['days'])}."
            ),
            data={"available_days": doctor["days"]},
        )

    valid_slots = generate_slots(doctor)
    if time_hhmm not in valid_slots:
        return OperationResult(
            success=False,
            code="invalid_slot",
            message=(
                f"{to_12_hour(time_hhmm)} is not a consultation slot for "
                f"{doctor['name']}. Consultation hours are {doctor['timings']}."
            ),
            data={"valid_slots_spoken": [to_12_hour(s) for s in valid_slots[:8]]},
        )

    if _slot_is_in_the_past(target, time_hhmm):
        return OperationResult(
            success=False,
            code="slot_in_past",
            message=(
                "That time has already passed or is too soon. Please choose a "
                "later slot or another day."
            ),
        )

    with _file_lock:
        store = _load()
        appointments = store["appointments"]

        if time_hhmm in _booked_times(appointments, doctor["id"], day_iso):
            taken = _booked_times(appointments, doctor["id"], day_iso)
            free = [
                s for s in valid_slots
                if s not in taken and not _slot_is_in_the_past(target, s)
            ]
            return OperationResult(
                success=False,
                code="slot_taken",
                message=(
                    f"{to_12_hour(time_hhmm)} is already booked with "
                    f"{doctor['name']}. I can offer another time."
                ),
                data={
                    "available_slots": free,
                    "available_slots_spoken": [to_12_hour(s) for s in free[:6]],
                },
            )

        # Same patient, same doctor, same day - do not silently double-book.
        for appt in appointments:
            if (
                appt["status"] in _ACTIVE_STATUSES
                and appt["doctor_id"] == doctor["id"]
                and appt["date"] == day_iso
                and _phone_matches(appt["patient_phone"], patient_phone)
            ):
                return OperationResult(
                    success=False,
                    code="duplicate_booking",
                    message=(
                        f"You already have an appointment with {doctor['name']} "
                        f"on {day_iso} at {to_12_hour(appt['time'])}. "
                        f"The reference is {appt['appointment_id']}. "
                        "Would you like to reschedule it instead?"
                    ),
                    data={"existing_appointment": appt},
                )

        record = {
            "appointment_id": _new_appointment_id(),
            "patient_name": patient_name,
            "patient_phone": patient_phone,
            "doctor_id": doctor["id"],
            "doctor_name": doctor["name"],
            "department": doctor["department"],
            "date": day_iso,
            "time": time_hhmm,
            "status": "confirmed",
            "created_at": _now_iso(),
            "updated_at": None,
            "reason": reason,
            "demo_record": True,
        }
        appointments.append(record)
        _save(store)

    return OperationResult(
        success=True,
        code="booked",
        message=(
            f"Appointment confirmed for {patient_name} with {doctor['name']}, "
            f"{doctor['department']}, on {day_name} {day_iso} at "
            f"{to_12_hour(time_hhmm)}. The reference number is "
            f"{record['appointment_id']}."
        ),
        data={
            "appointment": record,
            "spoken_time": to_12_hour(time_hhmm),
            "day": day_name,
            "room": doctor.get("room"),
            "consultation_fee_inr": doctor.get("consultation_fee_inr"),
        },
    )


def reschedule_appointment(
    appointment_id: str,
    date_input: str,
    time_hhmm: str,
    patient_phone: Optional[str] = None,
) -> OperationResult:
    """Move an existing active appointment to a new date/time."""
    target = resolve_date(date_input)
    if not target:
        return OperationResult(
            success=False,
            code="invalid_date",
            message="I could not understand that date. Please say the day and month.",
        )

    problem = _validate_booking_date(target)
    if problem:
        return OperationResult(success=False, code="invalid_date", message=problem)

    day_iso = target.isoformat()
    day_name = weekday_name(target)

    with _file_lock:
        store = _load()
        appointments = store["appointments"]

        record = next(
            (a for a in appointments if a["appointment_id"] == appointment_id), None
        )
        if not record:
            return OperationResult(
                success=False,
                code="not_found",
                message=(
                    f"I could not find an appointment with the reference "
                    f"{appointment_id}. Please check the number, or I can "
                    "connect you with hospital staff."
                ),
            )

        if record["status"] == "cancelled":
            return OperationResult(
                success=False,
                code="already_cancelled",
                message=(
                    f"Appointment {appointment_id} was already cancelled. "
                    "I can book a new one instead."
                ),
            )

        # SECURITY: see cancel_appointment. Ownership must be proven, not
        # merely checked when a phone number happens to be supplied.
        if not patient_phone:
            return OperationResult(
                success=False,
                code="verification_required",
                message=(
                    "For your security I need the phone number this "
                    "appointment was booked with before I can move it."
                ),
            )

        if not _phone_matches(record["patient_phone"], patient_phone):
            return OperationResult(
                success=False,
                code="verification_failed",
                message=(
                    "That phone number does not match the one on this "
                    "appointment. For your security I cannot change it. "
                    "I can connect you with hospital staff."
                ),
            )

        doctor = get_doctor(record["doctor_id"])
        if not doctor:
            return OperationResult(
                success=False,
                code="doctor_not_found",
                message=(
                    "The doctor on this appointment is no longer in the "
                    "records. Let me connect you with hospital staff."
                ),
            )

        if day_name not in doctor["days"]:
            return OperationResult(
                success=False,
                code="doctor_unavailable_that_day",
                message=(
                    f"{doctor['name']} does not consult on {day_name}. "
                    f"Available days are {', '.join(doctor['days'])}."
                ),
                data={"available_days": doctor["days"]},
            )

        valid_slots = generate_slots(doctor)
        if time_hhmm not in valid_slots:
            return OperationResult(
                success=False,
                code="invalid_slot",
                message=(
                    f"{to_12_hour(time_hhmm)} is not a consultation slot for "
                    f"{doctor['name']}. Consultation hours are {doctor['timings']}."
                ),
            )

        if _slot_is_in_the_past(target, time_hhmm):
            return OperationResult(
                success=False,
                code="slot_in_past",
                message="That time has already passed. Please choose a later slot.",
            )

        taken = _booked_times(
            appointments, doctor["id"], day_iso, ignore_id=appointment_id
        )
        if time_hhmm in taken:
            free = [
                s for s in valid_slots
                if s not in taken and not _slot_is_in_the_past(target, s)
            ]
            return OperationResult(
                success=False,
                code="slot_taken",
                message=(
                    f"{to_12_hour(time_hhmm)} is already booked. "
                    "I can offer another time."
                ),
                data={"available_slots_spoken": [to_12_hour(s) for s in free[:6]]},
            )

        previous = {"date": record["date"], "time": record["time"]}
        record["date"] = day_iso
        record["time"] = time_hhmm
        record["status"] = "rescheduled"
        record["updated_at"] = _now_iso()
        _save(store)

    return OperationResult(
        success=True,
        code="rescheduled",
        message=(
            f"Appointment {appointment_id} has been moved to {day_name} "
            f"{day_iso} at {to_12_hour(time_hhmm)} with {record['doctor_name']}."
        ),
        data={
            "appointment": record,
            "previous": previous,
            "spoken_time": to_12_hour(time_hhmm),
        },
    )


def cancel_appointment(
    appointment_id: str,
    patient_phone: Optional[str] = None,
    confirm: bool = True,
) -> OperationResult:
    """Cancel an appointment. The agent must confirm with the patient first."""
    if not confirm:
        return OperationResult(
            success=False,
            code="confirmation_required",
            message="Cancellation was not confirmed, so nothing has been changed.",
        )

    with _file_lock:
        store = _load()
        record = next(
            (a for a in store["appointments"] if a["appointment_id"] == appointment_id),
            None,
        )
        if not record:
            return OperationResult(
                success=False,
                code="not_found",
                message=(
                    f"I could not find an appointment with the reference "
                    f"{appointment_id}. Please check the number."
                ),
            )

        if record["status"] == "cancelled":
            return OperationResult(
                success=False,
                code="already_cancelled",
                message=f"Appointment {appointment_id} was already cancelled.",
                data={"appointment": record},
            )

        # SECURITY: ownership must be PROVEN, not merely checked when offered.
        # This used to verify only `if patient_phone`, so omitting the field
        # skipped verification entirely and let anyone cancel any appointment
        # they knew the ID of. Staff cancel through the authenticated portal
        # (staff_set_status), so nothing legitimate depends on the old hole.
        if not patient_phone:
            return OperationResult(
                success=False,
                code="verification_required",
                message=(
                    "For your security I need the phone number this "
                    "appointment was booked with before I can cancel it."
                ),
            )

        if not _phone_matches(record["patient_phone"], patient_phone):
            return OperationResult(
                success=False,
                code="verification_failed",
                message=(
                    "That phone number does not match the one on this "
                    "appointment, so I cannot cancel it. I can connect you "
                    "with hospital staff."
                ),
            )

        record["status"] = "cancelled"
        record["updated_at"] = _now_iso()
        _save(store)

    return OperationResult(
        success=True,
        code="cancelled",
        message=(
            f"Appointment {appointment_id} with {record['doctor_name']} on "
            f"{record['date']} at {to_12_hour(record['time'])} has been cancelled."
        ),
        data={"appointment": record},
    )


def staff_set_status(
    appointment_id: str,
    status: str,
    note: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Move an appointment into a new status on behalf of hospital staff.

    Unlike cancel_appointment this performs NO phone verification: the caller
    is a signed-in staff member working from the ward list, not a stranger on
    the phone. Returns the updated record, or None if the ID is unknown.
    """
    with _file_lock:
        store = _load()
        record = next(
            (a for a in store["appointments"] if a["appointment_id"] == appointment_id),
            None,
        )
        if not record:
            return None

        record["status"] = status
        record["updated_at"] = _now_iso()
        if note:
            record["staff_note"] = note[:500]
        _save(store)
        return dict(record)


def get_appointment(
    appointment_id: Optional[str] = None,
    patient_phone: Optional[str] = None,
) -> OperationResult:
    """Look up one appointment by reference, or all active ones by phone."""
    if not appointment_id and not patient_phone:
        return OperationResult(
            success=False,
            code="missing_identifier",
            message="Please give me your appointment reference or your phone number.",
        )

    with _file_lock:
        appointments = _load()["appointments"]

    if appointment_id:
        record = next(
            (a for a in appointments if a["appointment_id"] == appointment_id.upper()),
            None,
        )
        if not record:
            return OperationResult(
                success=False,
                code="not_found",
                message=(
                    f"I could not find an appointment with the reference "
                    f"{appointment_id}."
                ),
            )
        # SECURITY: reading a record discloses the patient's name, doctor and
        # visit time, so it needs the same ownership proof as changing one.
        if not patient_phone:
            return OperationResult(
                success=False,
                code="verification_required",
                message=(
                    "For your security I need the phone number this "
                    "appointment was booked with before I can read it out."
                ),
            )
        if not _phone_matches(record["patient_phone"], patient_phone):
            return OperationResult(
                success=False,
                code="verification_failed",
                message="That phone number does not match this appointment.",
            )
        return OperationResult(
            success=True,
            code="found",
            message=(
                f"Appointment {record['appointment_id']} with "
                f"{record['doctor_name']} on {record['date']} at "
                f"{to_12_hour(record['time'])}. Status: {record['status']}."
            ),
            data={"appointment": record},
        )

    matches = [
        a
        for a in appointments
        if _phone_matches(a["patient_phone"], patient_phone)
        and a["status"] in _ACTIVE_STATUSES
    ]
    matches.sort(key=lambda a: (a["date"], a["time"]))

    if not matches:
        return OperationResult(
            success=False,
            code="not_found",
            message="I could not find any active appointment for that phone number.",
        )

    return OperationResult(
        success=True,
        code="found",
        message=f"I found {len(matches)} active appointment(s) for that number.",
        data={"appointments": matches, "count": len(matches)},
    )


def list_appointments(include_cancelled: bool = False) -> List[Dict[str, Any]]:
    """All appointments, newest first. Used by the UI's appointment panel."""
    with _file_lock:
        appointments = _load()["appointments"]
    if not include_cancelled:
        appointments = [a for a in appointments if a["status"] in _ACTIVE_STATUSES]
    return sorted(appointments, key=lambda a: (a["date"], a["time"]))


def doctor_summary_for_agent(doctor_id: str) -> Optional[Dict[str, Any]]:
    doctor = get_doctor(doctor_id)
    return doctor_public_view(doctor) if doctor else None


__all__ = [
    "check_availability",
    "book_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "get_appointment",
    "list_appointments",
    "staff_set_status",
    "doctor_summary_for_agent",
    "MAX_BOOKING_HORIZON_DAYS",
]
