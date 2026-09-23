from pathlib import Path
import asyncio

from app.config import Settings
from app.llm import Generation
from app.schemas import DiscoveryOutput


PROMPTS = Path(__file__).resolve().parents[1] / 'prompts'


def prompt(name):
    return (PROMPTS / f'{name}.md').read_text(encoding='utf-8')


def test_discovery_is_event_only_and_allows_no_results():
    content = prompt('discovery')
    assert 'Return `{"events": []}`' in content
    assert 'does not select sales winners' in content
    assert 'does not prove a video budget' in content
    assert 'Creative Lock-in' in content
    assert 'can win or deliver' in content
    assert '2〜5' not in content
    assert 'does not mean a major news event' in content
    assert 'smaller private companies' in content
    assert 'Kanagawa, Yokohama, Kawasaki, Fujisawa, Shonan, and Tokyo' in content
    assert 'Do not treat Web Search as startup search or PR TIMES search' in content
    assert 'Could this change give us a credible reason to propose visual communication now?' in content


def test_fixed_extraction_respects_source_semantics_and_strength_ceiling():
    content = prompt('fixed_discovery')
    assert 'A keyword never overrides a warning or media category or a `DROP` decision.' in content
    assert 'A `HOLD` item is uncertain, not strong' in content
    assert 'Never raise it above the supplied ceiling.' in content
    assert 'The application sets `source_type`, `source_name`, `source_url`' in content


def test_cheap_win_keeps_risks_separate_from_unknowns_and_vc_scope():
    content = prompt('cheap_win')
    assert '`risk_tags`: only evidence-backed negative facts' in content
    assert '`unknown_factors`: material information gaps' in content
    assert '`inference_factors`' in content
    assert 'VC Profile describes the investor\'s support organization only.' in content
    assert 'Funding is not a video budget.' in content
    assert 'Low confidence does not mean DROP and does not itself require HOLD' in content
    assert 'current purchasing capacity while showing meaningful growth signals' in content
    assert 'listed_company' in content
    assert 'IT security requirements' in content


def test_cheap_win_gate_is_batched_and_hypothesis_first():
    content = prompt('cheap_win')
    assert 'credible sales-opportunity hypothesis' in content
    assert 'Unknown is NOT a reason to Research' in content
    assert 'Researchability by itself is NOT sufficient' in content
    assert 'trigger_quality' in content and 'target_fit' in content and 'research_value' in content
    assert 'exactly one result' in content
    assert 'no quota for RESEARCH' in content


def test_research_and_scoring_require_evidence_and_risk_alignment():
    diagnostic = prompt('diagnostic')
    scoring = prompt('scoring')
    assert 'Do not score “video not found” as high debt by itself.' in diagnostic
    assert 'One credit is evidence of one past use' in diagnostic
    assert 'evidence_type' in diagnostic and 'source_url' in diagnostic
    assert 'High severity is' in scoring or 'High severity' in scoring
    assert '`unknowns`' in scoring
    assert '181日以上' in scoring
    assert 'General procurement policy' in scoring
    assert '単一の制作CreditはLock-inではない' in scoring
    assert 'Research is observed-first' in diagnostic
    assert 'normal state' in diagnostic


def test_strategy_does_not_reopen_scores_or_treat_proposal_as_company_budget():
    content = prompt('strategy')
    assert 'Do not redo research to change its status, scores, or ranking.' in content
    assert 'not the company\'s known budget' in content


def test_v3_prompts_use_holistic_sales_judgment_and_stage_boundaries():
    shortlist = prompt('v3_shortlist')
    research = prompt('v3_research')
    final = prompt('v3_final_selector')
    assert 'not a mechanical classifier' in shortlist
    assert 'Do not use Web Search' in shortlist
    assert 'There is no industry quota' in shortlist
    assert 'Expression Gap means business reality versus expression reality' in research
    assert 'One production credit is not lock-in' in research
    assert 'Do not merely confirm' in research
    assert 'holistic comparison, not a score sort' in final
    assert 'Do not calculate or use NEED/WIN/DELIVER totals' in final


def test_v1_candidate_and_v22_event_discovery_use_separate_prompts():
    class CaptureClient:
        prompt_names = []

        def prompt(self, name):
            self.prompt_names.append(name)
            return name

        async def generate(self, *, instructions, output_type, **_kwargs):
            assert output_type is DiscoveryOutput
            assert instructions == 'legacy_discovery'
            return Generation(DiscoveryOutput(candidates=[]), set())

    client = CaptureClient()
    from app.discovery import discover_topic
    candidates, _evidence, rejected = asyncio.run(discover_topic(client, Settings(), 'test'))
    assert candidates == [] and rejected == 0
    assert client.prompt_names == ['legacy_discovery']
    assert 'return those changes as Events' in prompt('discovery')
    assert 'Legacy V1 Candidate discovery' in prompt('legacy_discovery')
