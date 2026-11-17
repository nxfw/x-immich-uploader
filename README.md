# X → Immich uploader

Small self-hosted web app that resolves an X/Twitter post with `yt-dlp`, lets you select a video resolution and capture date, then uploads the temporary file to Immich.

## Build and start with Docker Compose

From PowerShell:

```powershell
cd E:\git\x-immich-uploader
Copy-Item .env.example .env
notepad .env
docker compose build
docker compose up -d
```

Set these values in `.env` before starting:

```dotenv
IMMICH_URL=https://photos.example.com
IMMICH_API_KEY=your-immich-api-key
APP_PORT=8765
IMMICH_PUBLIC_URL=https://photos.example.com
IMMICH_LAN_URL=http://192.168.1.101:2283
```

`IMMICH_URL` is the server-to-server URL used by the uploader. `IMMICH_PUBLIC_URL` is the browser-facing URL used in post-upload links. When the uploader is accessed through a private LAN IP, the app uses `IMMICH_LAN_URL` instead.

### NSFW or age-restricted posts

Some X posts are only available to a logged-in account. Export cookies from a logged-in X browser session in Netscape `cookies.txt` format, place the file at `E:\git\x-immich-uploader\cookies.txt`, and keep these settings in `.env`:

```dotenv
X_COOKIES_FILE=/run/secrets/x-cookies.txt
X_COOKIES_PATH=./cookies.txt
```

The cookie file is mounted read-only and is never stored in the image. Do not commit it or expose this service publicly. Refresh the export while logged in if X still reports no media.

Open `http://localhost:${APP_PORT}` (normally <http://localhost:8765>).

To follow logs or stop the service:

```powershell
docker compose logs -f
docker compose down
```

To rebuild after changing source files:

```powershell
docker compose up -d --build
```

## Run locally without Docker

Requires Python 3.12+ and FFmpeg on `PATH`.

```powershell
cd E:\git\x-immich-uploader
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
$env:IMMICH_URL = "https://photos.example.com"
$env:IMMICH_API_KEY = "your-immich-api-key"
uvicorn app.main:app --reload
```

Then open <http://localhost:8765>.

The app does not persist URLs, media, or credentials. Downloaded media is held in a temporary container directory and removed after the upload attempt. Keep this service private or put it behind authentication before exposing it to the internet. Use it only for media you are allowed to download.
