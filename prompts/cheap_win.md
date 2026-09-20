# Purpose

Use only the supplied Opportunity, Events, deterministic hard-filter result, and local VC Profile context to decide whether further Deep Research is worth its cost. Do not run Web Search. This is a preliminary Gate, not the final WIN score and not a NEED or DELIVER evaluation.

# WIN scope

WIN_PRE estimates whether a small or medium project in adely's practical JPY 200,000–500,000 range has realistic external purchasing room, price fit, and a plausible way to reach a buyer. Consider only evidence relevant to those questions.

The following are not WIN evidence by themselves: company growth, startup status, funding, a new service, visual appeal, high video need, easy shooting, or technically simple production. Funding is not a video budget. Production scale, location, and technical execution mostly belong to DELIVER; do not let them raise WIN.

VC Profile data describes the investor's support organization only. It is supporting context about a possible route or resource, not a fact about the portfolio company. Do not infer that the portfolio company has a budget, needs video, has creative support, or uses a specific production partner from the investor profile.

# Evidence, risk, and unknowns

Use `risk_tags` only for supported negative evidence. Use `unknown_factors` for material information gaps such as an unverified budget, buyer, or procurement path. Do not put the same item in both lists. Not finding a partner or procurement contact does not prove none exists.

Set `hard_blocker` only for a clear, supported reason that makes the company ineligible or access practically impossible. Company size alone is not a blocker. A low score must be based on sufficient negative evidence, not merely missing information. High WIN_PRE requires direct positive evidence such as prior external purchasing, a reachable buying route, credible price fit, or a structure that allows a new external supplier. Funding, growth, or a new service alone cannot justify high WIN_PRE.

Set `confidence` to high when the main WIN factors have direct evidence, medium when important factors are inferred from relevant facts, and low when a material factor remains unknown. Unknown is neither positive nor negative evidence. Do not use a default midpoint just because data is absent; score what the supplied evidence supports and preserve the gap in `unknown_factors` and confidence.

The application, not this Prompt, assigns `DROP` / `HOLD` / `RESEARCH` using the supplied configured thresholds and confidence. A low-confidence result is held, regardless of its numeric WIN_PRE, unless a supported `hard_blocker` applies. A score below the configured DROP threshold is dropped only with high-confidence negative evidence; medium-confidence low scores are held. At or above the configured research threshold, medium/high-confidence results may proceed to Research. Do not choose Status yourself.

Return only the existing CheapWinOutput JSON fields: `win_pre`, `confidence`, `hard_blocker`, `risk_tags`, `unknown_factors`, and `reason`. Clearly distinguish observed facts, inferences, and unknowns in `reason`.
