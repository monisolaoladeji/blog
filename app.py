import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "posts.db"

UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-change-this-in-production")

CORS(app)


# ---------------- DB ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            image TEXT,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """)
        # Try to add user_id column if it doesn't exist (for existing databases)
        try:
            conn.execute("ALTER TABLE posts ADD COLUMN user_id INTEGER")
        except sqlite3.OperationalError:
            pass # Column already exists


def now():
    return datetime.now(timezone.utc).isoformat()


# ---------------- AUTH ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        if not username or not password:
            flash("All fields are required!", "error")
            return redirect(url_for("register"))
            
        hashed_pw = generate_password_hash(password)
        
        try:
            with get_db() as conn:
                conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
                conn.commit()
            flash("Account created! Please login.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username already exists!", "error")
            
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            flash(f"Welcome back, {username}!", "success")
            return redirect(url_for("my_posts"))
        else:
            flash("Invalid username or password", "error")
            
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


# ---------------- ROUTES ----------------

@app.route("/")
def index():
    search_query = request.args.get("search", "")
    with get_db() as conn:
        if search_query:
            query = """
                SELECT posts.*, users.username as author 
                FROM posts 
                JOIN users ON posts.user_id = users.id 
                WHERE title LIKE ? OR content LIKE ? 
                ORDER BY posts.id DESC
            """
            posts = conn.execute(query, (f"%{search_query}%", f"%{search_query}%")).fetchall()
        else:
            query = """
                SELECT posts.*, users.username as author 
                FROM posts 
                LEFT JOIN users ON posts.user_id = users.id 
                ORDER BY posts.id DESC
            """
            posts = conn.execute(query).fetchall()
    return render_template("index.html", posts=posts, search_query=search_query)


@app.route("/my-posts")
def my_posts():
    if "user_id" not in session:
        flash("Please login to view your posts.", "error")
        return redirect(url_for("login"))

    with get_db() as conn:
        posts = conn.execute("""
            SELECT posts.*, users.username as author
            FROM posts
            JOIN users ON posts.user_id = users.id
            WHERE posts.user_id = ?
            ORDER BY posts.id DESC
        """, (session["user_id"],)).fetchall()

    return render_template("my_posts.html", posts=posts)


@app.route("/posts/new")
def new_post():
    if "user_id" not in session:
        flash("Please login to create a post.", "error")
        return redirect(url_for("login"))
    return render_template("form.html", post=None)


@app.route("/posts", methods=["POST"])
def create_post():
    if "user_id" not in session:
        flash("Unauthorized!", "error")
        return redirect(url_for("login"))

    title = request.form.get("title", "")
    content = request.form.get("content", "")

    if not title or not content:
        flash("Title and content are required!", "error")
        return redirect(url_for("new_post"))

    image_file = request.files.get("image")
    image_name = None

    if image_file and image_file.filename:
        ext = Path(image_file.filename).suffix
        image_name = f"{uuid.uuid4().hex}{ext}"
        image_file.save(UPLOAD_FOLDER / image_name)

    with get_db() as conn:
        conn.execute("""
        INSERT INTO posts (user_id, title, content, image, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (session["user_id"], title, content, image_name, now(), now()))
        conn.commit()

    flash("Post created successfully!", "success")
    return redirect(url_for("index"))


@app.route("/posts/<int:post_id>")
def show_post(post_id):
    with get_db() as conn:
        post = conn.execute("""
            SELECT posts.*, users.username as author 
            FROM posts 
            LEFT JOIN users ON posts.user_id = users.id 
            WHERE posts.id=?
        """, (post_id,)).fetchone()

    if not post:
        flash("Post not found!", "error")
        return redirect(url_for("index"))

    return render_template("show.html", post=post)


@app.route("/posts/<int:post_id>/edit")
def edit_post(post_id):
    if "user_id" not in session:
        flash("Please login first.", "error")
        return redirect(url_for("login"))

    with get_db() as conn:
        post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()

    if not post:
        flash("Post not found!", "error")
        return redirect(url_for("index"))
    
    if post["user_id"] != session["user_id"]:
        flash("You can only edit your own posts!", "error")
        return redirect(url_for("index"))

    return render_template("form.html", post=post)


@app.route("/posts/<int:post_id>/update", methods=["POST"])
def update_post(post_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    title = request.form.get("title", "")
    content = request.form.get("content", "")

    if not title or not content:
        flash("Title and content are required!", "error")
        return redirect(url_for("edit_post", post_id=post_id))

    image_file = request.files.get("image")
    
    with get_db() as conn:
        post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        
        if post["user_id"] != session["user_id"]:
            flash("Unauthorized!", "error")
            return redirect(url_for("index"))

        image_name = post["image"]

        if image_file and image_file.filename:
            if image_name:
                old_image_path = UPLOAD_FOLDER / image_name
                if old_image_path.exists():
                    old_image_path.unlink()
            
            ext = Path(image_file.filename).suffix
            image_name = f"{uuid.uuid4().hex}{ext}"
            image_file.save(UPLOAD_FOLDER / image_name)

        conn.execute("""
        UPDATE posts
        SET title=?, content=?, image=?, updated_at=?
        WHERE id=?
        """, (title, content, image_name, now(), post_id))
        conn.commit()

    flash("Post updated successfully!", "success")
    return redirect(url_for("show_post", post_id=post_id))


@app.route("/posts/<int:post_id>/delete", methods=["POST"])
def delete_post(post_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    with get_db() as conn:
        post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        
        if post["user_id"] != session["user_id"]:
            flash("Unauthorized!", "error")
            return redirect(url_for("index"))

        if post["image"]:
            image_path = UPLOAD_FOLDER / post["image"]
            if image_path.exists():
                image_path.unlink()

        conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
        conn.commit()

    flash("Post deleted successfully!", "success")
    return redirect(url_for("index"))


@app.route("/ask-ai", methods=["POST"])
def ask_ai():
    data = request.json
    question = data.get("question", "").lower()
    
    with get_db() as conn:
        posts = conn.execute("SELECT title, content FROM posts").fetchall()
    
    # Simple "AI" logic: search through posts
    matches = []
    for post in posts:
        if question in post["title"].lower() or question in post["content"].lower():
            matches.append(post["title"])
    
    if "what does this app do" in question:
        reply = "This is a portfolio blogging application built with Flask. It allows you to create, read, update, and delete blog posts with image support."
    elif "who built this" in question or "author" in question:
        reply = "This app was built by Monisola Esther as a portfolio project."
    elif "database" in question or "tech" in question:
        reply = "The app uses Flask (Python) for the backend, SQLite for the database, and Jinja2 for templating."
    elif matches:
        reply = f"I found some posts related to your question: {', '.join(matches[:3])}."
    else:
        # Fallback to general search in content
        reply = "I'm not sure about that. Try asking about the app's features, the author, or search for specific post topics!"

    return jsonify({"reply": reply})


# ---------------- START ----------------

init_db()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
