from mongo_db import MONGO_DB_NAME, get_database, get_safe_mongo_uri


def main():
    db = get_database()
    db.command("ping")
    print(f"Connected to MongoDB: {get_safe_mongo_uri()}")
    print(f"Using database: {MONGO_DB_NAME}")


if __name__ == "__main__":
    main()
