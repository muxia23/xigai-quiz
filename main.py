import io
import json
import random
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from pathlib import Path

# Auto-bootstrap: install deps into .venv if fastapi not found
try:
    from fastapi import FastAPI, HTTPException, Query
except ImportError:
    venv_dir = Path(__file__).parent / ".venv"
    pip = str(venv_dir / "bin" / "pip")
    python = str(venv_dir / "bin" / "python")
    if not (venv_dir / "bin" / "python").exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    subprocess.run([pip, "install", "fastapi", "uvicorn", "weasyprint"], check=True)
    subprocess.run([python, __file__] + sys.argv[1:])
    sys.exit(0)

from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db


@asynccontextmanager
async def lifespan(_app):
    db.init_database()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RecordCreate(BaseModel):
    question_id: int
    user_answer: str
    is_correct: bool


class AnswerItem(BaseModel):
    question_id: int
    user_answer: str


class ExamSubmit(BaseModel):
    answers: list[AnswerItem]


@app.get("/api/questions")
def get_questions(
    mode: str = Query("fixed"),
    count: int = Query(20),
    resume: bool = Query(False),
    chapter: str = Query(None),
):
    conn = db.get_connection()
    chapter_filter = ""
    params = []
    if chapter and chapter != "all":
        chapters = [c.strip() for c in chapter.split(",")]
        placeholders = ",".join("?" for _ in chapters)
        chapter_filter = f" AND q.chapter IN ({placeholders})"
        params.extend(chapters)

    try:
        if mode == "fixed":
            sql = "SELECT * FROM questions q WHERE 1=1" + chapter_filter + " ORDER BY id"
            rows = conn.execute(sql, params).fetchall() if params else conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
            if resume and rows:
                answered_sql = "SELECT DISTINCT question_id FROM quiz_records"
                rows = [r for r in rows if r["id"] not in {x["question_id"] for x in conn.execute(answered_sql).fetchall()}]
        elif mode == "random":
            sql = "SELECT * FROM questions q WHERE 1=1" + chapter_filter
            all_rows = (conn.execute(sql, params).fetchall() if params else conn.execute("SELECT * FROM questions").fetchall())
            n = len(all_rows) if count <= 0 else min(count, len(all_rows))
            rows = random.sample(all_rows, n)
        elif mode == "wrong":
            sql = """
                SELECT q.*, w.user_answer, w.wrong_count, w.correct_count
                FROM questions q
                JOIN wrong_records w ON q.id = w.question_id
                WHERE 1=1""" + chapter_filter + """
                ORDER BY w.last_wrong_at DESC
            """
            rows = conn.execute(sql, params).fetchall() if params else conn.execute("""
                SELECT q.*, w.user_answer, w.wrong_count, w.correct_count
                FROM questions q
                JOIN wrong_records w ON q.id = w.question_id
                ORDER BY w.last_wrong_at DESC
            """).fetchall()
        else:
            rows = []
    finally:
        conn.close()

    result = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d["options"])
        result.append(d)
    return result


@app.get("/api/search")
def search_questions(q: str = Query(""), limit: int = Query(100)):
    """题目搜索：按题干、选项内容或章节名模糊匹配，返回题目及正确答案（只读）。"""
    keyword = q.strip()
    if not keyword:
        return []
    conn = db.get_connection()
    try:
        like = f"%{keyword}%"
        rows = conn.execute(
            "SELECT * FROM questions WHERE text LIKE ? OR options LIKE ? OR chapter LIKE ?"
            " ORDER BY id LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d["options"])
        result.append(d)
    return result


def _update_wrong_records(conn, question_id: int, user_answer: str, is_correct: bool):
    # correct_count 语义 = 连续答对次数；连续 4 次则自动移出错题集；
    # 任何一次答错都把它清零并累加 wrong_count。
    wr = conn.execute(
        "SELECT id, correct_count FROM wrong_records WHERE question_id = ?", (question_id,)
    ).fetchone()
    if not is_correct:
        if wr:
            conn.execute(
                "UPDATE wrong_records SET wrong_count = wrong_count + 1, correct_count = 0,"
                " user_answer = ?, last_wrong_at = CURRENT_TIMESTAMP WHERE question_id = ?",
                (user_answer, question_id),
            )
        else:
            conn.execute(
                "INSERT INTO wrong_records (question_id, user_answer, wrong_count, correct_count)"
                " VALUES (?,?,1,0)",
                (question_id, user_answer),
            )
    elif wr:
        new_cc = (wr["correct_count"] or 0) + 1
        if new_cc >= 4:
            conn.execute("DELETE FROM wrong_records WHERE question_id = ?", (question_id,))
        else:
            conn.execute(
                "UPDATE wrong_records SET correct_count = ? WHERE question_id = ?",
                (new_cc, question_id),
            )


@app.post("/api/record")
def save_record(record: RecordCreate):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO quiz_records (question_id, user_answer, is_correct) VALUES (?,?,?)",
            (record.question_id, record.user_answer, 1 if record.is_correct else 0),
        )
        _update_wrong_records(conn, record.question_id, record.user_answer, record.is_correct)
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/api/stats")
def get_stats():
    conn = db.get_connection()
    try:
        total_answered = conn.execute("SELECT COUNT(*) FROM quiz_records").fetchone()[0]
        correct_count = conn.execute("SELECT COUNT(*) FROM quiz_records WHERE is_correct = 1").fetchone()[0]
        wrong_count = conn.execute("SELECT COUNT(*) FROM wrong_records").fetchone()[0]
        total_questions = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        distinct_done = conn.execute("SELECT COUNT(DISTINCT question_id) FROM quiz_records").fetchone()[0]

        chapters = conn.execute("SELECT DISTINCT chapter FROM questions ORDER BY id").fetchall()
        chapter_list = [r["chapter"] for r in chapters]

        chapter_stats = conn.execute("""
            SELECT q.chapter,
                   COUNT(DISTINCT q.id) as total,
                   COUNT(DISTINCT r.question_id) as done,
                   SUM(CASE WHEN r.is_correct = 1 THEN 1 ELSE 0 END) as correct,
                   COUNT(r.id) as attempts
            FROM questions q
            LEFT JOIN quiz_records r ON q.id = r.question_id
            GROUP BY q.chapter
            ORDER BY MIN(q.id)
        """).fetchall()
    finally:
        conn.close()

    return {
        "total_answered": total_answered,
        "total_correct": correct_count,
        "wrong_count": wrong_count,
        "total_questions": total_questions,
        "fixed_progress": distinct_done,
        "chapters": chapter_list,
        "accuracy": round(correct_count / total_answered * 100, 1) if total_answered > 0 else 0,
        "chapter_stats": [dict(r) for r in chapter_stats],
    }


@app.post("/api/reset")
def reset_records():
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM quiz_records")
        conn.execute("DELETE FROM wrong_records")
        conn.execute("DELETE FROM exam_answers")
        conn.execute("DELETE FROM exam_sessions")
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/api/wrong/{question_id}")
def delete_wrong(question_id: int):
    """从错题集中手动移除某题。"""
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM wrong_records WHERE question_id = ?", (question_id,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


_STEM_STRIP = re.compile(r"[\s（）()。，,、？?！!：:；;\"'“”‘’．.　]")


def _stem_key(text: str) -> str:
    """Normalized question stem used to detect questions that read as identical
    (e.g. id 9 / id 31 share a stem but differ only in options)."""
    return _STEM_STRIP.sub("", text)


def _select_exam_questions(conn):
    """Pick 20 single + 10 multi questions for one paper, never repeating a
    question stem within the paper. Reads only; performs no writes."""
    single_pool = conn.execute(
        "SELECT * FROM questions WHERE type='single' ORDER BY RANDOM()"
    ).fetchall()
    multi_pool = conn.execute(
        "SELECT * FROM questions WHERE type='multi' ORDER BY RANDOM()"
    ).fetchall()

    seen_stems = set()

    def take(pool, n):
        picked = []
        for r in pool:
            if len(picked) >= n:
                break
            key = _stem_key(r["text"])
            if key in seen_stems:
                continue
            seen_stems.add(key)
            picked.append(r)
        return picked

    return take(single_pool, 20), take(multi_pool, 10)


@app.post("/api/exam/start")
def start_exam():
    conn = db.get_connection()
    try:
        singles, multis = _select_exam_questions(conn)
        cur = conn.execute("INSERT INTO exam_sessions DEFAULT VALUES")
        exam_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    questions = []
    for q in singles:
        d = dict(q)
        d["options"] = json.loads(d["options"])
        d["exam_index"] = len(questions)
        questions.append(d)
    for q in multis:
        d = dict(q)
        d["options"] = json.loads(d["options"])
        d["exam_index"] = len(questions)
        questions.append(d)

    return {"exam_id": exam_id, "questions": questions}


@app.post("/api/exam/submit/{exam_id}")
def submit_exam(exam_id: int, data: ExamSubmit):
    conn = db.get_connection()
    single_score = 0
    multi_score = 0
    try:
        for a in data.answers:
            q = conn.execute("SELECT type, answer FROM questions WHERE id=?", (a.question_id,)).fetchone()
            if not q:
                continue
            is_correct = a.user_answer == q["answer"]
            conn.execute(
                "INSERT INTO exam_answers (exam_id, question_id, user_answer, is_correct) VALUES (?,?,?,?)",
                (exam_id, a.question_id, a.user_answer, 1 if is_correct else 0),
            )
            conn.execute(
                "INSERT INTO quiz_records (question_id, user_answer, is_correct) VALUES (?,?,?)",
                (a.question_id, a.user_answer, 1 if is_correct else 0),
            )
            _update_wrong_records(conn, a.question_id, a.user_answer, is_correct)
            if is_correct:
                if q["type"] == "single":
                    single_score += 1
                else:
                    multi_score += 2
        total = single_score + multi_score
        conn.execute(
            "UPDATE exam_sessions SET submitted_at=CURRENT_TIMESTAMP, total_score=?, single_score=?, multi_score=? WHERE id=?",
            (total, single_score, multi_score, exam_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "exam_id": exam_id,
        "total_score": total,
        "single_score": single_score,
        "single_total": 20,
        "multi_score": multi_score,
        "multi_total": 20,
        "max_score": 40,
    }


@app.get("/api/exam/history")
def exam_history():
    conn = db.get_connection()
    try:
        rows = conn.execute("""
            SELECT id, started_at, submitted_at, total_score, single_score, multi_score
            FROM exam_sessions
            WHERE submitted_at IS NOT NULL
            ORDER BY submitted_at DESC
        """).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get("/api/exam/history/{exam_id}")
def exam_detail(exam_id: int):
    conn = db.get_connection()
    try:
        session = conn.execute(
            "SELECT * FROM exam_sessions WHERE id=? AND submitted_at IS NOT NULL", (exam_id,)
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="not found")

        answers = conn.execute("""
            SELECT ea.*, q.chapter, q.type, q.text, q.options, q.answer
            FROM exam_answers ea
            JOIN questions q ON ea.question_id = q.id
            WHERE ea.exam_id = ?
            ORDER BY q.id
        """, (exam_id,)).fetchall()
    finally:
        conn.close()

    result = dict(session)
    result["answers"] = []
    for a in answers:
        d = dict(a)
        d["options"] = json.loads(d["options"])
        result["answers"].append(d)
    return result


@app.get("/api/wrong/pdf")
def export_wrong_pdf():
    import weasyprint
    data = db.get_wrong_questions_for_pdf()

    lines_html = ""
    for i, q in enumerate(data, 1):
        opts_html = "".join(
            f'<li>{o["label"]}. {escape(o["text"])}</li>' for o in q["options"]
        )
        qtype = "单选题" if q["type"] == "single" else "多选题"
        lines_html += f"""
        <div class="q-item">
          <div class="q-header">第{i}题 [{qtype}] [{escape(q['chapter'])}]  <span class="badge">答错 {q['wrong_count']} 次</span></div>
          <div class="q-text">{escape(q['text'])}</div>
          <ul class="q-opts">{opts_html}</ul>
          <div class="q-ans wrong">你的答案：{escape(q['user_answer'])}</div>
          <div class="q-ans correct">正确答案：{escape(q['answer'])}</div>
        </div>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
  body{{font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;font-size:12px;color:#333;padding:20px;line-height:1.7}}
  h1{{text-align:center;font-size:20px;margin-bottom:4px}}
  .meta{{text-align:center;color:#999;font-size:11px;margin-bottom:20px}}
  .q-item{{background:#f9f9fb;border-radius:6px;padding:14px 16px;margin-bottom:12px;border-left:3px solid #e74c3c;page-break-inside:avoid}}
  .q-header{{font-size:11px;color:#666;margin-bottom:6px}}
  .q-header .badge{{display:inline-block;background:#e74c3c;color:#fff;padding:0 8px;border-radius:99px;font-size:10px}}
  .q-text{{font-size:13px;font-weight:500;margin-bottom:8px}}
  .q-opts{{margin:4px 0 8px 16px;padding:0;font-size:11px;color:#555}}
  .q-opts li{{margin-bottom:2px}}
  .q-ans{{font-size:11px;font-weight:600;padding:2px 0}}
  .q-ans.wrong{{color:#e74c3c}}
  .q-ans.correct{{color:#27ae60}}
  .empty{{text-align:center;padding:40px;color:#999;font-size:14px}}
</style></head><body>
<h1>错题导出报告</h1>
<div class="meta">导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 错题总数：{len(data)} 题</div>
{lines_html if data else '<div class="empty">暂无错题记录</div>'}
</body></html>"""

    pdf_bytes = weasyprint.HTML(string=html).write_pdf()

    from fastapi.responses import Response
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=wrong_questions_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"},
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
