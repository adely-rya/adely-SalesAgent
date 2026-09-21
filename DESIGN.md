# V2.2 Design

## Purpose

The system finds recent company changes, estimates whether those changes create a reasonable video communication need, and checks whether adely can realistically win and deliver the work at a sustainable scope. A news mention by itself is not an Opportunity.

## Pipeline

```text
Fixed Sources → Raw Items → Fixed Events ─┐
                                           ├→ Events → group by company → Opportunities
Web Search → Web Events ───────────────────┘                                ↓
                                                       Gate: DROP / HOLD / RESEARCH
                                                                           ↓
                                                           Deep Research → NEED/WIN/DELIVER
                                                                           ↓
                                                                          Top5
```

The daily entry point is `run_daily_pipeline()` in `app/v2_pipeline.py`. It loads pending Raw Items and VC Profiles, then passes Python objects through extraction, grouping, Gate, Research, and Scoring. It does not collect by default; the independent Collector command runs on its own schedule. A one-off collection can be requested explicitly. The internal fixed-source rules and batch router are called through `extract_fixed_events()`. Gate, Research, and Scoring are exposed through one named function each in `app/opportunity_stages.py`.

## Stage Responsibilities

- Discovery: find evidence-backed Events only; do not select likely winners or estimate production fit.
- Gate: allocate Deep Research effort from existing facts; low-confidence or medium-confidence low-score cases are held, not dropped for uncertainty alone.
- Research: collect cited evidence about expression, Expression Debt, and Creative Lock-in; do not make final scores.
- Scoring: make the final NEED / WIN / DELIVER judgment and map each material Risk to its affected score.
- Strategy: convert the completed judgment into a human-reviewed proposal hypothesis; it cannot change status, scores, or rank.

## Data Concepts

- `RawItem` (`app/domain.py`): a stored source record before business interpretation. The compatible `source_events` SQL table is its store.
- `Event` (`app/schemas.py`): a validated company change with source evidence, known facts, hypotheses, unknowns, and strength. Fixed and Web Discovery use this same schema.
- `Opportunity` (`app/domain.py`): the company-level aggregate. It owns its Events and is enriched in memory with Gate, Research, Score, status, and optional Strategy.
- `VCProfile`: slow-changing master data read with the pipeline inputs and selected in memory.
- `HumanFeedback`: independent human ratings keyed by company and Run.

The V1 `Candidate` type remains for the V1 discovery/scoring CLI and as the current Opportunity-to-Trigger persistence adapter. It is not the common V2.2 discovery result.

The V1 CLI uses `prompts/legacy_discovery.md` to keep its `Candidate` response contract. V2.2 uses `prompts/discovery.md` for typed Event discovery. This adapter prevents the V2 Event-only Prompt from changing the V1 `run-once` / `daemon` output shape.

## Persistence Boundaries

`V2Repository` (`app/v2_repository.py`) is the V2.2 database boundary.

- Reads: one input-loading boundary executes bounded Raw Item and VC Profile queries. The final save transaction batch-loads raw rows, prior prefilter rows, matching Companies, and their Triggers; query count is independent of the number of Events. The Run row is opened before transformations so failures remain auditable; that small early history write is the sole checkpoint.
- Writes: fixed-source collection persists Raw Items at collection time for deduplication and recovery. After all transformations, one `save_run_results()` transaction saves prefilter decisions, processed Raw Item IDs, Opportunity snapshots, legacy company/trigger links, Research, Score, Strategy, and errors.
- Gate, Research, and Scoring do not read or write the database.

`source_event_prefilters`, `diagnostics`, and `research_scores` remain for audit history and V2 compatibility. `candidates` is the Opportunity snapshot adapter; `merged_json` is the full per-Run trace. Existing tables are additive/retained and are not dropped. `companies`, `triggers`, and the V1 `scores` tables remain V1-compatible. Both `human_ratings` (V1 good/maybe/bad) and `human_feedback` (V2 reason-coded ratings) are retained because their schemas are incompatible; neither has a V2.2 runtime writer yet. They are a known legacy duplication, not two active Pipeline stages.

## Operation

- `collect-fixed`: one bounded, AI-free collection pass.
- `v2-daemon`: immediately collect fixed sources, repeat at `FIXED_COLLECTOR_INTERVAL_HOURS` (default 3), and run V2 Discovery/Gate/Research/Scoring plus Discord report at the configured daily time. It refreshes fixed sources immediately before the daily run.
- `daily-run`: one Web Discovery and Opportunity Pipeline pass; an optional `--collect-before-run` refreshes fixed sources first.
- `daemon`: legacy V1 scheduled pipeline, retained for CLI compatibility; Docker Compose defaults to `v2-daemon`.
- `trace-opportunity --company NAME` or `--opportunity-id ID`: show Events, Gate, Research, and Score from the stored Opportunity snapshot.

Compose's `v2-daemon` owns the fixed-collector interval and daily V2 schedule. Do not also run `collect-fixed-daemon` alongside it unless duplicate collection is intentional.

## Semantic Principles

1. `company fact != event text`: company identity facts and article language have separate scopes.
2. `not found != absent`: an unsuccessful search is not evidence that a site, video, partner, or capability does not exist.
3. `unknown != neutral`: unknowns and evidence confidence remain explicit; missing evidence is not silently converted into a midpoint.
4. Deterministic rules decide only obvious source noise or clear patterns. Ambiguous meaning stays in HOLD or is assessed at Gate.
5. Cheap processing precedes expensive processing. Cheap WIN uses current Opportunity facts and VC Profile data and has no Web Search tool.
6. The database stores raw evidence, history, restart state, and analysis snapshots. It is not a transport mechanism between pipeline functions.
7. Pipeline stages pass Python objects. SQL is limited to collection and repository boundaries.
8. Model outputs are schema-validated. Web Events and facts must cite URLs returned by Web Search; Fixed Events and facts must cite included Raw Items.
9. `unknown != neutral`: uncertainty has its own fields and low confidence routes to HOLD; missing evidence is not a midpoint or a DROP reason.
10. `risk must affect score or explain why not`: Scoring returns structured Risk assessments with an affected axis and explicit score effect; high-severity Risk must lower that axis.
11. Research claims carry an evidence type, confidence, and trusted source URL. Scoring receives the claim list and URL set, not only the Research conclusion.

The unknown-source `event_strength=50` is a neutral routing fallback required to keep unfamiliar sources reviewable. It is not a company-fit or evidence-quality score and is never used as a substitute for missing Research evidence.

## Semantic Audit

| Severity | Finding | V2.2 handling |
| --- | --- | --- |
| HIGH | The previous hard filter joined company name, Event title, and summary, then treated words such as “video production” or “agency” as company identity. | Removed this string-based rejection. Without a structured, verified company business type, the deterministic filter abstains and Cheap WIN receives the evidence. |
| HIGH | The previous Web Discovery result combined company facts, an event, and candidate selection in `Candidate`. | Web and Fixed Discovery now return the same `Event` schema; company grouping creates `Opportunity` afterwards. |
| MEDIUM | Name-only company grouping can still merge homonyms when one record lacks an official domain. | Grouping requires matching normalized names and rejects conflicting known domains. Ambiguous no-domain identities remain an unavoidable residual risk; review the attached sources before outreach. |
| MEDIUM | A keyword-based parser cannot reliably infer whether all source prose describes the same business event. | Regex is limited to source category, direct IPO/funding/investment patterns, and obvious noise. Other cases remain HOLD or are validated by Event extraction. |
| MEDIUM | Scores are numeric fields, while evidence may be incomplete. | Scoring schema keeps stage coverage and reasons, and the prompt instructs the model not to set all unknown dimensions to 5. Missing expression evidence remains in `unknowns`; it is not evidence of absence. A future schema can add per-dimension abstention if downstream consumers need it. |
| HIGH | Cheap WIN previously used numeric thresholds without confidence, so uncertain Opportunities could be dropped or researched based on a point score alone. | `unknown_factors` is stored separately from negative `risk_tags`; `confidence=low` routes to HOLD. Below the existing DROP threshold only high-confidence negative evidence drops; medium-confidence low scores are held. A clear `hard_blocker` remains authoritative. No new threshold was added. |
| HIGH | Research conclusions could reach Scoring without claim-level evidence URLs or with unverified generated URLs. | Diagnostic claims now carry evidence type, URL, and confidence. The Stage validates URLs against tool evidence or supplied Event/Profile sources; Scoring receives both claims and evidence URLs. |
| HIGH | A textual Risk could be listed without recording which score it should affect. | Scoring returns structured Risk fields (axis, dimension, severity, evidence, sources, and score effect). High-severity Risk cannot claim no score impact; Scoring Prompt requires a consistency check. |
| MEDIUM | Fixed/Web Event models previously regenerated metadata already known to the application. | LLM Event payloads no longer include source type/name, Fixed source URL/title/evidence, or raw item IDs. The application joins those values from trusted Raw Item or Web Search metadata. |
| MEDIUM | V1 `human_ratings` and V2 `human_feedback` encode overlapping review concepts with incompatible rating formats. | Both tables are retained for compatibility, but V2.2 does not write either. A future feedback import/repository decision should choose a canonical format before adding a runtime feedback path. |
| LOW | Cross-source Event deduplication only merges equal normalized titles or identical canonical URLs with matching company and event type. | Both source evidences are retained for these clear matches; paraphrased duplicates may remain separate Events in one Opportunity. |
| LOW | Final-score weights and the 3.5 / 5.5 Cheap WIN thresholds are configured as a small fixed rubric. | Values are in `Settings`; thresholds and equal NEED/WIN/DELIVER aggregation should be reviewed against Human Feedback. |

## Adversarial Cases

Tests ensure Event prose containing “video production company,” “advertising agency,” or “publicly listed company” does not set the subject company's business type. “Video production” in a positive rebrand Event also remains eligible. Prompt contract tests cover empty Discovery results, funding/budget separation, Source priority, Unknown vs Risk, single vs repeated Creative credits, and Strategy's no-rescoring boundary. Schema and routing tests verify low-confidence HOLD, trusted Research URLs, structured Risk effects, and Source metadata injection. V2.1 fixtures for formal IPO names, investment ranking, warning, product/event noise, anniversary-only, and anniversary plus rebrand remain covered in `tests/test_prefilters.py`. The production Fixed Event router is also tested against the KOMPEITO/SQUEEZE IPOs, a ranking DROP, an anniversary-only story, weak event Holds, and the 20-item batch limit.
