import json
import logging
from app.schemas import Candidate, ScoreOutput, EvaluationOutput, clamp
from app.config import Settings
from app.llm import LLMClient
from app.models import Score

WEIGHTS = dict(video_need=2.5, timing=2.0, budget_fit=1.5,
               adely_fit=1.5, entry_chance=1.5, location_fit=1.0)
log = logging.getLogger(__name__)


def calculate_total_score(scores: ScoreOutput | dict) -> float:
    values = scores.model_dump() if isinstance(scores, ScoreOutput) else scores
    return round(sum(clamp(values[key]) * weight for key, weight in WEIGHTS.items()), 2)


def select_top_candidates(scores: list[Score], limit: int = 5) -> list[Score]:
    """Select distinct companies, retaining the strongest trigger per company."""
    result, seen = [], set()
    for score in sorted(scores, key=lambda s: (-s.total_score, s.company_id, s.trigger_id)):
        if score.company_id not in seen:
            result.append(score)
            seen.add(score.company_id)
        if len(result) == limit:
            break
    return result


async def score_company(client: LLMClient, settings: Settings, candidate: Candidate,
                        v2_context: dict | None = None) -> EvaluationOutput:
    log.info('scoring started company=%s', candidate.company_name)
    input_text = candidate.model_dump_json()
    if v2_context is not None:
        input_text += '\nV2_DIAGNOSTIC_CONTEXT=' + json.dumps(v2_context, ensure_ascii=False)
    result = await client.generate(model=settings.scoring_model,
        instructions=client.prompt('scoring'), input_text=input_text,
        output_type=EvaluationOutput, reasoning_effort=settings.scoring_reasoning_effort)
    log.info('scoring completed company=%s', candidate.company_name)
    return result.value


def evaluation_priority(output: EvaluationOutput) -> float:
    """Provisional equal-stage geometric mean, 0–100; not a win probability."""
    means = [sum(getattr(output, stage).model_dump().values()) / 5
             for stage in ('need', 'win', 'deliver')]
    return round(10 * (means[0] * means[1] * means[2]) ** (1 / 3), 2)
