"""
Read-only hospital reference data: doctors, departments, hospital info.

Everything the voice agent says about doctors, timings and departments comes
from here. The agent is instructed never to invent a record; if a lookup misses,
these functions return an explicit "not found" the agent can read out honestly.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .config import DATA_DIR

IST = ZoneInfo("Asia/Kolkata")

WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]

# Natural-language date words the voice agent (or a patient) may pass through.
_RELATIVE_DATES: Dict[str, int] = {
    "today": 0, "tonight": 0, "aaj": 0, "आज": 0,
    "tomorrow": 1, "tmrw": 1, "kal": 1, "कल": 1,
    "day after tomorrow": 2, "parso": 2, "परसों": 2,
}

_DEPARTMENT_SYNONYMS: Dict[str, str] = {
    "heart": "Cardiology",
    "cardiac": "Cardiology",
    "cardiologist": "Cardiology",
    "dil": "Cardiology",
    "bone": "Orthopedics",
    "bones": "Orthopedics",
    "joint": "Orthopedics",
    "fracture": "Orthopedics",
    "ortho": "Orthopedics",
    "orthopaedics": "Orthopedics",
    "haddi": "Orthopedics",
    "child": "Pediatrics",
    "children": "Pediatrics",
    "kid": "Pediatrics",
    "kids": "Pediatrics",
    "baby": "Pediatrics",
    "paediatrics": "Pediatrics",
    "bachcha": "Pediatrics",
    "skin": "Dermatology",
    "hair": "Dermatology",
    "twacha": "Dermatology",
    "brain": "Neurology",
    "nerve": "Neurology",
    "headache": "Neurology",
    "migraine": "Neurology",
    "ear": "ENT",
    "nose": "ENT",
    "throat": "ENT",
    "gala": "ENT",
    "kaan": "ENT",
    "pregnancy": "Gynecology",
    "pregnant": "Gynecology",
    "maternity": "Gynecology",
    "gynaecology": "Gynecology",
    "obstetrics": "Gynecology",
    "physician": "General Medicine",
    "fever": "General Medicine",
    "general": "General Medicine",
    "diabetes": "General Medicine",
    "sugar": "General Medicine",
    "bp": "General Medicine",
    "xray": "Radiology",
    "x-ray": "Radiology",
    "scan": "Radiology",
    "ct": "Radiology",
    "ultrasound": "Radiology",
    "sonography": "Radiology",
    "blood test": "Pathology",
    "lab": "Pathology",
    "test": "Pathology",
    "casualty": "Emergency",
    "trauma": "Emergency",
}

_lock = threading.Lock()


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _read_json(filename: str) -> Dict[str, Any]:
    path = DATA_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Required data file is missing: {filename}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _doctors_raw() -> Dict[str, Any]:
    return _read_json("doctors.json")


@lru_cache(maxsize=1)
def _departments_raw() -> Dict[str, Any]:
    return _read_json("departments.json")


@lru_cache(maxsize=1)
def _hospital_raw() -> Dict[str, Any]:
    return _read_json("hospital.json")


def reload_reference_data() -> None:
    """Drop caches so edited JSON is picked up without a restart."""
    with _lock:
        _doctors_raw.cache_clear()
        _departments_raw.cache_clear()
        _hospital_raw.cache_clear()


# --------------------------------------------------------------------------
# Accessors
# --------------------------------------------------------------------------


def list_doctors() -> List[Dict[str, Any]]:
    return list(_doctors_raw().get("doctors", []))


def list_departments() -> List[Dict[str, Any]]:
    return list(_departments_raw().get("departments", []))


def get_hospital_info() -> Dict[str, Any]:
    return dict(_hospital_raw())


def get_doctor(doctor_id: str) -> Optional[Dict[str, Any]]:
    if not doctor_id:
        return None
    target = doctor_id.strip().upper()
    for doctor in list_doctors():
        if doctor["id"].upper() == target:
            return dict(doctor)
    return None


def get_department(name: str) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    canonical = normalize_department(name)
    if not canonical:
        return None
    for dept in list_departments():
        if dept["name"].lower() == canonical.lower():
            enriched = dict(dept)
            enriched["doctor_details"] = [
                {
                    "id": d["id"],
                    "name": d["name"],
                    "specialization": d["specialization"],
                    "days": d["days"],
                    "timings": d["timings"],
                }
                for d in list_doctors()
                if d["department"].lower() == dept["name"].lower()
            ]
            return enriched
    return None


def normalize_department(text: str) -> Optional[str]:
    """Map free-form speech ('heart doctor', 'haddi') to a real department."""
    if not text:
        return None
    cleaned = re.sub(r"[^\w\s-]", " ", text.lower()).strip()
    names = {d["name"].lower(): d["name"] for d in list_departments()}

    if cleaned in names:
        return names[cleaned]

    # Longest synonym first so "blood test" beats "test".
    for phrase in sorted(_DEPARTMENT_SYNONYMS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", cleaned):
            return _DEPARTMENT_SYNONYMS[phrase]

    for lowered, proper in names.items():
        if lowered in cleaned or cleaned in lowered:
            return proper
    return None


def search_doctors(
    query: Optional[str] = None,
    department: Optional[str] = None,
    specialization: Optional[str] = None,
    day: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Search by free text, department, specialization and/or weekday."""
    results = list_doctors()

    if department:
        canonical = normalize_department(department)
        if canonical:
            results = [d for d in results if d["department"].lower() == canonical.lower()]
        else:
            return []

    if specialization:
        needle = specialization.lower()
        results = [d for d in results if needle in d["specialization"].lower()]

    if query:
        needle = re.sub(r"\b(dr\.?|doctor)\b", " ", query.lower()).strip()
        canonical = normalize_department(query)
        filtered = []
        for d in results:
            haystack = " ".join([
                d["name"], d["department"], d["specialization"],
                d.get("qualification", ""),
            ]).lower()
            if (needle and needle in haystack) or (
                canonical and d["department"].lower() == canonical.lower()
            ):
                filtered.append(d)
        results = filtered

    if day:
        weekday = _normalize_weekday(day)
        if weekday:
            results = [d for d in results if weekday in d["days"]]

    return results


def _normalize_weekday(value: str) -> Optional[str]:
    if not value:
        return None
    lowered = value.strip().lower()
    for name in WEEKDAYS:
        if name.lower().startswith(lowered[:3]):
            return name
    resolved = resolve_date(value)
    if resolved:
        return WEEKDAYS[resolved.weekday()]
    return None


# --------------------------------------------------------------------------
# Date handling
# --------------------------------------------------------------------------


def today_ist() -> date:
    return datetime.now(IST).date()


def resolve_date(value: str) -> Optional[date]:
    """
    Turn whatever the voice agent passes into a real date.

    Accepts ISO 'YYYY-MM-DD', relative words in English/Hindi/Hinglish
    ('tomorrow', 'kal', 'कल'), weekday names ('friday' -> next Friday),
    and 'DD-MM-YYYY' / 'DD/MM/YYYY'.
    """
    if not value:
        return None
    text = str(value).strip().lower()

    if text in _RELATIVE_DATES:
        return today_ist() + timedelta(days=_RELATIVE_DATES[text])

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    # Bare weekday name -> the next occurrence (today counts).
    for index, name in enumerate(WEEKDAYS):
        if text.startswith(name.lower()[:3]) and len(text) <= len(name) + 4:
            current = today_ist()
            delta = (index - current.weekday()) % 7
            return current + timedelta(days=delta)

    return None


def weekday_name(value: date) -> str:
    return WEEKDAYS[value.weekday()]


# --------------------------------------------------------------------------
# Slot generation
# --------------------------------------------------------------------------


def parse_timings(timings: str) -> Optional[tuple[int, int]]:
    """'10:00 AM - 2:00 PM' -> (600, 840) minutes past midnight."""
    if not timings:
        return None
    match = re.match(
        r"\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*-\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*",
        timings,
    )
    if not match:
        return None
    sh, sm, sp, eh, em, ep = match.groups()

    def to_minutes(hour: str, minute: str, meridiem: Optional[str]) -> int:
        h = int(hour)
        if meridiem:
            meridiem = meridiem.lower()
            if meridiem == "pm" and h != 12:
                h += 12
            elif meridiem == "am" and h == 12:
                h = 0
        return h * 60 + int(minute)

    start, end = to_minutes(sh, sm, sp), to_minutes(eh, em, ep)
    if end <= start:
        return None
    return start, end


def generate_slots(doctor: Dict[str, Any]) -> List[str]:
    """All slot start times for a doctor's shift, as 24-hour 'HH:MM'."""
    window = parse_timings(doctor.get("timings", ""))
    if not window:
        return []
    start, end = window
    step = int(doctor.get("slot_minutes", 20)) or 20
    slots = []
    cursor = start
    while cursor + step <= end:
        slots.append(f"{cursor // 60:02d}:{cursor % 60:02d}")
        cursor += step
    return slots


def to_12_hour(hhmm: str) -> str:
    """'14:30' -> '2:30 PM' for natural speech."""
    try:
        hour, minute = (int(part) for part in hhmm.split(":"))
    except (ValueError, AttributeError):
        return hhmm
    meridiem = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {meridiem}"


def doctor_public_view(doctor: Dict[str, Any]) -> Dict[str, Any]:
    """Speech-friendly doctor record for the voice agent and the UI."""
    return {
        "id": doctor["id"],
        "name": doctor["name"],
        "department": doctor["department"],
        "specialization": doctor["specialization"],
        "qualification": doctor.get("qualification"),
        "experience_years": doctor.get("experience_years"),
        "languages": doctor.get("languages", []),
        "available_days": doctor["days"],
        "timings": doctor["timings"],
        "consultation_fee_inr": doctor.get("consultation_fee_inr"),
        "room": doctor.get("room"),
        "walk_in_only": doctor.get("walk_in_only", False),
        "demo_record": True,
    }


__all__ = [
    "IST",
    "WEEKDAYS",
    "list_doctors",
    "list_departments",
    "get_hospital_info",
    "get_doctor",
    "get_department",
    "normalize_department",
    "search_doctors",
    "resolve_date",
    "today_ist",
    "weekday_name",
    "generate_slots",
    "parse_timings",
    "to_12_hour",
    "doctor_public_view",
    "reload_reference_data",
]
