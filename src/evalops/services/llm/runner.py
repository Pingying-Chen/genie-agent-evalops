"""Run a skill against a Databricks serving endpoint (OpenAI-compatible chat API), async.

- Structured output: the skill's output model becomes a strict JSON schema.
- Model output shape: some models (gpt-oss) return `content` as a list of parts
  (`reasoning` summaries, then `text`); others return a string. Both are handled; the
  reasoning summary is kept as evidence for reviewers.
- One retry when the reply does not validate; then the call is reported as an error, never
  silently turned into a verdict.
- A semaphore caps concurrent calls (serving endpoints rate-limit).
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from evalops.clients.auth import Auth
from evalops.clients.http import request_json
from evalops.services.llm.skills import Skill

SERVICE = "serving"


@dataclass(frozen=True)
class SkillResult:
    output: BaseModel | None
    error: str | None
    skill: str  # name@version
    model: str  # the endpoint called
    reasoning: str
    latency_s: float
    tokens: int
    served_model: str = ""  # what the endpoint says it ran: an endpoint name can change model underneath


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}))  # strict mode wants every key listed
    return schema


def split_content(content: Any) -> tuple[str, str]:
    """(text, reasoning) from a chat message `content` that is a string or a list of parts."""
    if isinstance(content, str):
        return content, ""
    text, reasoning = [], []
    for part in content or []:
        if part.get("type") == "text":
            text.append(part.get("text", ""))
        elif part.get("type") == "reasoning":
            reasoning.extend(s.get("text", "") for s in part.get("summary", []))
    return "".join(text), " ".join(reasoning)


def parse_output(text: str, model: type[BaseModel]) -> BaseModel:
    """Validate the reply; tolerates a ```json fence around it."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    return model.model_validate(json.loads(cleaned))


class SkillRunner:
    def __init__(self, http: httpx.AsyncClient, auth: Auth, model: str, *, max_concurrent: int = 4):
        self._http = http
        self._auth = auth
        self.model = model
        self._slots = asyncio.Semaphore(max_concurrent)

    async def run(self, skill: Skill, data: BaseModel, *, temperature: float | None = None) -> SkillResult:
        prompt = skill.render(data)
        model = skill.endpoint or self.model
        messages = [{"role": "user", "content": prompt}]
        if effort := skill.model_params.get("reasoning_effort"):
            # gpt-oss and similar reasoning models read their effort from the system prompt.
            messages.insert(0, {"role": "system", "content": f"Reasoning: {effort}"})
        body = {
            "messages": messages,
            "temperature": skill.model_params.get("temperature", 0) if temperature is None else temperature,
            "max_tokens": skill.model_params.get("max_tokens", 1000),
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": skill.name, "schema": _schema(skill.output_schema), "strict": True},
            },
        }
        started = time.monotonic()
        tokens, reasoning, error, served = 0, "", None, ""
        async with self._slots:
            for attempt in (1, 2):
                response = await request_json(
                    self._http,
                    self._auth,
                    "POST",
                    f"/serving-endpoints/{model}/invocations",
                    service=SERVICE,
                    json=body,
                    # Pay-per-token endpoints have low QPS limits (Free Edition especially):
                    # back off longer than for control-plane APIs.
                    max_attempts=8,
                    base_delay=2.0,
                )
                tokens += int((response.get("usage") or {}).get("total_tokens", 0))
                served = str(response.get("model") or "")
                text, reasoning = split_content(response["choices"][0]["message"].get("content"))
                try:
                    output = parse_output(text, skill.output_schema)
                    return SkillResult(
                        output, None, skill.ref, model, reasoning, time.monotonic() - started, tokens, served
                    )
                except (ValidationError, json.JSONDecodeError) as exc:
                    error = f"attempt {attempt}: invalid output: {exc}"[:500]
        return SkillResult(
            None, error, skill.ref, model, reasoning, time.monotonic() - started, tokens, served
        )
