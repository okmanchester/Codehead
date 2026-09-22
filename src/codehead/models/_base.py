"""Shared configuration for Codehead domain models."""

from pydantic import BaseModel, ConfigDict


class CodeheadModel(BaseModel):
    """Immutable base for values exchanged between Codehead components."""

    model_config = ConfigDict(extra="forbid", frozen=True)
