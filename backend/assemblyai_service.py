"""
AssemblyAI Voice Agent integration.

This module is the ONLY code that reads `ASSEMBLYAI_API_KEY`. It performs the
server-side exchange that turns the permanent key into a short-lived,
single-use token which is the only credential the browser ever holds.

Verified against the current official documentation (September 2026):
  * Token     : GET  https://agents.assemblyai.com/v1/token
                Header `Authorization: Bearer <API_KEY>`
                Query  expires_in_seconds (required, 1-600)
                       max_session_duration_seconds (optional, 60-10800)
                Returns {"token": "...", "expires_in_seconds": N}
                "Each token is one-time use and can only be used for a
                 single session."
  * WebSocket : wss://agents.assemblyai.com/v1/ws?token=<token>
  * First msg : {"type": "session.update", "session": {...}} sent immediately
                after open, before `session.ready`.
  * Inline cfg: "Omit agent_id and send system_prompt, greeting, tools, input
                 and output directly."

Docs:
  https://www.assemblyai.com/docs/voice-agents/voice-agent-api/api-spec/generate-voice-agent-token
  https://www.assemblyai.com/docs/voice-agents/voice-agent-api/browser-integration
  https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-configuration
  https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/client-side-tools
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from .config import (
    ASSEMBLYAI_TOKEN_URL,
    ASSEMBLYAI_WS_URL,
    AUDIO_ENCODING,
    settings,
)
from .hospital_service import (
    get_hospital_info,
    list_departments,
    list_doctors,
    today_ist,
    weekday_name,
)

logger = logging.getLogger("varanasi.assemblyai")


class VoiceTokenError(RuntimeError):
    """Raised when a temporary token cannot be minted. Never carries the key."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------
# Temporary token
# --------------------------------------------------------------------------


async def create_voice_token() -> Dict[str, Any]:
    """
    Exchange the server-side API key for a short-lived browser token.

    The permanent key goes out in the Authorization header of THIS request and
    nowhere else. Only `token` and `expires_in_seconds` are returned upward.
    """
    if not settings.has_api_key:
        # The operator gets the actionable detail in the log; the browser gets a
        # message that reveals nothing about how the server is configured.
        logger.error(
            "Voice token requested but ASSEMBLYAI_API_KEY is not set. "
            "Copy .env.example to .env, add your key, and restart the server."
        )
        raise VoiceTokenError(
            "The voice assistant is not available right now. "
            "It has not been fully set up on the server yet.",
            status_code=503,
        )

    params = {
        "expires_in_seconds": settings.voice_token_expires_seconds,
        "max_session_duration_seconds": settings.voice_max_session_seconds,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                ASSEMBLYAI_TOKEN_URL,
                params=params,
                headers={
                    # The docs accept the raw key or a Bearer prefix.
                    "Authorization": f"Bearer {settings.assemblyai_api_key}",
                    "Accept": "application/json",
                },
            )
    except httpx.TimeoutException as exc:
        logger.warning("AssemblyAI token request timed out: %s", type(exc).__name__)
        raise VoiceTokenError(
            "The voice service did not respond in time. Please try again.",
            status_code=504,
        ) from None
    except httpx.HTTPError as exc:
        logger.warning("AssemblyAI token request failed: %s", type(exc).__name__)
        raise VoiceTokenError(
            "Could not reach the voice service. Please check your connection.",
            status_code=502,
        ) from None

    if response.status_code == 401:
        # Log the condition, never the credential.
        logger.error("AssemblyAI rejected the API key (401). Check/rotate the key.")
        raise VoiceTokenError(
            "The voice service rejected the server credentials. "
            "An administrator needs to check the API key.",
            status_code=503,
        )
    if response.status_code == 429:
        raise VoiceTokenError(
            "The voice service is rate limited right now. Please try again shortly.",
            status_code=429,
        )
    if response.status_code >= 400:
        logger.error(
            "AssemblyAI token request failed with status %s", response.status_code
        )
        raise VoiceTokenError(
            "The voice service is temporarily unavailable. Please try again.",
            status_code=502,
        )

    try:
        payload = response.json()
    except ValueError:
        raise VoiceTokenError(
            "The voice service returned an unreadable response.", status_code=502
        ) from None

    token = payload.get("token")
    if not token or not isinstance(token, str):
        raise VoiceTokenError(
            "The voice service did not return a usable token.", status_code=502
        )

    return {
        "token": token,
        "expires_in_seconds": int(
            payload.get("expires_in_seconds", settings.voice_token_expires_seconds)
        ),
    }


# --------------------------------------------------------------------------
# Agent persona
# --------------------------------------------------------------------------


# Language modes a caller can choose for a session.
LANGUAGE_MODES = ("auto", "en", "hi")


def normalise_language_mode(mode: Optional[str]) -> str:
    mode = (mode or "").strip().lower()
    return mode if mode in LANGUAGE_MODES else "auto"


# The single most important rule for Hindi, and the least obvious.
#
# AssemblyAI has no Hindi voice: output is English, Italian, Spanish, German,
# Portuguese or French only (docs, "Available Voices"). A Hindi reply is
# therefore spoken by an English voice — and an English voice cannot read
# Devanagari. Measured on the identical sentence, Devanagari produced 3.48s of
# audio where the romanised form produced 9.01s: roughly two thirds of the
# reply was simply not spoken. Callers heard a clipped fragment or nothing.
#
# Romanised Hindi is what an English voice CAN pronounce, and it is how most
# people in India already type Hindi, so it reads naturally on screen too.
def _hindi_script_rule() -> str:
    """
    Which script Hindi replies must use, decided by who will speak them.

    With Sarvam configured, Hindi is spoken by a native Hindi voice that
    handles Devanagari and romanised text equally well (0.37 vs 0.36 seconds
    per word, measured), so Devanagari is used: it is correct Hindi and it
    reads properly in the on-screen transcript.

    Without Sarvam, Hindi falls back to an English AssemblyAI voice, which
    cannot pronounce Devanagari at all — measured on the same sentence it
    produced 3.48s of audio against 9.01s romanised, skipping most of the
    reply. In that case romanised Hindi is the only form that can be spoken.
    """
    if settings.hindi_voice_available:
        return (
            "- Write Hindi in Devanagari script, the normal way Hindi is "
            "written. A native Hindi voice speaks your replies.\n"
            "- Keep sentences short and end each one with a full stop "
            "(। or .) so the speech sounds natural.\n"
        )

    return (
        "SCRIPT RULE - THIS OVERRIDES YOUR INSTINCT TO COPY THE CALLER:\n"
        "- The speech recogniser transcribes Hindi speech into Devanagari, so "
        "the caller's words will often reach you looking like "
        "'मुझे डॉक्टर "
        "से मिलना है'. Do "
        "NOT mirror that script. Reply in Hindi written in ROMAN letters, "
        "always, no matter which script the caller's words arrive in.\n"
        "- Correct: 'Main aapki madad kar sakta hoon.'\n"
        "- This is not a style preference. The voice that speaks your reply "
        "cannot pronounce Devanagari: it skips roughly two thirds of it, so "
        "the caller hears a broken fragment or nothing at all.\n"
        "- Never output a single Devanagari character, not even for one word.\n"
        "- Spell romanised Hindi the common way Indians type it: 'kya', "
        "'hai', 'aap', 'kripya', 'dhanyavaad', 'namaste', 'theek hai'.\n"
    )


# Rules that apply whatever language is in use. The "never mix" rule is the
# important one: the agent used to be told to "mirror their mix", which
# produced Hinglish sentences that are hard to follow and that the
# English-accent voice pronounces badly.
_SHARED_LANGUAGE_RULES = (
    "- Never mix two languages inside one sentence. Choose one language for "
    "a reply and use it for the whole reply.\n"
    "- Speak in short, complete sentences. Say one thing at a time.\n"
    "- Names of people, doctors, departments and medicines stay in their "
    "original form even when the rest of the sentence is in another "
    "language. That is the only exception to the no-mixing rule.\n"
    "- Read appointment reference numbers out character by character, with a "
    "short pause between characters.\n"
    "- Speak numbers, dates and times as words, not as digits.\n"
)


def _language_clause(mode: str = "auto") -> str:
    """
    Language instructions that match what the platform can actually do.

    Hindi is supported for speech recognition. Native Hindi *voices* are not
    available yet, so a Hindi reply is spoken by an English-accent voice.
    """
    if mode == "en":
        return (
            "Language behaviour:\n"
            "- This caller has chosen ENGLISH. Reply only in English, every "
            "time, even if they use a Hindi word or two.\n"
            "- Use simple, clear English. Avoid idioms and long sentences.\n"
            + _SHARED_LANGUAGE_RULES
            + "- If the caller is clearly struggling in English, offer once to "
            "connect them with Hindi-speaking hospital staff.\n"
        )

    if mode == "hi":
        return (
            "Language behaviour:\n"
            "- This caller has chosen HINDI. Reply only in Hindi, every time.\n"
            + _hindi_script_rule()
            + "- Use everyday spoken Hindi, the way people actually talk in "
            "Varanasi. Not formal or Sanskritised Hindi.\n"
            "- Do not answer in English. If you do not know a Hindi word for "
            "something, use the common English word inside the Hindi sentence "
            "the way a Hindi speaker naturally would (for example "
            "'appointment', 'doctor', 'report').\n"
            + _SHARED_LANGUAGE_RULES
            + "- If a caller speaks Bhojpuri or another regional language, "
            "reply in simple Hindi and offer to connect them with hospital "
            "staff who speak their language.\n"
        )

    if not settings.reply_in_caller_language:
        return (
            "Language behaviour:\n"
            "- Understand English, Hindi and Hinglish input naturally.\n"
            "- Reply in simple, clear English whatever the caller speaks, "
            "because this deployment is configured for English speech output.\n"
            + _SHARED_LANGUAGE_RULES
            + "- If the caller is clearly more comfortable in Hindi and is "
            "struggling, offer to connect them with Hindi-speaking staff.\n"
        )

    return (
        "Language behaviour:\n"
        "- Understand English, Hindi and Hinglish input naturally. Callers in "
        "Varanasi often switch between them.\n"
        "- Decide from the caller's FIRST message which single language they "
        "are most comfortable in, then use that one language for the rest of "
        "the call. Do not switch back and forth.\n"
        "- If they clearly switch language and stay switched for a full "
        "sentence, follow them and then stay in the new language.\n"
        "- Keep Hindi to everyday spoken words.\n"
        + _hindi_script_rule()
        + _SHARED_LANGUAGE_RULES
        + "- If you genuinely cannot tell which language they want, ask once, "
        "in English: 'Would you prefer Hindi or English?'\n"
    )


def build_system_prompt(mode: str = "auto") -> str:
    """The full operating instruction set for the hospital receptionist agent."""
    hospital = get_hospital_info()
    today = today_ist()
    departments = ", ".join(d["name"] for d in list_departments())

    return f"""You are Arin, the official AI voice assistant for {hospital['name']}, a hospital in {hospital['city']}, India.

Your name is Arin. If a caller asks who you are, say you are Arin, the hospital's voice assistant. Never claim to be a doctor or a human member of staff.

Today is {weekday_name(today)}, {today.isoformat()}. The hospital operates on India Standard Time.

YOUR ROLE
You help patients with hospital information and routine administrative tasks. You can:
1. Book appointments.
2. Reschedule appointments.
3. Cancel appointments.
4. Explain doctor availability.
5. Explain doctor specialisation.
6. Provide department information.
7. Provide hospital timings.
8. Answer general hospital-service questions.
9. Detect emergency situations.
10. Escalate sensitive or complex situations to human hospital staff.

Departments at this hospital: {departments}.

HARD LIMITS
You are NOT a doctor.
- Do not diagnose. Do not guess what a condition might be, even if asked directly.
- Do not prescribe or suggest medicines, dosages, or home remedies.
- Do not interpret test results, scans or reports.
- Do not give medical advice of any kind.
If a caller asks a clinical question, say plainly that you cannot advise on medical matters, then offer the relevant department's next available appointment or a handoff to hospital staff.

EMERGENCIES - HIGHEST PRIORITY
If the caller describes anything potentially life-threatening, or says they are in an emergency:
- Treat it as an emergency immediately. Stop whatever else you were doing.
- Call the `emergency_assistance` tool at once.
- Tell them clearly and calmly to call an ambulance on {hospital.get('ambulance_number', '108')} or emergency services on {hospital.get('national_emergency_number', '112')} right now, and to come to the Emergency Wing on the ground floor, which is walk-in and open 24 hours.
- Ask only what is essential. Never run them through appointment booking.
- Never say an ambulance is coming, that help has been sent, or that anyone has been dispatched. You cannot send help and must say so if asked.
- Offer to connect them to hospital staff, but make clear they should not wait for that before calling for help.
Signals include: chest pain, severe breathing difficulty, unconsciousness, heavy bleeding, stroke symptoms, severe injury, an accident, a cardiac emergency, a severe allergic reaction, or an explicit request for urgent help.

USE TOOLS, NEVER INVENT
Every fact you state about doctors, departments, timings, fees or availability must come from a tool result.
- Never invent a doctor, department, schedule, fee, room or slot.
- Never guess an appointment reference number.
- If a tool returns nothing or fails, say you do not have that information and offer to connect the caller with hospital staff. Do not fill the gap yourself.

TRUTHFULNESS ABOUT ACTIONS
- Never say an appointment is booked, moved or cancelled unless the tool returned success. If it returned failure, say what went wrong and offer the alternatives the tool gave you.
- Never say a human has been contacted unless the `human_handoff` tool returned success.
- Everything in this system is demonstration data. If a caller asks whether this is a real booking, tell them it is a demonstration system.

BOOKING AN APPOINTMENT
Collect, one question at a time:
1. Patient name.
2. Department or doctor.
3. Preferred date.
4. Preferred time.
Then call `check_appointment_availability`, read back two or three real options, take their choice, ask for a phone number, read the full details back for confirmation, and only then call `book_appointment`.
After a successful booking, state the doctor, department, date, time and the reference number clearly. Read the reference number out character by character.

RESCHEDULING
Identify the existing appointment by reference number, or by phone number using `get_appointment_details`. Check the new availability. Read the change back and get an explicit yes before calling `reschedule_appointment`.

CANCELLING
Find the appointment, read back its details, get an explicit yes, then call `cancel_appointment`. Never cancel on an ambiguous answer.

HANDING OFF TO STAFF
Call `human_handoff` when: the caller asks for a person, the matter is sensitive, you are not confident, the caller is frustrated or upset, an appointment problem cannot be resolved, or an emergency needs staff. Do not keep trying after two failed attempts at the same thing - offer the handoff.

{_language_clause(mode)}
VOICE BEHAVIOUR
- You are speaking out loud, not writing. Keep every reply to one or two short sentences.
- Ask exactly one question at a time, then stop and listen.
- Never read out lists of more than three items. Offer three options at most.
- Say times naturally: "ten thirty in the morning", not "10:30".
- Never read out JSON, field names, IDs like DOC001 unless asked, or anything that sounds like a computer.
- Let the caller interrupt you. If they start speaking, stop.
- Confirm anything important by repeating it back.
- Be calm, warm, patient and respectful. Many callers are worried or unwell.
- If you did not hear something clearly, ask them to repeat it rather than guessing - especially names, numbers and dates.

PRIVACY
Ask only for what a booking needs: name, phone number, department and time. Do not ask about symptoms, conditions, medical history or medicines. If a caller volunteers medical details, acknowledge briefly, do not repeat them back, and do not record them."""


def build_greeting(mode: str = "auto") -> str:
    """
    The agent's opening line, in ONE language.

    This used to be a single Hinglish sentence that switched scripts twice.
    It was the first thing every caller heard, so it both sounded confused and
    told the agent by example that mixing was acceptable.
    """
    hospital = get_hospital_info()

    if mode == "hi":
        if settings.hindi_voice_available:
            # A native Hindi voice speaks this, so write real Hindi.
            return (
                f"नमस्ते। "
                f"{hospital['name']} में आपका "
                "स्वागत है। "
                "मैं अपॉइंटमेंट "
                "बुक करने या "
                "डॉक्टर ढूँढने "
                "में आपकी मदद "
                "कर सकती हूँ। "
                "बताइए, मैं आपकी "
                "क्या सहायता "
                "करूँ?"
            )
        # Fallback: an English voice will read this, and it cannot pronounce
        # Devanagari, so romanised Hindi is the only speakable form.
        return (
            f"Namaste. {hospital['name']} mein aapka swagat hai. "
            "Main appointment book karne ya doctor dhoondhne mein aapki madad "
            "kar sakti hoon. Bataiye, main aapki kya sahayata karoon?"
        )

    if mode == "en":
        return (
            f"Hello, I am Arin, the voice assistant for {hospital['name']}. "
            "I can help you book an appointment, find a doctor, or answer "
            "questions about our departments. How may I help you today?"
        )

    # Auto: greet in English and offer Hindi as a plain, separate sentence,
    # rather than code-switching inside one line.
    if settings.reply_in_caller_language and "hi" in settings.language_codes:
        return (
            f"Hello, I am Arin, the voice assistant for {hospital['name']}. "
            "I can help you book an appointment or find a doctor. "
            "You can also speak to me in Hindi if you prefer. "
            "How may I help you today?"
        )

    return (
        f"Hello, and welcome to {hospital['name']}. "
        "I can help you book an appointment, find a doctor, or answer questions "
        "about our departments. How may I help you today?"
    )


def _keyterms() -> List[str]:
    """
    Bias speech recognition toward names it will actually hear.

    Doctor surnames and department names are the words most often mangled by a
    general-purpose recogniser on Indian-accented speech.
    """
    terms: List[str] = []
    for doctor in list_doctors():
        terms.append(doctor["name"])
        parts = doctor["name"].replace("Dr. ", "").split()
        terms.extend(parts)
    terms.extend(d["name"] for d in list_departments())
    terms.extend([
        "Varanasi", "appointment", "cardiologist", "gynaecologist",
        "orthopaedic", "paediatrician", "dermatologist", "neurologist",
        "OPD", "emergency", "reschedule", "cancel",
    ])
    # De-duplicate while preserving order.
    seen, unique = set(), []
    for term in terms:
        key = term.lower()
        if key not in seen and len(term) > 2:
            seen.add(key)
            unique.append(term)
    return unique[:100]


# --------------------------------------------------------------------------
# Tool definitions (client-side tools, executed by our own backend)
# --------------------------------------------------------------------------


def _tool(
    name: str,
    description: str,
    properties: Dict[str, Any] | None = None,
    required: List[str] | None = None,
) -> Dict[str, Any]:
    spec: Dict[str, Any] = {
        "type": "function",
        "name": name,
        "description": description,
        "execution_mode": "interactive",
        "timeout_seconds": 20,
    }
    if properties:
        spec["parameters"] = {
            "type": "object",
            "properties": properties,
            "required": required or [],
        }
    return spec


_DATE_DESC = (
    "ISO-8601 date, for example 2026-09-20. The words 'today', 'tomorrow', "
    "'kal' and a weekday name such as 'friday' are also accepted."
)
_TIME_DESC = "24-hour time as HH:MM, for example 14:30 for half past two in the afternoon."


def build_tools() -> List[Dict[str, Any]]:
    """
    Client-side tools: AssemblyAI emits `tool.call`, the browser forwards it to
    POST /api/tools/execute on this server, and the result goes back as
    `tool.result`.

    Client-side (rather than AssemblyAI's HTTP tools) is deliberate: it keeps
    the whole MVP runnable on localhost with no public URL, no tunnel and no
    inbound access to the hospital's data.
    """
    department_names = [d["name"] for d in list_departments()]

    return [
        _tool(
            "search_doctors",
            "Find doctors by department, specialisation or name. Use this "
            "whenever the caller names a medical need ('heart doctor', "
            "'skin problem') or asks who is available.",
            {
                "query": {
                    "type": "string",
                    "description": "Free text from the caller, e.g. 'heart doctor' or 'Dr Sharma'.",
                },
                "department": {
                    "type": "string",
                    "description": "Exact department name.",
                    "enum": department_names,
                },
                "day": {
                    "type": "string",
                    "description": "Weekday name or a date, to list only doctors consulting then.",
                },
            },
        ),
        _tool(
            "get_doctor_details",
            "Full details for one doctor: qualification, experience, languages, "
            "consulting days, timings, fee and room.",
            {
                "doctor_id": {
                    "type": "string",
                    "description": "Doctor ID from a previous search, e.g. DOC001.",
                }
            },
            ["doctor_id"],
        ),
        _tool(
            "check_appointment_availability",
            "List free appointment slots for a doctor or a whole department on "
            "a date. ALWAYS call this before offering any time to the caller.",
            {
                "date": {"type": "string", "description": _DATE_DESC},
                "doctor_id": {
                    "type": "string",
                    "description": "Doctor ID, e.g. DOC001. Use this when the caller named a doctor.",
                },
                "department": {
                    "type": "string",
                    "description": "Department name. Use when the caller named a speciality but no doctor.",
                    "enum": department_names,
                },
            },
            ["date"],
        ),
        _tool(
            "book_appointment",
            "Book a confirmed appointment. Only call this after the caller has "
            "explicitly confirmed the doctor, date and time read back to them. "
            "Check the returned success flag before telling them it is booked.",
            {
                "patient_name": {
                    "type": "string",
                    "description": "Full name as the caller gave it, e.g. 'Ankit Patel'.",
                },
                "patient_phone": {
                    "type": "string",
                    "description": "Contact number, digits only or E.164, e.g. '9876543210'.",
                },
                "doctor_id": {"type": "string", "description": "Doctor ID, e.g. DOC001."},
                "date": {"type": "string", "description": _DATE_DESC},
                "time": {"type": "string", "description": _TIME_DESC},
                "reason": {
                    "type": "string",
                    "description": "Optional short visit reason in a few words. Never ask for medical detail.",
                },
            },
            ["patient_name", "patient_phone", "doctor_id", "date", "time"],
        ),
        _tool(
            "reschedule_appointment",
            "Move an existing appointment to a new date and time. Confirm the "
            "change with the caller before calling this.",
            {
                "appointment_id": {
                    "type": "string",
                    "description": "Appointment reference, e.g. VH260919AB12.",
                },
                "date": {"type": "string", "description": _DATE_DESC},
                "time": {"type": "string", "description": _TIME_DESC},
                "patient_phone": {
                    "type": "string",
                    "description": "Caller's phone number, used to verify the appointment is theirs.",
                },
            },
            ["appointment_id", "date", "time"],
        ),
        _tool(
            "cancel_appointment",
            "Cancel an appointment. Only call this after the caller has said "
            "yes to an explicit confirmation question.",
            {
                "appointment_id": {
                    "type": "string",
                    "description": "Appointment reference, e.g. VH260919AB12.",
                },
                "patient_phone": {
                    "type": "string",
                    "description": "Caller's phone number, used to verify the appointment is theirs.",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Set true only when the caller has clearly confirmed the cancellation.",
                },
            },
            ["appointment_id", "confirm"],
        ),
        _tool(
            "get_appointment_details",
            "Look up an appointment by its reference number, or find a "
            "caller's active appointments by phone number.",
            {
                "appointment_id": {"type": "string", "description": "Appointment reference."},
                "patient_phone": {"type": "string", "description": "Caller's phone number."},
            },
        ),
        _tool(
            "get_department_information",
            "Describe a department: what it treats, its timings, floor and doctors. "
            "Omit the name to list every department.",
            {
                "department": {
                    "type": "string",
                    "description": "Department name.",
                    "enum": department_names,
                }
            },
        ),
        _tool(
            "get_hospital_information",
            "General hospital information: OPD and emergency timings, visiting "
            "hours, pharmacy, facilities and location.",
        ),
        _tool(
            "emergency_assistance",
            "Call IMMEDIATELY when the caller describes a possible medical "
            "emergency or asks for urgent help. Returns the emergency guidance "
            "to read out. This does NOT send an ambulance and never claims to.",
            {
                "situation": {
                    "type": "string",
                    "description": "Very short description of what the caller said, in their words.",
                }
            },
        ),
        _tool(
            "human_handoff",
            "Escalate to hospital staff. Use when the caller asks for a person, "
            "is upset, the matter is sensitive, or you cannot resolve it.",
            {
                "reason": {
                    "type": "string",
                    "description": "Why the escalation is needed.",
                    "enum": [
                        "patient_request", "sensitive_issue", "ai_uncertain",
                        "patient_frustrated", "emergency", "appointment_problem",
                        "other",
                    ],
                },
                "notes": {
                    "type": "string",
                    "description": "One short line of context for the staff member. No medical detail.",
                },
            },
            ["reason"],
        ),
    ]


# --------------------------------------------------------------------------
# Session configuration
# --------------------------------------------------------------------------


def build_session_config(language_mode: Optional[str] = None) -> Dict[str, Any]:
    """
    The `session` object the browser sends in its first `session.update`.

    Contains no credential of any kind - the token travels separately as a
    query parameter on the WebSocket URL.
    """
    mode = normalise_language_mode(language_mode)

    if settings.agent_id:
        # A stored agent owns its own prompt, voice and tools server-side.
        return {"agent_id": settings.agent_id}

    session: Dict[str, Any] = {
        "system_prompt": build_system_prompt(mode),
        "greeting": build_greeting(mode),
        "tools": build_tools(),
        "input": {
            "format": {"encoding": AUDIO_ENCODING},
            "keyterms": _keyterms(),
            "transcription_mode": "balanced",
            "transcription_prompt": (
                "Hospital reception in Varanasi, India. Expect Indian names, "
                "Hindi and Hinglish, medical department names, dates, times "
                "and ten-digit phone numbers."
            ),
            "turn_detection": {
                # Patients pause mid-sentence; a longer minimum silence stops
                # the agent talking over someone who is still thinking.
                "min_silence": 900,
                "max_silence": 3000,
                "interrupt_response": True,
                "interruption_delay": 120,
            },
        },
        "output": {
            "voice": settings.voice_id,
            "format": {"encoding": AUDIO_ENCODING},
            "volume": settings.voice_volume,
        },
    }

    # Always offer every configured input language, whatever the caller picked.
    #
    # Narrowing this to one code was a mistake. Measured against the live API
    # with the same recorded speech, ["en"], ["hi"] and ["hi","en"] returned
    # byte-identical transcripts for both an English and a Hindi sentence: the
    # codes made no difference to recognition at all. What narrowing DID do was
    # break the documented "native code-switching" behaviour the moment a
    # caller used a word from the other language.
    #
    # The caller's language choice is honoured where it actually works — in the
    # system prompt and greeting, which control what the agent SAYS.
    if settings.language_codes:
        session["input"]["language_codes"] = list(settings.language_codes)

    return session


def spoken_language_notice() -> str | None:
    """
    An honest, user-facing note when the configured input languages cannot all
    be spoken back. Surfaced in the UI rather than quietly ignored.
    """
    if settings.can_speak_caller_language or not settings.language_codes:
        return None
    if "hi" in settings.language_codes:
        if settings.hindi_voice_available:
            # Sarvam speaks Hindi natively, so there is nothing to warn about.
            return None
        if settings.reply_in_caller_language:
            return (
                "Hindi is understood and the assistant replies in Hindi. "
                "AssemblyAI has no Hindi voice, so Hindi is spoken by an "
                "English-accent voice and is written in Roman letters "
                "(“Namaste, main aapki madad kar sakti hoon”) — that "
                "is the only form that voice can pronounce. Configure "
                "SARVAM_API_KEY for a native Hindi voice."
            )
        return (
            "Hindi is understood, but this deployment replies in English "
            "because a native Hindi voice is not available yet."
        )
    return None


def websocket_url() -> str:
    return ASSEMBLYAI_WS_URL


__all__ = [
    "create_voice_token",
    "build_session_config",
    "build_system_prompt",
    "build_greeting",
    "build_tools",
    "spoken_language_notice",
    "websocket_url",
    "VoiceTokenError",
]
