"""Provider adapters for the audio briefing pipeline.

The default mode is deterministic and makes no network calls. Live text calls
require both credentials and an explicit raw-content opt-in.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable


class Providers:
    def __init__(self, mode: str = "offline", record_usage: Callable[..., None] | None = None, voice_preset: str = "default"):
        self.mode = mode.lower()
        self.record_usage = record_usage or (lambda *args, **kwargs: None)
        # Set when live extraction raised; preparation then falls back to the local heuristic.
        self.extraction_failed = False
        self.voice_preset = voice_preset

    @staticmethod
    def _usd(input_tokens: int = 0, output_tokens: int = 0, characters: int = 0, prefix: str = "OPENAI") -> float | None:
        """Estimate only when an explicit per-unit rate has been configured."""
        try:
            if characters:
                rate = os.getenv("ELEVENLABS_USD_PER_1000_CHARS")
                return float(rate) * characters / 1000 if rate is not None else None
            in_rate = os.getenv(prefix + "_INPUT_USD_PER_MILLION_TOKENS")
            out_rate = os.getenv(prefix + "_OUTPUT_USD_PER_MILLION_TOKENS")
            if in_rate is None or (output_tokens and out_rate is None): return None
            return float(in_rate) * input_tokens / 1_000_000 + float(out_rate or 0) * output_tokens / 1_000_000
        except ValueError:
            return None

    @property
    def live(self) -> bool:
        return self.mode in {"live", "openai", "elevenlabs"}

    def _allow_raw(self) -> bool:
        return os.getenv("ALLOW_EXTERNAL_PROVIDER_DATA", "").strip().lower() in {"1", "true", "yes"}

    def _client(self):
        if not self.live:
            raise RuntimeError("Live providers are disabled")
        if not self._allow_raw():
            raise RuntimeError("Set ALLOW_EXTERNAL_PROVIDER_DATA=1 to send article text to a provider")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install the openai package to use live providers") from exc
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not configured")
        return OpenAI()

    def embed(self, texts: list[str], *, stage: str) -> list[list[float]]:
        """Callers name the stage so briefing-time calls never count as preparation."""
        if not texts:
            return []
        if not self.live:
            # Stable, inexpensive local feature vectors for offline ranking.
            import hashlib
            vectors = []
            for text in texts:
                vector = [0.0] * 64
                for token in text.lower().split():
                    h = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16)
                    vector[h % 64] += 1.0 if (h >> 8) & 1 else -1.0
                vectors.append(vector)
            return vectors
        client = self._client()
        model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        result = client.embeddings.create(model=model, input=texts)
        in_tokens = getattr(result.usage, "prompt_tokens", 0) or 0
        self.record_usage(stage, mode=self.mode, input_tokens=in_tokens,
                          estimated_usd=self._usd(input_tokens=in_tokens, prefix="OPENAI_EMBEDDING"))
        return [list(item.embedding) for item in result.data]

    def extract(self, articles: list[dict[str, Any]]) -> dict[str, Any]:
        """Extract sourced facts from one bounded, explicitly opted-in batch."""
        if not self.live:
            return {}
        try:
            return self._extract(articles)
        except Exception as exc:
            self.extraction_failed = True
            logging.getLogger(__name__).warning("Live extraction failed (%s)", type(exc).__name__)
            raise

    def _extract(self, articles: list[dict[str, Any]]) -> dict[str, Any]:
        client = self._client()
        model = os.getenv("OPENAI_SMALL_MODEL", "gpt-4o-mini")
        payload =[{k: a.get(k) for k in ("article_id", "title", "source", "publication_date", "language", "body", "url")} for a in articles]
        payload_text = json.dumps(payload, ensure_ascii=False)
        max_chars = max(4000, int(os.getenv("LIQUID_LIVE_BATCH_CHARS", "48000")))
        if len(payload_text) > max_chars:
            raise ValueError("Provider extraction batch exceeds configured character limit")
        max_output_tokens = max(512, int(os.getenv("LIQUID_LIVE_EXTRACTION_OUTPUT_TOKENS", "4000")))
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=max_output_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Extract concise factual news clusters. Return JSON {stories:[{title,topics,publication_date,facts:[{value,unit,evidence,article_id}]}]}. Each fact must contain one reported numeric quantity, the exact unit, an exact evidence span of at most 160 characters containing that quantity and measure, and an existing article_id. Skip dates, IDs, and unsupported quantities. Preserve numeric values and units exactly. Merge duplicate coverage when clearly the same event; keep different reported values as separate claims and do not resolve disagreements. Never invent context or advise."},
                {"role": "user", "content": payload_text},
            ],
        )
        usage = getattr(response, "usage", None)
        in_tokens, out_tokens = getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0
        self.record_usage("story_extraction", mode=self.mode, input_tokens=in_tokens, output_tokens=out_tokens, estimated_usd=self._usd(input_tokens=in_tokens, output_tokens=out_tokens))
        return json.loads(response.choices[0].message.content or "{}")

    def generate_script(self, prompt: str, language: str = "en") -> str:
        """Return a live draft, or "" so the caller keeps its deterministic verified narration."""
        if not self.live:
            return ""
        try:
            return self._generate_script(prompt, language)
        except Exception as exc:
            logging.getLogger(__name__).warning("Script generation failed; using verified template (%s)", type(exc).__name__)
            return ""

    def _generate_script(self, prompt: str, language: str) -> str:
        client = self._client()
        model = os.getenv("OPENAI_BRIEFING_MODEL", "gpt-4.1")
        response = client.chat.completions.create(
            model=model, temperature=0.2,
            messages=[
                {"role": "system", "content": f"Write concise, neutral spoken narration in language code {language}. Use only the supplied verified amounts and units. Add no numbers, number words, IDs, sources, or unsupported claims. Paraphrase in fresh wording. Explain implications for the requested listener expertise and style only when supported by the supplied context. Identify conflicting values clearly and give no financial or other advice."},
                {"role": "user", "content": prompt},
            ],
        )
        usage = getattr(response, "usage", None)
        in_tokens, out_tokens = getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0
        estimate = self._usd(input_tokens=in_tokens, output_tokens=out_tokens, prefix="OPENAI_SCRIPT")
        self.record_usage("script_generation", mode=self.mode, input_tokens=in_tokens, output_tokens=out_tokens,
                          estimated_usd=estimate if estimate is not None else self._usd(input_tokens=in_tokens, output_tokens=out_tokens))
        return response.choices[0].message.content or ""

    def synthesize(self, text: str, output_path: str | Path, language: str = "en") -> None:
        """Create real audio through ElevenLabs. No placeholder audio is written."""
        if not self.live:
            raise RuntimeError("Speech synthesis is unavailable in offline mode")
        if not self._allow_raw():
            raise RuntimeError("Set ALLOW_EXTERNAL_PROVIDER_DATA=1 to send narration to a provider")
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not configured")
        from .config import configured_voice_presets
        presets = configured_voice_presets()
        preset = next((row for row in presets if row["id"] == self.voice_preset), None)
        if preset is None:
            preset = next(row for row in presets if row["id"] == "default")
        voice_id = os.getenv(preset["voice_id_env"]) or os.getenv("ELEVENLABS_VOICE_ID")
        if not voice_id:
            raise RuntimeError("ELEVENLABS_VOICE_ID is not configured")
        try:
            from elevenlabs import ElevenLabs
        except ImportError as exc:
            raise RuntimeError("Install the elevenlabs package to use live speech") from exc
        from .audio import speech_timeout
        client = ElevenLabs(api_key=api_key, timeout=speech_timeout())
        audio = client.text_to_speech.convert(text=text, voice_id=voice_id, model_id=os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2"), output_format="mp3_44100_128")
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as out:
            for chunk in audio:
                out.write(chunk)
        self.record_usage("speech_generation", mode=self.mode, characters=len(text), estimated_usd=self._usd(characters=len(text)))
