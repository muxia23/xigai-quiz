import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "quiz.db"
JSON_PATH = Path(__file__).parent / "26年上学期选择题汇总.json"


def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chapter TEXT NOT NULL,
            type TEXT NOT NULL,
            number INTEGER NOT NULL,
            text TEXT NOT NULL,
            options TEXT NOT NULL,
            answer TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quiz_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            user_answer TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
        CREATE TABLE IF NOT EXISTS wrong_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL UNIQUE,
            user_answer TEXT NOT NULL,
            wrong_count INTEGER DEFAULT 1,
            correct_count INTEGER DEFAULT 0,
            last_wrong_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
    """)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS exam_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            submitted_at TIMESTAMP,
            total_score INTEGER DEFAULT 0,
            single_score INTEGER DEFAULT 0,
            multi_score INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS exam_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            user_answer TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            FOREIGN KEY (exam_id) REFERENCES exam_sessions(id),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
    """)
    try:
        conn.execute("ALTER TABLE wrong_records ADD COLUMN correct_count INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    count = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    if count == 0:
        with open(JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for chapter_data in data:
            chapter = chapter_data["chapter"]
            for qtype, qshort in [("single_choice", "single"), ("multi_choice", "multi")]:
                for q in chapter_data[qtype]:
                    conn.execute(
                        "INSERT INTO questions (chapter, type, number, text, options, answer) VALUES (?,?,?,?,?,?)",
                        (chapter, qshort, q["number"], q["text"],
                         json.dumps(q["options"], ensure_ascii=False), q["answer"]),
                    )
        conn.commit()
    conn.close()


def get_wrong_questions_for_pdf():
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT q.chapter, q.type, q.text, q.options, q.answer,
                   w.user_answer, w.wrong_count, w.correct_count
            FROM questions q
            JOIN wrong_records w ON q.id = w.question_id
            ORDER BY w.wrong_count DESC, q.chapter, q.id
        """).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d["options"])
        result.append(d)
    return result
