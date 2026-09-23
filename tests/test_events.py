import pytest
from pydantic import TypeAdapter, ValidationError

from codehead.models import (
    AgentEvent,
    AssistantTextDelta,
    ContextCompacted,
    ContextCompactionStarted,
    LLMResponseCompleted,
    LLMStreamEvent,
    LLMTextDelta,
    ModelStarted,
)


def test_event_unions_parse_by_type_and_remain_separate() -> None:
    provider = TypeAdapter(LLMStreamEvent)
    agent = TypeAdapter(AgentEvent)
    assert provider.validate_python(
        {"type": "llm.text_delta", "delta": "hi"}
    ) == LLMTextDelta(delta="hi")
    completed = provider.validate_python(
        {
            "type": "llm.response_completed",
            "response": {"message": {"content": "done"}, "finish_reason": "end_turn"},
        }
    )
    assert isinstance(completed, LLMResponseCompleted)
    assert agent.validate_python({"type": "model.started", "step": 1}) == ModelStarted(
        step=1
    )
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        agent.validate_python(completed.model_dump())
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        provider.validate_python(
            {"type": "turn.completed", "step": 1, "answer": "done"}
        )


def test_events_reject_empty_deltas_invalid_steps_and_mutation() -> None:
    with pytest.raises(ValidationError):
        LLMTextDelta(delta="")
    with pytest.raises(ValidationError):
        AssistantTextDelta(step=0, delta="hi")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelStarted.model_validate({"step": 1, "unknown": True})
    event = AssistantTextDelta(step=1, delta="hi")
    with pytest.raises(ValidationError, match="frozen_instance"):
        event.delta = "changed"


def test_context_events_parse_as_agent_events_only():
    agent = TypeAdapter(AgentEvent)
    provider = TypeAdapter(LLMStreamEvent)
    for event in (
        ContextCompactionStarted(step=1, before_tokens=4000),
        ContextCompacted(step=1, before_tokens=4000, after_tokens=1000),
    ):
        assert agent.validate_python(event.model_dump()) == event
        with pytest.raises(ValidationError):
            provider.validate_python(event.model_dump())
