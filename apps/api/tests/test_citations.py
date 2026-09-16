"""Citation normalization tests: guardrails.normalize_citations + prompts V2 + allowlist."""
from app.agent import graph as G
from app.agent.guardrails import apply_guardrails, normalize_citations
from app.agent.prompts import RAG_USER_TEMPLATE_V2, SYSTEM_PROMPT_V2
from app.services.retriever import RetrievedChunk

C1 = RetrievedChunk(chunk_id="c1", doc_id="d1", title="KromaRAG Overview", content="hybrid text", ord=0, score=0.9)
C2 = RetrievedChunk(chunk_id="c2", doc_id="d2", title="Guide", content="rbac text", ord=1, score=0.6)


def test_prompts_v2_forbid_legacy_symbols():
    assert "⌗" in SYSTEM_PROMPT_V2  # explicitly mentioned as FORBIDDEN
    assert "FORBIDDEN" in SYSTEM_PROMPT_V2
    assert ":00.50" in SYSTEM_PROMPT_V2
    assert "[doc:" in SYSTEM_PROMPT_V2
    assert "{allowlist}" in RAG_USER_TEMPLATE_V2


def test_rag_prompt_builds_allowlist_and_sanitizes():
    chunks = [
        {"title": "Bad:Title[weird]", "ord": 0, "content": "x" * 5000},
        {"title": "Guide", "ord": 1, "content": "y"},
    ]
    prompt = G._rag_prompt("What is X?", chunks)
    # Sanitized title (no brackets/colons) + allowlist present
    assert "[doc:Bad-Title-weird-:0]" in prompt
    assert "Valid citations" in prompt
    assert "- [doc:Guide:1]" in prompt
    assert "What is X?" in prompt


def test_normalize_legacy_badge_with_timestamp():
    ans = "KromaRAG blends vectors ⌗ KromaRAG Overview:00.50 and more."
    cleaned, changed = normalize_citations(ans, [C1])
    assert changed is True
    assert "⌗" not in cleaned
    assert ":00.50" not in cleaned
    assert "[doc:KromaRAG Overview:0]" in cleaned


def test_normalize_canonical_decimal():
    ans = "Blends vectors [doc:KromaRAG Overview:00.50]."
    cleaned, changed = normalize_citations(ans, [C1])
    assert "[doc:KromaRAG Overview:0]" in cleaned
    assert ":00.50" not in cleaned


def test_normalize_drops_invented_and_numeric():
    ans = "Invented [doc:Nope:5] and numeric [1] plus placeholder [doc:Title:Ord]."
    cleaned, changed = normalize_citations(ans, [C1])
    assert changed is True
    assert "[doc:Nope:5]" not in cleaned
    assert "[1]" not in cleaned
    assert "[doc:Title:Ord]" not in cleaned
    assert "⌗" not in cleaned


def test_normalize_preserves_fenced_code():
    ans = "```\ncode ⌗ KromaRAG Overview:00.50 [doc:Nope:5]\n```\nReal [doc:Guide:1]."
    cleaned, _ = normalize_citations(ans, [C1, C2])
    assert "code ⌗ KromaRAG Overview:00.50 [doc:Nope:5]" in cleaned  # untouched in fence
    assert "[doc:Guide:1]" in cleaned


def test_apply_guardrails_flags_normalized():
    ans = "Hi ⌗ Guide:01.50 end."
    safe, flags = apply_guardrails(ans, [C2])
    assert "citations-normalized" in flags
    assert "⌗" not in safe
    assert "[doc:Guide:1]" in safe


def test_valid_citation_untouched_no_flag():
    ans = "Grounded [doc:Guide:1]."
    safe, flags = apply_guardrails(ans, [C2])
    assert safe == ans
    assert "citations-normalized" not in flags


def test_normalize_legacy_leading_newline_with_timestamp():
    ans = "Blends vectors\n⌗ KromaRAG Overview:00.41 and more."
    cleaned, changed = normalize_citations(ans, [C1])
    assert changed is True
    assert "⌗" not in cleaned
    assert ":00.41" not in cleaned
    assert "[doc:KromaRAG Overview:0]" in cleaned


def test_normalize_legacy_leading_space_plain_ord():
    ans = "Blends vectors  ⌗ Guide:0 end."
    cleaned, changed = normalize_citations(ans, [C2])
    assert changed is True
    assert "⌗" not in cleaned
    assert "[doc:Guide:0]" not in cleaned  # ord 0 not valid for Guide(ord=1) → stripped
    assert "Guide" in cleaned


def test_normalize_legacy_leading_space_with_timestamp():
    ans = "Hi \n ⌗ Guide:01.50 end."
    cleaned, changed = normalize_citations(ans, [C2])
    assert changed is True
    assert "⌗" not in cleaned
    assert ":01.50" not in cleaned
    assert "[doc:Guide:1]" in cleaned
