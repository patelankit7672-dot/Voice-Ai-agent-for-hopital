"""Pydantic request/response models and shared validation helpers."""

from __future__ import annotations

import re
from datetime import date as date_type
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# Validation primitives
# --------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[\wऀ-ॿ .'\-]{2,80}$", re.UNICODE)
_PHONE_RE = re.compile(r"^[0-9+\-\s()]{6,20}$")
_ID_RE = re.compile(r"^[A-Za-z0-9\-]{1,40}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _clean(value: str) -> str:
    """Collapse whitespace and strip control characters."""
    return re.sub(r"\s+", " ", str(value)).strip()


class StrictModel(BaseModel):
    """Reject unknown fields so malformed voice-tool payloads fail loudly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# Shared field types
# --------------------------------------------------------------------------


class PatientFields(StrictModel):
    patient_name: str = Field(..., min_length=2, max_length=80)
    patient_phone: str = Field(..., min_length=6, max_length=20)

    @field_validator("patient_name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = _clean(v)
        if not _NAME_RE.match(v):
            raise ValueError("patient_name contains unsupported characters")
        return v

    @field_validator("patient_phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        v = _clean(v)
        if not _PHONE_RE.match(v):
            raise ValueError("patient_phone must be a plausible phone number")
        digits = re.sub(r"\D", "", v)
        if not 6 <= len(digits) <= 15:
            raise ValueError("patient_phone must contain 6-15 digits")
        return v


# --------------------------------------------------------------------------
# Health / hospital
# --------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"
    hospital: str = "Varanasi Hospital"
    version: str
    voice_ready: bool
    demo_data: bool = True


class VoiceTokenResponse(BaseModel):
    """
    The ONLY voice credential that ever reaches the browser.

    `token` is a short-lived, single-use AssemblyAI token minted server-side.
    The permanent API key is never part of this model.
    """

    token: str
    expires_in_seconds: int
    websocket_url: str
    session_config: Dict[str, Any]


class VoiceConfigResponse(BaseModel):
    """Non-secret agent configuration for the UI (no token, no key)."""

    websocket_url: str
    audio_sample_rate: int
    audio_encoding: str
    language_codes: List[str]
    voice_id: str
    spoken_language_notice: Optional[str] = None
    # True when Hindi is spoken by a native Hindi voice (Sarvam) rather than
    # an English voice reading romanised text.
    hindi_voice_available: bool = False


# --------------------------------------------------------------------------
# Doctors / departments
# --------------------------------------------------------------------------


class DoctorSearchRequest(StrictModel):
    query: Optional[str] = Field(None, max_length=100)
    department: Optional[str] = Field(None, max_length=60)
    specialization: Optional[str] = Field(None, max_length=80)
    day: Optional[str] = Field(None, max_length=20)


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------


class AvailabilityRequest(StrictModel):
    date: str = Field(..., max_length=30, description="YYYY-MM-DD, or 'today'/'tomorrow'")
    doctor_id: Optional[str] = Field(None, max_length=40)
    department: Optional[str] = Field(None, max_length=60)

    @field_validator("doctor_id")
    @classmethod
    def _validate_doctor_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = _clean(v).upper()
        if not _ID_RE.match(v):
            raise ValueError("doctor_id has an invalid format")
        return v


class BookAppointmentRequest(PatientFields):
    doctor_id: str = Field(..., max_length=40)
    date: str = Field(..., max_length=30)
    time: str = Field(..., max_length=10, description="24-hour HH:MM")
    reason: Optional[str] = Field(None, max_length=200)

    @field_validator("doctor_id")
    @classmethod
    def _validate_doctor_id(cls, v: str) -> str:
        v = _clean(v).upper()
        if not _ID_RE.match(v):
            raise ValueError("doctor_id has an invalid format")
        return v

    @field_validator("time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        v = _clean(v)
        if not _TIME_RE.match(v):
            raise ValueError("time must be 24-hour HH:MM, for example 14:30")
        hh, mm = v.split(":")
        return f"{int(hh):02d}:{mm}"

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, v: Optional[str]) -> Optional[str]:
        # Deliberately kept short and optional: this MVP does not want a
        # detailed medical history in a flat JSON file.
        return _clean(v)[:200] if v else None


class RescheduleRequest(StrictModel):
    appointment_id: str = Field(..., max_length=40)
    date: str = Field(..., max_length=30)
    time: str = Field(..., max_length=10)
    patient_phone: Optional[str] = Field(None, max_length=20)

    @field_validator("appointment_id")
    @classmethod
    def _validate_appt_id(cls, v: str) -> str:
        v = _clean(v).upper()
        if not _ID_RE.match(v):
            raise ValueError("appointment_id has an invalid format")
        return v

    @field_validator("time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        v = _clean(v)
        if not _TIME_RE.match(v):
            raise ValueError("time must be 24-hour HH:MM, for example 14:30")
        hh, mm = v.split(":")
        return f"{int(hh):02d}:{mm}"


class CancelRequest(StrictModel):
    appointment_id: str = Field(..., max_length=40)
    patient_phone: Optional[str] = Field(None, max_length=20)
    confirm: bool = Field(True, description="Must be true; the agent confirms first.")

    @field_validator("appointment_id")
    @classmethod
    def _validate_appt_id(cls, v: str) -> str:
        v = _clean(v).upper()
        if not _ID_RE.match(v):
            raise ValueError("appointment_id has an invalid format")
        return v


class LookupAppointmentRequest(StrictModel):
    appointment_id: Optional[str] = Field(None, max_length=40)
    patient_phone: Optional[str] = Field(None, max_length=20)


class Appointment(BaseModel):
    appointment_id: str
    patient_name: str
    patient_phone: str
    doctor_id: str
    doctor_name: str
    department: str
    date: str
    time: str
    status: Literal["confirmed", "cancelled", "rescheduled", "completed"]
    created_at: str
    updated_at: Optional[str] = None
    reason: Optional[str] = None
    demo_record: bool = True


class OperationResult(BaseModel):
    """
    Uniform result envelope for every state-changing action.

    `success` is the single source of truth the voice agent is instructed to
    read before telling a patient that anything was actually done.
    """

    success: bool
    message: str
    code: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


# --------------------------------------------------------------------------
# Emergency / handoff
# --------------------------------------------------------------------------


class EmergencyRequest(StrictModel):
    transcript: Optional[str] = Field(None, max_length=1000)
    session_id: Optional[str] = Field(None, max_length=100)
    source: Literal["voice", "button", "tool"] = "voice"
    patient_name: Optional[str] = Field(None, max_length=80)
    patient_phone: Optional[str] = Field(None, max_length=20)


class EmergencyResponse(BaseModel):
    is_emergency: bool
    severity: Literal["none", "possible", "high"]
    matched_terms: List[str] = []
    headline: str
    instructions: List[str]
    emergency_contacts: Dict[str, str]
    emergency_id: Optional[str] = None
    disclaimer: str
    handoff_offered: bool = True

    # These two travel with every emergency response so that no consumer - the
    # UI, the voice agent, or any future integration - can imply that help is
    # on its way. They stay false until a real dispatch integration exists.
    dispatch_performed: bool = False
    dispatch_notice: str = (
        "No ambulance has been dispatched. This demo has no emergency dispatch "
        "integration. You must call for help yourself."
    )


class HandoffRequest(StrictModel):
    reason: Literal[
        "patient_request",
        "sensitive_issue",
        "ai_uncertain",
        "patient_frustrated",
        "emergency",
        "appointment_problem",
        "other",
    ] = "patient_request"
    notes: Optional[str] = Field(None, max_length=500)
    session_id: Optional[str] = Field(None, max_length=100)
    patient_name: Optional[str] = Field(None, max_length=80)
    patient_phone: Optional[str] = Field(None, max_length=20)


class HandoffResponse(BaseModel):
    success: bool
    message: str
    handoff_id: str
    queue_position: Optional[int] = None
    estimated_wait_minutes: Optional[int] = None
    simulated: bool = True
    notice: str


# --------------------------------------------------------------------------
# Voice agent tool bridge
# --------------------------------------------------------------------------


class ToolExecuteRequest(StrictModel):
    """
    One entry point the browser uses to run a tool the voice agent asked for.

    The browser never decides what a tool does - it forwards `name` and
    `arguments` here, and the server dispatches to the same services the REST
    routes use.
    """

    name: str = Field(..., max_length=60)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    call_id: Optional[str] = Field(None, max_length=100)
    session_id: Optional[str] = Field(None, max_length=100)

    @field_validator("name")
    @classmethod
    def _validate_tool_name(cls, v: str) -> str:
        v = _clean(v)
        if not re.match(r"^[a-z0-9_]{2,60}$", v):
            raise ValueError("tool name must be snake_case")
        return v


class ToolExecuteResponse(BaseModel):
    call_id: Optional[str] = None
    name: str
    ok: bool
    result: Dict[str, Any]



# ---------------------------------------------------------------------------
# Staff portal
# ---------------------------------------------------------------------------


class HindiSpeechRequest(StrictModel):
    text: str = Field(..., min_length=1, max_length=2000)


class HindiSpeechResponse(StrictModel):
    audio: str
    sample_rate: int
    seconds: float


class AdminLoginRequest(StrictModel):
    passcode: str = Field(..., max_length=200)


class AdminLoginResponse(StrictModel):
    token: str
    expires_at: str


class AdminStatusRequest(StrictModel):
    status: str = Field(..., max_length=20)
    note: Optional[str] = Field(None, max_length=500)


__all__ = [
    "HealthResponse",
    "VoiceTokenResponse",
    "VoiceConfigResponse",
    "DoctorSearchRequest",
    "AvailabilityRequest",
    "BookAppointmentRequest",
    "RescheduleRequest",
    "CancelRequest",
    "LookupAppointmentRequest",
    "Appointment",
    "OperationResult",
    "EmergencyRequest",
    "EmergencyResponse",
    "HandoffRequest",
    "HandoffResponse",
    "ToolExecuteRequest",
    "ToolExecuteResponse",
    "HindiSpeechRequest",
    "HindiSpeechResponse",
    "AdminLoginRequest",
    "AdminLoginResponse",
    "AdminStatusRequest",
]
