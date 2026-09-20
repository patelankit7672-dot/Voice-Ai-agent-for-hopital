"""
Emergency detection and escalation, plus simulated human handoff.

SCOPE AND LIMITS - read this before reusing the code
----------------------------------------------------
Keyword matching is a *safety net*, not triage. It is deliberately biased
toward false positives: showing the emergency panel to someone who did not
need it costs nothing, missing someone who did is unacceptable.

It cannot and does not:
  * assess clinical severity,
  * replace a trained triage nurse,
  * dispatch an ambulance.

In production the primary path must be the LLM's own understanding of the
conversation (the system prompt drives that), with this layer as a second,
independent check. See README, "Production improvements".
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .hospital_service import IST, get_hospital_info

# --------------------------------------------------------------------------
# Signal vocabulary
# --------------------------------------------------------------------------
# HIGH  - describes a condition that needs immediate emergency care.
# POSSIBLE - warrants the emergency prompt but is more ambiguous.
# Hindi (Devanagari) and Hinglish (romanised) forms are included because
# callers in Varanasi routinely mix all three.

_HIGH_SIGNALS: List[str] = [
    # cardiac / respiratory
    "chest pain", "chest is hurting", "pain in my chest", "tightness in chest",
    "heart attack", "cardiac arrest", "heart is racing",
    "can't breathe", "cannot breathe", "can not breathe", "not able to breathe",
    "trouble breathing", "difficulty breathing", "shortness of breath",
    "gasping", "choking", "suffocating",
    # neurological
    "stroke", "face drooping", "slurred speech", "can't speak",
    "sudden numbness", "one side of my body", "paralysed", "paralyzed",
    "seizure", "fitting", "convulsion",
    # consciousness
    "unconscious", "not responding", "passed out", "fainted", "collapsed",
    "not waking up", "no pulse", "not breathing",
    # bleeding / trauma
    "heavy bleeding", "bleeding a lot", "bleeding heavily", "blood loss",
    "severe bleeding", "won't stop bleeding", "severe injury", "head injury",
    "deep cut", "broken bone", "compound fracture",
    "accident", "road accident", "car accident", "hit by",
    "burn", "burnt badly", "electrocuted", "drowning",
    # allergic / poisoning
    "anaphylaxis", "severe allergic reaction", "throat closing",
    "swelling of throat", "tongue swelling", "poisoned", "poisoning",
    "overdose", "snake bite", "snakebite",
    # obstetric
    "water broke", "in labour", "in labor", "heavy bleeding pregnancy",
    # explicit
    "emergency", "this is an emergency", "help me immediately",
    "need help now", "need an ambulance", "call an ambulance", "ambulance",
    "it's urgent", "very urgent", "dying",
    # Hindi - Devanagari
    "सीने में दर्द", "छाती में दर्द", "दिल का दौरा", "साँस नहीं",
    "सांस नहीं आ रही", "साँस लेने में तकलीफ", "बेहोश", "होश नहीं",
    "बहुत खून", "खून बह रहा", "एक्सीडेंट", "दुर्घटना", "तुरंत मदद",
    "आपातकाल", "एम्बुलेंस", "गंभीर", "जान बचाओ", "बचाओ",
    # Hinglish
    "seene me dard", "seene mein dard", "chhati me dard", "chest me dard",
    "saans nahi", "sans nahi", "saans lene me", "dum ghut",
    "behosh", "behoshi", "hosh nahi",
    "khoon beh raha", "bahut khoon", "khoon nikal raha",
    "accident ho gaya", "turant madad", "jaldi madad", "emergency hai",
    "bachao", "jaan bachao", "ambulance bhejo", "ambulance chahiye",
]

_POSSIBLE_SIGNALS: List[str] = [
    "severe pain", "unbearable pain", "worst pain", "excruciating",
    "very high fever", "fever 104", "fever 105", "convulsing",
    "vomiting blood", "blood in vomit", "coughing blood", "blood in stool",
    "sudden vision loss", "can't see", "cannot see",
    "chest discomfort", "chest heaviness", "pressure in chest",
    "dizzy and", "about to faint", "feeling faint",
    "baby not moving", "child not responding",
    "बहुत दर्द", "तेज़ बुखार", "तेज बुखार", "खून की उल्टी", "चक्कर आ रहा",
    "bahut dard", "tez bukhar", "khoon ki ulti", "chakkar aa raha",
    "behad dard", "saans phool", "bahut takleef",
]

# Phrasings that clearly are NOT a live emergency, checked before the signals.
_NEGATION_PATTERNS = [
    r"\b(no|not|isn'?t|wasn'?t|don'?t have|doesn'?t have|never had)\b[^.?!]{0,30}\b"
    r"(chest pain|emergency|bleeding|breathing problem|accident)\b",
    r"\b(if|in case|suppose|what if|hypothetically|for example|example of|"
    r"just asking|curious about|wondering about)\b[^.?!]{0,40}\b"
    r"(emergency|chest pain|accident|bleeding)\b",
    r"\b(emergency)\s+(ward|department|wing|number|desk|contact|timing|"
    r"room|entrance|floor|doctor|staff|services?)\b",
    r"\bwhat (is|are|'s) (the|your) emergency\b",
    r"\b(had|have had|last year|last month|yesterday|recovered from)\b"
    r"[^.?!]{0,25}\b(chest pain|accident|stroke|heart attack)\b",
]

_lock = threading.Lock()
# In-memory only, and intentionally minimal. Nothing clinical is retained.
_emergency_log: List[Dict[str, Any]] = []
_handoff_log: List[Dict[str, Any]] = []
_MAX_LOG = 200

DISCLAIMER = (
    "This AI assistant is not a doctor and cannot assess or treat a medical "
    "emergency. For anything life-threatening, seek immediate medical help."
)


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation noise, collapse whitespace."""
    lowered = text.lower()
    lowered = lowered.replace("’", "'").replace("‐", "-")
    lowered = re.sub(r"[^\w\s'ऀ-ॿ-]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _is_negated(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in _NEGATION_PATTERNS)


def _find(signals: List[str], text: str) -> List[str]:
    hits = []
    for signal in signals:
        token = signal.lower()
        # Word-boundary match for ASCII; plain containment for Devanagari,
        # where \b does not behave usefully.
        if re.search(r"[ऀ-ॿ]", token):
            if token in text:
                hits.append(signal)
        elif re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text):
            hits.append(signal)
    return hits


def detect_emergency(transcript: Optional[str]) -> Dict[str, Any]:
    """
    Classify a single utterance.

    Returns severity 'high' | 'possible' | 'none' plus the matched terms, so
    the caller can show exactly why the panel appeared.
    """
    if not transcript or not transcript.strip():
        return {"is_emergency": False, "severity": "none", "matched_terms": []}

    text = _normalise(transcript)

    if _is_negated(text):
        return {
            "is_emergency": False,
            "severity": "none",
            "matched_terms": [],
            "note": "Emergency wording detected but read as non-urgent context.",
        }

    high = _find(_HIGH_SIGNALS, text)
    if high:
        return {"is_emergency": True, "severity": "high", "matched_terms": high[:5]}

    possible = _find(_POSSIBLE_SIGNALS, text)
    if possible:
        return {
            "is_emergency": True,
            "severity": "possible",
            "matched_terms": possible[:5],
        }

    return {"is_emergency": False, "severity": "none", "matched_terms": []}


def emergency_contacts() -> Dict[str, str]:
    info = get_hospital_info()
    return {
        "national_emergency": info.get("national_emergency_number", "112"),
        "ambulance": info.get("ambulance_number", "108"),
        "hospital_emergency_desk": info.get(
            "emergency_contact_demo", "PLACEHOLDER - configure in data/hospital.json"
        ),
        "emergency_wing": "Ground Floor, Emergency Wing (walk-in, open 24/7)",
    }


def _instructions(severity: str) -> List[str]:
    contacts = emergency_contacts()
    if severity == "high":
        return [
            f"Call {contacts['ambulance']} for an ambulance, or "
            f"{contacts['national_emergency']} for emergency services, right now.",
            "If someone is with you, ask them to make the call while you stay "
            "with the patient.",
            "Go straight to the Emergency Wing on the ground floor. "
            "Emergency care is walk-in and open 24 hours - no appointment is needed.",
            "Do not wait for this assistant. It cannot send help.",
        ]
    return [
        f"If this is getting worse or you are worried, call "
        f"{contacts['ambulance']} or go to the Emergency Wing immediately.",
        "Emergency care is walk-in and open 24 hours - no appointment is needed.",
        "I can connect you with hospital staff if you would like to speak to a person.",
    ]


def raise_emergency(
    transcript: Optional[str] = None,
    session_id: Optional[str] = None,
    source: str = "voice",
    patient_name: Optional[str] = None,
    patient_phone: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Evaluate an utterance (or an explicit button press) and return the
    emergency payload the UI and the voice agent should act on.

    A button press or an explicit tool call is always treated as an emergency;
    a voice transcript is classified first.
    """
    if source in {"button", "tool"}:
        detection = {"is_emergency": True, "severity": "high", "matched_terms": []}
        if transcript:
            scanned = detect_emergency(transcript)
            if scanned["matched_terms"]:
                detection["matched_terms"] = scanned["matched_terms"]
    else:
        detection = detect_emergency(transcript)

    if not detection["is_emergency"]:
        return {
            "is_emergency": False,
            "severity": "none",
            "matched_terms": [],
            "headline": "No emergency detected.",
            "instructions": [],
            "emergency_contacts": emergency_contacts(),
            "emergency_id": None,
            "disclaimer": DISCLAIMER,
            "handoff_offered": False,
        }

    severity = detection["severity"]
    emergency_id = f"EMG-{uuid.uuid4().hex[:8].upper()}"

    entry = {
        "emergency_id": emergency_id,
        "severity": severity,
        "source": source,
        "session_id": session_id,
        "matched_terms": detection["matched_terms"],
        "patient_name": patient_name,
        "patient_phone": patient_phone,
        "created_at": datetime.now(IST).isoformat(timespec="seconds"),
        # The raw transcript is deliberately NOT stored: this MVP keeps no
        # record of what a patient said about their health.
        "transcript_stored": False,
    }
    with _lock:
        _emergency_log.append(entry)
        del _emergency_log[:-_MAX_LOG]

    headline = (
        "EMERGENCY - SEEK IMMEDIATE MEDICAL HELP"
        if severity == "high"
        else "THIS MAY BE URGENT - PLEASE GET MEDICAL HELP"
    )

    return {
        "is_emergency": True,
        "severity": severity,
        "matched_terms": detection["matched_terms"],
        "headline": headline,
        "instructions": _instructions(severity),
        "emergency_contacts": emergency_contacts(),
        "emergency_id": emergency_id,
        "disclaimer": DISCLAIMER,
        "handoff_offered": True,
        # Stated plainly so no layer of this app can imply otherwise.
        "dispatch_performed": False,
        "dispatch_notice": (
            "No ambulance has been dispatched. This demo has no emergency "
            "dispatch integration. You must call for help yourself."
        ),
    }


# --------------------------------------------------------------------------
# Human handoff (simulated)
# --------------------------------------------------------------------------

_HANDOFF_MESSAGES = {
    "patient_request": "A hospital staff member will assist you.",
    "sensitive_issue": (
        "I am connecting you with hospital staff who can help with this properly."
    ),
    "ai_uncertain": (
        "I do not have that information. A hospital staff member will assist you."
    ),
    "patient_frustrated": (
        "I am sorry about the trouble. Let me get a hospital staff member for you."
    ),
    "emergency": (
        "Alerting hospital staff. Please call emergency services now - do not wait."
    ),
    "appointment_problem": (
        "I could not resolve this appointment. Hospital staff will take over."
    ),
    "other": "A hospital staff member will assist you.",
}


def request_handoff(
    reason: str = "patient_request",
    notes: Optional[str] = None,
    session_id: Optional[str] = None,
    patient_name: Optional[str] = None,
    patient_phone: Optional[str] = None,
) -> Dict[str, Any]:
    """
    DEMO handoff. Records an escalation and returns a queue position.

    No call is transferred and no person is paged. The response says so
    explicitly so neither the UI nor the agent can imply a real transfer.
    Swap the body of this function for a Twilio / SIP / ticketing call to make
    it real - the signature is designed to stay the same.
    """
    handoff_id = f"HO-{uuid.uuid4().hex[:8].upper()}"

    with _lock:
        queue_position = len([h for h in _handoff_log if h["status"] == "queued"]) + 1
        entry = {
            "handoff_id": handoff_id,
            "reason": reason,
            "notes": (notes or "")[:500] or None,
            "session_id": session_id,
            "patient_name": patient_name,
            "patient_phone": patient_phone,
            "status": "queued",
            "created_at": datetime.now(IST).isoformat(timespec="seconds"),
            "simulated": True,
        }
        _handoff_log.append(entry)
        del _handoff_log[:-_MAX_LOG]

    return {
        "success": True,
        "message": _HANDOFF_MESSAGES.get(reason, _HANDOFF_MESSAGES["other"]),
        "handoff_id": handoff_id,
        "queue_position": queue_position,
        "estimated_wait_minutes": min(2 + (queue_position - 1) * 3, 20),
        "simulated": True,
        "notice": (
            "DEMO HANDOFF - no call was transferred and no staff member has "
            "been paged. This MVP has no telephony integration. Connect Twilio, "
            "SIP or a ticketing system to make this real."
        ),
    }


def recent_emergencies(limit: int = 20) -> List[Dict[str, Any]]:
    with _lock:
        return list(_emergency_log[-limit:])


def recent_handoffs(limit: int = 20) -> List[Dict[str, Any]]:
    with _lock:
        return list(_handoff_log[-limit:])


__all__ = [
    "detect_emergency",
    "raise_emergency",
    "request_handoff",
    "emergency_contacts",
    "recent_emergencies",
    "recent_handoffs",
    "DISCLAIMER",
]
