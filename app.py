import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import gridfs
from bson import ObjectId
from bson.errors import InvalidId
from flask import Flask, Response, abort, redirect, render_template, request, session, url_for
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

import mongo_db
from mongo_db import MONGO_DB_NAME, ensure_mongo_ready, get_database


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["UPLOAD_FOLDER"] = str(Path("static") / "uploads")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "").lower() in {"1", "true", "yes"}

if os.getenv("TRUST_PROXY", "1").lower() not in {"0", "false", "no"}:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

UPLOAD_FOLDER = Path(app.config["UPLOAD_FOLDER"])
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

SOCIAL_DEFAULTS_SYNCED = False

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "mkv"}

CATEGORY_OPTIONS = [
    {"value": "memes", "label": "Memes", "emoji": "\U0001F602", "description": "Hindi-English memes and relatable chaos."},
    {"value": "jokes", "label": "Jokes", "emoji": "\U0001F923", "description": "Quick punchlines and silly one-liners."},
    {"value": "motivation", "label": "Motivation", "emoji": "\U0001F525", "description": "Energy boosts and comeback content."},
    {"value": "coding", "label": "Coding", "emoji": "\U0001F4BB", "description": "Developer humor and build-mode vibes."},
]

MOOD_OPTIONS = [
    {"value": "happy", "label": "Happy", "emoji": "\U0001F604", "description": "Bright, fun, and high-energy posts."},
    {"value": "sad", "label": "Sad", "emoji": "\U0001F614", "description": "Soft comfort, warm jokes, and gentle recovery."},
    {"value": "bored", "label": "Bored", "emoji": "\U0001F971", "description": "Fast, punchy posts to wake the feed up."},
    {"value": "focused", "label": "Focused", "emoji": "\U0001F9E0", "description": "Sharper content for grind mode."},
]


def get_db():
    return get_database()


def get_mongo_error():
    global SOCIAL_DEFAULTS_SYNCED
    ready, error = ensure_mongo_ready()
    if ready and not SOCIAL_DEFAULTS_SYNCED:
        sync_social_defaults()
        SOCIAL_DEFAULTS_SYNCED = True
    return None if ready else error


def get_option_map(options):
    return {option["value"]: option for option in options}


def get_current_user():
    username = session.get("user")
    if not username:
        return None
    return get_db().users.find_one({"username": username})


def get_avatar_for_username(username):
    return (username[:1].upper() if username else "U")


def with_social_defaults(user):
    if not user:
        return user
    normalized = dict(user)
    normalized.setdefault("followers", [])
    normalized.setdefault("friends", [])
    normalized.setdefault("friend_requests_sent", [])
    normalized.setdefault("friend_requests_received", [])
    normalized.setdefault("saved_posts", [])
    normalized.setdefault("blocked_users", [])
    return normalized


def sync_social_defaults():
    db = get_db()
    for user in db.users.find(
        {},
        {
            "username": 1,
            "followers": 1,
            "friends": 1,
            "friend_requests_sent": 1,
            "friend_requests_received": 1,
            "saved_posts": 1,
            "blocked_users": 1,
        },
    ):
        updates = {}
        for field in (
            "followers",
            "friends",
            "friend_requests_sent",
            "friend_requests_received",
            "saved_posts",
            "blocked_users",
        ):
            if field not in user:
                updates[field] = []
        if updates:
            db.users.update_one({"username": user["username"]}, {"$set": updates})


def build_relationship_state(current_user, viewed_user):
    current_username = (current_user or {}).get("username")
    viewed_username = (viewed_user or {}).get("username")
    if not current_username or not viewed_username or current_username == viewed_username:
        return {
            "is_following": False,
            "is_friend": False,
            "request_sent": False,
            "request_received": False,
            "has_blocked": False,
        }

    current_user = with_social_defaults(current_user)
    viewed_user = with_social_defaults(viewed_user)
    return {
        "is_following": current_username in viewed_user.get("followers", []),
        "is_friend": viewed_username in current_user.get("friends", []),
        "request_sent": viewed_username in current_user.get("friend_requests_sent", []),
        "request_received": viewed_username in current_user.get("friend_requests_received", []),
        "has_blocked": viewed_username in current_user.get("blocked_users", []),
    }


def enrich_user_card(user, current_user=None):
    user = add_avatar_fields(with_social_defaults(user))
    relationship = build_relationship_state(current_user, user)
    return {
        **user,
        "followers_count": len(user.get("followers", [])),
        "friends_count": len(user.get("friends", [])),
        "saved_posts_count": len(user.get("saved_posts", [])),
        "is_active": is_user_active(user),
        "last_seen_label": format_last_seen_label(user),
        **relationship,
    }


def rename_username_references(old_username, new_username):
    db = get_db()
    for user in db.users.find({}, {"username": 1, "followers": 1, "friends": 1, "friend_requests_sent": 1, "friend_requests_received": 1}):
        updates = {}
        for field in ("followers", "friends", "friend_requests_sent", "friend_requests_received"):
            values = user.get(field, [])
            if old_username in values:
                updates[field] = [new_username if value == old_username else value for value in values]
        if updates:
            db.users.update_one({"username": user["username"]}, {"$set": updates})


def allowed_media_file(filename):
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in ALLOWED_IMAGE_EXTENSIONS:
        return "image"
    if extension in ALLOWED_VIDEO_EXTENSIONS:
        return "video"
    return None


def build_media_url(file_id=None, filename=None):
    if file_id:
        return url_for("serve_media", file_id=file_id)
    if filename:
        return url_for("static", filename=f"uploads/{filename}")
    return None


def save_uploaded_media(file_storage):
    if not file_storage or not file_storage.filename:
        return None, None

    safe_name = secure_filename(file_storage.filename)
    media_type = allowed_media_file(safe_name)
    if not media_type:
        return None, "Only image and video files are allowed."

    unique_name = f"{uuid4().hex}_{safe_name}"
    if mongo_db.USING_LOCAL_STORE:
        file_storage.save(UPLOAD_FOLDER / unique_name)
        return {"media_filename": unique_name, "media_type": media_type}, None

    file_content = file_storage.read()
    if not file_content:
        return None, "Uploaded file is empty."

    file_id = gridfs.GridFS(get_db()).put(
        file_content,
        filename=unique_name,
        content_type=file_storage.mimetype or "application/octet-stream",
    )
    return {
        "media_filename": unique_name,
        "media_file_id": str(file_id),
        "media_type": media_type,
    }, None


def save_uploaded_avatar(file_storage):
    payload, error = save_uploaded_media(file_storage)
    if error:
        return None, error
    if payload and payload.get("media_type") != "image":
        return None, "Profile photo must be an image file."
    if not payload:
        return None, None
    return {
        "avatar_filename": payload.get("media_filename"),
        "avatar_file_id": payload.get("media_file_id"),
    }, None


def save_uploaded_image(file_storage):
    payload, error = save_uploaded_avatar(file_storage)
    if error or not payload:
        return payload, error
    return {
        "cover_filename": payload.get("avatar_filename"),
        "cover_file_id": payload.get("avatar_file_id"),
    }, None


def sync_avatar_media(username, avatar_payload):
    db = get_db()
    avatar_updates = {"avatar_filename": avatar_payload.get("avatar_filename")}
    if "avatar_file_id" in avatar_payload:
        avatar_updates["avatar_file_id"] = avatar_payload.get("avatar_file_id")
    db.posts.update_many({"author_username": username}, {"$set": avatar_updates})
    db.stories.update_many({"username": username}, {"$set": avatar_updates})


def format_created_label(created_at):
    created_at = normalize_datetime(created_at)
    return created_at.strftime("%d %b, %I:%M %p") if created_at else "Just now"


def add_avatar_fields(record):
    if not record:
        return record
    normalized = dict(record)
    normalized["avatar_url"] = build_media_url(
        normalized.get("avatar_file_id"),
        normalized.get("avatar_filename"),
    )
    normalized["cover_url"] = build_media_url(
        normalized.get("cover_file_id"),
        normalized.get("cover_filename"),
    )
    normalized["avatar_label"] = normalized.get("avatar") or get_avatar_for_username(
        normalized.get("username") or normalized.get("user")
    )
    return normalized


def is_user_active(user, minutes=5):
    last_seen = normalize_datetime((user or {}).get("last_seen"))
    if not last_seen:
        return False
    return (datetime.now(timezone.utc) - last_seen).total_seconds() <= minutes * 60


def format_last_seen_label(user):
    if is_user_active(user):
        return "Active now"
    last_seen = normalize_datetime((user or {}).get("last_seen"))
    if not last_seen:
        return "Offline"
    return f"Seen {format_created_label(last_seen)}"


def normalize_datetime(value):
    if not value or not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def normalize_post(post, current_username=None):
    normalized = add_avatar_fields(post)
    normalized["created_label"] = format_created_label(normalized.get("created_at"))
    normalized["media_url"] = build_media_url(
        normalized.get("media_file_id"),
        normalized.get("media_filename"),
    )
    normalized["is_video"] = normalized.get("media_type") == "video"
    normalized["profile_username"] = normalized.get("author_username")
    normalized["likes_count"] = len(normalized.get("liked_by", []))
    normalized["likes"] = normalized["likes_count"]
    normalized["liked_by_me"] = bool(
        current_username and current_username in normalized.get("liked_by", [])
    )
    normalized["saved_by_me"] = bool(
        current_username and current_username in normalized.get("saved_by", [])
    )
    normalized["saves_count"] = len(normalized.get("saved_by", []))
    comments = normalized.get("comments", [])
    normalized["comments"] = [
        {
            **comment,
            "created_label": format_created_label(comment.get("created_at")),
        }
        for comment in comments
    ]
    normalized["comments_count"] = len(normalized["comments"])
    return normalized


def normalize_story(story):
    normalized = add_avatar_fields(story)
    normalized["created_label"] = format_created_label(normalized.get("created_at"))
    normalized["media_url"] = build_media_url(
        normalized.get("media_file_id"),
        normalized.get("media_filename"),
    )
    normalized["is_video"] = normalized.get("media_type") == "video"
    return normalized


def build_feed_context(current_user):
    category_map = get_option_map(CATEGORY_OPTIONS)
    mood_map = get_option_map(MOOD_OPTIONS)
    selected_interests = current_user.get("interests", []) if current_user else []
    selected_mood_value = current_user.get("selected_mood") if current_user else None

    return {
        "category_map": category_map,
        "mood_map": mood_map,
        "selected_interests": [
            category_map[value] for value in selected_interests if value in category_map
        ],
        "selected_mood": mood_map.get(selected_mood_value),
        "mood_options": MOOD_OPTIONS,
    }


def get_suggested_users(current_username):
    current_user = add_avatar_fields(with_social_defaults(get_current_user() or {}))
    return [
        enrich_user_card(user, current_user)
        for user in get_db().users.find(
            {"username": {"$ne": current_username}},
            {
                "username": 1,
                "selected_mood": 1,
                "avatar": 1,
                "avatar_file_id": 1,
                "avatar_filename": 1,
                "bio": 1,
                "interests": 1,
                "followers": 1,
                "friends": 1,
                "friend_requests_sent": 1,
                "friend_requests_received": 1,
            },
        ).sort("username", 1).limit(10)
    ]


def search_users_by_username(query, current_username, limit=8):
    query = (query or "").strip().lower()
    if not query:
        return []

    current_user = with_social_defaults(get_current_user() or {})
    matches = []
    for user in get_db().users.find(
        {"username": {"$ne": current_username}},
        {
            "username": 1,
            "avatar": 1,
            "avatar_file_id": 1,
            "avatar_filename": 1,
            "bio": 1,
            "followers": 1,
            "friends": 1,
            "friend_requests_sent": 1,
            "friend_requests_received": 1,
        },
    ).sort("username", 1):
        username = (user or {}).get("username", "")
        if query in username.lower():
            matches.append(enrich_user_card(user, current_user))
        if len(matches) >= limit:
            break
    return matches


def get_incoming_friend_requests(current_username, limit=None):
    if not current_username:
        return []

    current_user = with_social_defaults(get_current_user() or {})
    request_usernames = current_user.get("friend_requests_received", [])
    if limit is not None:
        request_usernames = request_usernames[:limit]

    incoming_requests = []
    for username in request_usernames:
        user = get_db().users.find_one(
            {"username": username},
            {
                "username": 1,
                "avatar": 1,
                "avatar_file_id": 1,
                "avatar_filename": 1,
                "bio": 1,
                "followers": 1,
                "friends": 1,
                "friend_requests_sent": 1,
                "friend_requests_received": 1,
            },
        )
        if user:
            incoming_requests.append(enrich_user_card(user, current_user))
    return incoming_requests


def get_active_stories(limit=12):
    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = cutoff.timestamp() - (24 * 60 * 60)
    stories = []
    for story in get_db().stories.find({}, {"_id": 0}).sort("created_at", -1):
        created_at = story.get("created_at")
        if not created_at or created_at.timestamp() < cutoff:
            continue
        stories.append(normalize_story(story))
        if len(stories) >= limit:
            break
    return stories


def get_saved_posts_for_user(username, limit=12):
    current_user = with_social_defaults(get_current_user() or {})
    user = with_social_defaults(get_db().users.find_one({"username": username}, {"saved_posts": 1}))
    if not user:
        return []
    saved_slugs = user.get("saved_posts", [])[:limit]
    if not saved_slugs:
        return []
    posts = list(get_db().posts.find({"slug": {"$in": saved_slugs}}, {"_id": 0}))
    posts_by_slug = {post["slug"]: normalize_post(post, current_user.get("username")) for post in posts}
    return [posts_by_slug[slug] for slug in saved_slugs if slug in posts_by_slug]


def build_navigation_context():
    current_username = session.get("user")
    if not current_username:
        return {"nav_conversations_count": 0, "nav_friend_requests_count": 0}
    conversations = list(get_db().messages.find({"participants": current_username}))
    current_user = with_social_defaults(get_current_user() or {})
    return {
        "nav_conversations_count": len(conversations),
        "nav_friend_requests_count": len(current_user.get("friend_requests_received", [])),
    }


@app.before_request
def mark_user_active():
    if "user" not in session:
        return None
    try:
        get_db().users.update_one(
            {"username": session["user"]},
            {"$set": {"last_seen": datetime.now(timezone.utc)}},
        )
    except PyMongoError:
        return None
    return None


@app.route("/")
def home():
    return redirect(url_for("dashboard" if "user" in session else "login"))


@app.route("/healthz")
def healthcheck():
    ready, mongo_error = ensure_mongo_ready()
    return {
        "status": "ok" if ready else "error",
        "database": MONGO_DB_NAME,
        "storage_mode": "local-demo" if mongo_db.USING_LOCAL_STORE else "mongo",
        "mongo_error": mongo_error,
    }, 200 if ready else 503


@app.route("/media/<file_id>")
def serve_media(file_id):
    if mongo_db.USING_LOCAL_STORE:
        abort(404)

    try:
        media_file = gridfs.GridFS(get_db()).get(ObjectId(file_id))
    except (InvalidId, gridfs.errors.NoFile):
        abort(404)

    return Response(
        media_file.read(),
        mimetype=media_file.content_type or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000"},
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        mongo_error = get_mongo_error()
        if mongo_error:
            return render_template("register.html", error=mongo_error)

        email = request.form.get("email", "").strip().lower()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not email or not username or not password:
            return render_template("register.html", error="Email, username, and password are required.")

        try:
            get_db().users.insert_one(
                {
                    "email": email,
                    "username": username,
                    "password": generate_password_hash(password),
                    "avatar": get_avatar_for_username(username),
                    "bio": "Moodly user",
                    "interests": [],
                    "selected_mood": None,
                    "followers": [],
                    "friends": [],
                    "friend_requests_sent": [],
                    "friend_requests_received": [],
                    "saved_posts": [],
                    "blocked_users": [],
                    "avatar_file_id": None,
                    "avatar_filename": None,
                    "cover_file_id": None,
                    "cover_filename": None,
                    "last_seen": datetime.now(timezone.utc),
                    "created_at": datetime.now(timezone.utc),
                }
            )
        except DuplicateKeyError:
            return render_template("register.html", error="Email or username already exists.")
        except PyMongoError:
            return render_template("register.html", error="Could not create account. Please try again.")

        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        mongo_error = get_mongo_error()
        if mongo_error:
            return render_template("login.html", error=mongo_error)

        identity = request.form.get("identity", "").strip() or request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        try:
            user = get_db().users.find_one({"$or": [{"username": identity}, {"email": identity.lower()}]})
        except PyMongoError:
            user = None

        if user and check_password_hash(user.get("password", ""), password):
            session["user"] = user["username"]
            return redirect(url_for("dashboard"))

        return render_template("login.html", error="Invalid email/username or password.")

    return render_template("login.html")


@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))

    current_user = with_social_defaults(get_current_user() or {})
    if not current_user.get("interests"):
        return redirect(url_for("interests"))
    if not current_user.get("selected_mood"):
        return redirect(url_for("mood"))
    return redirect(url_for("feed"))


@app.route("/interests", methods=["GET", "POST"])
def interests():
    if "user" not in session:
        return redirect(url_for("login"))

    current_user = get_current_user() or {}
    mongo_error = get_mongo_error()

    if request.method == "POST" and not mongo_error:
        selected = request.form.getlist("interests")
        if not selected:
            return render_template(
                "interests.html",
                error="Pick at least one category.",
                category_options=CATEGORY_OPTIONS,
                selected_interests=[],
            )
        get_db().users.update_one(
            {"username": session["user"]},
            {"$set": {"interests": selected}},
        )
        return redirect(url_for("mood"))

    return render_template(
        "interests.html",
        error=mongo_error,
        category_options=CATEGORY_OPTIONS,
        selected_interests=current_user.get("interests", []),
    )


@app.route("/mood", methods=["GET", "POST"])
def mood():
    if "user" not in session:
        return redirect(url_for("login"))

    current_user = get_current_user() or {}
    mongo_error = get_mongo_error()

    if request.method == "POST" and not mongo_error:
        selected_mood = request.form.get("mood")
        if not selected_mood:
            return render_template(
                "mood.html",
                error="Select a mood before continuing.",
                mood_options=MOOD_OPTIONS,
                selected_mood=None,
            )
        get_db().users.update_one(
            {"username": session["user"]},
            {"$set": {"selected_mood": selected_mood}},
        )
        return redirect(url_for("feed"))

    return render_template(
        "mood.html",
        error=mongo_error,
        mood_options=MOOD_OPTIONS,
        selected_mood=current_user.get("selected_mood"),
    )


@app.route("/feed")
def feed():
    if "user" not in session:
        return redirect(url_for("login"))

    mongo_error = get_mongo_error()
    if mongo_error:
        return render_template(
            "feed.html",
            posts=[],
            feed_error=mongo_error,
            current_user=add_avatar_fields(get_current_user() or {}),
            mood_options=MOOD_OPTIONS,
            selected_mood=None,
            selected_interests=[],
            suggested_users=[],
            searched_users=[],
            username_query=request.args.get("username", "").strip(),
            incoming_requests=[],
            stories=[],
            **build_navigation_context(),
        )

    db = get_db()
    current_user = add_avatar_fields(get_current_user() or {})
    mood_map = get_option_map(MOOD_OPTIONS)
    selected_mood = request.args.get("mood", current_user.get("selected_mood"))
    username_query = request.args.get("username", "").strip()

    if selected_mood in mood_map and selected_mood != current_user.get("selected_mood"):
        db.users.update_one({"username": session["user"]}, {"$set": {"selected_mood": selected_mood}})
        current_user["selected_mood"] = selected_mood

    query = {}
    if selected_mood:
        query["mood"] = selected_mood
    if current_user.get("interests"):
        query["category"] = {"$in": current_user["interests"]}

    posts = list(db.posts.find(query, {"_id": 0}).sort("created_at", -1).limit(40))
    if not posts and selected_mood:
        posts = list(db.posts.find({"mood": selected_mood}, {"_id": 0}).sort("created_at", -1).limit(40))

    posts = [normalize_post(post, session["user"]) for post in posts]
    feed_context = build_feed_context(current_user)

    return render_template(
        "feed.html",
        posts=posts,
        feed_error=None,
        current_user=current_user,
        suggested_users=get_suggested_users(session["user"]),
        searched_users=search_users_by_username(username_query, session["user"]),
        username_query=username_query,
        incoming_requests=get_incoming_friend_requests(session["user"], limit=6),
        stories=get_active_stories(),
        **build_navigation_context(),
        **feed_context,
    )


@app.route("/stories", methods=["POST"])
def create_story():
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    current_user = with_social_defaults(get_current_user() or {})
    caption = request.form.get("caption", "").strip()
    media_payload, media_error = save_uploaded_media(request.files.get("story_media"))
    next_url = request.form.get("next_url") or url_for("feed")

    if media_error:
        return redirect(next_url)

    if not caption and not media_payload:
        return redirect(next_url)

    story_doc = {
        "slug": f"story-{uuid4().hex}",
        "username": session["user"],
        "avatar": current_user.get("avatar", get_avatar_for_username(session["user"])),
        "avatar_file_id": current_user.get("avatar_file_id"),
        "avatar_filename": current_user.get("avatar_filename"),
        "caption": caption,
        "created_at": datetime.now(timezone.utc),
    }
    if media_payload:
        story_doc.update(media_payload)

    db.stories.insert_one(story_doc)
    return redirect(next_url)


@app.route("/profile/photo", methods=["POST"])
def update_profile_photo():
    if "user" not in session:
        return redirect(url_for("login"))

    avatar_payload, avatar_error = save_uploaded_avatar(request.files.get("profile_photo"))
    next_url = request.form.get("next_url") or url_for("my_profile")

    if avatar_error or not avatar_payload:
        return redirect(next_url)

    username = session["user"]
    get_db().users.update_one({"username": username}, {"$set": avatar_payload})
    sync_avatar_media(username, avatar_payload)
    return redirect(next_url)


@app.route("/profile/cover", methods=["POST"])
def update_profile_cover():
    if "user" not in session:
        return redirect(url_for("login"))

    cover_payload, cover_error = save_uploaded_image(request.files.get("cover_photo"))
    next_url = request.form.get("next_url") or url_for("my_profile")

    if cover_error or not cover_payload:
        return redirect(next_url)

    get_db().users.update_one(
        {"username": session["user"]},
        {"$set": cover_payload},
    )
    return redirect(next_url)


@app.route("/posts/<slug>/like", methods=["POST"])
def like_post(slug):
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    post = db.posts.find_one({"slug": slug}, {"liked_by": 1})
    if post:
        liked_by = post.get("liked_by", [])
        if session["user"] in liked_by:
            db.posts.update_one({"slug": slug}, {"$pull": {"liked_by": session["user"]}})
        else:
            db.posts.update_one({"slug": slug}, {"$addToSet": {"liked_by": session["user"]}})

    return redirect(request.form.get("next_url") or url_for("feed"))


@app.route("/posts/<slug>/comment", methods=["POST"])
def comment_post(slug):
    if "user" not in session:
        return redirect(url_for("login"))

    content = request.form.get("comment", "").strip()
    if content:
        get_db().posts.update_one(
            {"slug": slug},
            {
                "$push": {
                    "comments": {
                        "id": uuid4().hex,
                        "username": session["user"],
                        "content": content,
                        "created_at": datetime.now(timezone.utc),
                    }
                }
            },
        )

    return redirect(request.form.get("next_url") or url_for("feed"))


@app.route("/posts/<slug>/save", methods=["POST"])
def save_post(slug):
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    user = with_social_defaults(db.users.find_one({"username": session["user"]}, {"saved_posts": 1}))
    if not user:
        return redirect(request.form.get("next_url") or url_for("feed"))

    if slug in user.get("saved_posts", []):
        db.users.update_one({"username": session["user"]}, {"$pull": {"saved_posts": slug}})
        db.posts.update_one({"slug": slug}, {"$pull": {"saved_by": session["user"]}})
    else:
        db.users.update_one({"username": session["user"]}, {"$addToSet": {"saved_posts": slug}})
        db.posts.update_one({"slug": slug}, {"$addToSet": {"saved_by": session["user"]}})

    return redirect(request.form.get("next_url") or url_for("feed"))


@app.route("/profile/<username>/follow", methods=["POST"])
def follow_user(username):
    if "user" not in session:
        return redirect(url_for("login"))

    current_username = session["user"]
    if username == current_username:
        return redirect(request.form.get("next_url") or url_for("my_profile"))

    db = get_db()
    current_user = with_social_defaults(db.users.find_one({"username": current_username}, {"blocked_users": 1}))
    target_user = with_social_defaults(db.users.find_one({"username": username}))
    if not target_user:
        return redirect(url_for("feed"))
    if username in current_user.get("blocked_users", []):
        return redirect(request.form.get("next_url") or url_for("profile", username=username))

    if current_username in target_user.get("followers", []):
        db.users.update_one({"username": username}, {"$pull": {"followers": current_username}})
    else:
        db.users.update_one({"username": username}, {"$addToSet": {"followers": current_username}})

    return redirect(request.form.get("next_url") or url_for("profile", username=username))


@app.route("/profile/<username>/friend-request", methods=["POST"])
def send_friend_request(username):
    if "user" not in session:
        return redirect(url_for("login"))

    current_username = session["user"]
    if username == current_username:
        return redirect(request.form.get("next_url") or url_for("my_profile"))

    db = get_db()
    current_user = with_social_defaults(db.users.find_one({"username": current_username}))
    target_user = with_social_defaults(db.users.find_one({"username": username}))
    if not current_user or not target_user:
        return redirect(url_for("feed"))
    if username in current_user.get("blocked_users", []):
        return redirect(request.form.get("next_url") or url_for("profile", username=username))

    if username in current_user.get("friends", []):
        return redirect(request.form.get("next_url") or url_for("profile", username=username))

    if username in current_user.get("friend_requests_sent", []):
        db.users.update_one({"username": current_username}, {"$pull": {"friend_requests_sent": username}})
        db.users.update_one({"username": username}, {"$pull": {"friend_requests_received": current_username}})
    else:
        db.users.update_one({"username": current_username}, {"$addToSet": {"friend_requests_sent": username}})
        db.users.update_one({"username": username}, {"$addToSet": {"friend_requests_received": current_username}})

    return redirect(request.form.get("next_url") or url_for("profile", username=username))


@app.route("/profile/<username>/friend-request/respond", methods=["POST"])
def respond_friend_request(username):
    if "user" not in session:
        return redirect(url_for("login"))

    current_username = session["user"]
    action = request.form.get("action")
    db = get_db()
    current_user = with_social_defaults(db.users.find_one({"username": current_username}))
    target_user = with_social_defaults(db.users.find_one({"username": username}))
    if not current_user or not target_user:
        return redirect(url_for("feed"))

    if username not in current_user.get("friend_requests_received", []):
        return redirect(request.form.get("next_url") or url_for("profile", username=username))

    db.users.update_one({"username": current_username}, {"$pull": {"friend_requests_received": username}})
    db.users.update_one({"username": username}, {"$pull": {"friend_requests_sent": current_username}})

    if action == "accept":
        db.users.update_one({"username": current_username}, {"$addToSet": {"friends": username}})
        db.users.update_one({"username": username}, {"$addToSet": {"friends": current_username}})

    return redirect(request.form.get("next_url") or url_for("profile", username=username))


@app.route("/profile")
def my_profile():
    if "user" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("profile", username=session["user"]))


@app.route("/profile/<username>", methods=["GET", "POST"])
def profile(username):
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    viewed_user = with_social_defaults(db.users.find_one({"username": username}))
    if not viewed_user:
        return redirect(url_for("feed"))

    profile_error = None
    is_own_profile = username == session["user"]

    if request.method == "POST" and is_own_profile:
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        category = request.form.get("category", "").strip()
        mood_value = request.form.get("mood", "").strip()
        language = request.form.get("language", "mixed").strip() or "mixed"
        media_payload, media_error = save_uploaded_media(request.files.get("media"))

        if not content:
            profile_error = "Write something before posting."
        elif category not in get_option_map(CATEGORY_OPTIONS):
            profile_error = "Select a valid category."
        elif mood_value not in get_option_map(MOOD_OPTIONS):
            profile_error = "Select a valid mood."
        elif media_error:
            profile_error = media_error
        else:
            post_doc = {
                "slug": f"user-{uuid4().hex}",
                "user": username,
                "author_username": username,
                "avatar": viewed_user.get("avatar", get_avatar_for_username(username)),
                "avatar_file_id": viewed_user.get("avatar_file_id"),
                "avatar_filename": viewed_user.get("avatar_filename"),
                "title": title,
                "category": category,
                "mood": mood_value,
                "language": language,
                "content": content,
                "liked_by": [],
                "comments": [],
                "likes": 0,
                "post_type": "user",
                "created_at": datetime.now(timezone.utc),
            }
            if media_payload:
                post_doc.update(media_payload)
            db.posts.insert_one(post_doc)
            return redirect(url_for("profile", username=username))

    posts = list(db.posts.find({"author_username": username}, {"_id": 0}).sort("created_at", -1).limit(30))
    posts = [normalize_post(post, session["user"]) for post in posts]
    saved_posts = get_saved_posts_for_user(username) if is_own_profile else []
    context = build_feed_context(viewed_user)
    current_user = add_avatar_fields(with_social_defaults(get_current_user() or {}))
    relationship = build_relationship_state(current_user, viewed_user)
    follower_users = [
        add_avatar_fields(
            db.users.find_one(
                {"username": follower_username},
                {"username": 1, "avatar": 1, "avatar_file_id": 1, "avatar_filename": 1, "bio": 1},
            )
        )
        for follower_username in viewed_user.get("followers", [])
    ]
    follower_users = [follower for follower in follower_users if follower]
    visible_follower_users = follower_users[:6]

    return render_template(
        "profile.html",
        profile_user=enrich_user_card(viewed_user, current_user),
        current_user=current_user,
        is_own_profile=is_own_profile,
        posts=posts,
        category_options=CATEGORY_OPTIONS,
        profile_error=profile_error,
        relationship=relationship,
        follower_users=follower_users,
        visible_follower_users=visible_follower_users,
        saved_posts=saved_posts,
        incoming_requests=get_incoming_friend_requests(session["user"]),
        stories=get_active_stories(),
        **build_navigation_context(),
        **context,
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    user = with_social_defaults(get_current_user() or {})
    error = None
    success = None

    if request.method == "POST":
        new_email = request.form.get("email", "").strip().lower()
        new_username = request.form.get("username", "").strip()
        new_bio = request.form.get("bio", "").strip()
        new_mood = request.form.get("selected_mood", "").strip()
        new_interests = request.form.getlist("interests")
        new_password = request.form.get("password", "").strip()
        new_avatar_payload, avatar_error = save_uploaded_avatar(request.files.get("profile_photo"))
        new_cover_payload, cover_error = save_uploaded_image(request.files.get("cover_photo"))

        if not new_email or not new_username:
            error = "Email and username are required."
        elif avatar_error:
            error = avatar_error
        elif cover_error:
            error = cover_error
        else:
            updates = {
                "email": new_email,
                "username": new_username,
                "avatar": get_avatar_for_username(new_username),
                "bio": new_bio,
                "selected_mood": new_mood or None,
                "interests": new_interests,
            }
            if new_avatar_payload:
                updates.update(new_avatar_payload)
            if new_cover_payload:
                updates.update(new_cover_payload)
            if new_password:
                updates["password"] = generate_password_hash(new_password)

            try:
                db.users.update_one({"username": session["user"]}, {"$set": updates})
                post_updates = {"author_username": new_username, "user": new_username, "avatar": get_avatar_for_username(new_username)}
                story_updates = {"username": new_username, "avatar": get_avatar_for_username(new_username)}
                if new_avatar_payload:
                    post_updates.update(new_avatar_payload)
                    story_updates.update(new_avatar_payload)
                if new_username != session["user"]:
                    old_username = session["user"]
                    session["user"] = new_username
                    db.posts.update_many(
                        {"author_username": old_username},
                        {"$set": post_updates},
                    )
                    db.stories.update_many({"username": old_username}, {"$set": story_updates})
                    db.posts.update_many(
                        {"comments.username": old_username},
                        {
                            "$set": {"comments.$[comment].username": new_username}
                        },
                        array_filters=[{"comment.username": old_username}],
                    )
                    bulk_ops = [
                        UpdateOne(
                            {"participants": conversation["participants"]},
                            {
                                "$set": {
                                    "participants": sorted(
                                        [new_username if p == old_username else p for p in conversation["participants"]]
                                    )
                                }
                            },
                        )
                        for conversation in db.messages.find({"participants": old_username}, {"participants": 1})
                    ]
                    if bulk_ops:
                        db.messages.bulk_write(bulk_ops)
                    db.messages.update_many({"sender": old_username}, {"$set": {"sender": new_username}})
                    db.messages.update_many({"receiver": old_username}, {"$set": {"receiver": new_username}})
                    rename_username_references(old_username, new_username)
                elif new_avatar_payload:
                    sync_avatar_media(session["user"], new_avatar_payload)

                success = "Profile updated successfully."
                user = add_avatar_fields(with_social_defaults(get_current_user() or {}))
            except DuplicateKeyError:
                error = "Email or username already exists."
            except PyMongoError:
                error = "Could not update settings right now."

    return render_template(
        "settings.html",
        current_user=add_avatar_fields(user),
        error=error,
        success=success,
        category_options=CATEGORY_OPTIONS,
        mood_options=MOOD_OPTIONS,
        **build_navigation_context(),
    )


@app.route("/messages")
def messages_index():
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    current_username = session["user"]
    current_user = with_social_defaults(db.users.find_one({"username": current_username}))
    threads = list(db.messages.find({"participants": current_username}).sort("updated_at", -1))

    conversations = []
    for thread in threads:
        other_user = next((name for name in thread.get("participants", []) if name != current_username), None)
        if not other_user:
            continue
        if other_user in current_user.get("blocked_users", []):
            continue
        user_doc = db.users.find_one(
            {"username": other_user},
            {"username": 1, "avatar": 1, "avatar_file_id": 1, "avatar_filename": 1},
        )
        user_doc = add_avatar_fields(user_doc)
        conversations.append(
            {
                "username": other_user,
                "avatar": (user_doc or {}).get("avatar", get_avatar_for_username(other_user)),
                "avatar_url": (user_doc or {}).get("avatar_url"),
                "is_active": is_user_active(user_doc),
                "last_message": thread.get("last_message", ""),
                "updated_label": format_created_label(thread.get("updated_at")),
            }
        )

    return render_template(
        "messages.html",
        current_user=current_user,
        conversations=conversations,
        active_user=None,
        chat_messages=[],
        **build_navigation_context(),
    )


@app.route("/messages/<username>", methods=["GET", "POST"])
def messages_chat(username):
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    current_username = session["user"]
    if username == current_username:
        return redirect(url_for("messages_index"))

    current_user = with_social_defaults(db.users.find_one({"username": current_username}))
    target_user = add_avatar_fields(with_social_defaults(db.users.find_one({"username": username})))
    if not target_user:
        return redirect(url_for("messages_index"))
    if username in current_user.get("blocked_users", []):
        return redirect(url_for("messages_index"))
    target_user["is_active"] = is_user_active(target_user)
    target_user["last_seen_label"] = format_last_seen_label(target_user)

    participants = sorted([current_username, username])

    if request.method == "POST":
        content = request.form.get("content", "").strip()
        if content:
            message_doc = {
                "id": uuid4().hex,
                "sender": current_username,
                "receiver": username,
                "content": content,
                "created_at": datetime.now(timezone.utc),
            }
            db.messages.update_one(
                {"participants": participants},
                {
                    "$set": {"updated_at": message_doc["created_at"], "last_message": content},
                    "$setOnInsert": {"participants": participants},
                    "$push": {"messages": message_doc},
                },
                upsert=True,
            )
        return redirect(url_for("messages_chat", username=username))

    threads = list(db.messages.find({"participants": current_username}).sort("updated_at", -1))
    conversations = []
    active_messages = []
    for thread in threads:
        other_user = next((name for name in thread.get("participants", []) if name != current_username), None)
        if not other_user:
            continue
        if other_user in current_user.get("blocked_users", []):
            continue
        user_doc = db.users.find_one(
            {"username": other_user},
            {"username": 1, "avatar": 1, "avatar_file_id": 1, "avatar_filename": 1},
        )
        user_doc = add_avatar_fields(user_doc)
        conversations.append(
            {
                "username": other_user,
                "avatar": (user_doc or {}).get("avatar", get_avatar_for_username(other_user)),
                "avatar_url": (user_doc or {}).get("avatar_url"),
                "is_active": is_user_active(user_doc),
                "last_message": thread.get("last_message", ""),
                "updated_label": format_created_label(thread.get("updated_at")),
            }
        )
        if other_user == username:
            active_messages = [
                {**message, "created_label": format_created_label(message.get("created_at"))}
                for message in thread.get("messages", [])
            ]

    return render_template(
        "messages.html",
        current_user=current_user,
        conversations=conversations,
        active_user=target_user,
        chat_messages=active_messages,
        **build_navigation_context(),
    )


@app.route("/profile/<username>/report", methods=["POST"])
def report_user(username):
    if "user" not in session:
        return redirect(url_for("login"))

    current_username = session["user"]
    if username == current_username:
        return redirect(url_for("my_profile"))

    db = get_db()
    target_user = db.users.find_one({"username": username}, {"username": 1})
    if target_user:
        db.reports.insert_one(
            {
                "id": uuid4().hex,
                "reporter": current_username,
                "reported_user": username,
                "created_at": datetime.now(timezone.utc),
            }
        )

    return redirect(request.form.get("next_url") or url_for("profile", username=username))


@app.route("/profile/<username>/block", methods=["POST"])
def block_user(username):
    if "user" not in session:
        return redirect(url_for("login"))

    current_username = session["user"]
    if username == current_username:
        return redirect(url_for("my_profile"))

    db = get_db()
    current_user = with_social_defaults(db.users.find_one({"username": current_username}))
    target_user = with_social_defaults(db.users.find_one({"username": username}))
    if not current_user or not target_user:
        return redirect(url_for("feed"))

    if username in current_user.get("blocked_users", []):
        db.users.update_one({"username": current_username}, {"$pull": {"blocked_users": username}})
    else:
        db.users.update_one(
            {"username": current_username},
            {
                "$addToSet": {"blocked_users": username},
                "$pull": {
                    "friends": username,
                    "friend_requests_sent": username,
                    "friend_requests_received": username,
                },
            },
        )
        db.users.update_one(
            {"username": username},
            {
                "$pull": {
                    "friends": current_username,
                    "friend_requests_sent": current_username,
                    "friend_requests_received": current_username,
                    "followers": current_username,
                }
            },
        )

    return redirect(request.form.get("next_url") or url_for("profile", username=username))


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
