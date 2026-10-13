from copilot.generation.citations import check_citations
from copilot.generation.prompt import REFUSAL
from copilot.models import Chunk, Role, ScoredChunk


def _sources(n):
    return [
        ScoredChunk(
            Chunk(f"d#{i}", f"doc{i}", "t", f"Doc {i}", "S", f"text {i}", Role.EMPLOYEE), 1.0
        )
        for i in range(1, n + 1)
    ]


def test_valid_citations_are_mapped():
    out = check_citations("Leave is 25 days per year [1]. Carry-over is 5 days [2].", _sources(2))
    assert [c.marker for c in out.citations] == [1, 2]
    assert out.uncited_sentences == []
    assert out.coverage == 1.0


def test_out_of_range_markers_are_removed():
    out = check_citations("Leave is 25 days per year [1][7].", _sources(2))
    assert "[7]" not in out.text
    assert out.invalid_markers == [7]


def test_uncited_sentences_are_flagged():
    out = check_citations(
        "Leave is 25 days per year [1]. You also get a free car every year.", _sources(1)
    )
    assert out.uncited_sentences == ["You also get a free car every year."]
    assert out.coverage == 0.5


def test_comma_separated_markers():
    out = check_citations("Both documents agree on this rule [1, 2].", _sources(2))
    assert [c.marker for c in out.citations] == [1, 2]


def test_refusal_needs_no_citations():
    out = check_citations(REFUSAL, _sources(2))
    assert out.citations == [] and out.uncited_sentences == []
