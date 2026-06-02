"""Tests for mock-exam question selection: no duplicate questions in one paper.

Uses an in-memory DB so the real quiz.db is never touched (DB-safe).
"""
import json
import sqlite3

import main


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE questions (id INTEGER PRIMARY KEY, chapter TEXT, type TEXT,"
        " number INTEGER, text TEXT, options TEXT, answer TEXT)"
    )
    return conn


def _insert(conn, qid, qtype, text, answer="A"):
    conn.execute(
        "INSERT INTO questions (id, chapter, type, number, text, options, answer)"
        " VALUES (?,?,?,?,?,?,?)",
        (qid, "ch", qtype, qid, text,
         json.dumps([{"label": "A", "text": "x"}], ensure_ascii=False), answer),
    )


def test_no_duplicate_stem_in_single_section():
    """Two single questions with the identical stem (the 9/31 case) must not
    both appear in one paper."""
    conn = _make_conn()
    # one duplicated-stem pair, plus enough filler to reach 20
    _insert(conn, 1, "single", "（ ）是近代以来中国人民的共同梦想。")
    _insert(conn, 2, "single", "（ ）是近代以来中国人民的共同梦想。")  # same stem, different question
    for i in range(3, 60):
        _insert(conn, i, "single", f"单选填充题{i}？")
    for i in range(60, 100):
        _insert(conn, i, "multi", f"多选填充题{i}？")

    for _ in range(200):  # randomized selection -> run many times
        singles, multis = main._select_exam_questions(conn)
        stems = [main._stem_key(r["text"]) for r in singles + multis]
        assert len(stems) == len(set(stems)), f"duplicate stem in paper: {stems}"
        assert not (any(r["id"] == 1 for r in singles) and
                    any(r["id"] == 2 for r in singles)), "both 9/31-style dups picked"


def test_counts_preserved():
    conn = _make_conn()
    for i in range(1, 51):
        _insert(conn, i, "single", f"单选{i}？")
    for i in range(51, 81):
        _insert(conn, i, "multi", f"多选{i}？")
    singles, multis = main._select_exam_questions(conn)
    assert len(singles) == 20
    assert len(multis) == 10


def test_no_duplicate_ids():
    conn = _make_conn()
    for i in range(1, 51):
        _insert(conn, i, "single", f"单选{i}？")
    for i in range(51, 81):
        _insert(conn, i, "multi", f"多选{i}？")
    for _ in range(100):
        singles, multis = main._select_exam_questions(conn)
        ids = [r["id"] for r in singles + multis]
        assert len(ids) == len(set(ids))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all tests passed")
