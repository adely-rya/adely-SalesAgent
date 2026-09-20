import json
import logging
from app.schemas import Candidate, StrategyOutput
from app.config import Settings
from app.llm import LLMClient, Generation
from app.models import Score

log = logging.getLogger(__name__)


async def generate_strategy(client: LLMClient, settings: Settings, candidate: Candidate,
                            score: Score | float, strategy_context: dict | None = None) -> Generation[StrategyOutput]:
    log.info('strategy generation started company=%s', candidate.company_name)
    # V1 and V2 persistence use distinct ORM score types; both expose the
    # same numeric projection, while V2.2 passes a plain float.
    score_value = float(getattr(score, 'total_score', score))
    input_text = candidate.model_dump_json() + '\nScore: ' + str(score_value)
    if strategy_context is not None:
        input_text += '\nV2_OPPORTUNITY_CONTEXT=' + json.dumps(strategy_context, ensure_ascii=False)
    return await client.generate(model=settings.strategy_model, instructions=client.prompt('strategy'),
        input_text=input_text,
        output_type=StrategyOutput, use_web_search=settings.strategy_web_search,
        reasoning_effort=settings.strategy_reasoning_effort)
