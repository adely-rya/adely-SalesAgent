import pytest
from pydantic import ValidationError
from app.schemas import ScoreOutput
from app.scoring import WEIGHTS, calculate_total_score, select_top_candidates
from app.models import Score


def test_total_score():
    assert calculate_total_score(dict.fromkeys(WEIGHTS, 10)) == 100
    assert calculate_total_score(dict.fromkeys(WEIGHTS, 0)) == 0
    assert calculate_total_score(dict(video_need=9, timing=9, budget_fit=8,
        adely_fit=9, entry_chance=7, location_fit=10)) == 86.5


def test_clamp():
    values = dict.fromkeys(WEIGHTS, 11)
    values['timing'] = -1
    result = ScoreOutput(**values, reason='test', risks=[])
    assert result.video_need == 10
    assert result.timing == 0
    assert calculate_total_score(values) == 80


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_rejected(value):
    with pytest.raises(ValidationError):
        ScoreOutput(**dict.fromkeys(WEIGHTS, value), reason='', risks=[])


def test_top_is_distinct_companies():
    scores = [Score(company_id=1, trigger_id=1, total_score=99),
              Score(company_id=1, trigger_id=2, total_score=98),
              Score(company_id=2, trigger_id=3, total_score=90)]
    assert [s.company_id for s in select_top_candidates(scores)] == [1, 2]


@pytest.mark.parametrize('value', [-1, 11, 5.5, '5', True, float('nan')])
def test_new_evaluation_rejects_invalid_rating(value):
    from app.schemas import NeedScores
    with pytest.raises(ValidationError):
        NeedScores(**dict.fromkeys(NeedScores.model_fields, value))


def test_evaluation_priority_uses_all_stages():
    from app.schemas import EvaluationOutput, NeedScores, WinScores, DeliverScores
    from app.scoring import evaluation_priority
    value = EvaluationOutput(
        need=dict.fromkeys(NeedScores.model_fields, 10),
        win=dict.fromkeys(WinScores.model_fields, 10),
        deliver=dict.fromkeys(DeliverScores.model_fields, 0),
        scope_hypothesis='test', reason='test', strongest_signals=[], risks=[], research_needed=[],
        evidence_coverage={stage: {'coverage': 'low', 'reason': 'test'}
                           for stage in ('need', 'win', 'deliver')})
    assert evaluation_priority(value) == 0
    value.deliver = DeliverScores(**dict.fromkeys(DeliverScores.model_fields, 10))
    assert evaluation_priority(value) == 100
