"""The AI model behind AureoClean insights: one small client for a few providers.

* ``gemini``  - Google Gemini. Free API key (no card) from https://aistudio.google.com/app/apikey
* ``claude``  - Claude (Anthropic). Paid API key from https://console.anthropic.com/settings/keys
* ``openai``  - anything that speaks the OpenAI chat API: a model running on the Pi with
                Ollama (http://127.0.0.1:11434/v1, no key), Groq, OpenRouter, OpenAI …

Set up in the app (Reports → Insights → AI settings) or in the config file:

    AUREOCLEAN_LLM_PROVIDER=gemini
    AUREOCLEAN_LLM_API_KEY=...
    # optional: AUREOCLEAN_LLM_MODEL=..., AUREOCLEAN_LLM_URL=... (openai provider)

The key is kept only on this Pi (~/.config/aureoclean/llm.json, readable by this user only).

Safety: the model only reads data and writes suggestions. It never decides or sends alerts —
emergencies are handled by fixed rules when a report or reading arrives.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

PROVIDERS = {
    "gemini": {"label": "Google Gemini", "model": "gemini-2.5-flash", "needsKey": True,
               "keyUrl": "https://aistudio.google.com/app/apikey", "note": "Free API key, no card needed."},
    "claude": {"label": "Claude (Anthropic)", "model": "claude-haiku-5-5", "needsKey": True,
               "keyUrl": "https://console.anthropic.com/settings/keys", "note": "Paid per use — needs credit on your Anthropic account."},
    "openai": {"label": "Other (OpenAI-compatible)", "model": "", "needsKey": False,
               "keyUrl": "", "note": "Ollama on the Pi, Groq, OpenRouter, OpenAI …", "url": "http://127.0.0.1:11434/v1"},
}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLOUD_TIMEOUT = 120
LOCAL_TIMEOUT = 600   # a model running on the Pi itself is slow


class LlmError(Exception):
    pass


def is_local(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")


def parse_json(text: str) -> Optional[dict]:
    """The model's answer as a dict (it may wrap the JSON in ``` fences or add words)."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class LlmClient:
    def __init__(self, settings_file: Path, http=None):
        self.file = settings_file
        self.http = http or requests.Session()

    # ------------------------------------------------------------------ settings
    def _saved(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def settings(self) -> dict:
        saved = self._saved()
        provider = (saved.get("provider") or os.environ.get("AUREOCLEAN_LLM_PROVIDER", "")).strip().lower()
        info = PROVIDERS.get(provider, {})
        return {
            "provider": provider if provider in PROVIDERS else "",
            "apiKey": saved.get("apiKey") or os.environ.get("AUREOCLEAN_LLM_API_KEY", "").strip(),
            "model": saved.get("model") or os.environ.get("AUREOCLEAN_LLM_MODEL", "").strip() or info.get("model", ""),
            "url": saved.get("url") or os.environ.get("AUREOCLEAN_LLM_URL", "").strip() or info.get("url", ""),
            "daily": bool(saved.get("daily", True)),
            "dailyHour": int(saved.get("dailyHour", 6)) if str(saved.get("dailyHour", 6)).isdigit() else 6,
        }

    @property
    def configured(self) -> bool:
        s = self.settings()
        if not s["provider"] or not s["model"]:
            return False
        return bool(s["apiKey"]) or not PROVIDERS[s["provider"]]["needsKey"]

    def public_status(self) -> dict:
        s = self.settings()
        key = s["apiKey"]
        return {
            "configured": self.configured, "provider": s["provider"],
            "providerLabel": PROVIDERS.get(s["provider"], {}).get("label", ""),
            "model": s["model"], "url": s["url"] if s["provider"] == "openai" else "",
            "keyHint": f"…{key[-4:]}" if len(key) >= 8 else ("set" if key else ""),
            "daily": s["daily"], "dailyHour": s["dailyHour"],
            "providers": [{"id": k, **{x: v[x] for x in ("label", "model", "needsKey", "keyUrl", "note")},
                           "url": v.get("url", "")} for k, v in PROVIDERS.items()],
        }

    def save(self, provider: str, api_key: str = "", model: str = "", url: str = "",
             daily: Optional[bool] = None, daily_hour: Optional[int] = None) -> None:
        provider = (provider or "").strip().lower()
        if provider not in PROVIDERS:
            raise LlmError("Choose Gemini, Claude or Other.")
        old = self._saved()
        api_key = (api_key or "").strip() or (old.get("apiKey", "") if old.get("provider") == provider else "")
        model = (model or "").strip() or PROVIDERS[provider]["model"]
        url = (url or "").strip().rstrip("/")
        if PROVIDERS[provider]["needsKey"] and len(api_key) < 12:
            raise LlmError("Paste the API key (it's long — copy all of it).")
        if not re.fullmatch(r"[A-Za-z0-9._:/@-]{2,120}", model):
            raise LlmError("Type the model name, e.g. " + (PROVIDERS[provider]["model"] or "llama3.2:3b") + ".")
        if provider == "openai":
            url = url or PROVIDERS["openai"]["url"]
            if urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
                raise LlmError("The address must start with http:// or https://")
        else:
            url = ""
        data = {"provider": provider, "apiKey": api_key, "model": model, "url": url,
                "daily": old.get("daily", True) if daily is None else bool(daily),
                "dailyHour": old.get("dailyHour", 6) if daily_hour is None else max(0, min(23, int(daily_hour)))}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass

    def set_daily(self, daily: bool, hour: Optional[int] = None) -> None:
        data = self._saved()
        data["daily"] = bool(daily)
        if hour is not None:
            data["dailyHour"] = max(0, min(23, int(hour)))
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.file.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ asking
    def timeout(self) -> int:
        s = self.settings()
        return LOCAL_TIMEOUT if s["provider"] == "openai" and is_local(s["url"]) else CLOUD_TIMEOUT

    def ask(self, system: str, user: str, max_tokens: int = 2500) -> str:
        """Send one question; returns the model's text (asked to be JSON)."""
        s = self.settings()
        if not self.configured:
            raise LlmError("The AI isn't set up yet (Reports → Insights → AI settings).")
        provider, key, model = s["provider"], s["apiKey"], s["model"]
        try:
            if provider == "gemini":
                resp = self.http.post(
                    GEMINI_URL.format(model=model), headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={"systemInstruction": {"parts": [{"text": system}]},
                          "contents": [{"role": "user", "parts": [{"text": user}]}],
                          "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens,
                                               "responseMimeType": "application/json"}},
                    timeout=self.timeout())
            elif provider == "claude":
                resp = self.http.post(
                    CLAUDE_URL, headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": model, "max_tokens": max_tokens, "temperature": 0.3, "system": system,
                          "messages": [{"role": "user", "content": user}]},
                    timeout=self.timeout())
            else:
                headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
                resp = self.http.post(
                    f"{s['url']}/chat/completions", headers=headers,
                    json={"model": model, "temperature": 0.3, "max_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                    timeout=self.timeout())
        except requests.Timeout as exc:
            raise LlmError("The AI took too long to answer. Try again" +
                           (" — a model on the Pi can take several minutes." if self.timeout() == LOCAL_TIMEOUT else ".")) from exc
        except requests.RequestException as exc:
            where = "the AI model on " + s["url"] if provider == "openai" else PROVIDERS[provider]["label"]
            raise LlmError(f"Can't reach {where}. Check the Pi's internet" +
                           (" or that Ollama is running." if provider == "openai" else ".")) from exc
        return self._text(provider, resp)

    @staticmethod
    def _text(provider: str, resp) -> str:
        status = resp.status_code
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if status in (401, 403):
            raise LlmError("The AI provider refused the API key. Check it in AI settings (or make a new key).")
        if status == 404:
            raise LlmError("That model name wasn't found. Check the model in AI settings.")
        if status == 429:
            raise LlmError("The AI's free limit is used up for now (too many requests). Try again later.")
        if status >= 400:
            msg = ""
            if isinstance(data, dict):
                err = data.get("error")
                msg = (err.get("message") if isinstance(err, dict) else str(err or "")) or ""
            raise LlmError(f"The AI provider answered with an error ({status}). {msg[:160]}".strip())
        try:
            if provider == "gemini":
                cand = (data.get("candidates") or [{}])[0]
                text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
                if not text and cand.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
                    raise LlmError("The AI declined to answer this one. Try asking differently.")
            elif provider == "claude":
                text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
            else:
                text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except (AttributeError, IndexError, TypeError) as exc:
            raise LlmError("The AI's answer couldn't be read.") from exc
        if not text.strip():
            raise LlmError("The AI gave an empty answer. Try again.")
        return text
