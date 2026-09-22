"""
End-to-end tests for the Varanasi Hospital AI Voice Assistant backend.

Runs entirely in-process against the FastAPI app - no server, no network and
no AssemblyAI key required. Appointment writes are redirected to a temporary
file so the demo data store is never touched.

    pip install pytest
    pytest tests/ -v
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import appointment_service as appointments  # noqa: E402
from backend import emergency_service as emergency        # noqa: E402
from backend import hospital_service as hospital          # noqa: E402
from backend.main import app                              # noqa: E402


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point appointment storage at a throwaway file for every test."""
    store = tmp_path / "appointments.json"
    store.write_text(json.dumps({"appointments": []}), encoding="utf-8")
    monkeypatch.setattr(appointments, "APPOINTMENTS_PATH", store)
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _next_consulting_day(doctor_id: str) -> str:
    """First upcoming date (from tomorrow) on which this doctor consults."""
    doctor = hospital.get_doctor(doctor_id)
    day = hospital.today_ist() + timedelta(days=1)
    for _ in range(14):
        if hospital.weekday_name(day) in doctor["days"]:
            return day.isoformat()
        day += timedelta(days=1)
    raise AssertionError(f"No consulting day found for {doctor_id}")


# ---------------------------------------------------------------- system


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["hospital"] == "Varanasi Hospital"
    assert body["demo_data"] is True


def test_config_exposes_no_secret(client):
    body = client.get("/api/config").json()
    assert "token" not in body
    assert not any("key" in k.lower() for k in body)


@pytest.fixture
def no_api_key(monkeypatch):
    """
    Force the "no key configured" state.

    This used to rely on the developer having an empty .env. Once a real key is
    present the test silently inverted: it sent a live request to AssemblyAI and
    got a 200, testing nothing and spending a token on every run. Settings is a
    frozen dataclass, so the property is patched on the class.
    """
    from backend.config import Settings

    monkeypatch.setattr(Settings, "has_api_key", property(lambda self: False))
    yield


def test_voice_token_without_key_is_503(client, no_api_key):
    """With no key configured the endpoint refuses cleanly and returns no token."""
    response = client.get("/api/voice-token")
    assert response.status_code == 503
    assert "token" not in response.json()


def test_errors_do_not_disclose_server_configuration(client):
    """
    A client-facing error must not name server internals. The actionable
    detail belongs in the server log, not in the browser.
    """
    body = client.get("/api/voice-token").text
    for internal in ("ASSEMBLYAI_API_KEY", ".env", "environment variable", "Traceback"):
        assert internal not in body, f"{internal!r} disclosed to the client"


# ---------------------------------------------------------------- hospital


def test_list_doctors(client):
    body = client.get("/api/doctors").json()
    assert body["count"] == 12
    assert all("id" in d and "name" in d for d in body["doctors"])


def test_filter_doctors_by_department(client):
    body = client.get("/api/doctors?department=Cardiology").json()
    assert body["count"] >= 1
    assert all(d["department"] == "Cardiology" for d in body["doctors"])


def test_doctor_detail_and_404(client):
    assert client.get("/api/doctors/DOC001").json()["doctor"]["name"] == "Dr. Rajesh Sharma"
    assert client.get("/api/doctors/NOPE999").status_code == 404


def test_departments_and_hospital(client):
    assert client.get("/api/departments").json()["count"] == 11
    assert client.get("/api/hospital").json()["hospital"]["city"] == "Varanasi"


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("heart doctor", "Cardiology"),
        ("haddi ka doctor", "Orthopedics"),
        ("skin problem", "Dermatology"),
        ("my child is sick", "Pediatrics"),
        ("blood test", "Pathology"),
    ],
)
def test_department_synonyms(phrase, expected):
    assert hospital.normalize_department(phrase) == expected


@pytest.mark.parametrize("word", ["today", "tomorrow", "kal", "कल"])
def test_relative_dates_resolve(word):
    assert hospital.resolve_date(word) is not None


# ---------------------------------------------------------------- appointments


def test_full_appointment_lifecycle(client):
    date = _next_consulting_day("DOC001")

    check = client.post(
        "/api/appointments/check", json={"date": date, "doctor_id": "DOC001"}
    ).json()
    assert check["success"] is True
    slots = check["data"]["doctors"][0]["available_slots"]
    assert slots, "expected free slots on a consulting day"

    booked = client.post("/api/appointments/book", json={
        "patient_name": "Ankit Patel",
        "patient_phone": "9876543210",
        "doctor_id": "DOC001",
        "date": date,
        "time": slots[0],
    }).json()
    assert booked["success"] is True
    reference = booked["data"]["appointment"]["appointment_id"]

    # Double booking the same slot must fail.
    clash = client.post("/api/appointments/book", json={
        "patient_name": "Someone Else",
        "patient_phone": "9000000001",
        "doctor_id": "DOC001",
        "date": date,
        "time": slots[0],
    }).json()
    assert clash["success"] is False
    assert clash["code"] == "slot_taken"

    moved = client.post("/api/appointments/reschedule", json={
        "appointment_id": reference,
        "date": date,
        "time": slots[1],
        "patient_phone": "9876543210",
    }).json()
    assert moved["success"] is True
    assert moved["data"]["appointment"]["time"] == slots[1]

    cancelled = client.post("/api/appointments/cancel", json={
        "appointment_id": reference,
        "patient_phone": "9876543210",
        "confirm": True,
    }).json()
    assert cancelled["success"] is True

    again = client.post("/api/appointments/cancel", json={
        "appointment_id": reference, "confirm": True
    }).json()
    assert again["success"] is False
    assert again["code"] == "already_cancelled"


def test_cancel_requires_confirmation(client):
    result = client.post("/api/appointments/cancel", json={
        "appointment_id": "VH000000XXXX", "confirm": False
    }).json()
    assert result["success"] is False
    assert result["code"] == "confirmation_required"


def test_wrong_phone_cannot_cancel(client):
    date = _next_consulting_day("DOC003")
    slots = client.post(
        "/api/appointments/check", json={"date": date, "doctor_id": "DOC003"}
    ).json()["data"]["doctors"][0]["available_slots"]

    reference = client.post("/api/appointments/book", json={
        "patient_name": "Test Patient",
        "patient_phone": "9111111111",
        "doctor_id": "DOC003",
        "date": date,
        "time": slots[0],
    }).json()["data"]["appointment"]["appointment_id"]

    result = client.post("/api/appointments/cancel", json={
        "appointment_id": reference, "patient_phone": "9222222222", "confirm": True
    }).json()
    assert result["success"] is False
    assert result["code"] == "verification_failed"


def test_booking_in_the_past_is_rejected(client):
    past = (hospital.today_ist() - timedelta(days=1)).isoformat()
    result = client.post("/api/appointments/book", json={
        "patient_name": "Past Patient",
        "patient_phone": "9876543210",
        "doctor_id": "DOC001",
        "date": past,
        "time": "10:00",
    }).json()
    assert result["success"] is False
    assert result["code"] == "invalid_date"


def test_emergency_department_cannot_be_booked(client):
    result = client.post("/api/appointments/book", json={
        "patient_name": "Walk In",
        "patient_phone": "9876543210",
        "doctor_id": "DOC009",
        "date": _next_consulting_day("DOC009"),
        "time": "10:00",
    }).json()
    assert result["success"] is False
    assert result["code"] == "walk_in_only"


def test_invalid_payload_is_422(client):
    response = client.post("/api/appointments/book", json={
        "patient_name": "A",              # too short
        "patient_phone": "abc",           # not a phone
        "doctor_id": "DOC001",
        "date": "2026-01-01",
        "time": "99:99",
    })
    assert response.status_code == 422


# ---------------------------------------------------------------- emergency


@pytest.mark.parametrize("utterance", [
    "I am having severe chest pain and difficulty breathing",
    "मुझे सीने में दर्द हो रहा है",
    "seene me dard ho raha hai aur saans nahi aa rahi",
    "my father is unconscious, please help me immediately",
    "there has been a road accident",
])
def test_emergency_detected(utterance):
    assert emergency.detect_emergency(utterance)["is_emergency"] is True


@pytest.mark.parametrize("utterance", [
    "I want to book an appointment with a cardiologist",
    "what are the emergency ward timings",
    "I do not have chest pain, just a routine checkup",
    "मुझे कल डॉक्टर से मिलना है",
])
def test_no_false_emergency(utterance):
    assert emergency.detect_emergency(utterance)["is_emergency"] is False


def test_emergency_endpoint_never_claims_dispatch(client):
    body = client.post("/api/emergency", json={
        "transcript": "I cannot breathe", "source": "voice"
    }).json()
    assert body["is_emergency"] is True
    assert body["severity"] == "high"
    assert body["emergency_id"].startswith("EMG-")
    assert "108" in json.dumps(body)

    # The response must actively state that nothing was dispatched.
    assert body["dispatch_performed"] is False
    assert "no ambulance" in body["dispatch_notice"].lower()

    raw = emergency.raise_emergency("I cannot breathe", source="voice")
    assert raw["dispatch_performed"] is False

    # Nothing anywhere may imply help is coming.
    blob = json.dumps(body).lower()
    for claim in ("ambulance is on the way", "help is on the way", "dispatched an", "we have sent"):
        assert claim not in blob


def test_emergency_button_always_escalates(client):
    body = client.post("/api/emergency", json={"source": "button"}).json()
    assert body["is_emergency"] is True
    assert body["instructions"]


def test_emergency_stores_no_transcript():
    emergency.raise_emergency("I have crushing chest pain", source="voice")
    entry = emergency.recent_emergencies(1)[0]
    assert entry["transcript_stored"] is False
    assert "transcript" not in entry


# ---------------------------------------------------------------- handoff


def test_handoff_is_labelled_demo(client):
    body = client.post("/api/handoff", json={"reason": "patient_request"}).json()
    assert body["success"] is True
    assert body["simulated"] is True
    assert "DEMO" in body["notice"].upper()
    assert body["handoff_id"].startswith("HO-")


# ---------------------------------------------------------------- tools


def test_every_declared_tool_is_implemented(client):
    from backend.assemblyai_service import build_tools

    declared = {tool["name"] for tool in build_tools()}
    implemented = set(client.get("/api/tools").json()["tools"])
    assert declared == implemented, declared.symmetric_difference(implemented)


def test_tool_bridge_books_an_appointment(client):
    date = _next_consulting_day("DOC004")

    availability = client.post("/api/tools/execute", json={
        "name": "check_appointment_availability",
        "arguments": {"date": date, "doctor_id": "DOC004"},
        "call_id": "call-1",
    }).json()
    assert availability["ok"] is True
    slot = availability["result"]["data"]["doctors"][0]["available_slots"][0]

    booking = client.post("/api/tools/execute", json={
        "name": "book_appointment",
        "arguments": {
            "patient_name": "Voice Caller",
            "patient_phone": "9876500000",
            "doctor_id": "DOC004",
            "date": date,
            "time": slot,
        },
        "call_id": "call-2",
    }).json()
    assert booking["ok"] is True
    assert booking["result"]["success"] is True


def test_unknown_tool_fails_safely(client):
    body = client.post("/api/tools/execute", json={
        "name": "delete_all_records", "arguments": {}
    }).json()
    assert body["ok"] is False
    assert body["result"]["success"] is False


def test_tool_never_invents_a_doctor(client):
    body = client.post("/api/tools/execute", json={
        "name": "get_doctor_details", "arguments": {"doctor_id": "DOC999"}
    }).json()
    assert body["result"]["found"] is False
    assert "invent" in body["result"]["message"].lower()


def test_book_tool_with_garbage_does_not_book(client):
    body = client.post("/api/tools/execute", json={
        "name": "book_appointment",
        "arguments": {"patient_name": "", "doctor_id": "DOC001"},
    }).json()
    assert body["result"]["success"] is False
    assert body["result"]["code"] == "invalid_input"


# ---------------------------------------------------------------- security


SENTINEL_KEY = "sk_test_SENTINEL_DO_NOT_LEAK_0123456789"


def test_no_endpoint_leaks_the_api_key(client, monkeypatch):
    """
    Configure a sentinel key, then sweep every GET route for it.

    Settings is a frozen dataclass, so a replacement instance is swapped into
    each module that imported the singleton by name.
    """
    import dataclasses

    from backend import assemblyai_service, config, main

    fake = dataclasses.replace(config.settings, assemblyai_api_key=SENTINEL_KEY)
    for module in (config, assemblyai_service, main):
        monkeypatch.setattr(module, "settings", fake)

    assert fake.has_api_key is True, "sentinel should read as a configured key"

    for path in [
        "/api/health", "/api/config", "/api/doctors", "/api/doctors/DOC001",
        "/api/departments", "/api/hospital", "/api/appointments", "/api/tools",
    ]:
        response = client.get(path)
        assert SENTINEL_KEY not in response.text, f"key leaked from {path}"
        assert SENTINEL_KEY not in str(response.headers), f"key leaked in headers of {path}"


def test_voice_token_response_model_cannot_carry_the_key():
    """The response schema has no field a key could travel in."""
    from backend.models import VoiceTokenResponse

    assert set(VoiceTokenResponse.model_fields) == {
        "token", "expires_in_seconds", "websocket_url", "session_config",
    }


def test_settings_safe_dict_omits_the_key():
    import dataclasses

    from backend import config

    fake = dataclasses.replace(config.settings, assemblyai_api_key=SENTINEL_KEY)
    assert SENTINEL_KEY not in json.dumps(fake.safe_dict())
    assert "assemblyai_api_key" not in fake.safe_dict()
    # repr is where secrets usually escape into logs and tracebacks.
    assert SENTINEL_KEY not in repr(fake)


def test_frontend_never_names_or_holds_a_credential():
    """
    Guard rail: the browser bundle must contain no key, no env-var name and no
    hand-rolled Authorization header. It may only call our own token endpoint.
    """
    import re

    def strip_comments(source: str) -> str:
        """Scan executable code only — comments legitimately discuss these terms."""
        source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)   # /* ... */
        source = re.sub(r"<!--.*?-->", " ", source, flags=re.DOTALL)  # <!-- ... -->
        source = re.sub(r"(?m)^\s*//.*$", " ", source)                # // line
        return source

    frontend = Path(__file__).resolve().parent.parent / "frontend"

    # Applies to every frontend file.
    forbidden_everywhere = [
        "ASSEMBLYAI_API_KEY",   # env var name
        "api_key",
        "apikey",
        "process.env",
    ]

    # Applies only to the voice client. It talks to AssemblyAI, so it must
    # never hand-roll an Authorization header: the sole credential it may hold
    # is the short-lived token from our own /api/voice-token.
    #
    # The staff portal is a different surface. It sends a Bearer token, but to
    # OUR origin and for a demo staff session, never to AssemblyAI — so the
    # blanket ban would be wrong there. It gets the same-origin check below.
    forbidden_in_voice_client = ["Bearer ", "authorization"]

    voice_files = ("index.html", "app.js", "style.css")
    admin_files = ("admin.html", "admin.js")

    # Browser storage is not banned outright — it holds UI preferences such as
    # the chosen theme and microphone. What must never be stored is a
    # credential, so every key written to localStorage/sessionStorage has to
    # appear here. Adding to this list is a deliberate act: a voice token, an
    # API key or a staff session must never be on it.
    allowed_storage_keys = {
        "varanasi.theme",
        "varanasi.micDeviceId",
        "varanasi.language",
        "varanasi.audioSetup",
        "varanasi.audioOutput",
        "THEME_KEY",
        "MIC_PREF_KEY",
        "LANG_PREF_KEY",
        "AUDIO_SETUP_KEY",
        "OUTPUT_PREF_KEY",
    }
    storage_write = re.compile(
        r"""(?:local|session)Storage\.setItem\(\s*(?:"""
        r"""(?P<literal>['"][^'"]*['"])|(?P<ident>[A-Za-z_$][\w$]*))"""
    )

    for name in voice_files + admin_files:
        raw = (frontend / name).read_text(encoding="utf-8")
        code = strip_comments(raw)

        needles = list(forbidden_everywhere)
        if name in voice_files:
            needles += forbidden_in_voice_client
        for needle in needles:
            assert needle.lower() not in code.lower(), f"{needle!r} found in {name}"

        # Wherever the staff portal does send a credential, it must be to our
        # own origin: a relative path, never an absolute URL.
        if name in admin_files:
            assert "assemblyai" not in code.lower(), f"{name} must not contact AssemblyAI"
            for url in re.findall(r"""fetch\(\s*[`'"]([^`'"]+)""", code):
                assert url.startswith("/"), f"{name} sends a request off-origin: {url!r}"

        for match in storage_write.finditer(code):
            key = match.group("literal") or match.group("ident")
            key = key.strip("'\"")
            assert key in allowed_storage_keys, (
                f"{key!r} is written to browser storage by {name}; "
                "credentials must never be stored there"
            )

        # Nothing that looks like an AssemblyAI key literal, comments included.
        assert not re.search(r"\b[0-9a-f]{32}\b", raw.lower()), f"key-shaped literal in {name}"

    app_js = (frontend / "app.js").read_text(encoding="utf-8")
    assert "/api/voice-token" in app_js, "the browser must obtain a token from our server"


def test_session_config_carries_no_credential():
    from backend.assemblyai_service import build_session_config

    blob = json.dumps(build_session_config()).lower()
    for forbidden in ("api_key", "apikey", "authorization", "bearer", "secret"):
        assert forbidden not in blob


def test_security_headers_present(client):
    headers = client.get("/api/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


# ---------------------------------------------------------------- staff portal


@pytest.fixture
def staff_passcode(monkeypatch):
    """
    Force a known passcode so the portal tests never depend on .env.

    Settings is a frozen dataclass, so the instance cannot be mutated. A
    replacement is swapped into admin_service instead; admin_enabled is a
    property derived from the passcode, so it turns on by itself.
    """
    import dataclasses

    from backend import admin_service
    from backend.config import settings as live_settings

    monkeypatch.setattr(
        admin_service,
        "settings",
        dataclasses.replace(live_settings, admin_passcode="TEST-PASSCODE"),
    )
    # Sessions outlive a single test otherwise.
    monkeypatch.setattr(admin_service, "_sessions", {})
    return "TEST-PASSCODE"


def _sign_in(client, passcode):
    return client.post("/api/admin/login", json={"passcode": passcode})


def test_admin_requires_sign_in(client, staff_passcode):
    """Every staff endpoint refuses an unauthenticated caller."""
    for path in (
        "/api/admin/dashboard",
        "/api/admin/appointments",
        "/api/admin/doctors",
        "/api/admin/escalations",
        "/api/admin/export.csv",
    ):
        assert client.get(path).status_code == 401, path


def test_admin_rejects_a_wrong_passcode(client, staff_passcode):
    assert _sign_in(client, "not-the-passcode").status_code == 401
    assert client.get("/api/admin/dashboard").status_code == 401


def test_admin_sign_in_sets_an_httponly_cookie(client, staff_passcode):
    """
    The browser's credential must be unreadable to page scripts, so an
    injected script has nothing to steal.
    """
    response = _sign_in(client, staff_passcode)
    assert response.status_code == 200

    cookie = response.headers.get("set-cookie", "")
    assert "vh_admin_session=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()

    assert client.get("/api/admin/dashboard").status_code == 200


def test_admin_sign_out_ends_the_session(client, staff_passcode):
    _sign_in(client, staff_passcode)
    assert client.get("/api/admin/session").status_code == 200
    client.post("/api/admin/logout")
    assert client.get("/api/admin/session").status_code == 401


def test_admin_dashboard_counts_match_the_store(client, staff_passcode):
    _sign_in(client, staff_passcode)
    date = _next_consulting_day("DOC001")
    booked = client.post(
        "/api/appointments/book",
        json={
            "patient_name": "Test Patient",
            "patient_phone": "9876500000",
            "doctor_id": "DOC001",
            "date": date,
            "time": "10:00",
        },
    ).json()
    assert booked["success"], booked

    body = client.get("/api/admin/dashboard").json()
    assert body["totals"]["active_total"] == 1
    assert body["totals"]["upcoming_7_days"] >= 0
    assert any(d["department"] == "Cardiology" for d in body["by_department"])


def test_admin_can_triage_an_appointment(client, staff_passcode):
    _sign_in(client, staff_passcode)
    date = _next_consulting_day("DOC001")
    booked = client.post(
        "/api/appointments/book",
        json={
            "patient_name": "Triage Patient",
            "patient_phone": "9876500001",
            "doctor_id": "DOC001",
            "date": date,
            "time": "10:20",
        },
    ).json()
    appointment_id = booked["data"]["appointment"]["appointment_id"]

    response = client.patch(
        f"/api/admin/appointments/{appointment_id}", json={"status": "completed"}
    )
    assert response.status_code == 200
    assert response.json()["appointment"]["status"] == "completed"

    listed = client.get("/api/admin/appointments?status=completed").json()
    assert listed["count"] == 1


def test_admin_rejects_an_unknown_status(client, staff_passcode):
    _sign_in(client, staff_passcode)
    date = _next_consulting_day("DOC001")
    booked = client.post(
        "/api/appointments/book",
        json={
            "patient_name": "Status Patient",
            "patient_phone": "9876500002",
            "doctor_id": "DOC001",
            "date": date,
            "time": "10:40",
        },
    ).json()
    appointment_id = booked["data"]["appointment"]["appointment_id"]

    response = client.patch(
        f"/api/admin/appointments/{appointment_id}", json={"status": "discharged"}
    )
    assert response.status_code == 400
    assert "discharged" in response.json()["detail"]


def test_admin_export_is_csv(client, staff_passcode):
    _sign_in(client, staff_passcode)
    response = client.get("/api/admin/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines()[0].startswith("appointment_id,date,time,status")


# ---------------------------------------------------------------- language


def _has_devanagari(text: str) -> bool:
    return any("\u0900" <= ch <= "\u097f" for ch in text)


def test_every_mode_keeps_code_switching_enabled():
    """
    The caller's language choice must NOT narrow input.language_codes.

    Measured against the live API with identical recorded speech, ["en"],
    ["hi"] and ["hi","en"] returned byte-identical transcripts for both an
    English and a Hindi sentence — the codes changed nothing about
    recognition. Narrowing them only broke the documented native
    code-switching as soon as a caller used a word from the other language.
    Language choice is enforced in the prompt, which controls what the agent
    says, not in the recogniser.
    """
    from backend.assemblyai_service import build_session_config
    from backend.config import settings

    expected = list(settings.language_codes)
    for mode in ("en", "hi", "auto"):
        assert build_session_config(mode)["input"]["language_codes"] == expected, mode


def test_unknown_language_mode_falls_back_to_auto():
    from backend.assemblyai_service import build_session_config

    assert build_session_config("klingon") == build_session_config("auto")
    assert build_session_config(None) == build_session_config("auto")


def test_english_greeting_contains_no_devanagari():
    """The greeting is the first thing a caller hears; it must not code-switch."""
    from backend.assemblyai_service import build_greeting

    assert not _has_devanagari(build_greeting("en"))
    assert not _has_devanagari(build_greeting("auto"))


@pytest.fixture
def no_hindi_voice(monkeypatch):
    """Force the no-Sarvam fallback, where an English voice reads Hindi."""
    from backend.config import Settings

    monkeypatch.setattr(Settings, "hindi_voice_available", property(lambda self: False))
    yield


@pytest.fixture
def with_hindi_voice(monkeypatch):
    """Force the Sarvam path, where a native Hindi voice speaks."""
    from backend.config import Settings

    monkeypatch.setattr(Settings, "hindi_voice_available", property(lambda self: True))
    yield


def test_hindi_uses_devanagari_when_a_hindi_voice_exists(with_hindi_voice):
    """
    Sarvam speaks Devanagari and romanised equally well (0.37 vs 0.36 seconds
    per word, measured), so real Hindi script is used — it is correct Hindi
    and it reads properly in the transcript.
    """
    from backend.assemblyai_service import build_greeting, build_system_prompt

    assert _has_devanagari(build_greeting("hi"))
    prompt = build_system_prompt("hi")
    assert "Devanagari script, the normal way" in prompt
    assert "ROMAN letters" not in prompt


def test_hindi_falls_back_to_roman_letters_without_a_hindi_voice(no_hindi_voice):
    """
    With no Sarvam key an English AssemblyAI voice reads Hindi, and it cannot
    pronounce Devanagari — measured at 3.48s of audio against 9.01s romanised
    for the same sentence. Romanised is then the only speakable form.
    """
    from backend.assemblyai_service import build_greeting, build_system_prompt

    greeting = build_greeting("hi")
    assert not _has_devanagari(greeting)
    assert "Namaste" in greeting

    for mode in ("hi", "auto"):
        prompt = build_system_prompt(mode)
        assert "ROMAN letters" in prompt, mode
        # The agent sees the caller's Hindi as Devanagari and will mirror it
        # unless explicitly told not to. That override is load-bearing.
        assert "mirror that script" in prompt, mode
        assert "Never output a single Devanagari character" in prompt, mode

    # English-only sessions never emit Hindi, so the rule is not needed there.
    assert "ROMAN letters" not in build_system_prompt("en")


def test_prompt_forbids_mixing_languages_in_a_sentence():
    """
    The agent used to be told to "mirror their mix", which produced Hinglish
    sentences. Every mode must now carry the no-mixing rule.
    """
    from backend.assemblyai_service import build_system_prompt

    for mode in ("auto", "en", "hi"):
        prompt = build_system_prompt(mode)
        assert "Never mix two languages inside one sentence" in prompt, mode
        assert "mirror their mix" not in prompt, mode


def test_voice_token_accepts_a_language(client, monkeypatch):
    """
    The caller's language choice reaches the session config — through the
    prompt and greeting, which is where it actually takes effect.
    """
    from backend import main

    async def fake_token():
        return {"token": "test-token", "expires_in_seconds": 120}

    monkeypatch.setattr(main, "create_voice_token", fake_token)

    hindi = client.get("/api/voice-token?lang=hi").json()["session_config"]
    # Hindi either way: Devanagari with a native voice, romanised without one.
    assert _has_devanagari(hindi["greeting"]) or "Namaste" in hindi["greeting"]

    english = client.get("/api/voice-token?lang=en").json()["session_config"]
    assert english["greeting"].startswith("Hello")
    assert "chosen ENGLISH" in english["system_prompt"]

    # Recognition stays code-switching in both, so a caller is never cut off
    # for using a word from the other language.
    assert hindi["input"]["language_codes"] == english["input"]["language_codes"]


# ---------------------------------------------------------------- security


def test_appointment_register_is_not_public(client):
    """
    The register lists every patient's name and phone number. It was open to
    anonymous callers; it must be staff-only.
    """
    assert client.get("/api/appointments").status_code == 401


def _book_one(client, phone="9876500111"):
    date = _next_consulting_day("DOC001")
    booked = client.post("/api/appointments/book", json={
        "patient_name": "Privacy Patient",
        "patient_phone": phone,
        "doctor_id": "DOC001",
        "date": date,
        "time": "10:00",
    }).json()
    assert booked["success"], booked
    return booked["data"]["appointment"]["appointment_id"], date


def test_cannot_cancel_without_proving_ownership(client):
    """
    Omitting the phone number used to skip verification entirely, so anyone
    holding an appointment ID could cancel a stranger's booking.
    """
    appointment_id, _ = _book_one(client)

    result = client.post("/api/appointments/cancel", json={
        "appointment_id": appointment_id, "confirm": True,
    }).json()
    assert result["success"] is False
    assert result["code"] == "verification_required"

    wrong = client.post("/api/appointments/cancel", json={
        "appointment_id": appointment_id,
        "patient_phone": "9000000000",
        "confirm": True,
    }).json()
    assert wrong["success"] is False
    assert wrong["code"] == "verification_failed"


def test_cannot_cancel_through_the_tool_bridge_either(client):
    """The voice tool bridge must not be a way around the same check."""
    appointment_id, _ = _book_one(client, phone="9876500222")

    body = client.post("/api/tools/execute", json={
        "name": "cancel_appointment",
        "arguments": {"appointment_id": appointment_id, "confirm": True},
    }).json()
    assert body["result"]["success"] is False
    assert body["result"]["code"] == "verification_required"


def test_cannot_reschedule_without_proving_ownership(client):
    appointment_id, date = _book_one(client, phone="9876500333")

    result = client.post("/api/appointments/reschedule", json={
        "appointment_id": appointment_id, "date": date, "time": "10:20",
    }).json()
    assert result["success"] is False
    assert result["code"] == "verification_required"


def test_cannot_read_an_appointment_without_proving_ownership(client):
    """Reading discloses name, doctor and time, so it needs the same proof."""
    appointment_id, _ = _book_one(client, phone="9876500444")

    body = client.post("/api/tools/execute", json={
        "name": "get_appointment_details",
        "arguments": {"appointment_id": appointment_id},
    }).json()
    assert body["result"]["success"] is False
    assert body["result"]["code"] == "verification_required"


def test_the_owner_can_still_cancel(client):
    """The fix must not lock out the legitimate patient."""
    appointment_id, _ = _book_one(client, phone="9876500555")

    result = client.post("/api/appointments/cancel", json={
        "appointment_id": appointment_id,
        "patient_phone": "9876500555",
        "confirm": True,
    }).json()
    assert result["success"] is True, result


def test_staff_can_still_triage_without_a_phone_number(client, staff_passcode):
    """Authenticated staff act on the ward list, not on a phone number."""
    _sign_in(client, staff_passcode)
    appointment_id, _ = _book_one(client, phone="9876500666")

    response = client.patch(
        f"/api/admin/appointments/{appointment_id}", json={"status": "cancelled"}
    )
    assert response.status_code == 200
    assert response.json()["appointment"]["status"] == "cancelled"


# ---------------------------------------------------------------- hindi speech


def test_hindi_speech_key_never_reaches_the_browser():
    """Only sarvam_service may read the Sarvam key."""
    frontend = Path(__file__).resolve().parent.parent / "frontend"
    for name in ("index.html", "app.js", "style.css", "admin.html", "admin.js"):
        raw = (frontend / name).read_text(encoding="utf-8").lower()
        assert "sarvam_api_key" not in raw, name
        assert "api-subscription-key" not in raw, name
        assert "api.sarvam.ai" not in raw, name


def test_wav_container_is_unwrapped_to_pcm():
    """
    The player consumes bare PCM16. Sarvam returns a WAV, and the data chunk
    is located properly rather than assuming a 44-byte header, because a WAV
    may carry extra chunks first.
    """
    import struct

    from backend.sarvam_service import _pcm_from_wav

    pcm = struct.pack("<4h", 1000, -1000, 2000, -2000)
    # A LIST chunk before `data` is exactly the case a fixed offset gets wrong.
    extra = b"LIST" + struct.pack("<I", 4) + b"INFO"
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + b"\x00" * 16 + extra
    body += b"data" + struct.pack("<I", len(pcm)) + pcm
    wav = b"RIFF" + struct.pack("<I", len(body) + 4) + body

    assert _pcm_from_wav(wav) == pcm
    # Raw PCM passes straight through.
    assert _pcm_from_wav(pcm) == pcm


def test_hindi_speech_requires_a_key(client, monkeypatch):
    """With no Sarvam key the endpoint refuses cleanly, revealing nothing."""
    from backend.config import Settings

    monkeypatch.setattr(Settings, "has_sarvam_key", property(lambda self: False))
    response = client.post("/api/speech/hindi", json={"text": "नमस्ते"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "SARVAM" not in detail.upper()
    assert ".env" not in detail


def test_hindi_speech_rejects_empty_text(client):
    assert client.post("/api/speech/hindi", json={"text": ""}).status_code == 422


def test_hindi_speech_returns_pcm(client, monkeypatch):
    """The endpoint hands back base64 PCM16 at the player's sample rate."""
    import base64
    import struct

    from backend import main
    from backend.config import AUDIO_SAMPLE_RATE

    pcm = struct.pack("<2h", 500, -500)

    async def fake_synth(text):
        assert text == "नमस्ते"
        return {
            "audio": base64.b64encode(pcm).decode(),
            "sample_rate": AUDIO_SAMPLE_RATE,
            "seconds": 0.1,
        }

    monkeypatch.setattr(main, "synthesize_hindi", fake_synth)
    body = client.post("/api/speech/hindi", json={"text": "नमस्ते"}).json()
    assert body["sample_rate"] == AUDIO_SAMPLE_RATE
    assert base64.b64decode(body["audio"]) == pcm


def test_echo_guard_is_anchored_to_audible_audio_not_the_turn_start():
    """
    Regression: the guard used to compare against `reply.started`.

    Hindi is voiced by Sarvam over HTTP, so speech begins a network round-trip
    after the turn starts. A guard measured from reply.started had already
    expired by then, and at session start its anchor was 0 — so any room noise
    flushed the whole greeting before it was even synthesised. The guard must
    key off audio that is actually playing or in flight.
    """
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "function agentIsAudible(" in app_js
    assert "!agentIsAudible()" in app_js, "the VAD flush must consult the guard"
    # The old anchor must not come back as executable code.
    assert "Date.now() - app.replyStartedAt" not in app_js
    # The guard must consider synthesis still in flight, not just playback.
    assert "hindiSpeaker.pending > 0" in app_js


# ---------------------------------------------------------------- deployment


def test_serverless_store_moves_off_the_read_only_bundle(monkeypatch):
    """
    Vercel and Lambda mount the deployed bundle read-only; only the system
    temp directory is writable. Writing into DATA_DIR there raises OSError on
    every booking, so the store must relocate automatically.
    """
    import importlib
    import tempfile

    from backend import config

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("APPOINTMENTS_PATH", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.APPOINTMENTS_PATH.parent == Path(tempfile.gettempdir())
        assert reloaded.EPHEMERAL_STORAGE is True
    finally:
        monkeypatch.delenv("VERCEL", raising=False)
        importlib.reload(config)


def test_explicit_store_path_wins_and_is_not_flagged_ephemeral(monkeypatch, tmp_path):
    """A mounted volume is durable, so it must not be reported as ephemeral."""
    import importlib

    from backend import config

    target = tmp_path / "appointments.json"
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("APPOINTMENTS_PATH", str(target))
    reloaded = importlib.reload(config)
    try:
        assert reloaded.APPOINTMENTS_PATH == target
        assert reloaded.EPHEMERAL_STORAGE is False
    finally:
        monkeypatch.delenv("VERCEL", raising=False)
        monkeypatch.delenv("APPOINTMENTS_PATH", raising=False)
        importlib.reload(config)


def test_health_reports_storage_durability(client):
    assert client.get("/api/health").json()["ephemeral_storage"] is False


def test_vercel_entrypoint_exports_the_real_app():
    """The serverless entrypoint must re-export the app, not rebuild it."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from api.index import app as serverless_app

    from backend.main import app as real_app

    assert serverless_app is real_app


def test_microphone_is_gated_while_the_agent_speaks_on_speakers():
    """
    Regression: the microphone streamed continuously, so on laptop speakers
    the agent's own voice went back into the recogniser and the caller was
    not understood. The giveaway was that headphones worked and speakers did
    not — headphones are the only case with no speaker-to-microphone path.

    While the agent is audible the client must send silence, not the room.
    Silence keeps the stream continuous for turn detection; sending nothing
    would look like a stalled connection.
    """
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "app.speakerMode && agentIsAudible(MIC_GATE_TAIL_MS)" in app_js
    assert "MIC_GATE_TAIL_MS" in app_js
    # Suppression is by LEVEL, not by time. Muting for the whole reply meant a
    # caller who spoke during a thirty-second answer was ignored throughout;
    # measured, quiet leakage (peak 1019) is silenced while speech (25479)
    # passes and interrupts.
    # The threshold is RELATIVE to the microphone. A fixed value sat five
    # times above the loudest word a quiet laptop array produced (589 of
    # 32767, about -35 dBFS), silencing that caller whenever Arin spoke.
    assert "noiseFloor * 6" in app_js
    assert "peak < bargeIn" in app_js
    assert "BARGE_IN_THRESHOLD" not in app_js, "fixed threshold must not return"
    # Headphone users keep full duplex so they can still interrupt.
    assert "el.audioSetup.value === 'headphones'" in app_js
    # Echo cancellation follows the setup rather than being hard-coded on.
    assert "echoCancellation: app.speakerMode" in app_js


def test_audio_output_manager_handles_all_support_levels():
    """
    Phase 4: the page must route Arin's voice to a chosen output device, and
    must be honest when the browser cannot. Verified in a real browser:
      context path  -> AudioContext.setSinkId called with the device id
      element path  -> bridge node + <audio>.setSinkId called
      neither       -> selector disabled, told to set the OS default
    """
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "AudioContext.prototype.setSinkId" in app_js
    assert "HTMLMediaElement.prototype.setSinkId" in app_js
    assert "createMediaStreamDestination()" in app_js, "element fallback bridge"
    assert "'unsupported'" in app_js
    assert "cannot choose an output device" in app_js

    # Output routing must not touch capture: the player takes the sink node,
    # the microphone graph is built separately.
    assert "constructor(context, sinkNode = null)" in app_js
    assert "new AudioPlayer(app.audioContext, sinkNode)" in app_js


def test_assistant_is_named_arin():
    """The agent must know its own name and never claim to be a clinician."""
    from backend.assemblyai_service import build_greeting, build_system_prompt

    prompt = build_system_prompt("en")
    assert "You are Arin" in prompt
    assert "Never claim to be a doctor" in prompt
    assert "Arin" in build_greeting("en")


def test_ui_reports_a_thinking_state():
    """Phase 5: the gap between the caller stopping and Arin replying."""
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "thinking:" in app_js
    assert "Arin is thinking" in app_js
    assert "Arin is listening" in app_js
    assert "Arin is speaking" in app_js


def test_quiet_microphones_are_amplified_before_sending():
    """
    Some laptop arrays deliver speech far below what recognition expects.
    Measured on the reporter's HP OMEN array, the loudest spoken word peaked
    at 589 of 32767 — about -35 dBFS, roughly seventeen times quieter than
    normal speech. The browser's own autoGainControl did not lift it, so
    audio arrived and voice activity detection never called it speech.

    Verified in a real browser by simulating that exact level:
      raw peak 398 -> sent peak 8192 (gain 20.6x) -> transcript.user  ✓
      raw peak 7643 (normal mic) -> gain 1.07x, no clipping           ✓
    """
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "AGC_TARGET_PEAK" in app_js
    assert "AGC_MAX_GAIN" in app_js
    assert "amplify(int16, gain)" in app_js
    # Floored at 1 so a healthy microphone is left alone rather than reduced.
    assert "Math.max(1, AGC_TARGET_PEAK" in app_js
    # And clamped, so a near-silent chunk cannot produce runaway gain.
    assert "Math.min(\n      AGC_MAX_GAIN" in app_js or "Math.min(AGC_MAX_GAIN" in app_js


def test_silent_microphone_is_reported_not_ignored():
    """
    A Bluetooth headset cannot carry high-quality audio and a microphone at
    the same time. Windows exposes the two profiles separately —
    "Headphones (X)" is A2DP, playback only, and capturing from it yields an
    endless stream of zeros; "Headset (X Hands-Free)" is HFP and has a
    working microphone.

    The page used to sit on "Connected — Arin is ready" while receiving
    literal digital silence (observed: raw peak 0, analyser 0.0000). It must
    say what happened and name a device that would work.

    Verified in a browser with a silent stream and that exact device list:
    the picker labelled the A2DP endpoint "— no microphone", and after the
    watchdog window the error read "...Try selecting 'Headset (Nirvana Space
    Hands-Free)' as the microphone above, then press Reconnect."
    """
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "SILENCE_WATCHDOG_MS" in app_js
    assert "startSilenceWatchdog" in app_js
    assert "app.sawRealAudio" in app_js
    # Names the real cause rather than a generic "check your microphone".
    assert "Bluetooth cannot provide high-quality audio and a microphone" in app_js
    # And flags the unusable endpoint in the picker itself.
    assert "no microphone" in app_js
    assert "hands[- ]?free" in app_js


# ---------------------------------------------------------------- latency


def test_outbound_http_uses_a_pooled_client():
    """
    Both integrations opened a new AsyncClient per call, paying for a fresh
    DNS lookup, TCP handshake and TLS negotiation to another continent every
    time. The token mint sits directly on the caller's connect path.
    """
    from backend import assemblyai_service, sarvam_service

    for module in (assemblyai_service, sarvam_service):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "httpx.AsyncClient(" not in source, f"{module.__name__} still builds its own client"
        assert "get_client()" in source


def test_pooled_client_is_reused_across_calls():
    from backend.http_client import get_client

    assert get_client() is get_client()


def test_connection_is_guarded_against_duplicate_sessions():
    """
    isActive() only turns true once the socket opens, so rapid clicks during
    the connect window each started their own session — several microphone
    streams, several sockets, several tokens burned. Verified in a browser:
    three clicks produced exactly one WebSocket and one session.ready.
    """
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "if (isActive() || app.connecting) return;" in app_js
    assert "el.startButton.disabled = true;" in app_js
    # And a ceiling so the UI can never sit on "Connecting…" forever.
    assert "CONNECT_TIMEOUT_MS" in app_js
    assert "taking too long" in app_js


def test_token_request_overlaps_microphone_permission():
    """
    The token does not depend on the microphone, so it must not queue behind
    it. Measured in a browser: the token stage fell from 1012 ms to 261 ms
    and total connect from 2300 ms to 1633 ms.
    """
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    # Scope to startVoiceSession: testMicrophone() also calls getUserMedia,
    # and comparing against that one proves nothing about the session path.
    start = app_js.index("async function startVoiceSession()")
    body = app_js[start:app_js.index("\nfunction send(", start)]

    token_start = body.index("tokenPromise = apiGet(")
    mic_await = body.index("await navigator.mediaDevices.getUserMedia")
    assert token_start < mic_await, "the token request must be in flight before the mic await"
    assert "credentials = await tokenPromise;" in body


def test_connection_stages_are_measured():
    """Latency must be attributable to a stage, not guessed at."""
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    for mark in ("microphone", "token", "audioGraph", "socketOpen", "sessionReady"):
        assert f"timings.mark('{mark}')" in app_js, mark


def test_a_silent_microphone_is_switched_automatically():
    """
    Telling the caller to pick a different device only helps if they read the
    message and know which to choose. The page already knows: a Bluetooth
    A2DP endpoint ("Headphones (X)") has no microphone at all, so it can
    never be the answer, and a built-in array is preferred over a hands-free
    profile because hands-free drags playback quality down with it.

    Verified in a browser with the reporter's real device list, where the
    A2DP endpoint yields silence and the array yields tone:
      opened ['bt-a2dp', 'omen'], picker moved to the array, sawRealAudio true.
    """
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "autoSwitchTried" in app_js
    assert "switching to" in app_js
    # An A2DP endpoint must never be chosen as the replacement.
    assert "const capable = alternatives.filter(" in app_js
    # teardown() leaves app.state alone and isActive() treats anything but
    # idle/error as live, so the restart needs an explicit reset or it
    # silently early-returns.
    assert "setState('idle');" in app_js
    # One automatic attempt only, then hand over to the caller.
    assert "if (!app.autoSwitchTried && suggestion)" in app_js


def test_playback_continues_seamlessly_instead_of_inserting_gaps():
    """
    The agent's voice broke up. Measured over one English reply:

      before : 1197 buffers of 10 ms, 51 gaps, 3654 ms of injected silence
      after  :  203 coalesced buffers,  1 gap of 311 ms (a natural pause),
                zero micro-gaps under 60 ms

    Two defects. Scheduling a BufferSource per 10 ms chunk left playback at
    the mercy of every late burst. And `Math.max(now + LEAD, cursor)` punched
    a hole whenever buffer depth merely dipped below the lead, even though
    the cursor was still in the future — the lead is a startup cushion, not a
    floor to re-apply mid-sentence.
    """
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8"
    )
    # Coalescing, so one BufferSource is not created per 10 ms.
    assert "COALESCE_SAMPLES" in app_js
    assert "flushPending" in app_js

    # Seamless continuation: the cursor wins whenever it is still ahead.
    assert "this.cursor > now" in app_js
    assert "? this.cursor" in app_js
    assert "Math.max(this.ctx.currentTime + PLAYBACK_LEAD_SECONDS, this.cursor)" not in app_js

    # A partial buffer must still be flushed, or a sentence loses its tail.
    assert "COALESCE_MAX_WAIT_MS" in app_js
