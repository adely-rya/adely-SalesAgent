"""Validation helpers for LLM holistic selections."""
from __future__ import annotations

from typing import Iterable

from app.v3_schemas import V3FinalSelectorOutput, V3RankingOutput, V3ShortlistOutput


class V3SelectionError(ValueError):
    pass


def research_allocation_mode(candidate_count: int, budget: int = 15) -> str:
    if candidate_count == 0:
        return 'empty'
    return 'bypass' if candidate_count <= budget else 'terra_ranked'


def deterministic_research_refs(records: list[dict], budget: int = 15) -> list[str]:
    """Stable fallback order; intentionally unrelated to any score field."""
    return [record['candidate_ref'] for record in records[:budget]]


def _item_ref(item) -> str:
    # ``model_copy(update=...)`` in older callers can leave a legacy key in
    # __dict__ without revalidation; honor it for compatibility tests.
    return item.__dict__.get('company_id', item.candidate_ref)


def _validate_refs(items: Iterable, known_refs: set[str], *, limit: int, label: str) -> None:
    values = list(items)
    if len(values) > limit:
        raise V3SelectionError(f'{label} exceeds limit {limit}')
    refs = [_item_ref(item) for item in values]
    enforce_run_ref_format = any(isinstance(ref, str) and ref.startswith('C')
                                 for ref in known_refs)
    if enforce_run_ref_format and any(
            not isinstance(ref, str) or not ref.startswith('C') or not ref[1:].isdigit()
            for ref in refs):
        raise V3SelectionError(f'invalid candidate_ref in {label}')
    if len(refs) != len(set(refs)):
        raise V3SelectionError(f'duplicate candidate_ref in {label}')
    unknown = set(refs) - known_refs
    if unknown:
        raise V3SelectionError(f'unknown candidate_ref in {label}: {sorted(unknown)}')


def validate_shortlist(output: V3ShortlistOutput, known_refs: set[str], limit: int) -> None:
    _validate_refs(output.selected, known_refs, limit=limit, label='shortlist')


def validate_ranking(output: V3RankingOutput, known_refs: set[str]) -> None:
    _validate_refs(output.ranking, known_refs, limit=len(known_refs), label='ranking')
    ranked = {_item_ref(item) for item in output.ranking}
    if ranked != known_refs:
        missing = sorted(known_refs - ranked)
        extra = sorted(ranked - known_refs)
        raise V3SelectionError(f'ranking must contain every candidate; missing={missing} extra={extra}')


def validate_final_selection(output: V3FinalSelectorOutput, known_refs: set[str], limit: int,
                             *, exact_count: int | None = None) -> None:
    _validate_refs(output.selected, known_refs, limit=limit, label='final selection')
    if exact_count is not None and len(output.selected) != exact_count:
        raise V3SelectionError(f'final selection must contain exactly {exact_count} items')
    ranks = [item.rank for item in output.selected]
    if len(ranks) != len(set(ranks)):
        raise V3SelectionError('duplicate rank in final selection')
    if sorted(ranks) != list(range(1, len(ranks) + 1)):
        raise V3SelectionError('final ranks must be contiguous starting at 1')
