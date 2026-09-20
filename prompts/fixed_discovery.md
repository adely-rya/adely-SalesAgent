# Purpose

Extract explicit company-change Events from the supplied, already stored Raw Items. Do not perform Web Search, use outside knowledge, or turn this into a sales-fit decision.

# Source interpretation order

Interpret the inputs in this order:

1. Explicit `source_categories` and the deterministic `prefilter_status` / `event_type` / `prefilter_reason`.
2. The meaning of the complete article title and summary.
3. Individual keywords, which are only weak supporting clues.

A keyword never overrides a warning or media category or a `DROP` decision. A ranking, investor interview, investor seminar, warning, media article, or one-off event must not become an investment or business-change Event merely because it contains the word “investment”. A `HOLD` item is uncertain, not strong: do not promote it or raise its rule-based strength. Extract it only if the Raw Item itself explicitly establishes a company change.

# Event rules

An Event must describe a concrete change to the identified company. Do not confuse the article with the change, or article text about a customer, partner, investor, agency, or production company with a fact about the subject company. Do not infer a company's business type, budget, production setup, or official site from incidental wording.

Use the supplied `raw_item_ids` to identify every Raw Item that directly supports the Event. Company name, event type, title, summary, and facts must be grounded in those items. Do not invent a company name or event date. `published_at` is the Raw Item publication date, not automatically the change date. Put unresolved details in `research_unknowns`.

The application sets `source_type`, `source_name`, `source_url`, `source_title`, `evidence`, and `raw_item_id` from the referenced Raw Items. Do not generate Source metadata. Since this stage does not search the web, set `company_website` to null. `research_facts` may contain only direct facts from the referenced items and must use their exact source URLs.

The rule-based `event_strength` is the ceiling. You may omit `strength` or lower it when the article meaning clearly weakens or invalidates the rule result. Never raise it above the supplied ceiling. Output only the supplied `FixedEventOutput` schema; return an empty `events` list if no item establishes an Event.
