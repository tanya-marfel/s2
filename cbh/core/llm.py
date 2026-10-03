"""LLM client that can route through the condense.chat proxy.

Two call paths, selected by configuration:

    direct    -> Gemini's OpenAI-compatible endpoint
    condense  -> https://api.condense.chat/openai/v1, with X-Condense-Upstream-Url
                 pointing back at Gemini, so one call exercises both partners.

Token usage is accumulated so the saving from compression can be measured
directly rather than taken on trust.
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

from .config import get_flag, get_secret, get_setting

CONDENSE_BASE = "https://api.condense.chat/openai/v1"
GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


class Usage:
    """Running token totals for one run."""

    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.wall_seconds = 0.0

    def add(self, usage: Dict[str, Any], seconds: float) -> None:
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        self.wall_seconds += seconds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "wall_seconds": round(self.wall_seconds, 2),
        }


class LLMClient:
    def __init__(
        self,
        use_condense: Optional[bool] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.model = model or get_setting("GEMINI_MODEL", "gemini-2.5-pro")
        self.gemini_key = get_secret("GEMINI_API_KEY")
        self.use_condense = (
            get_flag("USE_CONDENSE", True) if use_condense is None else use_condense
        )
        # Groups requests so condense's dashboard reports per-session savings.
        self.session_id = session_id or str(uuid.uuid4())
        self.usage = Usage()
        self.condense_key = get_secret("CONDENSE_API_KEY", required=False)
        if self.use_condense and not self.condense_key:
            self.use_condense = False

    def _endpoint(self) -> str:
        base = CONDENSE_BASE if self.use_condense else GEMINI_OPENAI_BASE
        return base + "/chat/completions"

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Upstream credential: condense forwards this and never stores it.
            "Authorization": "Bearer %s" % self.gemini_key,
        }
        if self.use_condense:
            headers["X-Condense-Auth-Token"] = self.condense_key or ""
            headers["X-Condense-Session-Id"] = self.session_id
            upstream = get_setting("CONDENSE_UPSTREAM_URL", GEMINI_OPENAI_BASE)
            if upstream:
                headers["X-Condense-Upstream-Url"] = upstream
        return headers

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_retries: int = 2,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        last_error = None
        for attempt in range(max_retries + 1):
            started = time.time()
            try:
                resp = requests.post(
                    self._endpoint(),
                    headers=self._headers(),
                    data=json.dumps(payload),
                    timeout=120,
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            elapsed = time.time() - started

            # The upstream-override header needs an entitlement on the condense
            # account. Rather than fail a live demo, fall back to calling Gemini
            # directly and carry on.
            if resp.status_code == 403 and self.use_condense:
                self.use_condense = False
                continue

            if resp.status_code >= 400:
                last_error = RuntimeError(
                    "%s %s" % (resp.status_code, resp.text[:300])
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise last_error

            body = resp.json()
            self.usage.add(body.get("usage", {}) or {}, elapsed)
            return body["choices"][0]["message"]["content"]

        raise RuntimeError("LLM call failed after retries: %s" % last_error)

    def report(self) -> Dict[str, Any]:
        return {
            "route": "condense -> gemini" if self.use_condense else "gemini (direct)",
            "model": self.model,
            "session_id": self.session_id,
            "usage": self.usage.to_dict(),
        }
