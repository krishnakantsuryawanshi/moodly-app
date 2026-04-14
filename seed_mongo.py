from pymongo.errors import PyMongoError

from mongo_db import MONGO_DB_NAME, ensure_mongo_ready, get_safe_mongo_uri


def main():
    ready, error = ensure_mongo_ready()

    if not ready:
        print(f"Could not connect to MongoDB at {get_safe_mongo_uri()}")
        print(error)
        raise SystemExit(1)

    print(f"MongoDB is ready. Database '{MONGO_DB_NAME}' was initialized.")
    print("Sample posts were inserted or updated successfully.")


if __name__ == "__main__":
    try:
        main()
    except PyMongoError as exc:
        print(f"MongoDB error: {exc}")
        raise SystemExit(1)
