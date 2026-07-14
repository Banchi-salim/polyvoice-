from __future__ import annotations

import httpx


CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"

# Llama 3.1 8B Instant is the only Groq-hosted model that realistically hits
# the PolyVoice 200 ms latency target on cold calls. Swap to a larger model
# (e.g. llama-3.3-70b-versatile) for offline quality testing.
DEFAULT_MODEL = "llama-3.1-8b-instant"

SYSTEM_PROMPT_TEMPLATE = (
    "You are replying on WhatsApp with a short voice note. "
    "Reply in {language_name} only — no translations, brackets, labels, or commentary. "
    "Speak in first person, casually, the way a friend would talk into their phone: "
    "one short sentence, natural rhythm, no emojis, no exclamation marks unless the user used one. "
    "Follow the latest message over older context; do not argue, lecture, or revive old topics. "
    "Keep it under {max_words} words. Do not start with 'Sure', 'Of course', 'I think', "
    "or any filler. Do not describe the audio, do not say you are an assistant."
)

LANGUAGE_DISPLAY_NAMES = {
    "en": "English",
    "fr": "French",
    "yo": "Yoruba",
    "ha": "Hausa",
    "ig": "Igbo",
}


class GroqConversationProvider:
    """Groq Chat Completions provider (OpenAI-compatible)."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        if not api_key:
            raise ValueError("Groq API key is required (set GROQ_API_KEY).")
        self._api_key = api_key
        self._model = model

    async def respond(self, text: str, language: str, max_words: int) -> str:
        language_name = LANGUAGE_DISPLAY_NAMES.get(language, language)
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            language_name=language_name,
            max_words=max_words,
        )

        # `max_tokens` is a hard ceiling on the response length. Setting it to
        # 2x the word target leaves headroom for tokenization overhead and
        # non-Latin scripts where one word can span several tokens.
        max_tokens = max(8, max_words * 2)
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.4,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(CHAT_COMPLETIONS_URL, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()

        choices = payload.get("choices", [])
        if not choices:
            raise RuntimeError(f"Groq returned no choices for input: {text!r}")
        return choices[0].get("message", {}).get("content", "").strip()
