from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    send_from_directory,
    abort
)
import sqlite3
import io
import os
import uuid
from werkzeug.security import generate_password_hash, check_password_hash
import json

# 画像変換はオプション機能。Pillowがなくてもアプリ本体は起動できる。
try:
    from PIL import Image, ImageOps, ImageFile, UnidentifiedImageError
    PIL_AVAILABLE = True

    # 軽微に欠損したJPEG/PNGでも、可能な範囲で読み込めるようにする。
    ImageFile.LOAD_TRUNCATED_IMAGES = True

    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        HEIF_AVAILABLE = True
    except ImportError:
        HEIF_AVAILABLE = False
except ImportError:
    Image = None
    ImageOps = None
    UnidentifiedImageError = Exception
    PIL_AVAILABLE = False
    HEIF_AVAILABLE = False

MAX_IMAGE_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_STORED_IMAGE_EDGE = 2000

app = Flask(__name__)

# =========================
# 基本設定
# =========================

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "dev-secret-key"
)

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

# Railwayでは DATA_DIR=/data を設定する
# ローカルでは従来通りapp.pyの場所を使う
DATA_DIR = os.environ.get(
    "DATA_DIR",
    BASE_DIR
)

os.makedirs(
    DATA_DIR,
    exist_ok=True
)

DB_NAME = os.path.join(
    DATA_DIR,
    "user.db"
)

# Railwayでは /data/uploads
# ローカルでは従来通り static/uploads
if os.environ.get("DATA_DIR"):
    UPLOAD_DIR = os.path.join(
        DATA_DIR,
        "uploads"
    )
else:
    UPLOAD_DIR = os.path.join(
        app.static_folder,
        "uploads"
    )

os.makedirs(
    UPLOAD_DIR,
    exist_ok=True
)

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
        """
    )
    
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            tags TEXT,
            short_description TEXT,
            description TEXT,
            interest INTEGER DEFAULT 3,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS works (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            tags TEXT,
            progress INTEGER DEFAULT 0,
            interest INTEGER DEFAULT 50,

            content_json TEXT DEFAULT '{"pageWidth":794,"pageHeight":1123,"pages":[{"elements":[]}]}',
            reference_sites TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (room_id) REFERENCES rooms(id)
        )
        """
    )
    
    conn.commit()
    conn.close()

init_db()

@app.context_processor
def inject_sidebar_data():
    """ログイン後の各画面で共通左バーに必要な展示室・作品情報を渡す。"""
    user_id = session.get("user_id")

    empty_context = {
        "sidebar_rooms": [],
        "sidebar_current_room": None,
        "sidebar_current_room_id": None,
        "sidebar_current_work_id": None,
        "sidebar_works": [],
        "sidebar_tags": [],
    }

    if not user_id:
        return empty_context

    view_args = request.view_args or {}
    current_room_id = view_args.get("room_id")
    current_work_id = view_args.get("work_id")

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row

    room_rows = conn.execute(
        """
        SELECT
            rooms.id,
            rooms.name,
            rooms.interest,
            COUNT(works.id) AS work_count
        FROM rooms
        LEFT JOIN works
            ON works.room_id = rooms.id
        WHERE rooms.user_id = ?
        GROUP BY rooms.id, rooms.name, rooms.interest
        ORDER BY rooms.id DESC
        """,
        (user_id,),
    ).fetchall()

    sidebar_rooms = [dict(row) for row in room_rows]
    current_room = next(
        (room for room in sidebar_rooms if room["id"] == current_room_id),
        None,
    )

    sidebar_works = []
    sidebar_tags = []

    if current_room is not None:
        work_rows = conn.execute(
            """
            SELECT
                works.id,
                works.room_id,
                works.title,
                COALESCE(works.tags, '') AS tags,
                works.progress,
                works.interest
            FROM works
            JOIN rooms
                ON rooms.id = works.room_id
            WHERE
                works.room_id = ?
                AND rooms.user_id = ?
            ORDER BY works.id DESC
            """,
            (current_room_id, user_id),
        ).fetchall()

        sidebar_works = [dict(row) for row in work_rows]

        tag_set = set()
        for work in sidebar_works:
            for tag in work["tags"].split(","):
                cleaned = tag.strip()
                if cleaned:
                    tag_set.add(cleaned)

        sidebar_tags = sorted(tag_set, key=str.casefold)

    conn.close()

    return {
        "sidebar_rooms": sidebar_rooms,
        "sidebar_current_room": current_room,
        "sidebar_current_room_id": current_room_id,
        "sidebar_current_work_id": current_work_id,
        "sidebar_works": sidebar_works,
        "sidebar_tags": sidebar_tags,
    }


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["POST"])
def register():
    err = {}
    
    username = request.form.get("name").strip()
    password = request.form.get("password").strip()
    
    if not username:
        err["username"] = "ユーザ名を入力してください"
    if not password:
        err["password"] = "パスワードを入力してください"
    
    if len(list(err)) > 0:
        return render_template(
            "index.html",
            err_rname = err.get("username"),
            err_rpass = err.get("password")
        )
    
    password_hashed = generate_password_hash(password)

    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()    
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hashed)
        )

        user_id = cur.lastrowid
        
        conn.commit()
        conn.close()

    except sqlite3.IntegrityError:
        err["username"] = "そのユーザ名は使われています"
        return render_template(
            "index.html",
            err_rname=err.get("username"),
        )
    
    session.clear()
    session["user_id"] = user_id
    session["username"] = username    
    
    return redirect(url_for("room_list"))

@app.route("/login", methods=["POST"])
def login():
    err = {}
    
    username = request.form.get("name").strip()
    password = request.form.get("password").strip()
    
    if not username:
        err["username"] = "ユーザ名を入力してください"
    if not password:
        err["password"] = "パスワードを入力してください"

    if len(list(err)) > 0:
        return render_template(
            "index.html",
            err_lname=err.get("username"),
            err_lpass = err.get("password")
        )

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()    
    cur.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,)
    )
    user = cur.fetchone()
    conn.close()
    
    if user is None:
        return render_template(
            "index.html",
            err_lname="ユーザが存在しません"
        )
    
    password_hash = user[2]
    if check_password_hash(password_hash, password):
        session.clear()
        session["user_id"] = user[0]
        session["username"] = user[1]

        return redirect(url_for("room_list"))
    else:
        return render_template(
            "index.html",
            err_lpass="パスワードが違います"
        )
    
@app.route("/rooms")
def room_list():
    if "user_id" not in session:
        return redirect(url_for("index"))
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    rooms = conn.execute(
        """
        SELECT 
            rooms.id,
            rooms.name,
            rooms.tags,
            rooms.short_description,
            rooms.description,
            rooms.interest,
            rooms.created_at,
            rooms.updated_at,
            COUNT(works.id) AS work_count
        FROM rooms
        LEFT JOIN works ON works.room_id = rooms.id
        WHERE rooms.user_id = ?
        GROUP BY 
            rooms.id,
            rooms.name,
            rooms.tags,
            rooms.short_description,
            rooms.description,
            rooms.interest,
            rooms.created_at,
            rooms.updated_at
        ORDER BY rooms.id DESC
        """,
        (session["user_id"], )
    ).fetchall()
    conn.close()
    
    return render_template(
        "rooms.html",
        username=session["username"],
        rooms=rooms
    )
    
@app.route("/rooms/create", methods=["GET", "POST"])
def room_create():
    if "user_id" not in session:
        return redirect(url_for("index"))
    
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        tags = request.form .get("tags", "").strip()

        short_description = request.form.get("short_description", "").strip()
        description = request.form.get("description", "").strip()
        interest = request.form.get("interest", "50")
        
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO rooms(
                user_id, 
                name, 
                tags,
                short_description,
                description,
                interest,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
            """,
            (
                session["user_id"],
                name,
                tags,
                short_description, 
                description, 
                interest
            )
        )

        conn.commit()
        conn.close()

        return redirect(url_for("room_list"))
    return render_template(
        "room_create.html",
        mode="create"
    )

@app.route("/rooms/<int:room_id>")
def room_detail(room_id):
    if "user_id" not in session:
        return redirect(url_for("index"))

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row

    room = conn.execute(
        """
        SELECT
            id,
            name,
            tags,
            short_description,
            description,
            interest
        FROM rooms
        WHERE id = ? AND user_id = ?
        """,
        (
            room_id,
            session["user_id"]
        )
    ).fetchone()

    if room is None:
        conn.close()
        return redirect(url_for("room_list"))

    works = conn.execute(
        """
        SELECT
            id,
            title,
            tags,
            progress,
            interest,
            created_at,
            updated_at
        FROM works
        WHERE room_id = ?
        ORDER BY id DESC
        """,
        (room_id,)
    ).fetchall()

    conn.close()

    return render_template(
        "works.html",
        room=room,
        works=works,
        username=session["username"]
    )


@app.route("/rooms/<int:room_id>/edit", methods=["GET", "POST"])
def room_edit(room_id):
    if "user_id" not in session:
        return redirect(url_for("index"))
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if request.method == "POST":        
        name = request.form.get("name", "").strip()
        tags = request.form.get("tags", "").strip()

        short_description = request.form.get("short_description", "").strip()
        description = request.form.get("description", "").strip()
        interest = request.form.get("interest", "50")

        cur.execute(
            """
            UPDATE rooms
            SET
                name = ?,
                tags = ?,
                short_description = ?,
                description = ?,
                interest = ?,
                updated_at = datetime('now', 'localtime')
            WHERE id = ? AND user_id = ?
            """,
            (
                name,
                tags,
                short_description,
                description,
                interest,
                room_id,
                session["user_id"]
            )
        )
        
        conn.commit()
        conn.close()
        
        return redirect(url_for("room_list"))
    
    room = cur.execute(
        """
        SELECT
            id,
            name,
            tags,
            short_description,
            description,
            interest
        FROM rooms
        WHERE id = ? AND user_id = ?
        """,
        (room_id, session["user_id"])
    ).fetchone()

    conn.close()

    if room is None:
        return redirect(url_for("room_list"))

    return render_template(
        "room_create.html",
        room = room,
        mode = "edit"
    )
    
@app.route("/rooms/<int:room_id>/delete", methods=["POST"])
def room_delete(room_id):
    if "user_id" not in session:
        return redirect(url_for("index"))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # 本当にログイン中ユーザーの展示室か確認
    room = cur.execute(
        """
        SELECT id
        FROM rooms
        WHERE id = ? AND user_id = ?
        """,
        (
            room_id,
            session["user_id"]
        )
    ).fetchone()

    if room is None:
        conn.close()
        return redirect(url_for("room_list"))

    # 展示室内の作品を先に削除
    cur.execute(
        """
        DELETE FROM works
        WHERE room_id = ?
        """,
        (room_id,)
    )

    # 展示室を削除
    cur.execute(
        """
        DELETE FROM rooms
        WHERE id = ? AND user_id = ?
        """,
        (
            room_id,
            session["user_id"]
        )
    )

    conn.commit()
    conn.close()

    return redirect(url_for("room_list"))
    
@app.route("/rooms/<int:room_id>/works/create", methods=["GET", "POST"])
def work_create(room_id):
    if "user_id" not in session:
        return redirect(url_for("index"))

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    content_json = request.form.get(
        "content_json",
        '{"pageWidth":794,"pageHeight":1123,"pages":[{"elements":[]}]}'
    )

    reference_sites = request.form.get(
        "reference_sites",
        ""
    ).strip()

    # この展示室がログインユーザーのものか確認
    room = cur.execute(
        """
        SELECT
            id,
            name
        FROM rooms
        WHERE id = ? AND user_id = ?
        """,
        (
            room_id,
            session["user_id"]
        )
    ).fetchone()

    if room is None:
        conn.close()
        return redirect(url_for("room_list"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        tags = request.form.get("tags", "").strip()
        progress = request.form.get("progress", "0")
        interest = request.form.get("interest", "50")

        if not title:
            conn.close()

            return render_template(
                "work_create.html",
                room=room,
                error_title="作品名を入力してください"
            )

        cur.execute(
            """
            INSERT INTO works(
                room_id,
                title,
                tags,
                progress,
                interest,
                content_json,
                reference_sites,
                created_at,
                updated_at
            )
            VALUES(
                ?, ?, ?, ?, ?, ?, ?,
                datetime('now', 'localtime'),
                datetime('now', 'localtime')
            )
            """,
            (
                room_id,
                title,
                tags,
                progress,
                interest,
                content_json,
                reference_sites
            )
        )

        conn.commit()
        conn.close()

        return redirect(
            url_for(
                "room_detail",
                room_id=room_id
            )
        )

    conn.close()

    return render_template(
        "work_create.html",
        room=room,
        work=None,
        mode="create",
        report={
            "pageWidth": 794,
            "pageHeight": 1123,
            "pages": [{"elements": []}]
        }
    )

@app.route("/rooms/<int:room_id>/works/<int:work_id>")
def work_detail(room_id, work_id):
    if "user_id" not in session:
        return redirect(url_for("index"))

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row

    work = conn.execute(
        """
        SELECT
            works.id,
            works.room_id,
            works.title,
            works.tags,
            works.progress,
            works.interest,
            works.content_json,
            works.reference_sites,
            works.created_at,
            works.updated_at,
            rooms.name AS room_name
        FROM works
        JOIN rooms
            ON rooms.id = works.room_id
        WHERE
            works.id = ?
            AND works.room_id = ?
            AND rooms.user_id = ?
        """,
        (
            work_id,
            room_id,
            session["user_id"]
        )
    ).fetchone()

    conn.close()

    if work is None:
        return redirect(
            url_for(
                "room_detail",
                room_id=room_id
            )
        )

    try:
        report = json.loads(
            work["content_json"]
            or '{"pageWidth":794,"pageHeight":1123,"pages":[{"elements":[]}]}'
        )
    except json.JSONDecodeError:
        report = {
            "pageWidth": 794,
            "pageHeight": 1123,
            "pages": [{"elements": []}]
        }

    return render_template(
        "work_detail.html",
        work=work,
        report=report
    )
    
@app.route(
    "/rooms/<int:room_id>/works/<int:work_id>/edit",
    methods=["GET", "POST"]
)
def work_edit(room_id, work_id):
    if "user_id" not in session:
        return redirect(url_for("index"))

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 展示室がログインユーザーのものか確認
    room = cur.execute(
        """
        SELECT
            id,
            name
        FROM rooms
        WHERE id = ? AND user_id = ?
        """,
        (
            room_id,
            session["user_id"]
        )
    ).fetchone()

    if room is None:
        conn.close()
        return redirect(url_for("room_list"))

    # 編集対象作品
    work = cur.execute(
        """
        SELECT
            id,
            room_id,
            title,
            tags,
            progress,
            interest,
            content_json,
            reference_sites,
            created_at,
            updated_at
        FROM works
        WHERE id = ? AND room_id = ?
        """,
        (
            work_id,
            room_id
        )
    ).fetchone()

    if work is None:
        conn.close()

        return redirect(
            url_for(
                "room_detail",
                room_id=room_id
            )
        )

    # =========================
    # 保存
    # =========================

    if request.method == "POST":

        title = request.form.get(
            "title",
            ""
        ).strip()

        tags = request.form.get(
            "tags",
            ""
        ).strip()

        progress = request.form.get(
            "progress",
            "0"
        )

        interest = request.form.get(
            "interest",
            "50"
        )

        content_json = request.form.get(
            "content_json",
            '{"pageWidth":794,"pageHeight":1123,"pages":[{"elements":[]}]}'
        )

        reference_sites = request.form.get(
            "reference_sites",
            ""
        ).strip()

        if not title:

            try:
                report = json.loads(
                    content_json
                )
            except json.JSONDecodeError:
                report = {
                    "pageWidth": 794,
                    "pageHeight": 1123,
                    "pages": [{"elements": []}]
                }

            work_data = dict(work)

            work_data.update({
                "title": title,
                "tags": tags,
                "progress": progress,
                "interest": interest,
                "content_json": content_json,
                "reference_sites": reference_sites
            })

            conn.close()

            return render_template(
                "work_create.html",
                room=room,
                work=work_data,
                report=report,
                mode="edit",
                error_title="作品名を入力してください"
            )

        cur.execute(
            """
            UPDATE works
            SET
                title = ?,
                tags = ?,
                progress = ?,
                interest = ?,
                content_json = ?,
                reference_sites = ?,
                updated_at = datetime(
                    'now',
                    'localtime'
                )
            WHERE
                id = ?
                AND room_id = ?
            """,
            (
                title,
                tags,
                progress,
                interest,
                content_json,
                reference_sites,
                work_id,
                room_id
            )
        )

        conn.commit()
        conn.close()

        return redirect(
            url_for(
                "work_detail",
                room_id=room_id,
                work_id=work_id
            )
        )

    # =========================
    # 編集画面を開く
    # =========================

    try:
        report = json.loads(
            work["content_json"]
            or '{"pageWidth":794,"pageHeight":1123,"pages":[{"elements":[]}]}'
        )
    except json.JSONDecodeError:
        report = {
            "pageWidth": 794,
            "pageHeight": 1123,
            "pages": [{"elements": []}]
        }

    conn.close()

    return render_template(
        "work_create.html",
        room=room,
        work=work,
        report=report,
        mode="edit"
    )
    
@app.route(
    "/rooms/<int:room_id>/works/<int:work_id>/delete",
    methods=["POST"]
)
def work_delete(room_id, work_id):
    if "user_id" not in session:
        return redirect(url_for("index"))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # 展示室がログインユーザーのものか確認
    room = cur.execute(
        """
        SELECT id
        FROM rooms
        WHERE id = ? AND user_id = ?
        """,
        (
            room_id,
            session["user_id"]
        )
    ).fetchone()

    if room is None:
        conn.close()
        return redirect(url_for("room_list"))

    # 指定された展示室に存在する作品だけ削除
    cur.execute(
        """
        DELETE FROM works
        WHERE id = ? AND room_id = ?
        """,
        (
            work_id,
            room_id
        )
    )

    conn.commit()
    conn.close()

    return redirect(
        url_for(
            "room_detail",
            room_id=room_id
        )
    )

@app.route("/api/images/convert", methods=["POST"])
def convert_image():
    if "user_id" not in session:
        return jsonify(error="ログインが必要です。"), 401

    if not PIL_AVAILABLE:
        return jsonify(
            error="画像変換機能には Pillow が必要です。python -m pip install Pillow を実行してください。"
        ), 501

    uploaded = request.files.get("image")

    if uploaded is None or not uploaded.filename:
        return jsonify(error="画像ファイルが選択されていません。"), 400

    raw = uploaded.read()

    if len(raw) > MAX_IMAGE_UPLOAD_BYTES:
        return jsonify(error="画像ファイルが大きすぎます。50MB以下の画像を使用してください。"), 413

    filename = uploaded.filename.lower()

    if filename.endswith((".heic", ".heif")) and not HEIF_AVAILABLE:
        return jsonify(
            error="HEIC/HEIFの変換には pillow-heif が必要です。"
                  "PNG/JPGだけを使う場合は pillow-heif は不要です。"
        ), 415

    try:
        with Image.open(io.BytesIO(raw)) as image:
            try:
                image.seek(0)
            except EOFError:
                pass

            image.load()
            image = ImageOps.exif_transpose(image)

            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image.thumbnail(
                (MAX_STORED_IMAGE_EDGE, MAX_STORED_IMAGE_EDGE),
                resampling
            )

            has_alpha = (
                image.mode in ("RGBA", "LA") or
                (image.mode == "P" and "transparency" in image.info)
            )

            # Base64をJSONへ埋め込むと作品保存時のフォームが巨大化するため、
            # 変換後画像はstatic/uploadsへ保存してURLだけを返す。
            user_folder = os.path.join(
                UPLOAD_DIR,
                f"user_{session['user_id']}"
            )
            os.makedirs(user_folder, exist_ok=True)

            image_id = uuid.uuid4().hex

            if has_alpha:
                converted = image.convert("RGBA")
                stored_name = f"{image_id}.png"
                stored_path = os.path.join(user_folder, stored_name)
                converted.save(stored_path, format="PNG", optimize=True)
            else:
                converted = image.convert("RGB")
                stored_name = f"{image_id}.jpg"
                stored_path = os.path.join(user_folder, stored_name)
                converted.save(
                    stored_path,
                    format="JPEG",
                    quality=88,
                    optimize=True,
                    progressive=False
                )

            relative_path = (
                f"user_{session['user_id']}/{stored_name}"
            )

            return jsonify(
                src=url_for(
                    "uploaded_file",
                    filename=relative_path
                ),
                width=converted.width,
                height=converted.height
            )

    except UnidentifiedImageError:
        return jsonify(
            error="画像として認識できませんでした。拡張子がPNG/JPGでも、内部データが別形式または破損している可能性があります。"
        ), 415
    except (OSError, ValueError) as exc:
        return jsonify(error=f"画像データを正常に展開できませんでした: {exc}"), 415
    except Exception as exc:
        return jsonify(error=f"画像の変換に失敗しました: {exc}"), 500

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    if "user_id" not in session:
        return redirect(url_for("index"))

    filename = filename.replace("\\", "/")

    user_prefix = (
        f"user_{session['user_id']}/"
    )

    if not filename.startswith(user_prefix):
        abort(403)

    return send_from_directory(
        UPLOAD_DIR,
        filename
    )

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))

def _percent_arg(name):
    """0～100 の数値検索条件を安全に取得する。空欄・不正値は None。"""
    raw = request.args.get(name, "").strip()

    if raw == "":
        return None

    try:
        value = int(raw)
    except ValueError:
        return None

    return max(0, min(100, value))


def _append_text_condition(conditions, params, columns, value, partial):
    """複数列に対する文字検索条件を追加する。"""
    value = (value or "").strip()

    if not value:
        return

    if partial:
        conditions.append(
            "(" + " OR ".join(
                f"COALESCE({column}, '') LIKE ?" for column in columns
            ) + ")"
        )
        params.extend([f"%{value}%"] * len(columns))
    else:
        conditions.append(
            "(" + " OR ".join(
                f"COALESCE({column}, '') = ?" for column in columns
            ) + ")"
        )
        params.extend([value] * len(columns))


def _append_date_conditions(conditions, params, column, date_from, date_to):
    if date_from:
        conditions.append(f"date({column}) >= date(?)")
        params.append(date_from)

    if date_to:
        conditions.append(f"date({column}) <= date(?)")
        params.append(date_to)


@app.route("/search")
def search_page():
    if "user_id" not in session:
        return redirect(url_for("index"))

    # -------------------------
    # 検索条件
    # -------------------------
    target = request.args.get("target", "all")

    if target not in {"all", "rooms", "works"}:
        target = "all"

    keyword = request.args.get("keyword", "").strip()
    room_name = request.args.get("room_name", "").strip()
    work_name = request.args.get("work_name", "").strip()

    created_from = request.args.get("created_from", "").strip()
    created_to = request.args.get("created_to", "").strip()
    updated_from = request.args.get("updated_from", "").strip()
    updated_to = request.args.get("updated_to", "").strip()

    progress_min = _percent_arg("progress_min")
    progress_max = _percent_arg("progress_max")
    interest_min = _percent_arg("interest_min")
    interest_max = _percent_arg("interest_max")

    # チェック時は部分一致。外した場合は完全一致。
    partial = request.args.get("partial", "1") == "1"

    submitted = request.args.get("submitted") == "1"

    filters = {
        "target": target,
        "keyword": keyword,
        "room_name": room_name,
        "work_name": work_name,
        "created_from": created_from,
        "created_to": created_to,
        "updated_from": updated_from,
        "updated_to": updated_to,
        "progress_min": "" if progress_min is None else progress_min,
        "progress_max": "" if progress_max is None else progress_max,
        "interest_min": "" if interest_min is None else interest_min,
        "interest_max": "" if interest_max is None else interest_max,
        "partial": partial,
    }

    room_results = []
    work_results = []

    if submitted:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row

        # ==========================================================
        # 展示室検索
        # ==========================================================
        # 作品名・進捗度は作品だけが持つため、これらが指定された場合は
        # 「すべて」検索でも展示室単体は結果に含めない。
        room_search_allowed = (
            target in {"all", "rooms"}
            and not work_name
            and progress_min is None
            and progress_max is None
        )

        if room_search_allowed:
            room_conditions = ["rooms.user_id = ?"]
            room_params = [session["user_id"]]

            _append_text_condition(
                room_conditions,
                room_params,
                [
                    "rooms.name",
                    "rooms.tags",
                    "rooms.short_description",
                    "rooms.description",
                ],
                keyword,
                partial,
            )

            _append_text_condition(
                room_conditions,
                room_params,
                ["rooms.name"],
                room_name,
                partial,
            )

            _append_date_conditions(
                room_conditions,
                room_params,
                "rooms.created_at",
                created_from,
                created_to,
            )

            _append_date_conditions(
                room_conditions,
                room_params,
                "rooms.updated_at",
                updated_from,
                updated_to,
            )

            if interest_min is not None:
                room_conditions.append("rooms.interest >= ?")
                room_params.append(interest_min)

            if interest_max is not None:
                room_conditions.append("rooms.interest <= ?")
                room_params.append(interest_max)

            room_rows = conn.execute(
                f"""
                SELECT
                    rooms.id,
                    rooms.name,
                    rooms.tags,
                    rooms.short_description,
                    rooms.description,
                    rooms.interest,
                    rooms.created_at,
                    rooms.updated_at,
                    COUNT(works.id) AS work_count
                FROM rooms
                LEFT JOIN works
                    ON works.room_id = rooms.id
                WHERE {' AND '.join(room_conditions)}
                GROUP BY
                    rooms.id,
                    rooms.name,
                    rooms.tags,
                    rooms.short_description,
                    rooms.description,
                    rooms.interest,
                    rooms.created_at,
                    rooms.updated_at
                ORDER BY rooms.updated_at DESC, rooms.id DESC
                """,
                room_params,
            ).fetchall()

            room_results = [dict(row) for row in room_rows]

        # ==========================================================
        # 作品検索
        # ==========================================================
        if target in {"all", "works"}:
            work_conditions = ["rooms.user_id = ?"]
            work_params = [session["user_id"]]

            _append_text_condition(
                work_conditions,
                work_params,
                [
                    "works.title",
                    "works.tags",
                    "rooms.name",
                ],
                keyword,
                partial,
            )

            _append_text_condition(
                work_conditions,
                work_params,
                ["works.title"],
                work_name,
                partial,
            )

            _append_text_condition(
                work_conditions,
                work_params,
                ["rooms.name"],
                room_name,
                partial,
            )

            _append_date_conditions(
                work_conditions,
                work_params,
                "works.created_at",
                created_from,
                created_to,
            )

            _append_date_conditions(
                work_conditions,
                work_params,
                "works.updated_at",
                updated_from,
                updated_to,
            )

            if progress_min is not None:
                work_conditions.append("works.progress >= ?")
                work_params.append(progress_min)

            if progress_max is not None:
                work_conditions.append("works.progress <= ?")
                work_params.append(progress_max)

            if interest_min is not None:
                work_conditions.append("works.interest >= ?")
                work_params.append(interest_min)

            if interest_max is not None:
                work_conditions.append("works.interest <= ?")
                work_params.append(interest_max)

            work_rows = conn.execute(
                f"""
                SELECT
                    works.id,
                    works.room_id,
                    works.title,
                    works.tags,
                    works.progress,
                    works.interest,
                    works.created_at,
                    works.updated_at,
                    rooms.name AS room_name
                FROM works
                JOIN rooms
                    ON rooms.id = works.room_id
                WHERE {' AND '.join(work_conditions)}
                ORDER BY works.updated_at DESC, works.id DESC
                """,
                work_params,
            ).fetchall()

            work_results = [dict(row) for row in work_rows]

        conn.close()

    return render_template(
        "search.html",
        filters=filters,
        submitted=submitted,
        room_results=room_results,
        work_results=work_results,
        result_count=len(room_results) + len(work_results),
    )

if __name__ == "__main__":
    init_db()
    # app.run(debug=True)
    app.run(host="0.0.0.0", port=5000, debug=True)