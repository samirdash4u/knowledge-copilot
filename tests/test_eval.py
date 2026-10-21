from pathlib import Path

from copilot.eval.runner import evaluate, format_table, load_questions
from copilot.retrieval.vector import load_chunks

ROOT = Path(__file__).resolve().parents[1]


def test_eval_runs_with_zero_leaks(settings, service):
    questions = load_questions(ROOT / "eval" / "questions.jsonl")
    chunks = load_chunks(settings.index_dir / "chunks.jsonl")
    report = evaluate(service, questions, chunks, ["bm25", "hybrid"])
    assert all(m["leaks"] == 0 for m in report["modes"].values()), report["leak_details"]
    assert report["cache_poisoning_leaks"] == 0
    # BM25 alone should find most answers in this small corpus
    assert report["modes"]["bm25"]["recall@6"] >= 0.8
    assert "| bm25 |" in format_table(report)


def test_cache_poisoning_test_catches_a_shared_cache(settings, service, monkeypatch):
    """If the cache ignored the caller's access scope, the poisoning pass must report leaks."""
    from copilot import cache as cache_mod
    from copilot.eval.runner import cache_poisoning_test

    questions = load_questions(ROOT / "eval" / "questions.jsonl")
    chunks = load_chunks(settings.index_dir / "chunks.jsonl")
    assert cache_poisoning_test(service, questions, chunks) == 0
    monkeypatch.setattr(cache_mod, "partition_key", lambda principal, version: version)
    assert cache_poisoning_test(service, questions, chunks) > 0


def test_eval_answers_only_in_answer_modes(settings, service):
    from copilot.eval.runner import sample_questions

    questions = sample_questions(load_questions(ROOT / "eval" / "questions.jsonl"), 8)
    assert len(questions) == 8 and len({q.type for q in questions}) == 8
    chunks = load_chunks(settings.index_dir / "chunks.jsonl")
    steps = []
    report = evaluate(
        service,
        questions,
        chunks,
        ["bm25", "hybrid"],
        answer_modes=["hybrid"],
        progress=lambda done, total, label: steps.append((done, total)),
    )
    assert steps[-1] == (8, 8)  # one LLM answer per question, in one mode
    assert report["modes"]["bm25"]["answered"] is False
    assert report["modes"]["bm25"]["groundedness"] is None
    assert report["modes"]["hybrid"]["groundedness"] is not None
    assert report["cache_poisoning_leaks"] == 0
    assert "bm25 (retrieval only)" in format_table(report)
