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
