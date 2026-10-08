from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RuleIn(BaseModel):
    rule_id: str
    name: str
    priority: int
    condition: dict[str, Any]
    points: float = 0.0
    weight: float = 1.0
    route: str | None = None
    error_policy: str = "degrade"


class RuleSetIn(BaseModel):
    version: str
    rules: list[RuleIn] = Field(default_factory=list)
    pass_score: float = 0.0
    default_route: str | None = None


class LeadIn(BaseModel):
    lead_id: str
    data: dict[str, Any] = Field(default_factory=dict)


class LeadsIn(BaseModel):
    leads: list[LeadIn] = Field(default_factory=list)


class PreviewRequest(BaseModel):
    ruleset: RuleSetIn
    leads: list[LeadIn] = Field(default_factory=list)
    require_no_degradation: bool = True


class PublishRequest(BaseModel):
    version: str


class ReplayRequest(BaseModel):
    version: str
    leads: list[LeadIn] = Field(default_factory=list)
