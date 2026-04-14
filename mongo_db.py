import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import generate_password_hash


def load_local_env():
    env_path = Path(".env")

    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in os.environ:
            continue
        os.environ[normalized_key] = value.strip().strip('"').strip("'")


load_local_env()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "moodly_db")
LOCAL_STORE_PATH = Path("data") / "moodly_local.json"
LOCAL_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)

USING_LOCAL_STORE = False
MONGO_ERROR_MESSAGE = None
_db_instance = None
_mongo_ready_result = None


def get_client():
    return MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        socketTimeoutMS=3000,
        waitQueueTimeoutMS=3000,
    )


def _serialize_value(value):
    if isinstance(value, datetime):
        return {"__type__": "datetime", "value": value.isoformat()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    return value


def _deserialize_value(value):
    if isinstance(value, dict) and value.get("__type__") == "datetime":
        return datetime.fromisoformat(value["value"])
    if isinstance(value, list):
        return [_deserialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _deserialize_value(item) for key, item in value.items()}
    return value


def _get_nested_values(document, path):
    current = [document]
    for part in path.split("."):
        next_values = []
        for item in current:
            if isinstance(item, list):
                for child in item:
                    if isinstance(child, dict) and part in child:
                        next_values.append(child[part])
            elif isinstance(item, dict) and part in item:
                next_values.append(item[part])
        current = next_values
    return current


def _matches_condition(value, condition):
    if isinstance(condition, dict):
        if "$ne" in condition:
            return value != condition["$ne"]
        if "$in" in condition:
            options = condition["$in"]
            if isinstance(value, list):
                return any(item in options for item in value)
            return value in options
    if isinstance(value, list) and isinstance(condition, list):
        return value == condition
    if isinstance(value, list):
        return condition in value
    return value == condition


def _matches(document, query):
    if not query:
        return True

    for key, value in query.items():
        if key == "$or":
            if not any(_matches(document, option) for option in value):
                return False
            continue

        nested_values = _get_nested_values(document, key)
        if not nested_values:
            if not _matches_condition(None, value):
                return False
            continue

        if not any(_matches_condition(item, value) for item in nested_values):
            return False

    return True


def _apply_projection(document, projection):
    if not projection:
        return deepcopy(document)

    include_fields = [key for key, enabled in projection.items() if enabled and key != "_id"]
    if include_fields:
        return {field: deepcopy(document.get(field)) for field in include_fields if field in document}

    projected = deepcopy(document)
    if projection.get("_id") == 0:
        projected.pop("_id", None)
    return projected


class LocalCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, field, direction=ASCENDING):
        reverse = direction == DESCENDING or direction == -1
        self.documents.sort(
            key=lambda item: (_get_nested_values(item, field) or [None])[0] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=reverse,
        )
        return self

    def limit(self, count):
        self.documents = self.documents[:count]
        return self

    def __iter__(self):
        return iter(self.documents)


class LocalCollection:
    UNIQUE_FIELDS = {
        "users": ("username", "email"),
        "posts": ("slug",),
        "stories": ("slug",),
    }

    def __init__(self, database, name):
        self.database = database
        self.name = name

    @property
    def documents(self):
        return self.database.data[self.name]

    def create_index(self, *args, **kwargs):
        return None

    def find(self, query=None, projection=None):
        matched = [_apply_projection(doc, projection) for doc in self.documents if _matches(doc, query or {})]
        return LocalCursor(matched)

    def find_one(self, query=None, projection=None):
        for document in self.documents:
            if _matches(document, query or {}):
                return _apply_projection(document, projection)
        return None

    def insert_one(self, document):
        self._check_uniques(document)
        self.documents.append(deepcopy(document))
        self.database.save()
        return {"inserted_id": document.get("slug") or document.get("username")}

    def update_one(self, query, update, upsert=False, array_filters=None):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                updated_document = self._apply_update(document, update, array_filters=array_filters)
                self._check_uniques(updated_document, ignore_document=document)
                self.documents[index] = updated_document
                self.database.save()
                return {"matched_count": 1}

        if upsert:
            base = deepcopy(query)
            if "$setOnInsert" in update:
                base.update(deepcopy(update["$setOnInsert"]))
            if "$set" in update:
                base.update(deepcopy(update["$set"]))
            if "$push" in update:
                for key, value in update["$push"].items():
                    base[key] = [deepcopy(value)]
            self._check_uniques(base)
            self.documents.append(base)
            self.database.save()
            return {"matched_count": 0, "upserted": True}

        return {"matched_count": 0}

    def update_many(self, query, update, array_filters=None):
        matched = 0
        changed = False
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                updated_document = self._apply_update(document, update, array_filters=array_filters)
                self._check_uniques(updated_document, ignore_document=document)
                self.documents[index] = updated_document
                matched += 1
                changed = True

        if changed:
            self.database.save()
        return {"matched_count": matched}

    def delete_one(self, query):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                del self.documents[index]
                self.database.save()
                return {"deleted_count": 1}
        return {"deleted_count": 0}

    def delete_many(self, query):
        remaining = [document for document in self.documents if not _matches(document, query)]
        deleted_count = len(self.documents) - len(remaining)
        if deleted_count:
            self.database.data[self.name] = remaining
            self.database.save()
        return {"deleted_count": deleted_count}

    def bulk_write(self, operations):
        for operation in operations:
            if isinstance(operation, UpdateOne):
                self.update_one(operation._filter, operation._doc, upsert=operation._upsert)
        return {"ok": 1}

    def _check_uniques(self, candidate, ignore_document=None):
        for field in self.UNIQUE_FIELDS.get(self.name, ()):
            value = candidate.get(field)
            if value is None:
                continue
            for document in self.documents:
                if ignore_document is document:
                    continue
                if document.get(field) == value:
                    raise DuplicateKeyError(f"Duplicate value for {field}")

    def _apply_update(self, document, update, array_filters=None):
        updated = deepcopy(document)

        if "$set" in update:
            for key, value in update["$set"].items():
                if key == "comments.$[comment].username":
                    old_username = None
                    if array_filters:
                        old_username = array_filters[0].get("comment.username")
                    for comment in updated.get("comments", []):
                        if comment.get("username") == old_username:
                            comment["username"] = value
                else:
                    updated[key] = deepcopy(value)

        if "$setOnInsert" in update:
            for key, value in update["$setOnInsert"].items():
                updated.setdefault(key, deepcopy(value))

        if "$push" in update:
            for key, value in update["$push"].items():
                updated.setdefault(key, [])
                updated[key].append(deepcopy(value))

        if "$addToSet" in update:
            for key, value in update["$addToSet"].items():
                updated.setdefault(key, [])
                if value not in updated[key]:
                    updated[key].append(value)

        if "$pull" in update:
            for key, value in update["$pull"].items():
                updated[key] = [item for item in updated.get(key, []) if item != value]

        self._check_uniques(updated, ignore_document=document)
        return updated


class LocalDatabase:
    def __init__(self, path):
        self.path = path
        self.data = self._load()
        self.users = LocalCollection(self, "users")
        self.posts = LocalCollection(self, "posts")
        self.stories = LocalCollection(self, "stories")
        self.messages = LocalCollection(self, "messages")
        self.reports = LocalCollection(self, "reports")

    def __getitem__(self, item):
        if not hasattr(self, item):
            raise KeyError(item)
        return getattr(self, item)

    def _load(self):
        if not self.path.exists():
            return {"users": [], "posts": [], "stories": [], "messages": [], "reports": []}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        data = _deserialize_value(raw)
        data.setdefault("users", [])
        data.setdefault("posts", [])
        data.setdefault("stories", [])
        data.setdefault("messages", [])
        data.setdefault("reports", [])
        return data

    def save(self):
        payload = _serialize_value(self.data)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def command(self, name):
        if name == "ping":
            return {"ok": 1}
        raise NotImplementedError(f"LocalDatabase command '{name}' is not implemented")


def get_database():
    global _db_instance

    if _db_instance is not None:
        return _db_instance

    if USING_LOCAL_STORE:
        _db_instance = LocalDatabase(LOCAL_STORE_PATH)
    else:
        client = get_client()
        _db_instance = client[MONGO_DB_NAME]
    return _db_instance


def init_mongo():
    db = get_database()
    db.users.create_index([("username", ASCENDING)], unique=True)
    db.users.create_index([("email", ASCENDING)], unique=True)
    db.users.create_index([("selected_mood", ASCENDING)])
    db.posts.create_index([("slug", ASCENDING)], unique=True)
    db.posts.create_index([("category", ASCENDING)])
    db.posts.create_index([("mood", ASCENDING)])
    db.posts.create_index([("author_username", ASCENDING)])
    db.posts.create_index([("created_at", DESCENDING)])
    db.stories.create_index([("slug", ASCENDING)], unique=True)
    db.stories.create_index([("username", ASCENDING)])
    db.stories.create_index([("created_at", DESCENDING)])
    db.messages.create_index([("participants", ASCENDING)])
    db.messages.create_index([("updated_at", DESCENDING)])


DEMO_FIRST_NAMES = [
    "aashi",
    "vivaan",
    "kiara",
    "reyansh",
    "myra",
    "aarav",
    "suhani",
    "laksh",
    "ira",
    "veer",
]

DEMO_LAST_NAMES = [
    "gupta",
    "sehgal",
    "bansal",
    "khurana",
    "saxena",
]

DEMO_BIO_STARTERS = [
    "late-night playlists",
    "campus gossip",
    "startup chaos",
    "weekend coffee runs",
    "relatable meme drops",
    "quiet motivation",
]

DEMO_BIO_ENDINGS = [
    "and low-key main character energy.",
    "with zero chill and solid timing.",
    "plus a dangerously active notes app.",
    "and screenshots saved for later.",
    "with stories that never stay boring.",
]

DEMO_POST_LINES = {
    "memes": [
        "Aaj productivity itni cinematic thi ki ek task karke bhi background score sunai de raha tha.",
        "Group project mein mera role bas panic ka premium subscription lena tha.",
        "Calendar full hai, but actual kaam dekh kar laptop bhi emotional ho gaya.",
    ],
    "jokes": [
        "Maine bola bas ek reel dekhunga. Ab algorithm aur meri dosti official ho chuki hai.",
        "Room clean karne gaya tha, nostalgia mil gaya. Kaam abhi bhi pending hai.",
        "Mera self-control aur midnight snacks kabhi same room mein survive nahi karte.",
    ],
    "motivation": [
        "Perfect plan se zyada important hai ki tum shuru karo aur momentum ko kaam karne do.",
        "Slow progress bhi progress hai, especially jab tum quietly rebuild kar rahe ho.",
        "Discipline boring lag sakta hai, but results ka aesthetic wahi banata hai.",
    ],
    "coding": [
        "Issue fix karne gaya tha, pura architecture ka trust exercise ban gaya.",
        "Jitna clean code likha tha, utna hi confidently bug ne production choose kiya.",
        "Deployment ke baad jo silence hota hai wahi actual thriller soundtrack hai.",
    ],
}

DEMO_POST_TITLES = {
    "memes": ["Chaos check", "Scroll energy", "Daily meme report"],
    "jokes": ["Tiny standup set", "Random thought", "Room temperature comedy"],
    "motivation": ["Reset note", "Build mode", "Quiet comeback"],
    "coding": ["Build log", "Debug diary", "Ship notes"],
}

DEMO_STORY_CAPTIONS = [
    "Quick check-in before the next tab overload.",
    "Moodboard open, responsibilities minimized.",
    "Aaj ka vibe thoda extra curated hai.",
    "Just passing through with one strong opinion and coffee.",
]


def generated_demo_users(now, default_password):
    users = []
    base_followers = [
        "riyamehra",
        "sanakhan",
        "ishaanverma",
        "mehuljoshi",
        "anaykapoor",
        "tanvichauhan",
    ]
    category_pairs = [
        ["memes", "jokes"],
        ["coding", "motivation"],
        ["memes", "motivation"],
        ["jokes", "coding"],
    ]
    moods = ["happy", "focused", "sad", "bored"]

    index = 0
    for first_name in DEMO_FIRST_NAMES:
        for last_name in DEMO_LAST_NAMES:
            username = f"{first_name}{last_name}"
            bio = (
                f"{DEMO_BIO_STARTERS[index % len(DEMO_BIO_STARTERS)].capitalize()}, "
                f"{DEMO_BIO_ENDINGS[index % len(DEMO_BIO_ENDINGS)]}"
            )
            users.append(
                {
                    "email": f"{first_name}.{last_name}@moodly.app",
                    "username": username,
                    "password": default_password,
                    "avatar": username[0].upper(),
                    "bio": bio,
                    "interests": category_pairs[index % len(category_pairs)],
                    "selected_mood": moods[index % len(moods)],
                    "followers": base_followers[: (index % 4) + 1],
                    "friends": [base_followers[index % len(base_followers)]] if index % 9 == 0 else [],
                    "friend_requests_sent": [],
                    "friend_requests_received": ["riyamehra"] if index % 11 == 0 else [],
                    "saved_posts": [],
                    "blocked_users": [],
                    "last_seen": now - timedelta(minutes=(index * 7) % 240),
                    "created_at": now - timedelta(days=3 + index),
                }
            )
            index += 1
    return users


def generated_demo_posts(now, users):
    posts = []
    base_likers = [
        "riyamehra",
        "sanakhan",
        "anaykapoor",
        "ishaanverma",
        "mehuljoshi",
        "tanvichauhan",
    ]
    languages = ["hinglish", "english", "hindi", "mixed"]

    for index, user in enumerate(users):
        interests = user.get("interests", ["memes"])
        category = interests[index % len(interests)]
        mood = user.get("selected_mood", "happy")
        content_pool = DEMO_POST_LINES[category]
        title_pool = DEMO_POST_TITLES[category]
        author = user["username"]
        post = {
            "slug": f"user-{author}-demo-{index + 1}",
            "user": author,
            "author_username": author,
            "avatar": user.get("avatar", author[0].upper()),
            "title": title_pool[index % len(title_pool)],
            "category": category,
            "mood": mood,
            "language": languages[index % len(languages)],
            "content": content_pool[index % len(content_pool)],
            "liked_by": base_likers[: ((index % 3) + 2)],
            "comments": [],
            "post_type": "user",
            "created_at": now - timedelta(minutes=18 * (index + 1)),
        }
        if index % 4 == 0:
            post["comments"] = [
                {
                    "id": f"comment-{author}-{index + 1}",
                    "username": base_likers[index % len(base_likers)],
                    "content": "This one is actually too real.",
                    "created_at": now - timedelta(minutes=(18 * (index + 1)) - 6),
                }
            ]
        posts.append(post)
    return posts


def generated_demo_stories(now, users):
    stories = []
    for index, user in enumerate(users[:12]):
        stories.append(
            {
                "slug": f"story-{user['username']}-demo",
                "username": user["username"],
                "avatar": user.get("avatar", user["username"][0].upper()),
                "caption": DEMO_STORY_CAPTIONS[index % len(DEMO_STORY_CAPTIONS)],
                "created_at": now - timedelta(hours=index + 1),
            }
        )
    return stories


def fake_users_catalog():
    now = datetime.now(timezone.utc)
    default_password = generate_password_hash("moodly123")
    base_users = [
        {
            "email": "riya.mehra@moodly.app",
            "username": "riyamehra",
            "password": default_password,
            "avatar": "R",
            "avatar_filename": "avatar-riya.svg",
            "bio": "Chai, reels, and chaotic memes.",
            "interests": ["memes", "jokes"],
            "selected_mood": "happy",
            "followers": ["sanakhan", "ishaanverma", "mehuljoshi"],
            "friends": ["sanakhan"],
            "friend_requests_sent": [],
            "friend_requests_received": ["tanvichauhan"],
            "created_at": now - timedelta(days=20),
        },
        {
            "email": "anay.kapoor@moodly.app",
            "username": "anaykapoor",
            "password": default_password,
            "avatar": "A",
            "avatar_filename": "avatar-anay.svg",
            "bio": "Building side projects and collecting punchlines.",
            "interests": ["coding", "jokes"],
            "selected_mood": "focused",
            "followers": ["kabirmalhotra", "priyanshisingh"],
            "friends": ["kabirmalhotra"],
            "friend_requests_sent": [],
            "friend_requests_received": ["devanshpatel"],
            "created_at": now - timedelta(days=18),
        },
        {
            "email": "sana.khan@moodly.app",
            "username": "sanakhan",
            "password": default_password,
            "avatar": "S",
            "avatar_filename": "avatar-sana.svg",
            "bio": "Soft thoughts, Hindi captions, and sunset moods.",
            "interests": ["motivation", "memes"],
            "selected_mood": "sad",
            "followers": ["riyamehra", "kavyanair"],
            "friends": ["riyamehra"],
            "friend_requests_sent": [],
            "friend_requests_received": ["priyanshisingh"],
            "created_at": now - timedelta(days=16),
        },
        {
            "email": "kabir.malhotra@moodly.app",
            "username": "kabirmalhotra",
            "password": default_password,
            "avatar": "K",
            "avatar_filename": "avatar-kabir.svg",
            "bio": "Gym, grind, and coding at 2am.",
            "interests": ["motivation", "coding"],
            "selected_mood": "focused",
            "followers": ["anaykapoor", "ishaanverma", "tanvichauhan"],
            "friends": ["anaykapoor"],
            "friend_requests_sent": [],
            "friend_requests_received": [],
            "created_at": now - timedelta(days=14),
        },
        {
            "email": "tara.arora@moodly.app",
            "username": "taraarora",
            "password": default_password,
            "avatar": "T",
            "avatar_filename": "avatar-tara.svg",
            "bio": "Fashion, funny videos, and random voice notes.",
            "interests": ["memes", "jokes"],
            "selected_mood": "bored",
            "followers": ["kavyanair"],
            "friends": [],
            "friend_requests_sent": [],
            "friend_requests_received": [],
            "created_at": now - timedelta(days=12),
        },
        {
            "email": "ishaan.verma@moodly.app",
            "username": "ishaanverma",
            "password": default_password,
            "avatar": "I",
            "avatar_filename": "avatar-ishaan.svg",
            "bio": "Engineer by day, meme reviewer by night.",
            "interests": ["coding", "memes"],
            "selected_mood": "happy",
            "followers": ["riyamehra", "devanshpatel", "tanvichauhan"],
            "friends": [],
            "friend_requests_sent": [],
            "friend_requests_received": [],
            "created_at": now - timedelta(days=10),
        },
        {
            "email": "mehul.joshi@moodly.app",
            "username": "mehuljoshi",
            "password": default_password,
            "avatar": "M",
            "avatar_filename": "avatar-mehul.svg",
            "bio": "Cricket edits, metro observations, and peak Pune sarcasm.",
            "interests": ["memes", "coding"],
            "selected_mood": "happy",
            "followers": ["devanshpatel", "priyanshisingh"],
            "friends": [],
            "friend_requests_sent": [],
            "friend_requests_received": [],
            "created_at": now - timedelta(days=9),
        },
        {
            "email": "kavya.nair@moodly.app",
            "username": "kavyanair",
            "password": default_password,
            "avatar": "K",
            "avatar_filename": "avatar-kavya.svg",
            "bio": "Malayali playlists, soft memes, and rainy day posts.",
            "interests": ["memes", "motivation"],
            "selected_mood": "sad",
            "followers": ["riyamehra", "tanvichauhan", "arpitmishra"],
            "friends": [],
            "friend_requests_sent": [],
            "friend_requests_received": [],
            "created_at": now - timedelta(days=8),
        },
        {
            "email": "devansh.patel@moodly.app",
            "username": "devanshpatel",
            "password": default_password,
            "avatar": "D",
            "avatar_filename": "avatar-devansh.svg",
            "bio": "Ahmedabad startup brain with meme page timing.",
            "interests": ["coding", "jokes"],
            "selected_mood": "focused",
            "followers": ["mehuljoshi", "arpitmishra"],
            "friends": [],
            "friend_requests_sent": ["anaykapoor"],
            "friend_requests_received": [],
            "created_at": now - timedelta(days=7),
        },
        {
            "email": "tanvi.chauhan@moodly.app",
            "username": "tanvichauhan",
            "password": default_password,
            "avatar": "T",
            "avatar_filename": "avatar-tanvi.svg",
            "bio": "Delhi chaos correspondent. Coffee, campus gossip, and savage captions.",
            "interests": ["jokes", "memes"],
            "selected_mood": "bored",
            "followers": ["kavyanair", "mehuljoshi"],
            "friends": [],
            "friend_requests_sent": ["riyamehra"],
            "friend_requests_received": [],
            "created_at": now - timedelta(days=6),
        },
        {
            "email": "arpit.mishra@moodly.app",
            "username": "arpitmishra",
            "password": default_password,
            "avatar": "A",
            "avatar_filename": "avatar-arpit.svg",
            "bio": "Lucknow lines, low effort jokes, high effort friendships.",
            "interests": ["jokes", "motivation"],
            "selected_mood": "happy",
            "followers": ["sanakhan"],
            "friends": [],
            "friend_requests_sent": [],
            "friend_requests_received": [],
            "created_at": now - timedelta(days=5),
        },
        {
            "email": "priyanshi.singh@moodly.app",
            "username": "priyanshisingh",
            "password": default_password,
            "avatar": "P",
            "avatar_filename": "avatar-priyanshi.svg",
            "bio": "Jaipur aesthetics with loud laugh energy.",
            "interests": ["memes", "jokes"],
            "selected_mood": "happy",
            "followers": ["tanvichauhan", "riyamehra"],
            "friends": [],
            "friend_requests_sent": ["sanakhan"],
            "friend_requests_received": [],
            "created_at": now - timedelta(days=4),
        },
    ]
    return base_users + generated_demo_users(now, default_password)


def fake_posts_catalog():
    now = datetime.now(timezone.utc)
    base_posts = [
        {
            "slug": "bot-meme-happy-1",
            "user": "LOL Sharma Bot",
            "avatar": "L",
            "title": "Monday bhi dancer nikla",
            "category": "memes",
            "mood": "happy",
            "language": "hinglish",
            "content": "Me after finishing one tiny task: bhai aaj to LinkedIn pe founder wala post dalunga.",
            "liked_by": ["riyamehra", "ishaanverma"],
            "comments": [],
            "media_filename": "meme-office-crash.svg",
            "media_type": "image",
            "post_type": "bot",
            "created_at": now - timedelta(hours=1),
        },
        {
            "slug": "bot-jokes-sad-1",
            "user": "Dil Se Comic Bot",
            "avatar": "D",
            "title": "Comfort joke",
            "category": "jokes",
            "mood": "sad",
            "language": "hinglish",
            "content": "Mera mood aur chai ek jaise hain, dono ko thoda extra sugar chahiye hoti hai.",
            "liked_by": ["sanakhan"],
            "comments": [],
            "post_type": "bot",
            "created_at": now - timedelta(hours=2),
        },
        {
            "slug": "bot-motivation-focused-1",
            "user": "Rise Rani Bot",
            "avatar": "R",
            "title": "Lock-in mode",
            "category": "motivation",
            "mood": "focused",
            "language": "english",
            "content": "Do not wait for perfect energy. Start with messy energy and let momentum clean it up.",
            "liked_by": ["kabirmalhotra", "anaykapoor"],
            "comments": [],
            "post_type": "bot",
            "created_at": now - timedelta(hours=3),
        },
        {
            "slug": "user-riya-post-1",
            "user": "riyamehra",
            "author_username": "riyamehra",
            "avatar": "R",
            "avatar_filename": "avatar-riya.svg",
            "title": "Bestie meme drop",
            "category": "memes",
            "mood": "happy",
            "language": "hinglish",
            "content": "Jab best friend bolta hai 5 min mein nikal raha hoon and tum dono ko pata hota hai ki woh 45 min lega.",
            "liked_by": ["sanakhan", "ishaanverma"],
            "comments": [
                {
                    "id": "c-riya-1",
                    "username": "sanakhan",
                    "content": "Too real yaar.",
                    "created_at": now - timedelta(hours=5, minutes=20),
                }
            ],
            "media_filename": "meme-late-friend.svg",
            "media_type": "image",
            "post_type": "user",
            "created_at": now - timedelta(hours=5),
        },
        {
            "slug": "user-anay-post-1",
            "user": "anaykapoor",
            "author_username": "anaykapoor",
            "avatar": "A",
            "avatar_filename": "avatar-anay.svg",
            "title": "Debug thoughts",
            "category": "coding",
            "mood": "focused",
            "language": "english",
            "content": "The bug was one missing comma. The emotional damage was enterprise-level.",
            "liked_by": ["kabirmalhotra", "ishaanverma"],
            "comments": [],
            "post_type": "user",
            "created_at": now - timedelta(hours=6),
        },
        {
            "slug": "user-sana-post-1",
            "user": "sanakhan",
            "author_username": "sanakhan",
            "avatar": "S",
            "avatar_filename": "avatar-sana.svg",
            "title": "Night mood",
            "category": "motivation",
            "mood": "sad",
            "language": "hindi",
            "content": "Kabhi kabhi bas thoda sa ruk kar saans leni chahiye. Har race aaj hi jeetna zaroori nahi.",
            "liked_by": ["riyamehra"],
            "comments": [],
            "post_type": "user",
            "created_at": now - timedelta(hours=7),
        },
        {
            "slug": "user-kabir-post-1",
            "user": "kabirmalhotra",
            "author_username": "kabirmalhotra",
            "avatar": "K",
            "avatar_filename": "avatar-kabir.svg",
            "title": "Discipline check",
            "category": "motivation",
            "mood": "focused",
            "language": "english",
            "content": "Motivation is optional. Routine is the real main character.",
            "liked_by": ["anaykapoor", "riyamehra"],
            "comments": [],
            "post_type": "user",
            "created_at": now - timedelta(hours=8),
        },
        {
            "slug": "user-tara-post-1",
            "user": "taraarora",
            "author_username": "taraarora",
            "avatar": "T",
            "avatar_filename": "avatar-tara.svg",
            "title": "Bored feed energy",
            "category": "jokes",
            "mood": "bored",
            "language": "hinglish",
            "content": "Main itni bored hoon ki ab notification tone ko bhi personally judge kar rahi hoon.",
            "liked_by": ["riyamehra", "sanakhan"],
            "comments": [],
            "post_type": "user",
            "created_at": now - timedelta(hours=9),
        },
        {
            "slug": "user-ishaan-post-1",
            "user": "ishaanverma",
            "author_username": "ishaanverma",
            "avatar": "I",
            "avatar_filename": "avatar-ishaan.svg",
            "title": "Ship it",
            "category": "coding",
            "mood": "happy",
            "language": "hinglish",
            "content": "Code chal gaya first try mein. Aaj lag raha hai laptop ne meri izzat rakh li.",
            "liked_by": ["kabirmalhotra", "anaykapoor"],
            "comments": [],
            "post_type": "user",
            "created_at": now - timedelta(hours=10),
        },
        {
            "slug": "user-mehul-post-1",
            "user": "mehuljoshi",
            "author_username": "mehuljoshi",
            "avatar": "M",
            "avatar_filename": "avatar-mehul.svg",
            "title": "WFH reality check",
            "category": "memes",
            "mood": "happy",
            "language": "hinglish",
            "content": "Team call pe camera on, neeche shorts and side mein poha. Founder energy alag hi chal rahi thi.",
            "liked_by": ["riyamehra", "tanvichauhan", "arpitmishra"],
            "comments": [],
            "media_filename": "meme-office-crash.svg",
            "media_type": "image",
            "post_type": "user",
            "created_at": now - timedelta(hours=4, minutes=20),
        },
        {
            "slug": "user-kavya-post-1",
            "user": "kavyanair",
            "author_username": "kavyanair",
            "avatar": "K",
            "avatar_filename": "avatar-kavya.svg",
            "title": "Exam week meme",
            "category": "memes",
            "mood": "sad",
            "language": "hinglish",
            "content": "Notes kholte hi brain ne bola: aaj nahi behen, kal dekhte hain.",
            "liked_by": ["sanakhan", "riyamehra"],
            "comments": [],
            "media_filename": "meme-exam-panic.svg",
            "media_type": "image",
            "post_type": "user",
            "created_at": now - timedelta(hours=3, minutes=35),
        },
        {
            "slug": "user-priyanshi-post-1",
            "user": "priyanshisingh",
            "author_username": "priyanshisingh",
            "avatar": "P",
            "avatar_filename": "avatar-priyanshi.svg",
            "title": "Hostel timing",
            "category": "jokes",
            "mood": "happy",
            "language": "hinglish",
            "content": "Warden se bola library ja rahi hoon. Plot twist: library canteen ke samose the.",
            "liked_by": ["tanvichauhan", "mehuljoshi"],
            "comments": [],
            "post_type": "user",
            "created_at": now - timedelta(hours=2, minutes=15),
        },
        {
            "slug": "user-devansh-post-1",
            "user": "devanshpatel",
            "author_username": "devanshpatel",
            "avatar": "D",
            "avatar_filename": "avatar-devansh.svg",
            "title": "Deployment meme",
            "category": "coding",
            "mood": "focused",
            "language": "english",
            "content": "Production only breaks when everyone says it looks stable. That is the actual uptime test.",
            "liked_by": ["anaykapoor", "ishaanverma"],
            "comments": [],
            "media_filename": "meme-deploy-fire.svg",
            "media_type": "image",
            "post_type": "user",
            "created_at": now - timedelta(hours=1, minutes=25),
        },
        {
            "slug": "user-tanvi-post-1",
            "user": "tanvichauhan",
            "author_username": "tanvichauhan",
            "avatar": "T",
            "avatar_filename": "avatar-tanvi.svg",
            "title": "Campus fit check",
            "category": "memes",
            "mood": "happy",
            "language": "hinglish",
            "content": "Class 8 baje ki thi, but outfit planning ne mujhe influencer bana diya.",
            "liked_by": ["priyanshisingh", "riyamehra", "kavyanair"],
            "comments": [],
            "media_filename": "post-tanvi-campus.svg",
            "media_type": "image",
            "post_type": "user",
            "created_at": now - timedelta(minutes=50),
        },
        {
            "slug": "user-arpit-post-1",
            "user": "arpitmishra",
            "author_username": "arpitmishra",
            "avatar": "A",
            "avatar_filename": "avatar-arpit.svg",
            "title": "Shaadi season survivor",
            "category": "jokes",
            "mood": "bored",
            "language": "hinglish",
            "content": "Mummy ne bola bas 10 minute ke liye chalna hai. Ab main 4th relative ke saath photo khichwa raha hoon.",
            "liked_by": ["mehuljoshi", "devanshpatel"],
            "comments": [],
            "media_filename": "post-arpit-wedding.svg",
            "media_type": "image",
            "post_type": "user",
            "created_at": now - timedelta(minutes=35),
        },
    ]
    generated_users = generated_demo_users(now, generate_password_hash("moodly123"))
    return base_posts + generated_demo_posts(now, generated_users)


def fake_stories_catalog():
    now = datetime.now(timezone.utc)
    base_stories = [
        {
            "slug": "story-riya-evening",
            "username": "riyamehra",
            "avatar": "R",
            "avatar_filename": "avatar-riya.svg",
            "caption": "Evening chai and one reel break before I pretend to be productive.",
            "media_filename": "post-tanvi-campus.svg",
            "media_type": "image",
            "created_at": now - timedelta(hours=3),
        },
        {
            "slug": "story-anay-build",
            "username": "anaykapoor",
            "avatar": "A",
            "avatar_filename": "avatar-anay.svg",
            "caption": "Build fixed. Confidence restored for 11 minutes.",
            "media_filename": "meme-deploy-fire.svg",
            "media_type": "image",
            "created_at": now - timedelta(hours=6),
        },
        {
            "slug": "story-sana-soft",
            "username": "sanakhan",
            "avatar": "S",
            "avatar_filename": "avatar-sana.svg",
            "caption": "Rain playlist, low light, zero social battery.",
            "media_filename": "post-arpit-wedding.svg",
            "media_type": "image",
            "created_at": now - timedelta(hours=10),
        },
    ]
    generated_users = generated_demo_users(now, generate_password_hash("moodly123"))
    return base_stories + generated_demo_stories(now, generated_users)


def fake_messages_catalog():
    now = datetime.now(timezone.utc)
    return [
        {
            "participants": ["anaykapoor", "kabirmalhotra"],
            "updated_at": now - timedelta(minutes=30),
            "last_message": "Bro that bug screenshot was illegal.",
            "messages": [
                {
                    "id": "m1",
                    "sender": "kabirmalhotra",
                    "receiver": "anaykapoor",
                    "content": "Bro that bug screenshot was illegal.",
                    "created_at": now - timedelta(minutes=30),
                }
            ],
        },
        {
            "participants": ["riyamehra", "sanakhan"],
            "updated_at": now - timedelta(minutes=55),
            "last_message": "Kal meme dump bhejungi.",
            "messages": [
                {
                    "id": "m2",
                    "sender": "riyamehra",
                    "receiver": "sanakhan",
                    "content": "Kal meme dump bhejungi.",
                    "created_at": now - timedelta(minutes=55),
                }
            ],
        },
    ]


def seed_users():
    db = get_database()
    for user in fake_users_catalog():
        db.users.update_one(
            {"username": user["username"]},
            {"$set": user},
            upsert=True,
        )


def seed_posts():
    db = get_database()
    for post in fake_posts_catalog():
        db.posts.update_one({"slug": post["slug"]}, {"$set": post}, upsert=True)


def seed_stories():
    db = get_database()
    for story in fake_stories_catalog():
        db.stories.update_one({"slug": story["slug"]}, {"$set": story}, upsert=True)


def seed_messages():
    db = get_database()
    for thread in fake_messages_catalog():
        db.messages.update_one(
            {"participants": thread["participants"]},
            {"$set": thread},
            upsert=True,
        )


def ping_mongo():
    client = get_client()
    client.admin.command("ping")


def get_safe_mongo_uri():
    parts = urlsplit(MONGO_URI)
    if "@" not in parts.netloc:
        return MONGO_URI

    credentials, host = parts.netloc.rsplit("@", 1)
    username = credentials.split(":", 1)[0]
    safe_netloc = f"{username}:***@{host}"
    return urlunsplit((parts.scheme, safe_netloc, parts.path, parts.query, parts.fragment))


def enable_local_store(message):
    global USING_LOCAL_STORE, MONGO_ERROR_MESSAGE, _db_instance

    USING_LOCAL_STORE = True
    MONGO_ERROR_MESSAGE = message
    _db_instance = None


def ensure_mongo_ready():
    global _mongo_ready_result

    if _mongo_ready_result is not None:
        return _mongo_ready_result

    try:
        if "<db_password>" in MONGO_URI or "YOUR_ATLAS_LINK" in MONGO_URI:
            raise PyMongoError("MongoDB URI still contains a placeholder value.")

        ping_mongo()
        init_mongo()
        seed_users()
        seed_posts()
        seed_stories()
        seed_messages()
        _mongo_ready_result = (True, None)
        return _mongo_ready_result
    except Exception as exc:
        enable_local_store(
            f"MongoDB connection failed ({get_safe_mongo_uri()}; {type(exc).__name__}: {exc}). "
            "Moodly is running on local demo data instead."
        )
        init_mongo()
        seed_users()
        seed_posts()
        seed_stories()
        seed_messages()
        _mongo_ready_result = (True, MONGO_ERROR_MESSAGE)
        return _mongo_ready_result
