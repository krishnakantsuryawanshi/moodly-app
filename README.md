# Moodly

Moodly is a Flask social app backed by MongoDB with a local demo-data fallback.

## Local run

```bash
pip install -r requirements.txt
python app.py
```

Create a `.env` file from `.env.example` before running with MongoDB.

## Render deploy

Render can use the included `render.yaml`.

Required environment variables:

- `SECRET_KEY`: a long random string for Flask sessions
- `MONGO_URI`: your MongoDB Atlas connection string
- `MONGO_PASSWORD`: optional raw Atlas password; useful when your password contains special characters and you want to keep `<db_password>` in `MONGO_URI`
- `MONGO_DB_NAME`: database name, for example `moodly_db`
- `COOKIE_SECURE=true`

Render example:

```env
SECRET_KEY=replace-with-a-long-random-secret
MONGO_URI=mongodb+srv://krishnakantsuryawanshii_db_user:<db_password>@moodly.cbtdije.mongodb.net/moodly_db?retryWrites=true&w=majority&appName=moodly
MONGO_PASSWORD=your-actual-atlas-password
MONGO_DB_NAME=moodly_db
COOKIE_SECURE=true
```

Health check:

- `GET /healthz`

## Notes

- When MongoDB is configured, uploaded images and videos are stored in GridFS so they persist across Render restarts and redeploys.
- If the app falls back to local demo mode, uploads still use `static/uploads`, which is only suitable for local development.
- If your Atlas password contains characters like `@`, `:`, `/`, or `?`, either URL-encode it inside `MONGO_URI` or keep the placeholder in `MONGO_URI` and set `MONGO_PASSWORD` separately.
