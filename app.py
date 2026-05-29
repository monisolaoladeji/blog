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

def _is_platform_runtime() -> bool:
    return bool(os.getenv("VERCEL") or os.getenv("RENDER") or os.getenv("NETLIFY"))

# Use writable temporary DB on serverless platforms (VERCEL/RENDER/NETLIFY)
DB_PATH = Path(os.getenv("BLOG_DB_PATH", "/tmp/posts.db" if _is_platform_runtime() else str(BASE_DIR / "posts.db")))

# On serverless platforms use a writable temp dir; otherwise use the repo static/uploads
UPLOAD_FOLDER = Path(os.getenv("BLOG_UPLOAD_FOLDER", "/tmp/uploads" if _is_platform_runtime() else str(BASE_DIR / "static" / "uploads")))
try:
    UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
except OSError:
    # Some platforms may disallow creating directories; fall back to temp dir
    UPLOAD_FOLDER = Path("/tmp/uploads")
    try:
        UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-change-this-in-production")

CORS(app)

# Optional MongoDB support: enable when MONGO_URI env var is provided
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "blog_db")
MONGO_ENABLED = False
users_collection = posts_collection = None
if MONGO_URI:
    try:
        from pymongo import MongoClient
        from bson.objectid import ObjectId

        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # trigger server selection
        mongo_client.server_info()
        mongo_db = mongo_client[MONGO_DB_NAME]
        users_collection = mongo_db["users"]
        posts_collection = mongo_db["posts"]
        MONGO_ENABLED = True
    except Exception as exc:
        print(f"MongoDB init failed: {exc}")
        MONGO_ENABLED = False


# ---------------- DB ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    if MONGO_ENABLED and users_collection is not None and posts_collection is not None:
        try:
            users_collection.create_index("username", unique=True)
            posts_collection.create_index("created_at")
        except Exception:
            pass
        return

    # Ensure DB directory exists when using SQLite fallback
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

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

        if MONGO_ENABLED and users_collection is not None:
            try:
                users_collection.insert_one({"username": username, "password": hashed_pw})
                flash("Account created! Please login.", "success")
                return redirect(url_for("login"))
            except Exception:
                flash("Username already exists!", "error")
        else:
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
        
        if MONGO_ENABLED and users_collection is not None:
            user = users_collection.find_one({"username": username})
            if user and check_password_hash(user.get("password", ""), password):
                session["user_id"] = str(user.get("_id"))
                session["username"] = user.get("username")
                flash(f"Welcome back, {username}!", "success")
                return redirect(url_for("my_posts"))
            else:
                flash("Invalid username or password", "error")
        else:
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
    if MONGO_ENABLED and posts_collection is not None:
        if search_query:
            docs = list(posts_collection.find({"$or": [{"title": {"$regex": search_query, "$options": "i"}}, {"content": {"$regex": search_query, "$options": "i"}}]}).sort([("created_at", -1)]))
        else:
            docs = list(posts_collection.find({}).sort("created_at", -1))
        posts = []
        for d in docs:
            posts.append({
                "id": str(d.get("_id")),
                "user_id": d.get("user_id"),
                "title": d.get("title"),
                "content": d.get("content"),
                "image": d.get("image"),
                "created_at": d.get("created_at"),
                "author": d.get("author", "")
            })
    else:
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
    if MONGO_ENABLED and posts_collection is not None:
        posts = list(posts_collection.find({"user_id": session["user_id"]}).sort("created_at", -1))
        # map ids
        posts = [{"id": str(p.get("_id")), "title": p.get("title"), "content": p.get("content"), "image": p.get("image"), "created_at": p.get("created_at"), "author": p.get("author")} for p in posts]
    else:
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

    image_name = None
    if image_file and image_file.filename:
        ext = Path(image_file.filename).suffix
        image_name = f"{uuid.uuid4().hex}{ext}"
        try:
            image_file.save(UPLOAD_FOLDER / image_name)
        except Exception as exc:
            image_name = None
            flash(f"Image upload failed: {exc}", "error")

    if MONGO_ENABLED and posts_collection is not None:
        post_doc = {
            "user_id": session.get("user_id"),
            "author": session.get("username"),
            "title": title,
            "content": content,
            "image": image_name,
            "created_at": now(),
            "updated_at": now(),
        }
        posts_collection.insert_one(post_doc)
    else:
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
    if MONGO_ENABLED and posts_collection is not None:
        from bson.objectid import ObjectId
        try:
            doc = posts_collection.find_one({"_id": ObjectId(post_id)})
        except Exception:
            doc = None
        if not doc:
            flash("Post not found!", "error")
            return redirect(url_for("index"))
        post = {"id": str(doc.get("_id")), "title": doc.get("title"), "content": doc.get("content"), "image": doc.get("image"), "created_at": doc.get("created_at"), "author": doc.get("author")}
        return render_template("show.html", post=post)

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

    if MONGO_ENABLED and posts_collection is not None:
        from bson.objectid import ObjectId
        try:
            doc = posts_collection.find_one({"_id": ObjectId(post_id)})
        except Exception:
            doc = None
        if not doc:
            flash("Post not found!", "error")
            return redirect(url_for("index"))
        if doc.get("user_id") != session.get("user_id"):
            flash("You can only edit your own posts!", "error")
            return redirect(url_for("index"))
        post = {"id": str(doc.get("_id")), "title": doc.get("title"), "content": doc.get("content"), "image": doc.get("image"), "created_at": doc.get("created_at"), "author": doc.get("author")}
        return render_template("form.html", post=post)

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

    if MONGO_ENABLED and posts_collection is not None:
        from bson.objectid import ObjectId
        try:
            doc = posts_collection.find_one({"_id": ObjectId(post_id)})
        except Exception:
            doc = None
        if not doc:
            flash("Post not found!", "error")
            return redirect(url_for("index"))
        if doc.get("user_id") != session.get("user_id"):
            flash("Unauthorized!", "error")
            return redirect(url_for("index"))

        image_name = doc.get("image")
        if image_file and image_file.filename:
            if image_name:
                old_image_path = UPLOAD_FOLDER / image_name
                if old_image_path.exists():
                    old_image_path.unlink()
            try:
                ext = Path(image_file.filename).suffix
                image_name = f"{uuid.uuid4().hex}{ext}"
                image_file.save(UPLOAD_FOLDER / image_name)
            except Exception as exc:
                flash(f"Image upload failed: {exc}", "error")

        posts_collection.update_one({"_id": ObjectId(post_id)}, {"$set": {"title": title, "content": content, "image": image_name, "updated_at": now()}})
        return redirect(url_for("show_post", post_id=post_id))

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
            try:
                ext = Path(image_file.filename).suffix
                image_name = f"{uuid.uuid4().hex}{ext}"
                image_file.save(UPLOAD_FOLDER / image_name)
            except Exception as exc:
                flash(f"Image upload failed: {exc}", "error")

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

    if MONGO_ENABLED and posts_collection is not None:
        from bson.objectid import ObjectId
        try:
            doc = posts_collection.find_one({"_id": ObjectId(post_id)})
        except Exception:
            doc = None
        if not doc:
            flash("Post not found!", "error")
            return redirect(url_for("index"))
        if doc.get("user_id") != session.get("user_id"):
            flash("Unauthorized!", "error")
            return redirect(url_for("index"))

        if doc.get("image"):
            image_path = UPLOAD_FOLDER / doc.get("image")
            if image_path.exists():
                image_path.unlink()

        posts_collection.delete_one({"_id": ObjectId(post_id)})
        flash("Post deleted successfully!", "success")
        return redirect(url_for("index"))

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
