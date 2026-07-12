from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any


class LLMError(RuntimeError):
    pass


class LLMClient:
    """LLM client supporting OpenAI-compatible APIs.

    Works with: DeepSeek, Qwen, OpenAI, Moonshot, local vLLM/Ollama, etc.

    Configuration via environment variables:
      SCHEDX_LLM_API_BASE  - API base URL (default: https://api.deepseek.com/v1)
      SCHEDX_LLM_API_KEY   - API key
      SCHEDX_LLM_MODEL     - Model name (default: deepseek-v4-pro)
    """

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        config = _load_env_file()
        self.api_base = (
            api_base
            or os.environ.get("SCHEDX_LLM_API_BASE")
            or config.get("SCHEDX_LLM_API_BASE")
            or "https://api.deepseek.com/v1"
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("SCHEDX_LLM_API_KEY") or config.get(
            "SCHEDX_LLM_API_KEY", ""
        )
        self.model = (
            model
            or os.environ.get("SCHEDX_LLM_MODEL")
            or config.get("SCHEDX_LLM_MODEL")
            or "deepseek-v4-pro"
        )

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 2000,
        response_format: dict[str, str] | None = None,
    ) -> str:
        if not self.api_key:
            raise LLMError("LLM not configured: set SCHEDX_LLM_API_KEY")

        url = f"{self.api_base}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise LLMError(f"LLM API error {e.code}: {body[:500]}") from e
        except Exception as e:
            raise LLMError(f"LLM error: {e}") from e

    def chat_json(self, messages: list[dict[str, str]], max_tokens: int = 2000) -> dict[str, Any]:
        content = self.chat(
            messages,
            temperature=0.1,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError("LLM returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise LLMError("LLM JSON response must be an object")
        return result

    def analyze_workload(self, classification: dict, pressure: dict, topology: dict) -> str:
        system = """You are a Linux scheduling expert analyzing workload patterns for SchedX-Agent.
Given workload classification data, system pressure, and CPU topology, provide:
1. A brief analysis of the workload mix
2. Recommended scheduling strategy with justification
3. Specific parameter suggestions (cpu.weight, cpu.max, nice values)
Keep the response concise and actionable. Respond in the same language as the user's data."""

        user = f"""Workload Classification:
{json.dumps(classification, indent=2, ensure_ascii=False)}

System Pressure (PSI):
{json.dumps(pressure, indent=2, ensure_ascii=False)}

CPU Topology:
{json.dumps(topology, indent=2, ensure_ascii=False)}

Analyze this workload and recommend the optimal scheduling strategy."""

        return self.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])

    def generate_report(self, experiment_data: dict) -> str:
        system = """You are a performance analysis expert generating a technical report for SchedX-Agent.
Given experiment results, produce a clear Markdown report with:
1. Executive summary
2. Workload analysis
3. Optimization strategy explanation
4. Results analysis with improvement percentages
5. Conclusions and recommendations
Be professional and data-driven. Respond in the same language as the data."""

        user = f"""Experiment Data:
{json.dumps(experiment_data, indent=2, ensure_ascii=False)}

Generate a comprehensive technical report."""

        return self.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ], max_tokens=4000)

    def diagnose_issue(self, before: dict, after: dict, context: dict) -> str:
        system = """You are a Linux performance debugging expert.
Given before/after experiment results that show degradation, diagnose the likely cause and suggest fixes.
Be specific and actionable."""

        user = f"""Before (no optimization):
{json.dumps(before, indent=2, ensure_ascii=False)}

After (with SchedX optimization):
{json.dumps(after, indent=2, ensure_ascii=False)}

Context:
{json.dumps(context, indent=2, ensure_ascii=False)}

The optimization made things worse. Diagnose why and suggest fixes."""

        return self.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])


def _load_env_file() -> dict[str, str]:
    configured = os.environ.get("SCHEDX_LLM_ENV_FILE")
    candidates = [Path(configured)] if configured else [
        Path("/etc/schedx/llm.env"),
        Path(".schedx/secrets/llm.env"),
    ]
    for path in candidates:
        try:
            result = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.strip().partition("=")
                if separator and key.startswith("SCHEDX_LLM_"):
                    result[key] = value.strip().strip("\"'")
            return result
        except OSError:
            continue
    return {}
