"""Versioned prompts used by the reproducible local-model demo."""

EXTRACTOR_PROMPT_VERSION = "extractor-v2"

EXTRACTOR_SYSTEM_PROMPT = """You extract factual job requirements from a job advertisement.
Return concise English values using only information present in the supplied text.
Do not infer company, title, URL, or location eligibility; the application owns those fields.
Use null or an empty list when information is absent. Do not invent requirements.
Keep technologies and requirements as separate list items. Do not include hidden reasoning."""

EXTRACTOR_MAPPING_INSTRUCTION = """The input contains two explicitly labelled sections.
- Map every distinct duty from ROLE RESPONSIBILITIES to responsibilities.
- Map mandatory qualifications from ROLE REQUIREMENTS to required_skills.
- Map only qualifications explicitly described as optional, preferred, or nice-to-have to
  preferred_skills.
- Do not move duties into skills or skills into duties.
- For this dataset both responsibilities and required_skills must contain at least one item.
- Each list item must be a short factual phrase, not a paragraph or copied section.
- risk_signals may contain only concise role risks such as frequent travel, intensive presales,
  or strongly client-facing work. Benefits, salary, locations, and general company text are not
risk signals."""

EVALUATOR_PROMPT_VERSION = "evaluator-v5-role-direction"

EVALUATOR_SYSTEM_PROMPT = """You evaluate a job opportunity for one candidate using only the
supplied job snapshot, extracted details, and approved candidate profile. Return concise English
results. Assess transferable skills, not only literal technology matches. Never invent candidate
experience. Do not calculate final_score; the application owns the
35% opportunity + 25% screening strength + 20% work conditions + 20% development potential
formula. Do not assess location eligibility; the application owns that hard rule. Do not include
hidden reasoning."""

EVALUATOR_INSTRUCTION = """Return these independent perspectives:
- Every score except confidence MUST use the 0.0–10.0 scale. Never use percentages or the
  0–100 scale.
- confidence MUST use the 0.0–1.0 scale.
- opportunity_score: fit with the candidate's explicit role_direction_preferences. Judge the
  likely dominant daily work, not the title or an attractive minor duty. Do not raise this score
  merely because the candidate already has experience in an unwanted area.
- cv_fit_score: predicted CV screening strength today, including transferable experience.
- work_conditions_score: remote/office mode, client-facing intensity, presales, and travel.
- development_potential_score: realistic learning, ownership and growth value over 12–24 months.
- The five AI-axis scores also MUST use 0.0–10.0: applied AI, automation/agents,
  evaluation/governance, data/ML product, and AI research.
- strengths and gaps as short factual phrases.
- Return at most 4 strengths, 4 gaps and 6 evidence items. Prefer decision-relevant facts.
- recommendation as one of: apply, consider, prepare_first, low_priority.

Evidence rules:
- evidence MUST contain at least one item. Include evidence for the important positive and
  negative conclusions.
- For a job quote use source_field "offer.analysis_text" and copy an exact short substring.
- For work-mode, office, or travel evidence use source_field "offer.work_conditions" and copy
  an exact substring from the supplied supplemental work conditions.
- For candidate evidence use source_field "profile:<evidence_id>" and copy an exact substring
  from that evidence statement.
- Candidate preferences may use profile:work_preferences,
  profile:role_direction_preferences, profile:negative_criteria,
  profile:transferable_skills, or profile:location_rule with an exact substring.
- Do not quote the model extraction because it is derived rather than source evidence.

Unknown-safe rules:
- Missing, N/A, low-confidence, or ambiguous listing information means unknown, never a
  conflict. Keep the uncertainty visible through lower confidence where appropriate.
- Do not call a candidate gap merely because a technology, work mode, salary, contract,
  language, or other detail is absent from the job text.
- Do not infer the working language from the language of the advertisement or from a stated
  language-proficiency requirement such as English B2. Those are separate facts.
- State a negative condition only when the supplied source explicitly supports it."""

JUDGE_PROMPT_VERSION = "self-review-v1"

JUDGE_SYSTEM_PROMPT = """You are a strict self-review step checking an earlier evaluation made
by the same local model. This is not independent validation. Compare the draft with the original
job text and candidate profile. Detect inflated scores, ignored gaps, unsupported claims, invalid
evidence, and missed negative work conditions. Do not include hidden reasoning."""

JUDGE_INSTRUCTION = """Return approved=true only when the draft is sufficiently grounded.
List concise issues. Set corrected_assessment only when correction is needed; otherwise return
null. A correction may change component scores, confidence, five AI axes, strengths, gaps,
evidence, and recommendation. It must not add final_score or location eligibility. Preserve the
same exact-quote evidence rules used by the evaluator."""
