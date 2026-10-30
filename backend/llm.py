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
    "gemini": {"label": "Google Gemini", "model": "", "needsKey": True,      # "" = newest Flash model, found automatically
               "keyUrl": "https://aistudio.google.com/app/apikey", "note": "Free API key, no card needed."},
    "claude": {"label": "Claude (Anthropic)", "model": "claude-haiku-5-5", "needsKey": True,
               "keyUrl": "https://console.anthropic.com/settings/keys", "note": "Paid per use — needs credit on your Anthropic account."},
    "openai": {"label": "Other (OpenAI-compatible)", "model": "", "needsKey": False,
               "keyUrl": "", "note": "Ollama on the Pi, Groq, OpenRouter, OpenAI …", "url": "http://127.0.0.1:11434/v1"},
}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODELS_URL = "https://api.anthropic.com/v1/models"
CLOUD_TIMEOUT = 120
LOCAL_TIMEOUT = 600   # a model running on the Pi itself is slow


class LlmError(Exception):
    pass


class ModelGone(LlmError):
    """The provider doesn't offer this model (any more) — e.g. Google retired it."""


class KeyRefused(LlmError):
    """The provider says the API key is wrong (or not allowed)."""


class CutShort(LlmError):
    """The answer stopped at the token limit before any text came out (thinking models use
    part of the limit to think first)."""


_SKIP_GEMINI = re.compile(r"lite|tts|live|image|audio|embed|robotic|native|computer|exp|learnlm|thinking|vision|aqa", re.I)


def pick_model(provider: str, models: list[dict]) -> str:
    """The best everyday model from the provider's list: Gemini's newest regular Flash
    (stable before preview), or Claude's newest Haiku. "" if nothing suitable."""
    if provider == "gemini":
        names = [m["name"].removeprefix("models/") for m in models
                 if "generateContent" in (m.get("supportedGenerationMethods") or []) and m.get("name")]

        def score(n):
            v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            return ("preview" not in n, float(v.group(1)) if v else 0.0, -len(n))
        for pool in ([n for n in names if "flash" in n and not _SKIP_GEMINI.search(n)],
                     [n for n in names if "flash" in n and "lite" in n and not re.search(r"tts|live|image|audio", n)],
                     [n for n in names if n.startswith("gemini") and not _SKIP_GEMINI.search(n)]):
            if pool:
                return max(pool, key=score)
        return ""
    if provider == "claude":
        for family in ("haiku", "sonnet"):
            pool = [m for m in models if family in str(m.get("id", ""))]
            if pool:
                return max(pool, key=lambda m: (str(m.get("created_at", "")), str(m["id"])))["id"]
    return ""


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
        self._thinking_refused: set[str] = set()   # models that rejected the "think less" setting

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
            "autoTips": bool(saved.get("autoTips", True)),
            "dailyHour": int(saved.get("dailyHour", 6)) if str(saved.get("dailyHour", 6)).isdigit() else 6,
        }

    @property
    def configured(self) -> bool:
        s = self.settings()
        if not s["provider"] or (s["provider"] == "openai" and not s["model"]):
            return False      # Gemini/Claude can find their model by themselves
        return bool(s["apiKey"]) or not PROVIDERS[s["provider"]]["needsKey"]

    def public_status(self) -> dict:
        s = self.settings()
        key = s["apiKey"]
        return {
            "configured": self.configured, "provider": s["provider"],
            "providerLabel": PROVIDERS.get(s["provider"], {}).get("label", ""),
            "model": s["model"], "url": s["url"] if s["provider"] == "openai" else "",
            "keyHint": f"…{key[-4:]}" if len(key) >= 8 else ("set" if key else ""),
            "daily": s["daily"], "dailyHour": s["dailyHour"], "autoTips": s["autoTips"],
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
        model = (model or "").strip().removeprefix("models/") or PROVIDERS[provider]["model"]
        url = (url or "").strip().rstrip("/")
        if PROVIDERS[provider]["needsKey"] and len(api_key) < 12:
            raise LlmError("Paste the API key (it's long — copy all of it).")
        if (model or provider == "openai") and not re.fullmatch(r"[A-Za-z0-9._:/@-]{2,120}", model):
            raise LlmError("Type the model name, e.g. " + (PROVIDERS[provider]["model"] or "llama3.2:3b") + ".")
        if provider == "openai":
            url = url or PROVIDERS["openai"]["url"]
            if urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
                raise LlmError("The address must start with http:// or https://")
        else:
            url = ""
        data = {"provider": provider, "apiKey": api_key, "model": model, "url": url,
                "daily": old.get("daily", True) if daily is None else bool(daily),
                "dailyHour": old.get("dailyHour", 6) if daily_hour is None else max(0, min(23, int(daily_hour))),
                "autoTips": old.get("autoTips", True)}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass

    def _set_model(self, model: str) -> None:
        data = self._saved()
        if not data:          # settings from the config file: remember the model for this run only
            os.environ["AUREOCLEAN_LLM_MODEL"] = model
            return
        data["model"] = model
        self.file.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass

    def list_models(self) -> list[dict]:
        """The models this key may use (Gemini / Claude)."""
        s = self.settings()
        try:
            if s["provider"] == "gemini":
                resp = self.http.get(GEMINI_MODELS_URL, headers={"x-goog-api-key": s["apiKey"]},
                                     params={"pageSize": 1000}, timeout=30)
                key = "models"
            elif s["provider"] == "claude":
                resp = self.http.get(CLAUDE_MODELS_URL, headers={"x-api-key": s["apiKey"], "anthropic-version": "2023-06-01"},
                                     params={"limit": 100}, timeout=30)
                key = "data"
            else:
                return []
        except requests.RequestException as exc:
            raise LlmError(f"Can't reach {PROVIDERS[s['provider']]['label']}. Check the Pi's internet.") from exc
        if resp.status_code in (401, 403):
            raise KeyRefused("The AI provider refused the API key. Check it in AI settings (or make a new key).")
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return [m for m in (data.get(key) or []) if isinstance(m, dict)] if isinstance(data, dict) else []

    def set_daily(self, daily: Optional[bool] = None, hour: Optional[int] = None, auto_tips: Optional[bool] = None) -> None:
        data = self._saved()
        if daily is not None:
            data["daily"] = bool(daily)
        if hour is not None:
            data["dailyHour"] = max(0, min(23, int(hour)))
        if auto_tips is not None:
            data["autoTips"] = bool(auto_tips)
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

    def ask(self, system: str, user: str, max_tokens: int = 2500, fast: bool = False) -> str:
        """Send one question; returns the model's text (asked to be JSON).

        Providers retire models now and then. If the model is gone (or none was chosen),
        the newest suitable one is looked up, saved, and the question is sent again.

        fast=True: for quick jobs (tips, form filling, questions, suggestions) the model is asked to
        think as little as possible — Gemini's thinking is most of its waiting time."""
        self._fast = fast
        if not self.configured:
            raise LlmError("The AI isn't set up yet (Reports → Insights → AI settings).")
        self.switched: Optional[tuple[str, str]] = None
        s = self.settings()
        if s["provider"] == "openai":
            return self._ask_roomy(system, user, max_tokens)
        if not s["model"]:
            self._replace_model("")
            return self._ask_roomy(system, user, max_tokens)
        try:
            return self._ask_roomy(system, user, max_tokens)
        except ModelGone:
            self._replace_model(s["model"])
            return self._ask_roomy(system, user, max_tokens)

    def _ask_roomy(self, system: str, user: str, max_tokens: int) -> str:
        """Thinking models spend part of the token limit thinking; if nothing was left for
        the answer, ask once more with much more room."""
        try:
            return self._ask_once(system, user, max_tokens)
        except CutShort:
            return self._ask_once(system, user, max(max_tokens * 4, 16384))

    def _replace_model(self, old: str) -> str:
        provider = self.settings()["provider"]
        new = pick_model(provider, self.list_models())
        if not new or new == old:
            who = "Google" if provider == "gemini" else "Anthropic"
            raise LlmError((f"{who} doesn't offer “{old}” any more, and" if old else f"{who}:") +
                           " no suitable model was found for this key. Type a model name from "
                           + ("AI Studio" if provider == "gemini" else "the Anthropic console") + " in AI settings.")
        self._set_model(new)
        self.switched = (old, new)
        return new

    @staticmethod
    def thinking_config(model: str) -> Optional[dict]:
        """Gemini's "think less" setting: none for 2.5 Flash models, low for Gemini 3+; None if unknown."""
        v = re.search(r"gemini-(\d+(?:\.\d+)?)", model or "")
        if not v:
            return None
        ver = float(v.group(1))
        if ver >= 3:
            return {"thinkingLevel": "low"}
        if ver >= 2.5 and "flash" in model:
            return {"thinkingBudget": 0}
        return None

    def _ask_once(self, system: str, user: str, max_tokens: int) -> str:
        s = self.settings()
        provider, key, model = s["provider"], s["apiKey"], s["model"]
        try:
            if provider == "gemini":
                gen = {"temperature": 0.3, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}
                think = self.thinking_config(model) if getattr(self, "_fast", False) and model not in self._thinking_refused else None
                if think:
                    gen["thinkingConfig"] = think
                body = {"systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
                headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
                resp = self.http.post(GEMINI_URL.format(model=model), headers=headers, json=body, timeout=self.timeout())
                if think and resp.status_code == 400 and "think" in str(getattr(resp, "text", "") or resp.json()).lower():
                    self._thinking_refused.add(model)       # this model doesn't take it: ask normally from now on
                    gen.pop("thinkingConfig", None)
                    resp = self.http.post(GEMINI_URL.format(model=model), headers=headers, json=body, timeout=self.timeout())
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
        msg = ""
        if status >= 400 and isinstance(data, dict):
            err = data.get("error")
            msg = (err.get("message") if isinstance(err, dict) else str(err or "")) or ""
        if status in (401, 403) or (status == 400 and re.search(r"api.?key", msg, re.I)):
            raise KeyRefused("The AI provider refused the API key. Check it in AI settings (or make a new key).")
        if status == 404 or (status == 400 and re.search(r"not (found|supported)|no longer|deprecat|retired|unknown model|invalid model", msg, re.I)):
            raise ModelGone("That model isn't offered any more. Check the model in AI settings.")
        if status == 429:
            raise LlmError("The AI's free limit is used up for now (too many requests). Try again later.")
        if status >= 400:
            raise LlmError(f"The AI provider answered with an error ({status}). {msg[:160]}".strip())
        try:
            if provider == "gemini":
                cand = (data.get("candidates") or [{}])[0]
                text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
                if not text and cand.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
                    raise LlmError("The AI declined to answer this one. Try asking differently.")
                if not text.strip() and cand.get("finishReason") in ("MAX_TOKENS", None, ""):
                    raise CutShort("The AI ran out of room before answering.")
            elif provider == "claude":
                text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
                if not text.strip() and data.get("stop_reason") == "max_tokens":
                    raise CutShort("The AI ran out of room before answering.")
            else:
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                if not text.strip() and choice.get("finish_reason") == "length":
                    raise CutShort("The AI ran out of room before answering.")
        except (AttributeError, IndexError, TypeError) as exc:
            raise LlmError("The AI's answer couldn't be read.") from exc
        if not text.strip():
            raise LlmError("The AI gave an empty answer. Try again.")
        return text
