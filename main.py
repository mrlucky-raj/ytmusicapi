import asyncio, logging, os
from typing import Dict, Literal
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from ytmusicapi import YTMusic
from pytubefix import YouTube
from cachetools import TTLCache

# -------------------------------------------------
# Logging setup
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ytm-api")

# -------------------------------------------------
# FastAPI boilerplate
# -------------------------------------------------
app = FastAPI(title="YT-Music Lite", version="3.0.0")
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],   # tighten in production
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )
# ---------- secure CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://www.mrlucky.cloud",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------
# Globals
# -------------------------------------------------
ytm = YTMusic()
audio_cache: TTLCache = TTLCache(maxsize=1_000, ttl=int(os.getenv("AUDIO_TTL", 300)))

@app.get("/")
def root():
    return {"message": "Hey Lucky, your API is working!!"}

# -------------------------------------------------
# Health & favicon
# -------------------------------------------------
@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

# -------------------------------------------------
# Thumbnail helper (deterministic fallback)
# -------------------------------------------------
@lru_cache(maxsize=2_000)
def thumbnail_url(video_id: str, kind: str) -> str:
    tier = (
        ("maxresdefault", "hqdefault", "mqdefault", "default")
        if kind == "song"
        else ("hqdefault", "mqdefault", "default")
    )
    for quality in tier:
        url = f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"
        # YouTube rules are stable – no extra HEAD requests
        return url  # last fallback always exists
    return f"https://img.youtube.com/vi/{video_id}/default.jpg"

# -------------------------------------------------
# Unified search (async, concurrent)
# -------------------------------------------------
@app.get("/search")
async def search_tracks(
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    type: Literal["song", "video", "all"] = Query("all"),
) -> Dict:
    """
    Fast concurrent search that returns songs **and/or** videos.
    """
    try:
        if type == "all":
            songs, videos = await asyncio.gather(
                asyncio.to_thread(ytm.search, q, filter="songs", limit=limit),
                asyncio.to_thread(ytm.search, q, filter=None, limit=limit),
                return_exceptions=True,
            )
            if isinstance(songs, Exception) or isinstance(videos, Exception):
                raise songs if isinstance(songs, Exception) else videos
            raw = songs + videos
        else:
            raw = await asyncio.to_thread(ytm.search, q, filter=type, limit=limit)
    except Exception as exc:
        logger.exception("Search failed")
        raise HTTPException(502, "Upstream unavailable") from exc

    if not raw:
        raise HTTPException(404, "No results")

    results, seen = [], set()
    for item in raw:
        vid = item.get("videoId")
        if not vid or vid in seen:
            continue
        seen.add(vid)

        kind = item.get("resultType") or "video"
        artists = [
            a.get("name", "Unknown")
            for a in (item.get("artists") or item.get("channel") or [])
            if isinstance(a, dict)
        ]
        results.append(
            {
                "title": item.get("title", "No title"),
                "artist": ", ".join(artists) if artists else "Unknown",
                "thumbnail": thumbnail_url(vid, kind),
                "videoId": vid,
                "type": kind,
            }
        )

    return {"results": results}

# -------------------------------------------------
# Audio URL extractor (cached + timeout)
# -------------------------------------------------
def _extract_audio_url(video_id: str) -> str:
    try:
        yt = YouTube(f"https://music.youtube.com/watch?v={video_id}")
        stream = (
            yt.streams.filter(only_audio=True, file_extension="mp4")
            .order_by("abr")
            .last()
        )
        if not stream:
            raise ValueError("No audio stream")
        return stream.url
    except Exception as exc:
        logger.warning("Audio fail for %s: %s", video_id, exc)
        raise exc

@app.get("/track/{video_id}")
async def track_details(
    video_id: str = Path(..., regex=r"^[a-zA-Z0-9_-]{11}$")
) -> Dict:
    cache_key = hash(video_id)
    if (audio_url := audio_cache.get(cache_key)) is None:
        try:
            audio_url = await asyncio.wait_for(
                asyncio.to_thread(_extract_audio_url, video_id), timeout=8
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "Audio timeout")
        except Exception:
            raise HTTPException(503, "Audio extraction failed")
        audio_cache[cache_key] = audio_url
    else:
        logger.debug("Cache hit %s", video_id)

    try:
        meta = await asyncio.to_thread(ytm.get_song, video_id)
    except Exception:
        raise HTTPException(503, "Metadata unavailable")

    return {
        "title": meta["videoDetails"]["title"],
        "artist": meta["videoDetails"]["author"],
        "thumbnail": meta["videoDetails"]["thumbnail"]["thumbnails"][-1]["url"],
        "audioUrl": audio_url,
    }

# -------------------------------------------------
# Run
# -------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
