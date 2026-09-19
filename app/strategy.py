import logging
from app.schemas import Candidate, StrategyOutput
from app.config import Settings
from app.llm import LLMClient, Generation
from app.models import Score

log = logging.getLogger(__name__)


async def generate_strategy(client: LLMClient, settings: Settings, candidate: Candidate,
                            score: Score) -> Generation[StrategyOutput]:
    log.info('strategy generation started company=%s', candidate.company_name)
    return await client.generate(model=settings.strategy_model, instructions=client.prompt('strategy'),
        input_text=candidate.model_dump_json() + '\nScore: ' + str(score.total_score),
        output_type=StrategyOutput, use_web_search=settings.strategy_web_search,
        reasoning_effort=settings.strategy_reasoning_effort)
