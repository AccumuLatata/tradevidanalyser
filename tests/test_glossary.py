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
    assert "ETH" in first.level_tokens
    assert "ETH." not in first.level_tokens
    assert "3c" in first.playbook_terms
    assert "ONH Touch" in first.playbook_terms
    assert "Brief" in first.platform_terms
    assert all(not token.endswith(".") for token in first.tokens)
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


def test_glossary_strips_trailing_list_punctuation() -> None:
    glossary = parse_glossary(
        "## Levels / locations\nNY open, ETH.\n"
        "## Playbooks / rules (spoken)\nONH Touch.\n"
        "## Platform\nBrief.\n"
    )
    assert glossary.level_tokens == ("NY open", "ETH")
    assert glossary.playbook_terms == ("ONH Touch",)
    assert glossary.platform_terms == ("Brief",)
    assert "ETH." not in glossary.initial_prompt
    assert "Touch." not in glossary.initial_prompt


def test_load_glossary_picks_up_file_change(tmp_path) -> None:
    path = tmp_path / "GLOSSARY.md"
    path.write_text("## Levels / locations\nAAA\n", encoding="utf-8")
    assert load_glossary(path).level_tokens == ("AAA",)
    path.write_text("## Levels / locations\nBBB\n", encoding="utf-8")
    assert load_glossary(path).level_tokens == ("BBB",)


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


def test_fake_extract_matches_level_tokens_case_insensitively(monkeypatch) -> None:
    custom = parse_glossary("## Levels / locations\nONH, dVWAP, ETH\n")
    monkeypatch.setattr(extract_mod, "load_glossary", lambda: custom)
    transcript = Transcript(
        provider="fake",
        model="keyword-v1",
        segments=[
            TranscriptSegment(
                id="seg_001",
                t0=0.0,
                t1=1.0,
                text="ich warte am onh, dann am dvwap, und am eth.",
            )
        ],
    )
    insights = FakeExtractProvider().extract(transcript)
    assert {span.token for span in insights.stated_levels} == {"ONH", "dVWAP", "ETH"}


def test_fake_extract_finds_eth_from_committed_glossary() -> None:
    transcript = Transcript(
        provider="fake",
        model="keyword-v1",
        segments=[
            TranscriptSegment(id="seg_001", t0=0.0, t1=1.0, text="Wir sind nach ETH raus."),
        ],
    )
    insights = FakeExtractProvider().extract(transcript)
    assert any(span.token == "ETH" for span in insights.stated_levels)
