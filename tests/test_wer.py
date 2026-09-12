from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from tradevidanalyser import config
from tradevidanalyser.cli import main
from tradevidanalyser.glossary import load_glossary
from tradevidanalyser.wer import jargon_recall, n_words, word_error_rate


GERMAN_JARGON = "Ich warte am ONH, dann am dVWAP, und gehe 3c gegen die Bewegung."


def test_wer_identical_text_is_zero() -> None:
    text = "Bias ist long. Playbook ONH Touch."
    assert word_error_rate(text, text) == 0.0
    assert word_error_rate(text, text.lower()) == 0.0
    assert word_error_rate("Hallo, Welt!", "hallo welt") == 0.0


def test_wer_known_substitution() -> None:
    ref = "eins zwei drei vier"
    hyp = "eins zwei vier vier"
    assert word_error_rate(ref, hyp) == 0.25
    assert n_words(ref) == 4


def test_wer_known_deletion() -> None:
    assert word_error_rate("eins zwei drei", "eins zwei") == pytest.approx(1 / 3)


def test_wer_known_insertion() -> None:
    assert word_error_rate("eins zwei drei", "eins zwei drei vier") == pytest.approx(1 / 3)


def test_wer_nfc_matches_nfd_umlauts() -> None:
    nfc = "prüfen Größe"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert word_error_rate(nfc, nfd) == 0.0


def test_wer_umlaut_folding_off_by_default() -> None:
    ref = "Größe prüfen"
    hyp = "Groesse pruefen"
    assert word_error_rate(ref, hyp) > 0
    assert word_error_rate(ref, hyp, fold_umlauts=True) == 0.0


def test_jargon_recall_onh_dvwap_3c_in_german() -> None:
    tokens = ("ONH", "dVWAP", "3c")
    assert jargon_recall(GERMAN_JARGON, GERMAN_JARGON, tokens) == 1.0
    dropped_onh = "Ich warte am Open, dann am dVWAP, und gehe 3c gegen die Bewegung."
    assert jargon_recall(GERMAN_JARGON, dropped_onh, tokens) == pytest.approx(2 / 3)
    glossary = load_glossary()
    assert jargon_recall(GERMAN_JARGON, GERMAN_JARGON, glossary.tokens) == 1.0
    # Full glossary must actually see ONH — vacuous 1.0 when nothing matches is not enough.
    assert jargon_recall(GERMAN_JARGON, dropped_onh, glossary.tokens) < 1.0


@pytest.mark.golden
def test_golden_excerpt_identity_or_skip() -> None:
    path = config.golden_dir(config.resolve_root())
    if not path.is_dir():
        pytest.skip("golden excerpt absent")
    ref = path / "reference.txt"
    if not ref.is_file():
        pytest.skip("golden reference.txt absent")
    text = ref.read_text(encoding="utf-8")
    assert word_error_rate(text, text) == 0.0


def test_cli_wer_json(tva_root: Path, sample_video: Path, tmp_path: Path, capsys) -> None:
    assert main(["--root", str(tva_root), "ingest", str(sample_video)]) == 0
    capsys.readouterr()
    assert main(["--root", str(tva_root), "transcribe", "2026-09-11_143000"]) == 0
    capsys.readouterr()
    ref = tmp_path / "reference.txt"
    ref.write_text(
        "Bias long. Playbook ONH Touch. Stop unter dem Level. Check-in zur halben Stunde.",
        encoding="utf-8",
    )
    code = main(["--root", str(tva_root), "wer", "2026-09-11_143000", "--ref", str(ref)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wer"] == 0.0
    assert payload["jargon_recall"] == 1.0
    assert payload["n_words"] > 0
    assert set(payload) == {"wer", "jargon_recall", "n_words"}
