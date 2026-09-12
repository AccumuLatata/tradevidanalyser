import tradevidanalyser.providers.extract as extract_mod
from tradevidanalyser.glossary import MAX_INITIAL_PROMPT_TOKENS, load_glossary, parse_glossary
from tradevidanalyser.providers.extract import FakeExtractProvider
from tradevidanalyser.schema import Transcript, TranscriptSegment


def test_glossary_parse_is_deterministic() -> None:
    first = load_glossary()
    second = load_glossary()
    assert first == second
    assert first.level_tokens == second.level_tokens
    assert isinstance(first.level_tokens, tuple)
    assert isinstance(first.playbook_terms, tuple)
    assert "ONH" in first.level_tokens
    assert "dVWAP" in first.level_tokens
    assert "3c" in first.playbook_terms
    assert len(first.initial_prompt.split()) <= MAX_INITIAL_PROMPT_TOKENS
    assert len(first.initial_prompt.split()) > 0


def test_glossary_parse_from_text_stable() -> None:
    text = (
        "# header\n"
        "## How this is used\n"
        "ignore this prose, it is not a token list\n"
        "## Levels / locations\n"
        "ONH, dVWAP\n"
        "## Playbooks / rules (spoken)\n"
        "3c, Scalp\n"
        "## Platform\n"
        "Grok\n"
    )
    first = parse_glossary(text)
    second = parse_glossary(text)
    assert first == second
    assert first.level_tokens == ("ONH", "dVWAP")
    assert first.playbook_terms == ("3c", "Scalp")
    assert first.platform_terms == ("Grok",)
    assert first.initial_prompt == "ONH dVWAP 3c Scalp Grok"


def test_initial_prompt_caps_at_220_tokens() -> None:
    tokens = ", ".join(f"tok{i}" for i in range(300))
    glossary = parse_glossary(f"## Levels / locations\n{tokens}\n")
    assert len(glossary.level_tokens) == 300
    assert len(glossary.initial_prompt.split()) == MAX_INITIAL_PROMPT_TOKENS
    assert glossary.initial_prompt.split()[0] == "tok0"
    assert glossary.initial_prompt.split()[-1] == "tok219"


def test_fake_extract_reads_glossary_tokens(monkeypatch) -> None:
    custom = parse_glossary(
        "## Levels / locations\nZZTOKEN\n\n## Playbooks / rules (spoken)\nScalp\n"
    )
    monkeypatch.setattr(extract_mod, "load_glossary", lambda: custom)
    transcript = Transcript(
        provider="fake",
        model="keyword-v1",
        segments=[
            TranscriptSegment(
                id="seg_001",
                t0=0.0,
                t1=1.0,
                text="Wir halten ZZTOKEN und nicht ONH.",
            )
        ],
    )
    insights = FakeExtractProvider().extract(transcript)
    assert any(span.token == "ZZTOKEN" for span in insights.stated_levels)
    assert not any(span.token == "ONH" for span in insights.stated_levels)
