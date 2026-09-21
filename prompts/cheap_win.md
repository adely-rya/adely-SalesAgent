# Purpose

Use only the supplied Opportunity, Events, deterministic hard-filter result, and local VC Profile context. Do not run Web Search. Assess preliminary WIN evidence only; do not decide whether the Opportunity deserves Deep Research, and do not give final NEED or DELIVER scores.

# WIN scope

`win_pre` estimates whether an adely-sized JPY 200,000–500,000 external project has a realistic purchasing route, price fit, and reachable buyer. It is not a score for Event importance or research value.

Company growth, startup status, funding, a new service, visual appeal, high video need, easy shooting, and technical simplicity are not WIN proof by themselves. Funding is not a video budget. Production scale, location, and technical feasibility mostly belong to DELIVER.

VC Profile data describes the investor's support organization only. It is supporting context about a possible route or resource, not a fact about the portfolio company. Do not infer that the portfolio company has a budget, needs video, has creative support, or uses a specific production partner from the investor profile. A null/unknown capability, including `creative_support_level`, is neither positive nor negative evidence about portfolio-company WIN; do not raise or lower WIN_PRE because that field is unknown. VC fundraising support is not evidence of the portfolio company's video budget.
Current purchasing likelihood and future growth potential can differ: a small or early-stage company may have limited or unknown current purchasing capacity while showing meaningful growth signals. Treat company size/stage as a fact, not a negative Risk by itself. Do not DROP or suppress an Opportunity only because it is a startup, small, young, or its current budget is unknown. Growth signals may support later Research allocation but must not inflate WIN_PRE.

# Facts, risks, inferences, and unknowns

- `risk_tags`: only evidence-backed negative facts relevant to external purchasing, price fit, or buyer access. Do not use these for company size, unknown procurement, or a possible barrier.
- `unknown_factors`: material information gaps, such as unverified budget, buyer, procurement route, or supplier access. Not finding a partner/contact does not prove none exists.
- `inference_factors`: plausible but unconfirmed interpretations from supplied facts, such as “large procurement may be complex”. Do not present an inference as a confirmed Risk.

Set `hard_blocker` only for a clear, supported reason that makes the company ineligible or external access practically impossible. High WIN_PRE requires positive evidence such as prior external purchasing, a reachable buying route, credible price fit, or a structure open to a new supplier. Funding, growth, or a new service alone cannot justify high WIN_PRE. Do not set a default midpoint because data is missing; score only what the supplied evidence supports.

`confidence` describes certainty in `win_pre`, not whether Deep Research is worthwhile. Low confidence does not mean DROP and does not itself require HOLD; preserve the unknown and let the application route using Event value, hard blockers, and existing evidence. Do not choose `DROP` / `HOLD` / `RESEARCH` yourself.

Return only `CheapWinOutput`: `win_pre`, `confidence`, `hard_blocker`, `risk_tags`, `unknown_factors`, `inference_factors`, and `reason`. In `reason`, distinguish observed fact, inference, and unknown. Keep the lists concise and do not repeat one item across them.
