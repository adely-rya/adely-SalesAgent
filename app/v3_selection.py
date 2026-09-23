"""Validation helpers for LLM holistic selections."""
from __future__ import annotations

from typing import Iterable

from app.v3_schemas import V3FinalSelectorOutput, V3ShortlistOutput


class V3SelectionError(ValueError):
    pass


def _validate_ids(items: Iterable, known_ids: set[str], *, limit: int, label: str) -> None:
    values = list(items)
    if len(values) > limit:
        raise V3SelectionError(f'{label} exceeds limit {limit}')
    ids = [item.company_id for item in values]
    if len(ids) != len(set(ids)):
        raise V3SelectionError(f'duplicate company_id in {label}')
    unknown = set(ids) - known_ids
    if unknown:
        raise V3SelectionError(f'unknown company_id in {label}: {sorted(unknown)}')


def validate_shortlist(output: V3ShortlistOutput, known_ids: set[str], limit: int) -> None:
    _validate_ids(output.selected, known_ids, limit=limit, label='shortlist')


def validate_final_selection(output: V3FinalSelectorOutput, known_ids: set[str], limit: int) -> None:
    _validate_ids(output.selected, known_ids, limit=limit, label='final selection')
    ranks = [item.rank for item in output.selected]
    if len(ranks) != len(set(ranks)):
        raise V3SelectionError('duplicate rank in final selection')
    if sorted(ranks) != list(range(1, len(ranks) + 1)):
        raise V3SelectionError('final ranks must be contiguous starting at 1')
