# Legacy V1 Candidate discovery

This Prompt is used only by the V1 `run-once` / `daemon` Candidate adapter. V2.2 Web Discovery uses `discovery.md` and returns Events instead. For the supplied topic, find up to five clearly evidenced companies; fewer than five, including zero, is valid. Do not invent companies or fill a count with weak candidates.

Select only companies with a recent, concrete business or brand change that creates a plausible communication question for a brand, corporate, service, technical, or recruitment video. Funding is a supporting signal, not proof of video need or a video budget. Product launches, popularity, visual appeal, or a single event alone are not enough. The final purchase likelihood and production fit are scored later; do not assign final WIN or DELIVER scores.

Keep the article subject distinct from other companies and partners mentioned in it. Do not infer that the subject company is an agency, production company, large enterprise, or overseas company from an incidental phrase. Do not treat a missing website, video, credit, budget, or buyer as proof of absence.

Use primary sources when available. `source_url`, `source_title`, `website`, publication date, and each `research_facts.source_url` must come from pages returned by this Web Search. Include only directly supported facts in `research_facts`; use `possible_video_need` for a clearly labeled hypothesis and `research_unknowns` for unresolved questions. Never invent a URL, date, company fact, budget, production partner, or contact person. Do not contact a company.

Use the supplied `candidate_schema` and return only `{"candidates": [...]}`. The application validates every source URL against Web Search evidence and assigns `discovered_at` itself.
