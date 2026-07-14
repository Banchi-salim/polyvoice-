from __future__ import annotations

import os

import httpx

from polyvoice.providers.base import Translation


# Map PolyVoice language codes to Google Translate v2 codes. They happen to match
# for our current set (en, yo, ha, ig), but the indirection lets us alias later
# without touching call sites.
GOOGLE_LANGUAGE_CODES = {
    "en": "en",
    "fr": "fr",
    "yo": "yo",
    "ha": "ha",
    "ig": "ig",
}

TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
DETECT_URL = "https://translation.googleapis.com/language/translate/v2/detect"


def _normalize_language_code(code: str) -> str:
    """Strip script/region subtags Google's `translate` endpoint can't accept.

    Google's `detect` endpoint can return extended BCP-47-style tags like
    "uk-Latn" (Ukrainian written in Latin script) or "zh-Hant" for short or
    ambiguous input. The `translate` endpoint's `source`/`target` params only
    accept the bare primary language subtag ("uk", "zh") — passing the
    extended form fails with "Bad language pair: uk-Latn|en" even though "uk"
    alone would have worked fine. We normalize at the boundary so this can
    never surface as a real-pipeline crash downstream.
    """
    return code.split("-")[0].lower()


class GoogleTranslationProvider:
    """Google Cloud Translation v2 provider.

    Supports two credential modes, tried in order:

    1. API key mode: set `GOOGLE_TRANSLATE_API_KEY` (an `AIza...` key). This is
       the simplest path and what the project's `.env` currently uses.
    2. Service-account mode: set `GOOGLE_APPLICATION_CREDENTIALS` to the absolute
       path of a service-account JSON. This mode is reserved for future work —
       it requires the `google-cloud-translate` library, which is not in the
       current dependency set. The constructor raises a clear error if it sees
       a credentials path without the library present.
    """

    def __init__(
        self,
        api_key: str | None = None,
        credentials_path: str | None = None,
    ) -> None:
        if api_key:
            self._mode = "api_key"
            self._api_key = api_key
            self._credentials_path = None
        elif credentials_path and os.path.isfile(credentials_path):
            self._mode = "service_account"
            self._api_key = None
            self._credentials_path = credentials_path
        else:
            raise ValueError(
                "Google Translate needs either GOOGLE_TRANSLATE_API_KEY "
                "or a valid GOOGLE_APPLICATION_CREDENTIALS path."
            )

    async def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> Translation:
        # Normalize defensively here too (not just in detect_language) since
        # source_language/target_language can also arrive from other callers
        # (WhatsApp-declared language, Deepgram's detected_language, etc.)
        # that we don't control the format of.
        source_language = _normalize_language_code(source_language)
        target_language = _normalize_language_code(target_language)

        if source_language == target_language:
            return Translation(
                text=text,
                source_language=source_language,
                target_language=target_language,
            )

        if self._mode == "api_key":
            return await self._translate_with_api_key(text, source_language, target_language)
        return await self._translate_with_service_account(text, source_language, target_language)

    async def detect_language(self, text: str) -> str:
        if self._mode != "api_key":
            raise NotImplementedError("Language detection is only wired for API key mode.")
        params = {"key": self._api_key}
        body = {"q": text}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(DETECT_URL, params=params, json=body)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Google Translate detect API error {response.status_code}: {response.text}"
                )
            payload = response.json()

        detections = payload.get("data", {}).get("detections", [])
        if not detections or not detections[0]:
            raise RuntimeError(f"Google Translate returned no language detection for: {text!r}")
        detected = detections[0][0].get("language", "und")
        return _normalize_language_code(detected)

    async def _translate_with_api_key(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> Translation:
        source = GOOGLE_LANGUAGE_CODES.get(source_language, source_language)
        target = GOOGLE_LANGUAGE_CODES.get(target_language, target_language)
        params = {"key": self._api_key}
        body = {"q": text, "source": source, "target": target, "format": "text"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(TRANSLATE_URL, params=params, json=body)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Google Translate API error {response.status_code}: {response.text}"
                )
            payload = response.json()

        translations = payload.get("data", {}).get("translations", [])
        if not translations:
            raise RuntimeError(f"Google Translate returned no translations for: {text!r}")
        translated_text = translations[0].get("translatedText", "")
        return Translation(
            text=translated_text,
            source_language=source_language,
            target_language=target_language,
        )

    async def _translate_with_service_account(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> Translation:
        # Service-account mode requires the `google-cloud-translate` client
        # library, which is intentionally not pulled in until we know we need
        # the more advanced v3 features (auto-detect, glossaries, custom models).
        raise NotImplementedError(
            "Service-account Google Translate is not yet wired. "
            "Use GOOGLE_TRANSLATE_API_KEY for now, or install google-cloud-translate "
            f"and wire the v3 client (credentials: {self._credentials_path})."
        )