"""Evaluation-only accounting, including context-summary calls and failures."""

import time
from contextlib import aclosing
from pathlib import Path

from codehead.errors import ModelError
from codehead.llm.base import LLMProvider
from codehead.models import LLMResponseCompleted
from scripts.swebench.common import append_json, save_json


class BudgetExceeded(ModelError):
    """A request was refused before contacting the model."""


class MeteredProvider(LLMProvider):
    def __init__(self, provider: LLMProvider, settings: dict, output: Path, spent=0.0):
        self.provider = provider
        self.settings = settings
        self.output = output
        self.spent = spent
        self.calls = []
        self.budget_reason = None
        self._save()

    def totals(self) -> dict:
        return {
            "requests": len(self.calls),
            "summary_requests": sum(c["summary"] for c in self.calls),
            "missing_usage": sum(c["usage"] is None for c in self.calls),
            "input_tokens": sum(
                (c["usage"] or {}).get("input_tokens", 0) for c in self.calls
            ),
            "output_tokens": sum(
                (c["usage"] or {}).get("output_tokens", 0) for c in self.calls
            ),
            "estimated_cost_cny": sum(c["estimated_cost_cny"] for c in self.calls),
            "prior_cost_cny": self.spent,
            "budget_reason": self.budget_reason,
        }

    def _save(self):
        save_json(self.output / "usage.json", {**self.totals(), "calls": self.calls})

    async def stream(self, request):
        cfg = self.settings
        # One UTF-8 byte per token is deliberately more conservative than the
        # core context heuristic. Reserve a full input budget even for short calls.
        input_bound = max(
            cfg["context_budget"], len(request.model_dump_json().encode("utf-8"))
        )
        reserve = (
            input_bound * cfg["input_cny_per_million"]
            + cfg["max_output_tokens"] * cfg["output_cny_per_million"]
        ) / 1_000_000
        cost = self.spent + self.totals()["estimated_cost_cny"]
        if len(self.calls) >= cfg["max_requests"]:
            self.budget_reason = "total_request_limit"
        elif cost >= cfg["stop_cost_cny"]:
            self.budget_reason = "cost_stop_threshold"
        elif cost + reserve > cfg["max_cost_cny"]:
            self.budget_reason = "next_request_would_exceed_budget"
        if self.budget_reason:
            self._save()
            raise BudgetExceeded(self.budget_reason)
        record = {
            "request": len(self.calls) + 1,
            # Core normal requests advertise tools; summary requests do not.
            "summary": not bool(request.tools),
            "usage": None,
            "input_token_bound": input_bound,
            "output_token_bound": cfg["max_output_tokens"],
            "estimated_cost_cny": reserve,
            "status": "started",
        }
        self.calls.append(record)
        self._save()  # Reserve before I/O, including an interrupted stream.
        append_json(self.output / "requests.jsonl", record)
        started = time.monotonic()
        try:
            async with aclosing(self.provider.stream(request)) as events:
                async for event in events:
                    if isinstance(event, LLMResponseCompleted):
                        usage = event.response.usage
                        if usage is not None:
                            record["usage"] = usage.model_dump(mode="json")
                            record["estimated_cost_cny"] = (
                                usage.input_tokens * cfg["input_cny_per_million"]
                                + usage.output_tokens * cfg["output_cny_per_million"]
                            ) / 1_000_000
                        record["status"] = "completed"
                        # Persist before yielding: core validation can still fail.
                        self._save()
                    yield event
        except BaseException as exc:
            record["status"] = type(exc).__name__
            raise
        finally:
            record["seconds"] = time.monotonic() - started
            self._save()
            append_json(self.output / "requests.jsonl", record)

    async def aclose(self):
        await self.provider.aclose()
