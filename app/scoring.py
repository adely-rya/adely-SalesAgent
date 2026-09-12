import logging
from app.schemas import Candidate, ScoreOutput, clamp
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


async def score_company(client: LLMClient, settings: Settings, candidate: Candidate) -> ScoreOutput:
    log.info('scoring started company=%s', candidate.company_name)
    result = await client.generate(model=settings.scoring_model,
        instructions=client.prompt('scoring'), input_text=candidate.model_dump_json(),
        output_type=ScoreOutput)
    log.info('scoring completed company=%s', candidate.company_name)
    return result.value
