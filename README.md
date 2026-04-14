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
- `MONGO_DB_NAME`: database name, for example `moodly_db`
- `COOKIE_SECURE=true`

Health check:

- `GET /healthz`

## Notes

- When MongoDB is configured, uploaded images and videos are stored in GridFS so they persist across Render restarts and redeploys.
- If the app falls back to local demo mode, uploads still use `static/uploads`, which is only suitable for local development.
