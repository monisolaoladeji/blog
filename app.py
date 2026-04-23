import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "posts.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-secret-key"


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


@app.get("/")
def index():
    with get_db() as conn:
        posts = conn.execute(
            """
            SELECT id, title, content, created_at, updated_at
            FROM posts
            ORDER BY id DESC;
            """
        ).fetchall()
    return render_template("index.html", posts=posts)


@app.get("/posts/new")
def new_post():
    return render_template("form.html", post=None)


@app.post("/posts")
def create_post():
    title = (request.form.get("title") or "").strip()
    content = (request.form.get("content") or "").strip()

    if not title or not content:
        flash("Title and content are required.", "error")
        return render_template("form.html", post={"title": title, "content": content}), 400

    now = utc_now_iso()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO posts (title, content, created_at, updated_at)
            VALUES (?, ?, ?, ?);
            """,
            (title, content, now, now),
        )
        post_id = cur.lastrowid

    flash("Post created.", "success")
    return redirect(url_for("show_post", post_id=post_id))


@app.get("/posts/<int:post_id>")
def show_post(post_id: int):
    with get_db() as conn:
        post = conn.execute(
            """
            SELECT id, title, content, created_at, updated_at
            FROM posts
            WHERE id = ?;
            """,
            (post_id,),
        ).fetchone()

    if not post:
        return render_template("404.html"), 404

    return render_template("show.html", post=post)


@app.get("/posts/<int:post_id>/edit")
def edit_post(post_id: int):
    with get_db() as conn:
        post = conn.execute(
            """
            SELECT id, title, content, created_at, updated_at
            FROM posts
            WHERE id = ?;
            """,
            (post_id,),
        ).fetchone()

    if not post:
        return render_template("404.html"), 404

    return render_template("form.html", post=post)


@app.post("/posts/<int:post_id>/update")
def update_post(post_id: int):
    title = (request.form.get("title") or "").strip()
    content = (request.form.get("content") or "").strip()

    if not title or not content:
        flash("Title and content are required.", "error")
        return render_template(
            "form.html",
            post={"id": post_id, "title": title, "content": content},
        ), 400

    now = utc_now_iso()
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE posts
            SET title = ?, content = ?, updated_at = ?
            WHERE id = ?;
            """,
            (title, content, now, post_id),
        )

    if cur.rowcount == 0:
        return render_template("404.html"), 404

    flash("Post updated.", "success")
    return redirect(url_for("show_post", post_id=post_id))


@app.post("/posts/<int:post_id>/delete")
def delete_post(post_id: int):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM posts WHERE id = ?;", (post_id,))

    if cur.rowcount == 0:
        return render_template("404.html"), 404

    flash("Post deleted.", "success")
    return redirect(url_for("index"))


init_db()


if __name__ == "__main__":
    app.run(debug=True)
