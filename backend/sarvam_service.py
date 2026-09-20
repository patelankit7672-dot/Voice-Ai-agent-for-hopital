"""
Sarvam AI integration — native Hindi speech.

WHY THIS EXISTS
---------------
AssemblyAI's Voice Agent has no Hindi voice: output is English, Italian,
Spanish, German, Portuguese or French only. A Hindi reply was therefore spoken
by an English voice, which cannot pronounce Devanagari — measured on the same
sentence, Devanagari produced 3.48s of audio against 9.01s romanised, so most
of the reply was simply never spoken.

Sarvam's Bulbul model speaks Hindi natively, and handles Devanagari and
romanised text equally well (0.37 vs 0.36 seconds per word, measured). So the
division of labour is:

    English : AssemblyAI end to end (recognition, reasoning, speech)
    Hindi   : AssemblyAI for recognition and reasoning, Sarvam for speech

SECURITY
--------
This module is the ONLY code that reads `SARVAM_API_KEY`. The key is sent in
the `api-subscription-key` header of the request made HERE, server side, and
never reaches the browser. The browser posts text to our own endpoint and gets
audio back.

Docs: https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/rest-api
  POST https://api.sarvam.ai/text-to-speech
  Header: api-subscription-key: <KEY>
  Body:   text, target_language_code ("hi-IN"), speaker, model, speech_sample_rate
  Returns: {"request_id": "...", "audios": ["<base64 WAV>"]}
"""

from __future__ import annotations

import base64
import logging
import struct
from typing import Any, Dict

import httpx

from .config import AUDIO_SAMPLE_RATE, settings

logger = logging.getLogger("varanasi.sarvam")

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

# The REST endpoint accepts up to 2500 characters per call. Replies are one or
# two sentences, so this is a guard rail rather than a routine limit.
MAX_TEXT_CHARS = 2000

HINDI_LANGUAGE_CODE = "hi-IN"


class SarvamError(RuntimeError):
    """Raised when speech cannot be synthesised. Never carries the key."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def _pcm_from_wav(data: bytes) -> bytes:
    """
    Strip the RIFF/WAVE container and return raw PCM16.

    The browser's player consumes bare PCM16 at 24 kHz, which is exactly what
    Sarvam produces inside the WAV, so unwrapping is all that is needed. The
    data chunk is located properly rather than assuming a 44-byte header,
    because WAV files may carry extra chunks before `data`.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        # Not a container we recognise; assume it is already raw PCM.
        return data

    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        chunk_size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        body = offset + 8
        if chunk_id == b"data":
            return data[body:body + chunk_size]
        offset = body + chunk_size + (chunk_size % 2)   # chunks are word aligned

    raise SarvamError("The speech service returned audio we could not read.")


async def synthesize_hindi(text: str) -> Dict[str, Any]:
    """
    Turn Hindi text into PCM16 audio at the rate the browser player expects.

    Returns {"audio": "<base64 PCM16>", "sample_rate": 24000, "seconds": float}.
    """
    if not settings.has_sarvam_key:
        logger.error(
            "Hindi speech requested but SARVAM_API_KEY is not set. "
            "Add it to .env and restart the server."
        )
        raise SarvamError(
            "Hindi speech is not available right now. "
            "It has not been fully set up on the server yet.",
            status_code=503,
        )

    text = (text or "").strip()
    if not text:
        raise SarvamError("There was no text to speak.", status_code=400)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    payload = {
        "text": text,
        "target_language_code": HINDI_LANGUAGE_CODE,
        "speaker": settings.sarvam_speaker,
        "model": settings.sarvam_model,
        "speech_sample_rate": AUDIO_SAMPLE_RATE,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                SARVAM_TTS_URL,
                json=payload,
                headers={
                    "api-subscription-key": settings.sarvam_api_key,
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        logger.warning("Sarvam TTS timed out")
        raise SarvamError(
            "The Hindi speech service did not respond in time.", status_code=504
        ) from None
    except httpx.HTTPError as exc:
        logger.warning("Sarvam TTS transport error: %s", type(exc).__name__)
        raise SarvamError(
            "Could not reach the Hindi speech service.", status_code=502
        ) from None

    if response.status_code == 401 or response.status_code == 403:
        logger.error("Sarvam rejected the API key (%s).", response.status_code)
        raise SarvamError(
            "The Hindi speech service rejected the server credentials. "
            "An administrator needs to check the key.",
            status_code=503,
        )
    if response.status_code == 429:
        raise SarvamError(
            "The Hindi speech service is rate limited right now.", status_code=429
        )
    if response.status_code >= 400:
        # Log the provider's reason for the operator; keep it out of the browser.
        logger.error(
            "Sarvam TTS failed with %s: %s",
            response.status_code,
            response.text[:300],
        )
        raise SarvamError(
            "The Hindi speech service is temporarily unavailable.", status_code=502
        )

    try:
        body = response.json()
        audios = body["audios"]
        raw = base64.b64decode(audios[0])
    except (ValueError, KeyError, IndexError):
        raise SarvamError(
            "The Hindi speech service returned an unreadable response.",
            status_code=502,
        ) from None

    pcm = _pcm_from_wav(raw)
    return {
        "audio": base64.b64encode(pcm).decode("ascii"),
        "sample_rate": AUDIO_SAMPLE_RATE,
        "seconds": round(len(pcm) / 2 / AUDIO_SAMPLE_RATE, 2),
    }


__all__ = ["synthesize_hindi", "SarvamError", "SARVAM_TTS_URL", "MAX_TEXT_CHARS"]
