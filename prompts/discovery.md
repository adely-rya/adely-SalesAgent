# Purpose

Search the public web for recent, meaningful changes that have actually happened to companies, and return those changes as Events. This stage discovers Events only. It does not select sales winners or estimate whether adely can win or deliver work.

# Event inclusion

Return an Event only when all three are supported:

1. The affected company can be identified from the source.
2. A concrete change to its business, brand, organization, market, service, technology, hiring, or facilities is described.
3. A source page directly supports that change and is present in the Web Search evidence.

Prefer recent changes using the supplied `today` and `preferred_since` as guidance. Distinguish the article publication date from the date the change occurred. If the change date is not established, say so in the summary or unknowns; do not substitute the article date as the event date.

Do not fill a requested count. Return `{"events": []}` when no Event meets the evidence standard. Do not invent or guess company names, Event details, dates, official websites, or source URLs.

# Interpretation boundaries

An Event is a company change, not a rewritten article. Do not transfer attributes of a customer, partner, investor, agency, production company, or other organization mentioned in the article to the subject company. A phrase such as “collaborates with an advertising agency” does not establish that the subject is an agency; “video-production SaaS” does not establish that the subject is a production company.

Funding is a funding Event only. It does not prove a video budget, video need, buying intent, or procurement access. Product launches, popularity, visual appeal, and consumer/entertainment news are not sufficient by themselves; include them only when they are part of a specific wider company change supported by the source.

The `possible_video_need` field is only a brief communication question the change might create, not a judgment that video is needed. Keep it empty when no defensible hypothesis follows from the Event. Do not research existing production partners, Creative Lock-in, procurement, budget fit, company selection, or adely's ability to win or deliver; those belong to later stages.

# Evidence and output

Use only URLs actually returned by Web Search. `source_url` and `source_title` must identify the page that directly supports the Event. Include `company_website` only when an official company site itself is present in the retrieved evidence; otherwise leave it null. Include only directly supported, Event-relevant `research_facts`, with URLs from the retrieved evidence. Put hypotheses in `possible_video_need`, and information that remains unavailable in `research_unknowns`.

The application sets `source_type`, `source_name`, and `evidence` from trusted tool metadata; do not generate them. Do not generate a strength score. Follow the supplied `event_schema` for the Event fields and return only `{"events": [...]}`. The list may be empty.
