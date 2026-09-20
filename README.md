# 🏥 Varanasi Hospital — AI Voice Assistant

A working MVP hospital voice agent: patients speak naturally in **Hindi, English or Hinglish** and the assistant books, reschedules and cancels appointments, answers questions about doctors and departments, detects emergencies, and escalates to human staff.

Built on the **AssemblyAI Voice Agent API** with a **FastAPI** backend and a plain **HTML/CSS/JavaScript** frontend. The permanent API key never leaves the server.

> **Demonstration software.** Every doctor, department, schedule, fee, phone number and address in this repository is fictional. This is not a medical service, makes no HIPAA / DPDP / medical-device / clinical-validation claim, and must not be used for real patient care without a full rebuild of its data, storage and safety layers.

---

## Table of contents

1. [Project overview](#1-project-overview)
2. [Features](#2-features)
3. [Architecture](#3-architecture)
4. [Folder structure](#4-folder-structure)
5. [Installation](#5-installation)
6. [Environment setup](#6-environment-setup)
7. [Running the app](#7-running-the-app)
8. [How the temporary token works](#8-how-the-temporary-token-works)
9. [Why the browser never sees the API key](#9-why-the-browser-never-sees-the-api-key)
10. [API reference](#10-api-reference)
11. [Voice agent tools](#11-voice-agent-tools)
12. [Appointment demo](#12-appointment-demo)
13. [Emergency workflow](#13-emergency-workflow)
14. [Human handoff](#14-human-handoff)
15. [Multilingual support — what actually works today](#15-multilingual-support--what-actually-works-today)
16. [Testing](#16-testing)
17. [Security](#17-security)
18. [Privacy and healthcare safety](#18-privacy-and-healthcare-safety)
19. [Production improvements](#19-production-improvements)
20. [Troubleshooting](#20-troubleshooting)

---

## 1. Project overview

A patient opens the site, presses **Start Voice Assistant**, and has a real spoken conversation with a hospital receptionist agent:

> **Patient:** "Hello, I want to see a cardiologist."
> **AI:** "Sure, I can help with that. What date would you prefer?"
> **Patient:** "Tomorrow."
> **AI:** *(checks the live availability tool)* "Dr. Rajesh Sharma has ten, ten twenty and ten forty in the morning. Which suits you?"

The agent never invents a doctor, a slot or a fee — every fact it states comes from a tool call into this backend. It never claims a booking succeeded unless the booking operation actually returned success.

**Five core capabilities**

| | Capability | What it does |
|---|---|---|
| 1 | **Smart appointment booking** | Book, reschedule and cancel by voice, with double-booking prevention and phone verification |
| 2 | **Doctor & department information** | Availability, specialisation, timings, fees, languages, room numbers |
| 3 | **Emergency assistance** | LLM judgement plus an independent keyword safety net in English, Hindi and Hinglish |
| 4 | **Multilingual voice** | Hindi + English + Hinglish code-switching (see [§15](#15-multilingual-support--what-actually-works-today) for exactly what is and isn't supported today) |
| 5 | **Human handoff** | Escalation to hospital staff for sensitive, complex or unresolved conversations |

---

## 2. Features

**Voice**
- Real-time, full-duplex browser conversation over WebSocket
- Barge-in: the patient can interrupt the agent mid-sentence and playback flushes immediately
- Live partial and final transcripts for both speakers
- Microphone permission handling with a distinct message for each failure mode
- Clean disconnect handling and a reconnect button

**Hospital operations**
- 12 demo doctors across 11 departments, with per-doctor slot lengths (15/20/30 min)
- Slot generation from consulting hours, filtered by weekday, existing bookings and lead time
- Double-booking prevention, past-date rejection, 60-day booking horizon
- Phone-number verification before any reschedule or cancellation
- Natural-speech date handling: `tomorrow`, `kal`, `कल`, `friday`, `21-09-2026`, `2026-09-21`
- Free-text department mapping: `heart doctor` → Cardiology, `haddi` → Orthopedics, `skin` → Dermatology

**Interface**
- Responsive hospital UI, tested from 390 px to 1440 px
- Large microphone button with distinct connecting / listening / speaking / emergency states
- Unmissable emergency banner with real, tappable emergency numbers
- Form-based booking, doctor search, department browser and appointment lookup — so the demo works even before you add an API key
- Keyboard navigation, focus trapping in dialogs, ARIA live regions, screen-reader labels, and a `prefers-reduced-motion` path

---

## 3. Architecture

```
                 ┌─────────────────────┐
                 │      Browser        │
                 │ HTML/CSS/JavaScript │
                 └──────────┬──────────┘
                            │
                            │ 1. GET /api/voice-token
                            ▼
                 ┌─────────────────────┐
                 │   FastAPI Backend   │
                 │                     │
                 │  API key in .env    │
                 │  (server-side only) │
                 └──────────┬──────────┘
                            │
                            │ 2. GET /v1/token
                            │    Authorization: Bearer <API_KEY>
                            ▼
                 ┌─────────────────────┐
                 │    AssemblyAI       │
                 │   Voice Agent API   │
                 └──────────┬──────────┘
                            │
                            │ 3. { "token": "...", "expires_in_seconds": 120 }
                            ▼
                 ┌─────────────────────┐
                 │      Browser        │
                 │ 4. wss://…/v1/ws    │
                 │      ?token=<token> │
                 └──────────┬──────────┘
                            │
                            ▼
                  Hospital AI Assistant
                            │
                   5. tool.call events
                            │
                            ▼
                 ┌─────────────────────┐
                 │  POST /api/tools/   │
                 │       execute       │
                 └──────────┬──────────┘
                            │
             ┌──────────────┼───────────────┐
             ▼              ▼               ▼
       Appointments      Hospital       Emergency
                         Database        / Handoff
```

**Two design decisions worth knowing about:**

**Inline session configuration, not a stored agent.** The backend sends the full agent definition (system prompt, greeting, tools, audio and language settings) with every session rather than pre-registering an agent with AssemblyAI. You can clone this repo, add a key, and run it — with no provisioning step. The prompt also embeds today's date, so the agent always knows what "tomorrow" means. Set `ASSEMBLYAI_AGENT_ID` in `.env` if you would rather bind to a stored agent.

**Client-side tools, not HTTP tools.** AssemblyAI can call your HTTP endpoints directly, but that requires a publicly reachable URL. Instead the agent emits a `tool.call` over the WebSocket, the browser relays it to `POST /api/tools/execute`, and the JSON result goes back as `tool.result`. The whole thing runs on `localhost` with no tunnel, and the hospital database is never exposed to the internet. The browser is only a pipe — it never decides what a tool does.

---

## 4. Folder structure

```
varanasi-hospital-ai/
│
├── backend/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app, all routes, tool dispatch, rate limiting
│   ├── config.py               # env loading + validation. ONLY module that reads the API key
│   ├── assemblyai_service.py   # token exchange, system prompt, tool schemas, session config
│   ├── hospital_service.py     # doctors, departments, dates, slot generation
│   ├── appointment_service.py  # book / reschedule / cancel / lookup with atomic JSON writes
│   ├── emergency_service.py    # emergency detection + simulated human handoff
│   └── models.py               # Pydantic request/response models and validators
│
├── frontend/
│   ├── index.html              # UI structure, modals, accessibility landmarks
│   ├── style.css               # hospital design system
│   └── app.js                  # voice client, audio pipeline, tool relay, UI logic
│
├── data/
│   ├── doctors.json            # 12 demo doctors
│   ├── departments.json        # 11 demo departments
│   ├── appointments.json       # demo appointment store (written at runtime)
│   └── hospital.json           # hospital info, hours, emergency numbers
│
├── tests/
│   └── test_api.py             # 47 tests: API, booking, emergency, tools, key-leak guards
│
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## 5. Installation

Requires **Python 3.10+**.

```bash
# 1. Get the code
cd varanasi-hospital-ai

# 2. Create a virtual environment
python -m venv venv
```

**Windows**

```bash
venv\Scripts\activate
```

**macOS / Linux**

```bash
source venv/bin/activate
```

**Install dependencies**

```bash
pip install -r requirements.txt
```

That's five packages: `fastapi`, `uvicorn[standard]`, `httpx`, `python-dotenv`, `pydantic`. No AssemblyAI SDK is needed — the Voice Agent API is a plain REST token call plus a browser WebSocket, and calling it directly keeps the moving parts visible.

---

## 6. Environment setup

**Get an API key**

1. Sign in at [assemblyai.com](https://www.assemblyai.com/app/api-keys).
2. Copy your API key.
3. **If that key has ever been pasted into a chat, a commit, a screenshot or a README — rotate it first.** Treat any key that has been seen outside your own machine as burned.

**Create your `.env`**

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Then edit `.env`:

```env
ASSEMBLYAI_API_KEY=your_rotated_key_here
```

`.env` is already in `.gitignore`. Never commit it, and never put a real key in `.env.example`.

**Everything else is optional** — see `.env.example` for the full annotated list. The ones you may actually want:

| Variable | Default | What it does |
|---|---|---|
| `VOICE_ID` | `anna` | Agent voice. British: `anna`, `charles`, `paul`, `vera`. American: `alba`, `eve`, `george`, `jane`, `jean`, `mary`, `michael` |
| `VOICE_LANGUAGE_CODES` | `hi,en` | Recognition languages. Empty = automatic detection across all 18 |
| `REPLY_IN_CALLER_LANGUAGE` | `true` | `true`: reply in Hindi to Hindi. `false`: understand Hindi, reply in English. See [§15](#15-multilingual-support--what-actually-works-today) |
| `VOICE_TOKEN_EXPIRES_SECONDS` | `120` | Token redemption window (1–600) |
| `VOICE_MAX_SESSION_SECONDS` | `1800` | Hard cap per voice session (60–10800) |
| `CORS_ALLOW_ORIGINS` | localhost:8000 | Comma-separated allowed browser origins |
| `APP_ENV` | `development` | Set to `production` to disable `/docs` |

---

## 7. Running the app

One server serves both the API and the frontend:

```bash
uvicorn backend.main:app --reload
```

Then open **<http://localhost:8000>**.

On a different port or host:

```bash
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8080
```

> Remember to add the new origin to `CORS_ALLOW_ORIGINS` in `.env` if you change the port.

**Startup output tells you whether voice is live:**

```
INFO   Varanasi Hospital AI Voice Assistant v1.0.0
INFO   Loaded 12 demo doctors across 11 departments
INFO   AssemblyAI key detected. Voice assistant is ready.
```

Without a key you get a warning instead, and `/api/voice-token` returns `503`. **Everything else still works** — the booking form, doctor search, department browser, appointment lookup, emergency panel and handoff are all fully functional without an API key. That makes it easy to demo the hospital logic before you wire up voice.

Interactive API docs: **<http://localhost:8000/docs>** (development only).

---

## 8. How the temporary token works

The browser is never trusted with the permanent key. Instead there is a three-party exchange:

**Step 1 — the browser asks your server, not AssemblyAI**

```js
const credentials = await fetch('/api/voice-token').then(r => r.json());
```

**Step 2 — your server does the privileged call**

`backend/assemblyai_service.py` is the only module that reads the key:

```python
response = await client.get(
    "https://agents.assemblyai.com/v1/token",
    params={
        "expires_in_seconds": settings.voice_token_expires_seconds,
        "max_session_duration_seconds": settings.voice_max_session_seconds,
    },
    headers={"Authorization": f"Bearer {settings.assemblyai_api_key}"},
)
```

**Step 3 — only the temporary token comes back down**

```json
{
  "token": "eyJhbGciOi…",
  "expires_in_seconds": 120,
  "websocket_url": "wss://agents.assemblyai.com/v1/ws",
  "session_config": { "system_prompt": "…", "tools": [ … ] }
}
```

**Step 4 — the browser opens the WebSocket with the token**

```js
const wsUrl = new URL(credentials.websocket_url);
wsUrl.searchParams.set('token', credentials.token);
const ws = new WebSocket(wsUrl);
credentials.token = null;   // dropped immediately after use
```

**What makes this safe**

| Property | Value | Consequence if stolen |
|---|---|---|
| Lifetime | 120 seconds (configurable, max 600) | Expires before an attacker can use it |
| Uses | **One** — single session only | Already spent by the time it could leak |
| Scope | One voice session | Cannot list, create or delete anything on your account |
| Rate limit | 10 requests / minute / IP | Cannot be farmed for free sessions |

Compare that to the permanent key, which has full account access and never expires.

---

## 9. Why the browser never sees the API key

This is enforced structurally, not by convention:

1. **One reader.** `backend/config.py` is the only module that calls `os.getenv("ASSEMBLYAI_API_KEY")`. It is stored on a frozen dataclass with `field(repr=False)`, so it cannot appear in a `repr()`, a log line or a stack trace.
2. **No response field can carry it.** `VoiceTokenResponse` has exactly four fields — `token`, `expires_in_seconds`, `websocket_url`, `session_config`. FastAPI serialises only declared fields, so there is no field for a key to travel in.
3. **A safe config view exists on purpose.** `Settings.safe_dict()` deliberately omits the key; anything that needs to report configuration uses it.
4. **Errors are sanitised.** `VoiceTokenError` carries only operator-safe text. A `401` from AssemblyAI logs *"the key was rejected"* and returns *"the voice service rejected the server credentials"* — never the key, and never the name of the environment variable.
5. **Static analysis in CI.** `tests/test_api.py` fails the build if `api_key`, `apikey`, `Bearer`, `authorization`, `localStorage`, `sessionStorage` or any 32-hex-character literal appears in the executable frontend source, and sweeps every GET endpoint for a sentinel key value.
6. **No browser storage at all.** The token lives in one local variable and is nulled after the WebSocket opens. Nothing is written to `localStorage`, `sessionStorage`, a cookie or the DOM.

To verify it yourself:

```bash
grep -ri "assemblyai_api_key" frontend/    # no matches
curl -s localhost:8000/api/voice-token | python -m json.tool
```

---

## 10. API reference

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness + whether voice is configured |
| `GET` | `/api/config` | Non-secret client config (WS URL, audio format, languages) |
| `GET` | `/api/voice-token` | **Mint a short-lived AssemblyAI token** |
| `GET` | `/api/doctors` | List/filter doctors — `?department=`, `?q=`, `?day=`, `?specialization=` |
| `GET` | `/api/doctors/{doctor_id}` | One doctor |
| `GET` | `/api/departments` | All departments, or `?name=Cardiology` |
| `GET` | `/api/hospital` | Hospital info, hours, emergency numbers |
| `GET` | `/api/appointments` | All appointments — `?include_cancelled=true` |
| `POST` | `/api/appointments/check` | Free slots for a doctor or department on a date |
| `POST` | `/api/appointments/book` | Book a slot |
| `POST` | `/api/appointments/reschedule` | Move an appointment |
| `POST` | `/api/appointments/cancel` | Cancel an appointment |
| `POST` | `/api/appointments/lookup` | Find by reference or phone |
| `POST` | `/api/emergency` | Screen an utterance / handle the emergency button |
| `POST` | `/api/handoff` | Escalate to hospital staff (simulated) |
| `POST` | `/api/tools/execute` | Voice agent tool bridge |
| `GET` | `/api/tools` | List implemented tool names |

Every mutating endpoint returns the same envelope, and **`success` is the only thing the agent is permitted to treat as proof that something happened**:

```json
{
  "success": true,
  "code": "booked",
  "message": "Appointment confirmed for Ankit Patel with Dr. Rajesh Sharma…",
  "data": { "appointment": { "appointment_id": "VH260919AB12", "…": "…" } }
}
```

---

## 11. Voice agent tools

Eleven tools are exposed to the agent. All are **client-side tools** relayed through `POST /api/tools/execute`:

| Tool | Purpose |
|---|---|
| `search_doctors` | Find doctors by department, specialisation or free text |
| `get_doctor_details` | Full record for one doctor |
| `check_appointment_availability` | Free slots — **always called before offering a time** |
| `book_appointment` | Create a booking |
| `reschedule_appointment` | Move a booking |
| `cancel_appointment` | Cancel a booking |
| `get_appointment_details` | Look up by reference or phone |
| `get_department_information` | Department description, timings, floor, doctors |
| `get_hospital_information` | Hours, facilities, emergency numbers |
| `emergency_assistance` | Start the emergency workflow |
| `human_handoff` | Escalate to staff |

**Anti-hallucination design.** Tool results carry instructions, not just data. A miss returns:

```json
{
  "found": false,
  "message": "No doctor with that ID is in the records. Do not invent one."
}
```

A backend exception returns *"Do not claim anything was done. Offer to connect the caller with hospital staff."* An unknown tool name is rejected rather than guessed. `tests/test_api.py` asserts that the set of tools declared to AssemblyAI exactly equals the set implemented on the server, so the two can never drift apart.

**Tool result timing.** The AssemblyAI docs require `tool.result` to be sent when `reply.done` is the latest event. `app.js` queues results while a turn is active and flushes on `reply.done`, with a 2.5-second safety valve so a stuck turn can never strand a result.

---

## 12. Appointment demo

**By voice**

> "I want to book an appointment with a cardiologist tomorrow"
> "मुझे कल डॉक्टर से मिलना है"
> "I need to reschedule my appointment"
> "Who is available in orthopedics?"

**By form** — the **📅 Book Appointment** quick action. Pick a department, pick a doctor, pick a date, and real slots load from `/api/appointments/check`.

**By curl** — a full lifecycle:

```bash
# 1. Check availability (Dr. Rajesh Sharma consults Mon/Wed/Fri)
curl -s -X POST localhost:8000/api/appointments/check \
  -H 'Content-Type: application/json' \
  -d '{"date":"2026-09-21","doctor_id":"DOC001"}'

# 2. Book
curl -s -X POST localhost:8000/api/appointments/book \
  -H 'Content-Type: application/json' \
  -d '{"patient_name":"Ankit Patel","patient_phone":"9876543210",
       "doctor_id":"DOC001","date":"2026-09-21","time":"10:00"}'

# 3. Reschedule (use the reference from step 2)
curl -s -X POST localhost:8000/api/appointments/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"appointment_id":"VH260919AB12","date":"2026-09-21",
       "time":"11:20","patient_phone":"9876543210"}'

# 4. Cancel
curl -s -X POST localhost:8000/api/appointments/cancel \
  -H 'Content-Type: application/json' \
  -d '{"appointment_id":"VH260919AB12","patient_phone":"9876543210","confirm":true}'
```

**Guard rails you can demo by breaking them:** booking the same slot twice, booking a doctor on a day they don't consult, booking a past date, cancelling with the wrong phone number, and booking the Emergency department (walk-in only) all fail with a specific, speakable error message the agent reads out.

---

## 13. Emergency workflow

**Two independent layers, deliberately.**

1. **The agent's own judgement.** The system prompt makes emergencies the highest priority, instructs it to call `emergency_assistance` immediately and to stop whatever else it was doing.
2. **A keyword safety net.** Every final user transcript is also screened by `POST /api/emergency`, independently of what the LLM decided. If the model misses it, the panel still fires.

**Coverage** — English, Hindi (Devanagari) and Hinglish (romanised), because callers in Varanasi routinely mix all three:

| | Examples |
|---|---|
| **English** | chest pain, can't breathe, unconscious, heavy bleeding, stroke, accident, anaphylaxis, snake bite, call an ambulance |
| **Hindi** | सीने में दर्द, साँस नहीं आ रही, बेहोश, बहुत खून, एक्सीडेंट, तुरंत मदद, एम्बुलेंस |
| **Hinglish** | seene me dard, saans nahi, behosh, khoon beh raha, accident ho gaya, turant madad, bachao |

Severity is `high` or `possible`, and matched terms are returned so you can see exactly why it fired.

**False positives are filtered.** *"What are the emergency ward timings?"*, *"I do not have chest pain"*, and *"what if I have an emergency"* are all correctly read as non-urgent.

**What happens when it fires**

1. The conversation is marked as an emergency and a full-width red banner takes over the page.
2. The agent is told to stop the current task and give emergency guidance.
3. Real, tappable numbers: **108** (ambulance) and **112** (emergency services).
4. The microphone button turns red.
5. A staff handoff is offered — but the patient is told not to wait for it.

**What it will never do.** No ambulance is dispatched and the system says so, in three places: the API response (`"dispatch_performed": false`), the banner (*"This assistant cannot send an ambulance. No help has been dispatched — please call."*), and the agent's instructions (*"Never say help is on the way"*). A test asserts this. If you add a real dispatch integration, that's the flag to flip — and the honesty requirement should survive the change.

> **A note on the keyword layer.** Keyword matching is a safety net, not triage. It is deliberately biased toward false positives: showing the panel to someone who didn't need it costs nothing; missing someone who did is unacceptable. It cannot assess clinical severity and must never be the primary path in production.

---

## 14. Human handoff

`POST /api/handoff` records an escalation and returns a queue position:

```json
{
  "success": true,
  "message": "A hospital staff member will assist you.",
  "handoff_id": "HO-34B826FC",
  "queue_position": 1,
  "estimated_wait_minutes": 2,
  "simulated": true,
  "notice": "DEMO HANDOFF - no call was transferred and no staff member has been paged…"
}
```

**This is simulated.** No call is transferred and nobody is paged. `simulated: true` and the `notice` field say so explicitly, and the agent is instructed never to claim a transfer happened.

The agent escalates when the patient asks for a person, the matter is sensitive, it isn't confident, the patient is frustrated, an appointment problem can't be resolved, or an emergency needs staff.

**To make it real**, replace the body of `request_handoff()` in `backend/emergency_service.py` — the signature is designed to stay the same. Options: Twilio or a SIP provider for a warm call transfer (AssemblyAI also supports [inbound SIP](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/connect-to-twilio)), a ticketing system, or a WebSocket push to a staff dashboard. Then set `simulated: false` and update the notice — and only then.

---

## 15. Multilingual support — what actually works today

**Be aware of this before you demo.** As of September 2026, AssemblyAI's Voice Agent API supports Hindi for **speech recognition but not speech synthesis**.

| | Hindi | English |
|---|---|---|
| Understands the caller | ✅ Yes, natively, with code-switching | ✅ Yes |
| Displays the transcript | ✅ Yes, in Devanagari | ✅ Yes |
| **Speaks the reply** | ⚠️ **No native Hindi voice yet** | ✅ Yes |

Native-accent voices currently exist only for English, Italian, Spanish, German, Portuguese and French. [AssemblyAI lists Hindi as on the roadmap.](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/supported-languages)

**What this app does about it** — `REPLY_IN_CALLER_LANGUAGE` in `.env` picks the trade-off:

- **`true` (default)** — the agent replies *in Hindi*, as the spec asks. The text is correct Devanagari; the audio is an English-accent voice reading Hindi, which is intelligible but noticeably accented.
- **`false`** — the agent understands Hindi perfectly but replies in simple, clear English.

The UI states this honestly in a note under the microphone rather than hiding it, and the backend surfaces it at `/api/config`. When AssemblyAI ships Hindi voices, set `VOICE_ID` to one and the note disappears on its own.

**Regional languages.** Bhojpuri, Awadhi and other Varanasi-region languages are not supported by the recognition model. The agent is instructed to do its best, reply in simple Hindi, and offer a handoff to staff who speak the caller's language — rather than pretending to understand.

**What works very well today** is Hinglish input, which is how most people in Varanasi actually speak: *"मुझे cardiologist से appointment चाहिए"* and *"kal doctor se milna hai"* are both handled natively. Doctor and department names are also fed to the recogniser as keyterms, which noticeably improves accuracy on Indian names.

---

## 16. Testing

**Automated** — 47 tests, no server and no API key required:

```bash
pip install pytest
pytest tests/ -v
```

Covers: every endpoint; the full book → double-book → reschedule → cancel lifecycle; phone verification; past dates; walk-in-only departments; emergency detection in three languages *and* the false-positive filters; the handoff demo labelling; tool-registry/schema parity; malformed tool payloads; and six separate API-key-leak guards.

**Manual backend checks**

```bash
curl localhost:8000/api/health
curl localhost:8000/api/doctors
curl localhost:8000/api/departments
curl localhost:8000/api/hospital
curl localhost:8000/api/voice-token        # 503 without a key — that's correct
```

**Manual frontend checklist**

| Check | Expected |
|---|---|
| Microphone permission | Browser prompts; denial shows a specific, actionable message |
| Token retrieval | No key → clean error, no crash |
| Connection | Status goes `Connecting…` → `Connected to Varanasi Hospital AI` |
| Speaking | Status shows `Listening…`; partial transcript appears live |
| Agent reply | Audio plays; status shows `Speaking…` |
| Interruption | Speaking over the agent stops its audio immediately |
| Transcript | Both sides logged, Devanagari renders correctly |
| Disconnect | Closing the session shows "Voice session ended" |
| Reconnect | Reconnect button mints a **new** token (they are single-use) |
| Emergency button | Red banner, 4 instructions, 108/112 links, mic turns red |
| Mobile | No horizontal scroll at 390 px |
| Keyboard | Tab reaches everything; Escape closes dialogs; focus is trapped |

---

## 17. Security

**Implemented**

- API key in `.env` only, `.env` git-ignored, single reader module, `repr`-suppressed
- Short-lived single-use browser tokens, never the permanent key
- Rate limiting: 10/min per IP on `/api/voice-token`, 20/min on booking, 120/min on tools
- Pydantic models with `extra="forbid"` — unknown fields are rejected, not ignored
- Regex validation on names, phones, IDs and times; Devanagari-aware, injection-resistant
- Explicit CORS origins, `allow_credentials=False`
- Security headers: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy: microphone=(self)`
- Sanitised errors — no stack traces, no config details, no key, not even the env var's name
- Atomic writes (`tempfile` + `os.replace`) and a re-entrant lock on the appointment store
- No browser storage of any kind
- `/docs` disabled when `APP_ENV=production`
- Automated leak tests in CI

**Not implemented — you must add these before production**

- No authentication or authorisation — anyone who can reach the server can book
- Rate limiting is per-process in memory; use Redis behind multiple workers
- No CSRF protection (there are no cookies today, but add it if you add sessions)
- No audit log of who changed what
- Appointment data is stored unencrypted in a JSON file

---

## 18. Privacy and healthcare safety

**Claims this project does *not* make:** HIPAA compliance, DPDP Act compliance, medical-device certification, clinical validation, or any assessment of clinical severity. If you deploy something derived from this, do not make those claims until they are genuinely true and verified.

**Data minimisation is designed in.** The agent asks only for what a booking needs — name, phone, department, time — and is explicitly instructed **not** to ask about symptoms, conditions, medical history or medicines. If a patient volunteers medical details, it acknowledges briefly, does not repeat them back and does not record them.

The emergency log stores an ID, severity, matched keyword and timestamp. **It does not store the transcript** — `transcript_stored: false` is on every record, and a test asserts it. The optional `reason` field on an appointment is capped at 200 characters and is never solicited.

**Visible in the UI at all times:**

> This AI assistant provides hospital information and administrative assistance. It does not replace a doctor or emergency medical professional. Do not share unnecessary sensitive medical information. For life-threatening emergencies, seek immediate medical assistance.

**The agent is not a doctor and is prompted accordingly:** no diagnosis, no medicine suggestions, no interpretation of test results, no medical advice of any kind — clinical questions are redirected to a real appointment or a staff handoff.

---

## 19. Production improvements

**Before real patients, in priority order:**

1. **Replace the JSON store with PostgreSQL.** Flat files don't survive concurrent writers across processes. You need transactions, a unique constraint on `(doctor_id, date, time)` to make double-booking impossible at the database level rather than in application code, encryption at rest, and an audit trail.
2. **Add authentication.** OTP verification of the patient's phone number before any booking action, and staff authentication for the dashboard.
3. **Replace the keyword emergency layer.** Keep it as a backstop, but the primary path should be a purpose-built classifier validated against real triage data and reviewed by clinicians — with human review of every escalation.
4. **Implement real handoff.** Twilio or SIP warm transfer, or a staff dashboard with a live queue. Until then, keep it labelled as simulated.
5. **Integrate with the hospital's actual HIS/HMS** so doctors, schedules and fees are live rather than a JSON file that silently goes stale.
6. **Compliance review.** India's DPDP Act, medical record retention rules, and consent capture for voice recording. Get this reviewed by someone qualified — not from a README.
7. **Distributed rate limiting** (Redis), and per-phone-number booking limits to stop slot squatting.
8. **Observability.** Structured logs, per-tool latency, call success rate, escalation rate, and alerting on emergency-path failures.
9. **Session resume.** AssemblyAI keeps a session resumable for 30 seconds after a drop, via `session.resume`. This MVP starts a fresh session on reconnect (simpler and more predictable for a demo); implementing resume would preserve conversation context across a flaky mobile connection.
10. **HTTPS everywhere.** `getUserMedia` and `AudioWorklet` require a secure context — `localhost` is exempt, but any real deployment needs TLS.
11. **Accessibility audit** with real screen-reader users, and a text-chat fallback for patients who cannot use voice.
12. **Load testing.** Each concurrent call is a WebSocket plus a live LLM session; know your ceiling and your per-minute cost before launch.

---

## 20. Troubleshooting

**`/api/voice-token` returns 503**
The server has no API key. Check that `.env` exists in the project root (not in `backend/`), contains `ASSEMBLYAI_API_KEY=...`, and that you restarted the server. The server log states the exact problem — the browser message is deliberately vague, which is the intended behaviour.

**"The voice service rejected the server credentials"**
A `401` from AssemblyAI: the key is wrong, revoked, or has a stray quote or trailing space in `.env`. Write it bare: `ASSEMBLYAI_API_KEY=abc123`, no quotes.

**Microphone blocked**
Browsers only allow `getUserMedia` on `https://` or `localhost`. Use `http://localhost:8000`, not your LAN IP. Check the permission icon in the address bar.

**Connects, then drops immediately**
Tokens are single-use. Every connection needs a fresh one — `app.js` already fetches a new token on reconnect, so this usually means a token was reused by custom code, or it expired (raise `VOICE_TOKEN_EXPIRES_SECONDS`).

**Agent speaks but no audio**
Browsers block audio until a user gesture. Clicking Start Voice Assistant is that gesture, so if you autoplay a session some other way it will be silent. Check the tab isn't muted and `VOICE_VOLUME` isn't `0`.

**Agent talks over itself / echoes**
`echoCancellation: true` is already set. Use headphones, or raise `min_silence` in `build_session_config()` in `backend/assemblyai_service.py`.

**Hindi is understood but sounds wrong**
Expected — see [§15](#15-multilingual-support--what-actually-works-today). There is no Hindi voice yet. Set `REPLY_IN_CALLER_LANGUAGE=false` for English replies.

**`ModuleNotFoundError: No module named 'backend'`**
Run uvicorn from the project root, not from inside `backend/`: `uvicorn backend.main:app --reload`.

**Appointments vanished**
`data/appointments.json` is the store. If you edited it by hand and broke the JSON, the service falls back to an empty list rather than crashing. Restore it to `{"appointments": []}`.

---

## Credits

Built on the [AssemblyAI Voice Agent API](https://www.assemblyai.com/docs/voice-agents). API behaviour in this README was verified against the official documentation in September 2026; check the [current docs](https://www.assemblyai.com/docs/voice-agents/voice-agent-api) before relying on protocol details.

**Reminder:** demonstration software with fictional data. Not for real patient use.
