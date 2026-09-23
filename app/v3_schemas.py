"""Structured outputs for the V3 human-like sales pipeline.

The model-facing identifier is deliberately a run-local ``candidate_ref``.
Database/company identifiers are resolved by the pipeline after validation.
"""
from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator

from app.schemas import Output


class V3ShortlistItem(Output):
    candidate_ref: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=700)

    @model_validator(mode='before')
    @classmethod
    def accept_legacy_test_shape(cls, value):
        # Compatibility for pre-Ref callers; the generated schema still only
        # exposes candidate_ref and reason to the model.
        if isinstance(value, dict):
            value = dict(value)
            if 'candidate_ref' not in value and 'company_id' in value:
                value['candidate_ref'] = value.pop('company_id')
            if 'reason' not in value:
                value['reason'] = value.pop('why_selected', '') or 'legacy shortlist item'
            value.pop('company_name', None)
            value.pop('what_to_investigate', None)
        return value

    @property
    def company_id(self) -> str:
        return self.candidate_ref


class V3ShortlistOutput(Output):
    selected: list[V3ShortlistItem] = Field(max_length=15)


class V3RankingItem(Output):
    candidate_ref: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=700)


class V3RankingOutput(Output):
    ranking: list[V3RankingItem] = Field(min_length=1, max_length=200)


class V3Evidence(Output):
    claim: str = Field(min_length=1, max_length=500)
    evidence_type: Literal['observed', 'indirect', 'inference', 'unknown']
    source_url: str | None = None
    confidence: Literal['high', 'medium', 'low']

    @model_validator(mode='after')
    def source_matches_evidence_type(self):
        if self.evidence_type == 'unknown' and self.source_url:
            raise ValueError('Unknown evidence must not claim a source URL')
        if self.evidence_type != 'unknown' and not self.source_url:
            raise ValueError('Supported evidence requires a source URL')
        return self


class V3SalesMemoOutput(Output):
    candidate_ref: str = Field(min_length=1, max_length=32)
    company_name: str = Field(min_length=1, max_length=256)
    why_this_company: str = Field(min_length=1, max_length=700)
    why_now: str = Field(min_length=1, max_length=700)
    what_we_learned: list[str] = Field(min_length=1, max_length=8)
    current_expression: str = Field(min_length=1, max_length=900)
    expression_gap: Literal['strong', 'medium', 'weak']
    expression_gap_reason: str = Field(min_length=1, max_length=700)
    peer_comparison: str = Field(min_length=1, max_length=700)
    creative_situation: str = Field(min_length=1, max_length=500)
    opportunity_hypothesis: str = Field(min_length=1, max_length=700)
    reasons_not_to_pursue: list[str] = Field(default_factory=list, max_length=6)
    salesmans_take: str = Field(min_length=1, max_length=700)
    evidence: list[V3Evidence] = Field(min_length=1, max_length=30)

    @model_validator(mode='before')
    @classmethod
    def accept_legacy_company_id(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if 'candidate_ref' not in value and 'company_id' in value:
                value['candidate_ref'] = value.pop('company_id')
        return value

    @property
    def company_id(self) -> str:
        return self.candidate_ref


class V3FinalSelectionItem(Output):
    rank: int = Field(ge=1, le=5)
    candidate_ref: str = Field(min_length=1, max_length=32)
    company_name: str = Field(min_length=1, max_length=256)
    why_now: str = Field(min_length=1, max_length=600)
    why_this_company: str = Field(min_length=1, max_length=600)
    proposed_angle: str = Field(min_length=1, max_length=600)
    main_risk: str = Field(min_length=1, max_length=500)

    @model_validator(mode='before')
    @classmethod
    def accept_legacy_company_id(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if 'candidate_ref' not in value and 'company_id' in value:
                value['candidate_ref'] = value.pop('company_id')
        return value

    @property
    def company_id(self) -> str:
        return self.candidate_ref


class V3FinalSelectorOutput(Output):
    selected: list[V3FinalSelectionItem] = Field(max_length=5)
    not_selected_notes: list[str] = Field(default_factory=list, max_length=20)


class V3Error(Output):
    candidate_ref: str
    company_name: str
    error_type: str
