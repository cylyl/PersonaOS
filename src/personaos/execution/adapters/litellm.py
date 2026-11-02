"""LiteLLM runtime adapter — connects PersonaOS workers to LiteLLM AI Proxy.

Per ADR 0005: thin runtime adapter translating between LiteLLM / OpenAI-compatible
chat completion endpoints and PersonaOS domain types.
Per ADR 0009: receives frozen WorkerExecutionContext (worker + snapshot profile + task).
Per ADR 0010: returns Result. Kernel owns state machine transitions and leases.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from personaos.execution.context import WorkerExecutionContext
from personaos.execution.result import Result

if TYPE_CHECKING:
    from personaos.execution.adapters.base import Adapter

logger = structlog.get_logger(__name__)
std_logger = logging.getLogger(__name__)


def _redact_token(text: str, token: str | None) -> str:
    """Mask token from text if present."""
    if not token or len(token) < 4:
        return text
    return text.replace(token, "[REDACTED]")


@dataclass(frozen=True)
class LiteLLMConfig:
    """Configuration for LiteLLM runtime adapter."""

    api_base: str = "http://localhost:4000/v1"
    api_key: str = "sk-master-dev-key"
    model: str = "minimax"
    timeout_seconds: float = 300.0
    temperature: float = 0.2
    max_tokens: int = 16384


class LiteLLMAdapter:
    """Real LLM runtime adapter dispatching tasks to LiteLLM / OpenAI-compatible endpoints."""

    def __init__(
        self,
        config: LiteLLMConfig | None = None,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        default_base = (
            api_base
            or os.environ.get("PERSONAOS_LITELLM_API_BASE")
            or os.environ.get("LITELLM_API_BASE")
            or "http://localhost:4000/v1"
        )
        default_key = (
            api_key
            or os.environ.get("PERSONAOS_LITELLM_API_KEY")
            or os.environ.get("LITELLM_API_KEY")
            or "sk-master-dev-key"
        )
        default_model = (
            model
            or os.environ.get("PERSONAOS_LLM_MODEL")
            or "minimax"
        )
        default_max_tokens = (
            max_tokens
            if max_tokens is not None
            else int(os.environ.get("PERSONAOS_MAX_TOKENS", "16384"))
        )

        self.config = config or LiteLLMConfig(
            api_base=default_base,
            api_key=default_key,
            model=default_model,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else 300.0,
            temperature=temperature if temperature is not None else 0.2,
            max_tokens=default_max_tokens,
        )

    def _build_system_prompt(self, context: WorkerExecutionContext) -> str:
        """Construct system persona instruction."""
        skills_str = ", ".join(context.profile.skills) if context.profile.skills else "None specified"
        behavior_str = json.dumps(context.profile.persona.behavior) if context.profile.persona.behavior else "standard"

        return (
            f"You are the embodied PersonaOS Autonomous Agent '{context.worker.name}' (ID: {context.worker.id}).\n"
            f"Role: {context.profile.role}\n"
            f"Profile Version: {context.profile.version}\n"
            f"Registered Skills: {skills_str}\n"
            f"Behavior Directive: {behavior_str}\n\n"
            "You are assigned to execute real software engineering, devops, and operational tasks.\n"
            "Provide concrete, actionable solutions, code implementations, and reasoning.\n"
            "When requested to create files or code, always output the complete, functional code inside markdown code blocks (```python ... ```).\n"
            "Keep your reasoning concise and focused so you produce the complete implementation within the response."
        )

    def _build_user_prompt(self, context: WorkerExecutionContext) -> str:
        """Construct user task prompt."""
        parts: list[str] = [
            f"### Task: {context.task.title}",
            f"Objective: {context.task.objective}",
        ]

        if context.task.input:
            parts.append(f"Input Parameters:\n{json.dumps(context.task.input, indent=2)}")

        if context.upstream_results:
            parts.append(f"Upstream Dependencies Output:\n{json.dumps(context.upstream_results, indent=2)}")

        if context.memory:
            parts.append(f"Contextual Memory:\n{json.dumps(context.memory, indent=2)}")

        if context.recent_experience:
            clean_exp: list[dict[str, Any]] = []
            for item in context.recent_experience:
                if isinstance(item, dict):
                    clean_item = {}
                    for k, v in item.items():
                        if k == "reasoning":
                            continue
                        if isinstance(v, dict):
                            clean_item[k] = {vk: vv for vk, vv in v.items() if vk != "reasoning"}
                        else:
                            clean_item[k] = v
                    clean_exp.append(clean_item)
                else:
                    clean_exp.append(item)
            parts.append(f"Recent Experience:\n{json.dumps(clean_exp, indent=2)}")

        parts.append("\nPlease execute the task and provide your detailed output and results.")
        return "\n\n".join(parts)

    def _handle_workspace_artifacts(
        self,
        context: WorkerExecutionContext,
        response_text: str | None,
    ) -> list[str]:
        """Extract and persist workspace files if requested by task input."""
        written_files: list[str] = []
        task_input = context.task.input or {}

        workspace_dir = task_input.get("workspace")
        target_file = task_input.get("target_file") or task_input.get("file_path")

        if not workspace_dir and not target_file:
            return written_files

        text = response_text or ""
        if not text.strip():
            return written_files

        try:
            base_path = Path(workspace_dir) if workspace_dir else Path.cwd()
            base_path.mkdir(parents=True, exist_ok=True)

            if target_file:
                out_path = base_path / target_file if not Path(target_file).is_absolute() else Path(target_file)
                out_path.parent.mkdir(parents=True, exist_ok=True)

                # If code block exists in response, extract content; else write full text
                code_matches = re.findall(r"```(?:\w+)?\n([\s\S]*?)```", text)
                if code_matches:
                    file_content = max(code_matches, key=len)
                else:
                    unclosed = re.search(r"```(?:\w+)?\n([\s\S]+)", text)
                    if unclosed:
                        file_content = unclosed.group(1).rstrip("` \n\r")
                    else:
                        file_content = text

                out_path.write_text(file_content, encoding="utf-8")
                written_files.append(str(out_path))
        except Exception as exc:
            logger.warning(
                "workspace_artifact_write_failed",
                error=str(exc),
                target_file=target_file,
            )

        return written_files

    async def execute(
        self,
        context: WorkerExecutionContext,
        checkpoint: Any | None = None,
    ) -> Result:
        """Execute task via LiteLLM / OpenAI-compatible chat completions."""
        start_time = time.monotonic()
        system_prompt = self._build_system_prompt(context)
        user_prompt = self._build_user_prompt(context)

        url = f"{self.config.api_base.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "X-User-Id": context.worker.id,
            "X-Dept-Id": context.profile.role,
        }
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        # Keep reasoning focused and bounded for reasoning models
        if "minimax" in self.config.model.lower():
            payload["thinking"] = {"type": "enabled", "budget_tokens": 2048}

        logger.info(
            "litellm_execution_started",
            worker_id=context.worker.id,
            task_id=context.task.id,
            model=self.config.model,
            endpoint=self.config.api_base,
        )

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)

            elapsed_ms = int((time.monotonic() - start_time) * 1000)

            if response.status_code != 200:
                err_text = _redact_token(response.text, self.config.api_key)
                logger.error(
                    "litellm_request_failed",
                    status_code=response.status_code,
                    error=err_text,
                    task_id=context.task.id,
                )
                return Result(
                    status="failed",
                    error=f"LiteLLM HTTP {response.status_code}: {err_text}",
                    output=None,
                )

            data = response.json()
            choices = data.get("choices", [])
            if not choices:
                return Result(
                    status="failed",
                    error="LiteLLM returned empty choices array",
                    output=None,
                )

            choice = choices[0]
            message_obj = choice.get("message", {})
            content = message_obj.get("content") or ""
            reasoning = message_obj.get("reasoning_content") or ""

            # Effective text: prefer content, fallback to reasoning if content is empty
            effective_text = content.strip() if content.strip() else reasoning.strip()

            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

            written_files = self._handle_workspace_artifacts(context, effective_text)

            logger.info(
                "litellm_execution_completed",
                task_id=context.task.id,
                model=self.config.model,
                total_tokens=total_tokens,
                latency_ms=elapsed_ms,
            )

            return Result(
                status="completed",
                output={
                    "summary": f"Completed task '{context.task.title}'",
                    "text": effective_text,
                    "reasoning": reasoning,
                    "model": self.config.model,
                    "worker_id": context.worker.id,
                    "task_id": context.task.id,
                    "role": context.profile.role,
                    "tokens": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                    },
                    "latency_ms": elapsed_ms,
                    "files_written": written_files,
                },
                error=None,
            )

        except httpx.TimeoutException:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            logger.error("litellm_timeout", task_id=context.task.id, timeout_s=self.config.timeout_seconds)
            return Result(
                status="failed",
                error=f"LiteLLM call timed out after {self.config.timeout_seconds}s",
                output=None,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            safe_err = _redact_token(str(exc), self.config.api_key)
            logger.error("litellm_exception", task_id=context.task.id, error=safe_err)
            return Result(
                status="failed",
                error=f"LiteLLM adapter error: {safe_err}",
                output=None,
            )


# Protocol conformity check at module load
_: Adapter = LiteLLMAdapter()
