"""
Central configuration for the Varanasi Hospital AI Voice Assistant.

SECURITY CONTRACT
-----------------
`ASSEMBLYAI_API_KEY` is read here from the process environment and is the ONLY
place in the codebase that touches it. It is:

  * never returned by any API route,
  * never written to a log line,
  * never rendered into a template or static file,
  * never included in an exception message.

`Settings.safe_dict()` is the only serialisable view of the configuration and
deliberately omits the key. Use it for /api/health-style diagnostics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Project layout ------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"

# Load .env from the project root. Values already present in the real
# environment win, so container/systemd secrets are not clobbered by a file.
load_dotenv(BASE_DIR / ".env", override=False)


# AssemblyAI endpoints (verified against the current official documentation:
# https://www.assemblyai.com/docs/voice-agents/voice-agent-api)
ASSEMBLYAI_BASE_URL = "https://agents.assemblyai.com"
ASSEMBLYAI_TOKEN_URL = f"{ASSEMBLYAI_BASE_URL}/v1/token"
ASSEMBLYAI_AGENTS_URL = f"{ASSEMBLYAI_BASE_URL}/v1/agents"
ASSEMBLYAI_WS_URL = "wss://agents.assemblyai.com/v1/ws"

# Audio contract required by the Voice Agent API: PCM16, mono, 24 kHz.
AUDIO_SAMPLE_RATE = 24000
AUDIO_ENCODING = "audio/pcm"

# ISO 639-1 codes accepted by the Voice Agent API for speech recognition.
SUPPORTED_INPUT_LANGUAGES = {
    "en", "es", "fr", "de", "it", "pt", "tr", "nl",
    "sv", "da", "fi", "hi", "vi", "ar", "he", "ja", "zh", "no",
}

# Voices the Voice Agent API can speak with today.
SUPPORTED_VOICES = {
    "alba", "eve", "george", "jane", "jean", "mary", "michael",   # US English
    "anna", "charles", "paul", "vera",                            # UK English
    "giovanni", "lola", "juergen", "rafael", "estelle",           # it/es/de/pt/fr
}

# Languages AssemblyAI can currently SPEAK. Hindi is recognised but not yet
# spoken - see README, "Multilingual support - what actually works today".
SPOKEN_OUTPUT_LANGUAGES = {"en", "it", "es", "de", "pt", "fr"}


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_list(name: str, default: List[str] | None = None) -> List[str]:
    raw = _env_str(name)
    if not raw:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable, validated runtime settings."""

    # --- secret (never serialised) ---
    assemblyai_api_key: str = field(repr=False, default="")

    # --- token behaviour ---
    voice_token_expires_seconds: int = 120
    voice_max_session_seconds: int = 1800

    # --- agent behaviour ---
    voice_id: str = "anna"
    language_codes: List[str] = field(default_factory=lambda: ["hi", "en"])
    reply_in_caller_language: bool = True
    voice_volume: int = 100
    agent_id: str = ""

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8000
    cors_allow_origins: List[str] = field(default_factory=list)
    token_rate_limit_max: int = 10
    token_rate_limit_window_seconds: int = 60
    app_env: str = "development"

    # --- staff portal ---
    # DEMO-GRADE access control. A single shared passcode gates /admin so the
    # demo is not wide open on a shared network. It is NOT a user system:
    # there are no per-staff accounts, roles or audit trail. Anything
    # resembling production needs real authentication before it sees PHI.
    admin_passcode: str = field(repr=False, default="")
    admin_session_hours: int = 8

    # --- Sarvam AI (native Hindi speech) ---
    # AssemblyAI cannot speak Hindi. Sarvam can, so Hindi replies are spoken by
    # Sarvam while English stays entirely on AssemblyAI.
    sarvam_api_key: str = field(repr=False, default="")
    sarvam_model: str = "bulbul:v3"
    sarvam_speaker: str = "ritu"

    # ---------- derived helpers ----------

    @property
    def has_api_key(self) -> bool:
        key = self.assemblyai_api_key
        return bool(key) and not key.startswith("replace_with_")

    @property
    def has_sarvam_key(self) -> bool:
        key = self.sarvam_api_key
        return bool(key) and not key.startswith("your_")

    @property
    def hindi_voice_available(self) -> bool:
        """True when Hindi can be spoken by a native Hindi voice."""
        return self.has_sarvam_key

    @property
    def admin_enabled(self) -> bool:
        """The staff portal stays switched off until a passcode is set."""
        return bool(self.admin_passcode)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def can_speak_caller_language(self) -> bool:
        """True only if every configured input language can also be spoken."""
        if not self.language_codes:
            return False
        return all(code in SPOKEN_OUTPUT_LANGUAGES for code in self.language_codes)

    def safe_dict(self) -> dict:
        """Configuration view that is safe to return over HTTP or log."""
        return {
            "app_env": self.app_env,
            "voice_id": self.voice_id,
            "language_codes": list(self.language_codes),
            "reply_in_caller_language": self.reply_in_caller_language,
            "voice_volume": self.voice_volume,
            "voice_token_expires_seconds": self.voice_token_expires_seconds,
            "voice_max_session_seconds": self.voice_max_session_seconds,
            "uses_stored_agent": bool(self.agent_id),
            "assemblyai_key_configured": self.has_api_key,
            "audio_sample_rate": AUDIO_SAMPLE_RATE,
            "audio_encoding": AUDIO_ENCODING,
            "websocket_url": ASSEMBLYAI_WS_URL,
            "admin_enabled": self.admin_enabled,
            "hindi_voice_available": self.hindi_voice_available,
        }


def _load_settings() -> Settings:
    voice_id = _env_str("VOICE_ID", "anna").lower()
    if voice_id not in SUPPORTED_VOICES:
        voice_id = "anna"

    codes = [c.lower() for c in _env_list("VOICE_LANGUAGE_CODES", ["hi", "en"])]
    codes = [c for c in codes if c in SUPPORTED_INPUT_LANGUAGES]

    origins = _env_list(
        "CORS_ALLOW_ORIGINS",
        ["http://localhost:8000", "http://127.0.0.1:8000"],
    )

    return Settings(
        assemblyai_api_key=_env_str("ASSEMBLYAI_API_KEY"),
        voice_token_expires_seconds=_env_int("VOICE_TOKEN_EXPIRES_SECONDS", 120, 1, 600),
        voice_max_session_seconds=_env_int("VOICE_MAX_SESSION_SECONDS", 1800, 60, 10800),
        voice_id=voice_id,
        language_codes=codes,
        reply_in_caller_language=_env_bool("REPLY_IN_CALLER_LANGUAGE", True),
        voice_volume=_env_int("VOICE_VOLUME", 100, 0, 100),
        agent_id=_env_str("ASSEMBLYAI_AGENT_ID"),
        host=_env_str("HOST", "127.0.0.1"),
        port=_env_int("PORT", 8000, 1, 65535),
        cors_allow_origins=origins,
        token_rate_limit_max=_env_int("TOKEN_RATE_LIMIT_MAX", 10, 1, 10_000),
        token_rate_limit_window_seconds=_env_int(
            "TOKEN_RATE_LIMIT_WINDOW_SECONDS", 60, 1, 3600
        ),
        app_env=_env_str("APP_ENV", "development"),
        admin_passcode=_env_str("ADMIN_PASSCODE"),
        admin_session_hours=_env_int("ADMIN_SESSION_HOURS", 8, 1, 72),
        sarvam_api_key=_env_str("SARVAM_API_KEY"),
        sarvam_model=_env_str("SARVAM_TTS_MODEL", "bulbul:v3"),
        sarvam_speaker=_env_str("SARVAM_HINDI_SPEAKER", "ritu").lower(),
    )


settings = _load_settings()

__all__ = [
    "settings",
    "Settings",
    "BASE_DIR",
    "DATA_DIR",
    "FRONTEND_DIR",
    "ASSEMBLYAI_BASE_URL",
    "ASSEMBLYAI_TOKEN_URL",
    "ASSEMBLYAI_AGENTS_URL",
    "ASSEMBLYAI_WS_URL",
    "AUDIO_SAMPLE_RATE",
    "AUDIO_ENCODING",
    "SUPPORTED_VOICES",
    "SUPPORTED_INPUT_LANGUAGES",
    "SPOKEN_OUTPUT_LANGUAGES",
]
