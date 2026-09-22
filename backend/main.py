"""
Varanasi Hospital AI Voice Assistant — FastAPI application.

Run with:
    uvicorn backend.main:app --reload

The API key never crosses this boundary. `/api/voice-token` returns a
short-lived, single-use AssemblyAI token and nothing else.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any, Callable, Deque, Dict, List

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import admin_service as admin
from . import appointment_service as appointments
from . import emergency_service as emergency
from . import hospital_service as hospital
from .http_client import close_client, get_client
from .assemblyai_service import (
    VoiceTokenError,
    build_session_config,
    create_voice_token,
    spoken_language_notice,
    websocket_url,
)
from .sarvam_service import SarvamError, synthesize_hindi
from .config import (
    AUDIO_ENCODING,
    AUDIO_SAMPLE_RATE,
    EPHEMERAL_STORAGE,
    FRONTEND_DIR,
    settings,
)
from .models import (
    AdminLoginRequest,
    HindiSpeechRequest,
    HindiSpeechResponse,
    AdminLoginResponse,
    AdminStatusRequest,
    AvailabilityRequest,
    BookAppointmentRequest,
    CancelRequest,
    DoctorSearchRequest,
    EmergencyRequest,
    EmergencyResponse,
    HandoffRequest,
    HandoffResponse,
    HealthResponse,
    LookupAppointmentRequest,
    OperationResult,
    RescheduleRequest,
    ToolExecuteRequest,
    ToolExecuteResponse,
    VoiceConfigResponse,
    VoiceTokenResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logger = logging.getLogger("varanasi.api")


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Varanasi Hospital AI Voice Assistant v%s", __version__)
    logger.info(
        "Loaded %d demo doctors across %d departments",
        len(hospital.list_doctors()),
        len(hospital.list_departments()),
    )
    if settings.has_api_key:
        logger.info("AssemblyAI key detected. Voice assistant is ready.")
    else:
        logger.warning(
            "ASSEMBLYAI_API_KEY is not set. Everything except live voice will "
            "work; /api/voice-token will return 503. Copy .env.example to .env "
            "and add your key."
        )
    notice = spoken_language_notice()
    if notice:
        logger.info("Language note: %s", notice)
    # Open the pooled client up front so the first caller does not pay for
    # the handshake on the connect path.
    get_client()
    yield
    await close_client()
    logger.info("Shutting down.")


app = FastAPI(
    title="Varanasi Hospital AI Voice Assistant",
    description=(
        "MVP backend for a hospital voice agent built on the AssemblyAI Voice "
        "Agent API. All clinical data is demonstration data."
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=False,          # no cookies are used; keeps "*" mistakes harmless
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=600,
)


# --------------------------------------------------------------------------
# Security headers + rate limiting
# --------------------------------------------------------------------------


@app.middleware("http")
async def security_headers(request: Request, call_next: Callable):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
    return response


_rate_buckets: Dict[str, Deque[float]] = defaultdict(deque)


def _rate_limit(request: Request, bucket: str, limit: int, window: int) -> None:
    """Small in-memory fixed-window limiter. Per-process only — see README."""
    client_ip = request.client.host if request.client else "unknown"
    key = f"{bucket}:{client_ip}"
    now = time.monotonic()
    hits = _rate_buckets[key]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait a moment and try again.",
        )
    hits.append(now)


@app.exception_handler(VoiceTokenError)
async def voice_token_error_handler(_: Request, exc: VoiceTokenError):
    # exc carries only operator-safe text; the API key can never reach here.
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


# --------------------------------------------------------------------------
# Health & configuration
# --------------------------------------------------------------------------


@app.get("/api/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        hospital=hospital.get_hospital_info()["name"],
        version=__version__,
        voice_ready=settings.has_api_key,
        demo_data=True,
        # True on serverless, where bookings do not survive a cold start.
        ephemeral_storage=EPHEMERAL_STORAGE,
    )


@app.get("/api/config", response_model=VoiceConfigResponse, tags=["system"])
async def voice_config() -> VoiceConfigResponse:
    """Non-secret client configuration. Contains no token and no API key."""
    return VoiceConfigResponse(
        websocket_url=websocket_url(),
        audio_sample_rate=AUDIO_SAMPLE_RATE,
        audio_encoding=AUDIO_ENCODING,
        language_codes=list(settings.language_codes),
        voice_id=settings.voice_id,
        spoken_language_notice=spoken_language_notice(),
        # English only: no second speech provider is used.
        hindi_voice_available=False,
    )


# --------------------------------------------------------------------------
# Temporary voice token — the security-critical endpoint
# --------------------------------------------------------------------------


@app.get("/api/voice-token", response_model=VoiceTokenResponse, tags=["voice"])
async def voice_token(request: Request, lang: str | None = None) -> VoiceTokenResponse:
    """
    Mint a short-lived AssemblyAI token for this browser session.

    Flow:
      1. Read ASSEMBLYAI_API_KEY from the server environment.
      2. Call AssemblyAI server-side with that key.
      3. Return ONLY the temporary token, plus the non-secret session config.

    The permanent key is never serialised into this response, logged, or
    exposed in an error. The token is single-use and expires in
    VOICE_TOKEN_EXPIRES_SECONDS.
    """
    _rate_limit(
        request,
        "voice-token",
        settings.token_rate_limit_max,
        settings.token_rate_limit_window_seconds,
    )

    result = await create_voice_token()

    return VoiceTokenResponse(
        token=result["token"],
        expires_in_seconds=result["expires_in_seconds"],
        websocket_url=websocket_url(),
        # `lang` is the caller's choice of English, Hindi or auto-detect. An
        # unrecognised value falls back to auto rather than failing the call.
        session_config=build_session_config(lang),
    )


@app.exception_handler(SarvamError)
async def sarvam_error_handler(_: Request, exc: SarvamError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.post("/api/speech/hindi", response_model=HindiSpeechResponse, tags=["voice"])
async def hindi_speech(
    payload: HindiSpeechRequest, request: Request
) -> HindiSpeechResponse:
    """
    Speak Hindi text with Sarvam's native Hindi voice.

    AssemblyAI has no Hindi voice, so for a Hindi session the browser takes the
    agent's TEXT and asks us to voice it. The Sarvam key stays here on the
    server; the browser only ever sends text and receives audio.

    Returns base64 PCM16 at 24 kHz, the same format the player already handles.
    """
    _rate_limit(request, "hindi-speech", 120, 60)
    result = await synthesize_hindi(payload.text)
    return HindiSpeechResponse(**result)


# --------------------------------------------------------------------------
# Doctors & departments
# --------------------------------------------------------------------------


@app.get("/api/doctors", tags=["hospital"])
async def get_doctors(
    department: str | None = None,
    specialization: str | None = None,
    day: str | None = None,
    q: str | None = None,
) -> Dict[str, Any]:
    payload = DoctorSearchRequest(
        query=q, department=department, specialization=specialization, day=day
    )
    matches = hospital.search_doctors(
        query=payload.query,
        department=payload.department,
        specialization=payload.specialization,
        day=payload.day,
    )
    return {
        "count": len(matches),
        "doctors": [hospital.doctor_public_view(d) for d in matches],
        "demo_data": True,
    }


@app.get("/api/doctors/{doctor_id}", tags=["hospital"])
async def get_doctor_by_id(doctor_id: str) -> Dict[str, Any]:
    doctor = hospital.get_doctor(doctor_id)
    if not doctor:
        raise HTTPException(
            status_code=404, detail=f"No doctor with ID {doctor_id} in the records."
        )
    return {"doctor": hospital.doctor_public_view(doctor), "demo_data": True}


@app.get("/api/departments", tags=["hospital"])
async def get_departments(name: str | None = None) -> Dict[str, Any]:
    if name:
        dept = hospital.get_department(name)
        if not dept:
            raise HTTPException(
                status_code=404, detail=f"No department called {name} at this hospital."
            )
        return {"department": dept, "demo_data": True}
    departments = hospital.list_departments()
    return {"count": len(departments), "departments": departments, "demo_data": True}


@app.get("/api/hospital", tags=["hospital"])
async def get_hospital() -> Dict[str, Any]:
    return {"hospital": hospital.get_hospital_info(), "demo_data": True}


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------


@app.post("/api/appointments/check", response_model=OperationResult, tags=["appointments"])
async def appointments_check(payload: AvailabilityRequest) -> OperationResult:
    return appointments.check_availability(
        date_input=payload.date,
        doctor_id=payload.doctor_id,
        department=payload.department,
    )


@app.post("/api/appointments/book", response_model=OperationResult, tags=["appointments"])
async def appointments_book(
    payload: BookAppointmentRequest, request: Request
) -> OperationResult:
    _rate_limit(request, "book", 20, 60)
    return appointments.book_appointment(
        patient_name=payload.patient_name,
        patient_phone=payload.patient_phone,
        doctor_id=payload.doctor_id,
        date_input=payload.date,
        time_hhmm=payload.time,
        reason=payload.reason,
    )


@app.post(
    "/api/appointments/reschedule", response_model=OperationResult, tags=["appointments"]
)
async def appointments_reschedule(payload: RescheduleRequest) -> OperationResult:
    return appointments.reschedule_appointment(
        appointment_id=payload.appointment_id,
        date_input=payload.date,
        time_hhmm=payload.time,
        patient_phone=payload.patient_phone,
    )


@app.post("/api/appointments/cancel", response_model=OperationResult, tags=["appointments"])
async def appointments_cancel(payload: CancelRequest) -> OperationResult:
    return appointments.cancel_appointment(
        appointment_id=payload.appointment_id,
        patient_phone=payload.patient_phone,
        confirm=payload.confirm,
    )


@app.post("/api/appointments/lookup", response_model=OperationResult, tags=["appointments"])
async def appointments_lookup(payload: LookupAppointmentRequest) -> OperationResult:
    return appointments.get_appointment(
        appointment_id=payload.appointment_id, patient_phone=payload.patient_phone
    )


@app.get("/api/appointments", tags=["appointments"])
async def appointments_list(
    request: Request, include_cancelled: bool = False
) -> Dict[str, Any]:
    """
    The appointment register.

    SECURITY: this used to be open to anyone, returning every patient's name
    and phone number to an unauthenticated caller — the whole register, in one
    request. It is staff-only now. A patient looks up their OWN appointments
    through /api/appointments/lookup, which requires their phone number.
    """
    _admin_guard(request)
    records = appointments.list_appointments(include_cancelled=include_cancelled)
    return {"count": len(records), "appointments": records, "demo_data": True}


# --------------------------------------------------------------------------
# Emergency & handoff
# --------------------------------------------------------------------------


@app.post("/api/emergency", response_model=EmergencyResponse, tags=["safety"])
async def post_emergency(payload: EmergencyRequest) -> EmergencyResponse:
    """
    Evaluate an utterance, or handle an explicit emergency button press.

    A `button` or `tool` source is always treated as an emergency. A `voice`
    transcript is screened by the keyword safety net first.

    This endpoint does NOT dispatch an ambulance and never claims to.
    """
    result = emergency.raise_emergency(
        transcript=payload.transcript,
        session_id=payload.session_id,
        source=payload.source,
        patient_name=payload.patient_name,
        patient_phone=payload.patient_phone,
    )
    return EmergencyResponse(**{
        k: v for k, v in result.items()
        if k in EmergencyResponse.model_fields
    })


@app.post("/api/handoff", response_model=HandoffResponse, tags=["safety"])
async def post_handoff(payload: HandoffRequest) -> HandoffResponse:
    """DEMO handoff — records an escalation. No call is transferred."""
    result = emergency.request_handoff(
        reason=payload.reason,
        notes=payload.notes,
        session_id=payload.session_id,
        patient_name=payload.patient_name,
        patient_phone=payload.patient_phone,
    )
    return HandoffResponse(**result)


# --------------------------------------------------------------------------
# Voice agent tool bridge
# --------------------------------------------------------------------------


def _tool_search_doctors(args: Dict[str, Any]) -> Dict[str, Any]:
    matches = hospital.search_doctors(
        query=args.get("query"),
        department=args.get("department"),
        specialization=args.get("specialization"),
        day=args.get("day"),
    )
    if not matches:
        return {
            "found": False,
            "count": 0,
            "doctors": [],
            "message": (
                "No matching doctor is listed in the hospital records. "
                "Offer to connect the caller with hospital staff."
            ),
        }
    return {
        "found": True,
        "count": len(matches),
        # Cap the list: the agent must not read out a long roster aloud.
        "doctors": [hospital.doctor_public_view(d) for d in matches[:5]],
        "message": f"{len(matches)} doctor(s) found. Offer at most three by name.",
    }


def _tool_get_doctor_details(args: Dict[str, Any]) -> Dict[str, Any]:
    doctor = hospital.get_doctor(str(args.get("doctor_id", "")))
    if not doctor:
        return {
            "found": False,
            "message": "No doctor with that ID is in the records. Do not invent one.",
        }
    return {"found": True, "doctor": hospital.doctor_public_view(doctor)}


def _tool_check_availability(args: Dict[str, Any]) -> Dict[str, Any]:
    result = appointments.check_availability(
        date_input=str(args.get("date", "")),
        doctor_id=args.get("doctor_id"),
        department=args.get("department"),
    )
    return result.model_dump()


def _tool_book(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        payload = BookAppointmentRequest(
            patient_name=str(args.get("patient_name", "")),
            patient_phone=str(args.get("patient_phone", "")),
            doctor_id=str(args.get("doctor_id", "")),
            date=str(args.get("date", "")),
            time=str(args.get("time", "")),
            reason=args.get("reason"),
        )
    except Exception as exc:
        return {
            "success": False,
            "code": "invalid_input",
            "message": (
                "Some details were missing or unclear, so nothing was booked. "
                "Ask the caller to repeat them."
            ),
            "detail": str(exc)[:200],
        }
    return appointments.book_appointment(
        patient_name=payload.patient_name,
        patient_phone=payload.patient_phone,
        doctor_id=payload.doctor_id,
        date_input=payload.date,
        time_hhmm=payload.time,
        reason=payload.reason,
    ).model_dump()


def _tool_reschedule(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        payload = RescheduleRequest(
            appointment_id=str(args.get("appointment_id", "")),
            date=str(args.get("date", "")),
            time=str(args.get("time", "")),
            patient_phone=args.get("patient_phone"),
        )
    except Exception as exc:
        return {
            "success": False,
            "code": "invalid_input",
            "message": "Those details were unclear, so nothing was changed.",
            "detail": str(exc)[:200],
        }
    return appointments.reschedule_appointment(
        appointment_id=payload.appointment_id,
        date_input=payload.date,
        time_hhmm=payload.time,
        patient_phone=payload.patient_phone,
    ).model_dump()


def _tool_cancel(args: Dict[str, Any]) -> Dict[str, Any]:
    return appointments.cancel_appointment(
        appointment_id=str(args.get("appointment_id", "")).upper(),
        patient_phone=args.get("patient_phone"),
        confirm=bool(args.get("confirm", False)),
    ).model_dump()


def _tool_get_appointment(args: Dict[str, Any]) -> Dict[str, Any]:
    return appointments.get_appointment(
        appointment_id=args.get("appointment_id"),
        patient_phone=args.get("patient_phone"),
    ).model_dump()


def _tool_department_info(args: Dict[str, Any]) -> Dict[str, Any]:
    name = args.get("department")
    if not name:
        return {
            "found": True,
            "departments": [d["name"] for d in hospital.list_departments()],
            "message": "List at most three departments aloud unless asked for all.",
        }
    dept = hospital.get_department(str(name))
    if not dept:
        return {
            "found": False,
            "message": (
                f"There is no department called {name} at this hospital. "
                "Do not invent one."
            ),
            "available_departments": [d["name"] for d in hospital.list_departments()],
        }
    return {"found": True, "department": dept}


def _tool_hospital_info(_: Dict[str, Any]) -> Dict[str, Any]:
    info = hospital.get_hospital_info()
    return {
        "found": True,
        "hospital": {
            "name": info["name"],
            "city": info["city"],
            "outpatient_hours": info["outpatient"],
            "emergency": info["emergency"],
            "visiting_hours": info["visiting_hours"],
            "pharmacy_hours": info["pharmacy_hours"],
            "diagnostics_hours": info["diagnostics_hours"],
            "facilities": info["facilities"],
            "languages_supported": info["languages_supported"],
            "ambulance_number": info["ambulance_number"],
            "national_emergency_number": info["national_emergency_number"],
        },
        "note": (
            "Address and phone numbers are demo placeholders. If asked for "
            "them, say they are not configured and offer a staff handoff."
        ),
    }


def _tool_emergency(args: Dict[str, Any]) -> Dict[str, Any]:
    result = emergency.raise_emergency(
        transcript=args.get("situation"), source="tool"
    )
    result["agent_instruction"] = (
        "Read the instructions out calmly, one at a time. Tell the caller to "
        "call for an ambulance now. Do NOT say help is on the way — nothing "
        "has been dispatched."
    )
    return result


def _tool_handoff(args: Dict[str, Any]) -> Dict[str, Any]:
    result = emergency.request_handoff(
        reason=str(args.get("reason", "patient_request")),
        notes=args.get("notes"),
    )
    result["agent_instruction"] = (
        "Tell the caller a staff member will assist them. Do not claim the "
        "call has been transferred."
    )
    return result


_TOOL_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "search_doctors": _tool_search_doctors,
    "get_doctor_details": _tool_get_doctor_details,
    "check_appointment_availability": _tool_check_availability,
    "book_appointment": _tool_book,
    "reschedule_appointment": _tool_reschedule,
    "cancel_appointment": _tool_cancel,
    "get_appointment_details": _tool_get_appointment,
    "get_department_information": _tool_department_info,
    "get_hospital_information": _tool_hospital_info,
    "emergency_assistance": _tool_emergency,
    "human_handoff": _tool_handoff,
}


@app.post("/api/tools/execute", response_model=ToolExecuteResponse, tags=["voice"])
async def execute_tool(payload: ToolExecuteRequest, request: Request) -> ToolExecuteResponse:
    """
    Run a tool the voice agent asked for.

    The browser relays AssemblyAI's `tool.call` here and sends the JSON back as
    `tool.result`. The browser is only a pipe — it never decides what a tool
    does, and all hospital data stays server-side.
    """
    _rate_limit(request, "tools", 120, 60)

    handler = _TOOL_REGISTRY.get(payload.name)
    if handler is None:
        logger.warning("Unknown tool requested: %s", payload.name)
        return ToolExecuteResponse(
            call_id=payload.call_id,
            name=payload.name,
            ok=False,
            result={
                "success": False,
                "message": (
                    "That action is not available. Tell the caller you cannot "
                    "do it and offer to connect them with hospital staff."
                ),
            },
        )

    try:
        result = handler(payload.arguments or {})
        ok = bool(result.get("success", result.get("found", True)))
    except Exception:
        logger.exception("Tool %s failed", payload.name)
        return ToolExecuteResponse(
            call_id=payload.call_id,
            name=payload.name,
            ok=False,
            result={
                "success": False,
                "message": (
                    "Something went wrong on the hospital system. Do not claim "
                    "anything was done. Offer to connect the caller with staff."
                ),
            },
        )

    logger.info("tool=%s ok=%s", payload.name, ok)
    return ToolExecuteResponse(
        call_id=payload.call_id, name=payload.name, ok=ok, result=result
    )


@app.get("/api/tools", tags=["voice"])
async def list_tools() -> Dict[str, List[str]]:
    """Names of the tools the agent can call. Useful for debugging the demo."""
    return {"tools": sorted(_TOOL_REGISTRY)}


# --------------------------------------------------------------------------
# Staff portal API
#
# Guarded by admin_service.require_session, which is DEMO-GRADE: one shared
# passcode, in-memory bearer tokens, no per-staff accounts and no audit trail.
# See the module docstring in admin_service.py before putting this anywhere
# near real patient data.
# --------------------------------------------------------------------------


ADMIN_COOKIE = "vh_admin_session"


def _admin_token(request: Request) -> str | None:
    """
    The staff session token, from the cookie the browser holds or from a
    header for API clients.

    The browser path is the cookie, and it is deliberately httpOnly: page
    scripts cannot read it, so the portal never holds a credential that an
    injected script could steal. The header path exists for curl and tests.
    """
    cookie = request.cookies.get(ADMIN_COOKIE)
    if cookie:
        return cookie
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.headers.get("X-Admin-Token") or None


def _admin_guard(request: Request) -> None:
    admin.require_session(_admin_token(request))


@app.exception_handler(admin.AdminError)
async def admin_error_handler(_: Request, exc: admin.AdminError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.post("/api/admin/login", response_model=AdminLoginResponse, tags=["admin"])
async def admin_login(
    payload: AdminLoginRequest, request: Request, response: Response
) -> AdminLoginResponse:
    # Rate limited: a short passcode is guessable at speed otherwise.
    _rate_limit(request, "admin-login", 8, 300)
    session = admin.login(payload.passcode)
    response.set_cookie(
        ADMIN_COOKIE,
        session["token"],
        max_age=settings.admin_session_hours * 3600,
        httponly=True,          # page scripts can never read it
        samesite="strict",      # not sent on cross-site requests, so no CSRF
        secure=settings.is_production,
        path="/",
    )
    return AdminLoginResponse(**session)


@app.post("/api/admin/logout", tags=["admin"])
async def admin_logout(request: Request, response: Response) -> Dict[str, Any]:
    admin.logout(_admin_token(request))
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return {"success": True}


@app.get("/api/admin/session", tags=["admin"])
async def admin_session(request: Request) -> Dict[str, Any]:
    """Cheap check the portal uses on load to decide whether to show sign-in."""
    _admin_guard(request)
    return {"valid": True}


@app.get("/api/admin/dashboard", tags=["admin"])
async def admin_dashboard(request: Request) -> Dict[str, Any]:
    _admin_guard(request)
    return admin.dashboard()


@app.get("/api/admin/appointments", tags=["admin"])
async def admin_appointments(
    request: Request,
    scope: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    department: str | None = None,
    doctor_id: str | None = None,
    q: str | None = None,
) -> Dict[str, Any]:
    _admin_guard(request)
    rows = admin.list_for_staff(
        scope=scope,
        date_from=date_from,
        date_to=date_to,
        status=status,
        department=department,
        doctor_id=doctor_id,
        query=q,
    )
    return {"count": len(rows), "appointments": rows}


@app.patch("/api/admin/appointments/{appointment_id}", tags=["admin"])
async def admin_set_status(
    appointment_id: str, payload: AdminStatusRequest, request: Request
) -> Dict[str, Any]:
    _admin_guard(request)
    updated = admin.set_status(appointment_id, payload.status, payload.note)
    return {"success": True, "appointment": updated}


@app.get("/api/admin/doctors", tags=["admin"])
async def admin_doctors(request: Request) -> Dict[str, Any]:
    _admin_guard(request)
    board = admin.doctor_board()
    return {"count": len(board), "doctors": board}


@app.get("/api/admin/escalations", tags=["admin"])
async def admin_escalations(request: Request) -> Dict[str, Any]:
    _admin_guard(request)
    return admin.escalations()


@app.get("/api/admin/export.csv", tags=["admin"])
async def admin_export(
    request: Request,
    scope: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    department: str | None = None,
    doctor_id: str | None = None,
    q: str | None = None,
) -> Response:
    _admin_guard(request)
    rows = admin.list_for_staff(
        scope=scope,
        date_from=date_from,
        date_to=date_to,
        status=status,
        department=department,
        doctor_id=doctor_id,
        query=q,
    )
    stamp = hospital.today_ist().isoformat()
    return Response(
        content=admin.export_csv(rows),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="appointments-{stamp}.csv"'
        },
    )


# --------------------------------------------------------------------------
# Frontend (single-server deployment)
# --------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount(
        "/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static"
    )

    # These three files are edited constantly during development, and a stale
    # copy is indistinguishable from a broken change. "no-cache" only asks the
    # browser to revalidate, and some embedded/preview browsers ignore it, so
    # forbid storing them altogether. They are small and served locally.
    NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

    def _asset_version(name: str) -> str:
        """A short fingerprint that changes whenever the file changes."""
        try:
            stat = (FRONTEND_DIR / name).stat()
            return f"{int(stat.st_mtime)}-{stat.st_size}"
        except OSError:
            return "0"

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        # Stamp the asset URLs with the files' fingerprints. Cache headers
        # alone are not enough: some browsers keep serving a stale stylesheet
        # from an in-memory cache without revalidating, which makes an edit
        # look like it did nothing. A changed file gets a changed URL, so
        # there is nothing stale left to serve.
        html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace('href="/style.css"', f'href="/style.css?v={_asset_version("style.css")}"')
        html = html.replace('src="/app.js"', f'src="/app.js?v={_asset_version("app.js")}"')
        return HTMLResponse(html, headers=NO_CACHE)

    @app.get("/style.css", include_in_schema=False)
    async def style() -> FileResponse:
        return FileResponse(
            str(FRONTEND_DIR / "style.css"),
            media_type="text/css",
            headers=NO_CACHE,
        )

    @app.get("/app.js", include_in_schema=False)
    async def app_js() -> FileResponse:
        return FileResponse(
            str(FRONTEND_DIR / "app.js"),
            media_type="application/javascript",
            headers=NO_CACHE,
        )

    @app.get("/admin", include_in_schema=False)
    async def admin_page() -> HTMLResponse:
        html = (FRONTEND_DIR / "admin.html").read_text(encoding="utf-8")
        html = html.replace('href="/style.css"', f'href="/style.css?v={_asset_version("style.css")}"')
        html = html.replace('src="/admin.js"', f'src="/admin.js?v={_asset_version("admin.js")}"')
        return HTMLResponse(html, headers=NO_CACHE)

    @app.get("/admin.js", include_in_schema=False)
    async def admin_js() -> FileResponse:
        return FileResponse(
            str(FRONTEND_DIR / "admin.js"),
            media_type="application/javascript",
            headers=NO_CACHE,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app", host=settings.host, port=settings.port, reload=True
    )
