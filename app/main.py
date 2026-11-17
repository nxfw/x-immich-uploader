from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
import base64
import mimetypes
import os
import time
import math
import re
import subprocess
import ipaddress

import httpx
import yt_dlp
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="X → Immich")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
IMMICH_URL = os.getenv("IMMICH_URL", "").rstrip("/")
IMMICH_API_KEY = os.getenv("IMMICH_API_KEY", "")
IMMICH_PUBLIC_URL = os.getenv("IMMICH_PUBLIC_URL", "").rstrip("/")
IMMICH_LAN_URL = os.getenv("IMMICH_LAN_URL", "").rstrip("/")
X_COOKIES_FILE = os.getenv("X_COOKIES_FILE", "")

def browser_immich_url(request: Request) -> str:
    """Choose the browser-reachable Immich URL, not the container URL."""
    public_url = IMMICH_PUBLIC_URL or IMMICH_URL
    if not IMMICH_LAN_URL:
        return public_url
    hostname = request.headers.get("x-forwarded-host", request.url.hostname or "").split(":")[0]
    try:
        is_private = ipaddress.ip_address(hostname).is_private
    except ValueError:
        is_private = hostname in {"localhost", "127.0.0.1", "::1"}
    return IMMICH_LAN_URL if is_private else public_url

def ytdlp_options(**extra):
    options = {"quiet": True, "noplaylist": True}
    if X_COOKIES_FILE:
        options["cookiefile"] = X_COOKIES_FILE
    options.update(extra)
    return options

def third_party_formats(url: str):
    """Fallback for posts X hides from its public syndication response."""
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"}
        page = client.get("https://twittervideodownloader.com/en/", headers=headers)
        page.raise_for_status()
        csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
        if not csrf_match:
            return None
        result = client.post(
            "https://twittervideodownloader.com/download",
            data={"csrfmiddlewaretoken": csrf_match.group(1), "tweet": url},
            headers={**headers, "Referer": "https://twittervideodownloader.com/en/"},
        )
        result.raise_for_status()
    variants = []
    media_urls = re.findall(r'https://video\.twimg\.com/[^\s"\'<>\\]+\.mp4\?[^\s"\'<>\\]+', result.text)
    for media_url in dict.fromkeys(media_urls):
        match = re.search(r"/(\d+)x(\d+)/", media_url)
        height = int(match.group(2)) if match else 0
        encoded = base64.urlsafe_b64encode(media_url.encode()).decode()
        variants.append({"id": f"direct:{encoded}", "height": height, "label": f"{height or '?'}p (mp4)"})
    return sorted(variants, key=lambda item: item["height"], reverse=True) or None

def valid_x_url(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}

def get_formats(url: str):
    try:
        with yt_dlp.YoutubeDL(ytdlp_options(skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as original_error:
        status_id = urlparse(url).path.rstrip("/").split("/")[-1]
        try:
            variants = third_party_formats(url)
            if variants:
                return {"id": status_id, "title": "X video"}, variants
        except Exception:
            pass
        try:
            # This is the public endpoint used by embedded X posts and by
            # twittervideodownloader.com. Its token is derived from the ID.
            # JavaScript's Number.toString(36) differs from Python formatting;
            # use the exact small base-36 conversion required by X.
            value = float(status_id) / 1e15 * math.pi
            digits = "0123456789abcdefghijklmnopqrstuvwxyz"
            whole = int(value)
            fraction = value - whole
            token = ""
            while whole:
                whole, digit = divmod(whole, 36)
                token = digits[digit] + token
            token += "."
            for _ in range(8):
                fraction *= 36
                digit = int(fraction)
                token += digits[digit]
                fraction -= digit
            token = token.replace(".", "")
            response = httpx.get(f"https://cdn.syndication.twimg.com/tweet-result?id={status_id}&token={token}&lang=en", timeout=30)
            response.raise_for_status()
            tweet = response.json()
            media = tweet.get("mediaDetails", [])
            formats = []
            for item in media:
                if item.get("type") not in {"video", "animated_gif"}:
                    continue
                for variant in item.get("video_info", {}).get("variants", []):
                    if variant.get("content_type") != "video/mp4" or not variant.get("url"):
                        continue
                    match = re.search(r"/(\d+)x(\d+)/", variant["url"])
                    height = int(match.group(2)) if match else 0
                    encoded = base64.urlsafe_b64encode(variant["url"].encode()).decode()
                    formats.append({"id": f"direct:{encoded}", "height": height, "label": f"{height or '?'}p (mp4)"})
            if formats:
                return {"id": status_id, "title": tweet.get("text", "X video")[:120]}, sorted(formats, key=lambda x: x["height"], reverse=True)
        except Exception:
            try:
                response = httpx.get(f"https://api.fxtwitter.com/status/{status_id}", timeout=30)
                response.raise_for_status()
                tweet = response.json().get("tweet", {})
                for item in tweet.get("media", {}).get("all", []):
                    if item.get("type") == "video" and item.get("url"):
                        encoded = base64.urlsafe_b64encode(item["url"].encode()).decode()
                        return {"id": status_id, "title": tweet.get("text", "X video")[:120]}, [{"id": f"direct:{encoded}", "height": item.get("height") or 0, "label": f"{item.get('height', '?')}p (direct)"}]
            except Exception:
                pass
        raise original_error
    formats = []
    seen = set()
    for item in info.get("formats", []):
        height = item.get("height")
        if not height or not item.get("url") or item.get("vcodec") == "none":
            continue
        key = (height, item.get("ext", "mp4"))
        if key not in seen:
            seen.add(key)
            formats.append({"id": item["format_id"], "height": height, "label": f"{height}p ({item.get('ext', 'mp4')})"})
    return info, sorted(formats, key=lambda x: x["height"], reverse=True)

def preview_data_url(url: str, formats: list[dict]) -> str | None:
    """Return frame three of the smallest available rendition as a data URL."""
    if not formats:
        return None
    with TemporaryDirectory() as temp:
        source = Path(temp) / "preview.mp4"
        selected = min(formats, key=lambda item: item["height"] or 10**9)
        if selected["id"].startswith("direct:"):
            media_url = base64.urlsafe_b64decode(selected["id"][7:]).decode()
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                response = client.get(media_url)
                response.raise_for_status()
                source.write_bytes(response.content)
        else:
            opts = ytdlp_options(format=selected["id"], outtmpl=str(source), quiet=True)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            downloaded = list(Path(temp).glob("preview.*"))
            if not downloaded:
                return None
            source = downloaded[0]
        still = Path(temp) / "preview.jpg"
        subprocess.run(["ffmpeg", "-y", "-i", str(source), "-vf", "select=eq(n\\,2)", "-frames:v", "1", str(still)], check=True, capture_output=True)
        return "data:image/jpeg;base64," + base64.b64encode(still.read_bytes()).decode()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "configured": bool(IMMICH_URL and IMMICH_API_KEY)})

@app.post("/resolve", response_class=HTMLResponse)
async def resolve(request: Request, url: str = Form(...)):
    if not valid_x_url(url):
        return templates.TemplateResponse("index.html", {"request": request, "error": "Paste an x.com or twitter.com post URL."})
    try:
        info, formats = get_formats(url)
        if not formats:
            raise ValueError("No downloadable video was found in that post.")
        try:
            preview = preview_data_url(url, formats)
        except Exception:
            preview = None
        return templates.TemplateResponse("index.html", {"request": request, "url": url, "title": info.get("title", "X video"), "formats": formats, "preview": preview, "now": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")})
    except Exception as exc:
        return templates.TemplateResponse("index.html", {"request": request, "error": f"Could not read that post: {exc}"})

@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, url: str = Form(...), format_id: str = Form(...), taken_at: str = Form(...)):
    if not (IMMICH_URL and IMMICH_API_KEY):
        raise HTTPException(500, "IMMICH_URL and IMMICH_API_KEY are not configured.")
    if not valid_x_url(url):
        raise HTTPException(400, "Invalid X URL.")
    try:
        with TemporaryDirectory() as temp:
            output = str(Path(temp) / "media.%(ext)s")
            if format_id.startswith("direct:"):
                media_url = base64.urlsafe_b64decode(format_id[7:]).decode()
                upstream_name = Path(urlparse(media_url).path).name or "media.mp4"
                media = Path(temp) / upstream_name
                async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
                    response = await client.get(media_url)
                    response.raise_for_status()
                    media.write_bytes(response.content)
                info = {"id": urlparse(url).path.rstrip("/").split("/")[-1]}
            else:
                opts = ytdlp_options(format=f"{format_id}+bestaudio/best", merge_output_format="mp4", outtmpl=output)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                media = list(Path(temp).glob("media.*"))[0]
            if not media.exists():
                raise ValueError("The media download produced no file.")
            # Immich uses these multipart fields as the asset date. Keep the
            # format it documents, and also rewrite the temporary file's mtime
            # so embedded/container metadata cannot override the user's choice.
            selected_time = datetime.fromisoformat(taken_at)
            if selected_time.tzinfo is None:
                selected_time = selected_time.replace(tzinfo=timezone.utc)
            selected_time = selected_time.astimezone(timezone.utc)
            timestamp = selected_time.isoformat(timespec="seconds").replace("+00:00", "Z")
            os.utime(media, (selected_time.timestamp(), selected_time.timestamp()))
            payload = {"deviceAssetId": f"x-{info.get('id', media.stem)}-{selected_time.timestamp()}", "deviceId": "x-immich-uploader", "fileCreatedAt": timestamp, "fileModifiedAt": timestamp, "isFavorite": False}
            headers = {"x-api-key": IMMICH_API_KEY}
            with media.open("rb") as handle:
                files_payload = {"assetData": (media.name, handle, mimetypes.guess_type(media.name)[0] or "video/mp4")}
                async with httpx.AsyncClient(timeout=300) as client:
                    response = await client.post(f"{IMMICH_URL}/api/assets", data=payload, files=files_payload, headers={**headers, "Accept": "application/json"})
            if response.is_error:
                detail = response.text[:1000]
                raise ValueError(f"Immich returned HTTP {response.status_code}: {detail}")
            asset_id = response.json().get("id")
            if not asset_id:
                raise ValueError("Immich upload succeeded but returned no asset id.")
            # Immich may replace fileCreatedAt during metadata extraction when
            # the video contains its own CDN creation date. Lock the requested
            # date through the supported asset-update endpoint, then verify it.
            async with httpx.AsyncClient(timeout=300) as client:
                date_response = await client.put(
                    f"{IMMICH_URL}/api/assets/{asset_id}",
                    json={"dateTimeOriginal": selected_time.isoformat(timespec="milliseconds").replace("+00:00", "Z")},
                    headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
                )
            if date_response.is_error:
                raise ValueError(f"Immich upload succeeded, but date correction failed with HTTP {date_response.status_code}: {date_response.text[:1000]}")
        asset_url = f"{browser_immich_url(request)}/photos/{asset_id}"
        return templates.TemplateResponse("index.html", {"request": request, "success": f"Uploaded to Immich successfully: {media.name}", "asset_url": asset_url})
    except Exception as exc:
        return templates.TemplateResponse("index.html", {"request": request, "error": f"Upload failed: {exc}", "url": url})
