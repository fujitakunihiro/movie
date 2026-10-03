import hashlib
import json
import math
import mimetypes
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.status import HTTP_206_PARTIAL_CONTENT, HTTP_416_RANGE_NOT_SATISFIABLE

MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/media")).resolve()
SHORTCUT_ALIAS = "movie"
SHORTCUT_TARGET_VALUE = os.getenv("SHORTCUT_MOVIE_TARGET", "")
SHORTCUT_TARGET = Path(SHORTCUT_TARGET_VALUE).resolve() if SHORTCUT_TARGET_VALUE else None
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv",
    ".mpeg", ".mpg", ".ts", ".3gp", ".ogv",
}
CACHE_ROOT = Path(os.getenv("TRANSCODE_CACHE", "/cache")).resolve()
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
TRANSCODE_VARIANTS = ((360, "500k", "650k", "1000k"), (480, "1000k", "1300k", "2000k"), (720, "2500k", "3200k", "5000k"))
_jobs: set[str] = set()
_jobs_lock = threading.Lock()
_thumbnail_locks: dict[str, threading.Lock] = {}
_thumbnail_locks_guard = threading.Lock()
_search_index_lock = threading.Lock()
_search_index: dict[str, list[dict]] | None = None
_search_index_created = 0.0
SEARCH_INDEX_TTL = 60.0
CHUNK_SIZE = 1024 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
app = FastAPI(title="Home Video Streamer", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def normalize_relative_path(relative_path: str) -> str:
    """Normalize a URL path while keeping it relative to the media root."""
    if "\x00" in relative_path or relative_path.startswith(("/", "\\")):
        raise HTTPException(status_code=400, detail="不正なパスです")
    parts = []
    for part in relative_path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise HTTPException(status_code=403, detail="アクセスできないパスです")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def allowed_root_for_path(normalized_path: str) -> Path:
    if SHORTCUT_TARGET is not None and (normalized_path == SHORTCUT_ALIAS or normalized_path.startswith(SHORTCUT_ALIAS + "/")):
        return SHORTCUT_TARGET
    return MEDIA_ROOT


def resolve_inside_root(relative_path: str, *, must_exist: bool = True) -> Path:
    """Resolve a library path inside its configured read-only mount."""
    normalized = normalize_relative_path(relative_path)
    allowed_root = allowed_root_for_path(normalized)
    if allowed_root == SHORTCUT_TARGET and (normalized == SHORTCUT_ALIAS or normalized.startswith(SHORTCUT_ALIAS + "/")):
        subpath = normalized[len(SHORTCUT_ALIAS):].lstrip("/")
    else:
        subpath = normalized
    try:
        candidate = (allowed_root / subpath).resolve(strict=must_exist)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="パスが見つかりません")
    try:
        candidate.relative_to(allowed_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="アクセスできないパスです")
    return candidate


def is_directory_link(path: Path) -> bool:
    """Recognize symlinks and Windows junctions when the runtime exposes that API."""
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def file_info(path: Path, relative_path: str) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "path": relative_path,
        "size": stat.st_size,
        "modified": int(stat.st_mtime),
    }


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/browse")
def browse(path: str = Query(default="")) -> dict:
    display_path = normalize_relative_path(path)
    directory = resolve_inside_root(display_path)
    allowed_root = allowed_root_for_path(display_path)
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="フォルダを指定してください")

    folders = []
    videos = []
    try:
        children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
    except OSError:
        raise HTTPException(status_code=403, detail="フォルダを読み込めません")

    for child in children:
        if (
            display_path == ""
            and child.name.casefold() == "movie.lnk"
            and SHORTCUT_TARGET is not None
            and SHORTCUT_TARGET.is_dir()
        ):
            folders.append({"name": SHORTCUT_ALIAS, "path": SHORTCUT_ALIAS})
            continue
        try:
            linked = is_directory_link(child)
            resolved_child = child.resolve(strict=True)
            resolved_child.relative_to(allowed_root)
            child_display_path = f"{display_path}/{child.name}" if display_path else child.name
            if resolved_child.is_dir():
                folders.append({"name": child.name, "path": child_display_path})
            elif not linked and resolved_child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                videos.append(file_info(resolved_child, child_display_path))
        except (OSError, ValueError):
            # Ignore broken links and targets outside the active read-only mount.
            continue
    return {"path": display_path, "folders": folders, "videos": videos}


def _build_video_search_index() -> dict[str, list[dict]]:
    stack: list[tuple[Path, str, Path]] = [(MEDIA_ROOT, "", MEDIA_ROOT)]
    videos = []
    folders = []
    while stack:
        directory, display_path, allowed_root = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            continue

        for child in children:
            if (
                display_path == ""
                and child.name.casefold() == "movie.lnk"
                and SHORTCUT_TARGET is not None
                and SHORTCUT_TARGET.is_dir()
            ):
                folders.append({"name": SHORTCUT_ALIAS, "path": SHORTCUT_ALIAS})
                stack.append((SHORTCUT_TARGET, SHORTCUT_ALIAS, SHORTCUT_TARGET))
                continue
            try:
                linked = is_directory_link(child)
                resolved_child = child.resolve(strict=True)
                resolved_child.relative_to(allowed_root)
                child_path = f"{display_path}/{child.name}" if display_path else child.name
                if resolved_child.is_dir():
                    folders.append({"name": child.name, "path": child_path})
                    if not linked:
                        stack.append((resolved_child, child_path, allowed_root))
                elif (
                    not linked
                    and resolved_child.is_file()
                    and child.suffix.lower() in VIDEO_EXTENSIONS
                ):
                    videos.append(file_info(resolved_child, child_path))
            except (OSError, ValueError):
                continue

    videos.sort(key=lambda video: video["path"].casefold())
    folders.sort(key=lambda folder: folder["path"].casefold())
    return {"videos": videos, "folders": folders}


@app.get("/api/search")
def search_videos(q: str = Query(default="", max_length=200), refresh: bool = False) -> dict:
    """Search video filenames recursively through the visible library."""
    global _search_index, _search_index_created
    query = q.strip().casefold()
    if not query:
        return {"videos": [], "truncated": False}

    with _search_index_lock:
        if _search_index is None or refresh or time.monotonic() - _search_index_created > SEARCH_INDEX_TTL:
            _search_index = _build_video_search_index()
            _search_index_created = time.monotonic()
        index = _search_index

    matches = [video for video in index["videos"] if query in video["name"].casefold()]
    matched_folders = [folder for folder in index["folders"] if query in folder["name"].casefold()]
    limit = 500
    return {
        "videos": matches[:limit],
        "folders": matched_folders[:limit],
        "truncated": len(matches) > limit,
        "truncatedFolders": len(matched_folders) > limit,
    }

def _transcode_key(media_path: Path) -> str:
    stat = media_path.stat()
    # Bump the cache namespace when encoder settings change so failed or stale
    # renditions are not reused after an upgrade.
    identity = f"hls-v2:{media_path}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:24]


def _has_audio(media_path: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(media_path)],
        capture_output=True, text=True, timeout=20,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _media_duration(media_path: Path, cache_dir: Path) -> float | None:
    duration_file = cache_dir / "duration.txt"
    try:
        cached = float(duration_file.read_text(encoding="ascii").strip())
        if cached > 0:
            return cached
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=duration", "-of", "json", str(media_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        metadata = json.loads(result.stdout)
        format_duration = (metadata.get("format") or {}).get("duration")
        if format_duration not in (None, "N/A"):
            duration = float(format_duration)
            if math.isfinite(duration) and duration > 0:
                duration_file.write_text(f"{duration:.9f}", encoding="ascii")
                return duration
        candidates = []
        for stream in metadata.get("streams", []):
            value = stream.get("duration")
            if stream.get("codec_type") in {"video", "audio"} and value not in (None, "N/A"):
                candidates.append(float(value))
        duration = max(candidates, default=0.0)
        if math.isfinite(duration) and duration > 0:
            duration_file.write_text(f"{duration:.9f}", encoding="ascii")
            return duration
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None
    return None


def _ffmpeg_command(media_path: Path, cache_dir: Path, has_audio: bool) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(media_path)]
    for _ in TRANSCODE_VARIANTS:
        command.extend(["-map", "0:v:0"])
    if has_audio:
        for _ in TRANSCODE_VARIANTS:
            command.extend(["-map", "0:a:0"])
    command.extend(["-c:v", "libx264", "-preset", "veryfast", "-threads", "1", "-pix_fmt", "yuv420p"])
    command.extend(["-c:a", "aac"])
    for index, (height, bitrate, maxrate, bufsize) in enumerate(TRANSCODE_VARIANTS):
        command.extend([
            f"-filter:v:{index}", f"scale=-2:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
            f"-b:v:{index}", bitrate, f"-maxrate:v:{index}", maxrate,
            f"-bufsize:v:{index}", bufsize, f"-force_key_frames:v:{index}", "expr:gte(t,n_forced*4)",
        ])
        if has_audio:
            command.extend([f"-b:a:{index}", "96k"])
    variant_map = " ".join(
        f"v:{index},a:{index}" if has_audio else f"v:{index}"
        for index in range(len(TRANSCODE_VARIANTS))
    )
    command.extend([
        "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
        "-hls_flags", "independent_segments+temp_file",
        "-hls_segment_filename", str(cache_dir / "v%v" / "segment_%05d.ts"),
        "-master_pl_name", "ffmpeg-master.m3u8", "-var_stream_map", variant_map,
        str(cache_dir / "v%v" / "index.m3u8"),
    ])
    return command


def _write_master_playlist(cache_dir: Path) -> bool:
    for index, _variant in enumerate(TRANSCODE_VARIANTS):
        if not (cache_dir / f"v{index}" / "index.m3u8").is_file():
            return False
        if not (cache_dir / f"v{index}" / "segment_00000.ts").is_file():
            return False
    rows = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for index, (height, _bitrate, maxrate, _bufsize) in enumerate(TRANSCODE_VARIANTS):
        nominal = int(maxrate.rstrip("k")) * 1000 + 96_000
        width = round(height * 16 / 9)
        rows.extend([f"#EXT-X-STREAM-INF:BANDWIDTH={nominal},RESOLUTION={width}x{height}", f"v{index}/index.m3u8"])
    temporary = cache_dir / "master.tmp"
    temporary.write_text("\n".join(rows) + "\n", encoding="utf-8")
    temporary.replace(cache_dir / "master.m3u8")
    return True


def _playlist_duration(playlist_path: Path) -> float:
    """Return the media time currently advertised by an HLS variant playlist."""
    total = 0.0
    try:
        for line in playlist_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#EXTINF:"):
                value = line.split(":", 1)[1].split(",", 1)[0]
                duration = float(value)
                if math.isfinite(duration) and duration > 0:
                    total += duration
    except (OSError, ValueError):
        return total
    return total


def _transcode_worker(key: str, media_path: Path, cache_dir: Path, has_audio: bool) -> None:
    try:
        for index in range(len(TRANSCODE_VARIANTS)):
            (cache_dir / f"v{index}").mkdir(parents=True, exist_ok=True)
        with (cache_dir / "transcode.log").open("ab") as log_file:
            result = subprocess.run(_ffmpeg_command(media_path, cache_dir, has_audio), stdout=log_file, stderr=subprocess.STDOUT)
        if result.returncode == 0:
            (cache_dir / "complete").touch()
            _write_master_playlist(cache_dir)
        else:
            (cache_dir / "failed").write_text(str(result.returncode), encoding="ascii")
    except Exception as exc:
        (cache_dir / "failed").write_text(str(exc), encoding="utf-8")
    finally:
        with _jobs_lock:
            _jobs.discard(key)


def _start_transcode(key: str, media_path: Path, cache_dir: Path) -> None:
    with _jobs_lock:
        if key in _jobs or (cache_dir / "complete").exists():
            return
        if (cache_dir / "failed").exists():
            raise HTTPException(status_code=500, detail="動画変換に失敗しました。transcode.logを確認してください")
        try:
            has_audio = _has_audio(media_path)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(status_code=500, detail=f"動画情報を読み取れません: {exc}")
        _jobs.add(key)
        threading.Thread(target=_transcode_worker, args=(key, media_path, cache_dir, has_audio), daemon=True).start()


@app.get("/api/thumbnail")
def thumbnail(path: str = Query(...)) -> Response:
    media_path = resolve_inside_root(path)
    if not media_path.is_file() or media_path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=404, detail="動画ファイルが見つかりません")
    key = _transcode_key(media_path)
    thumbnail_dir = CACHE_ROOT / "thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_path = thumbnail_dir / f"{key}.jpg"
    with _thumbnail_locks_guard:
        lock = _thumbnail_locks.setdefault(key, threading.Lock())
    with lock:
        if not thumbnail_path.is_file():
            temporary_path = thumbnail_dir / f"{key}.tmp.jpg"
            for seek_seconds in ("2", "0"):
                try:
                    result = subprocess.run(
                        [
                            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", seek_seconds, "-i", str(media_path), "-frames:v", "1",
                            "-vf", "scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:(ow-iw)/2:(oh-ih)/2",
                            "-q:v", "4", str(temporary_path),
                        ],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45,
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                if result.returncode == 0 and temporary_path.is_file() and temporary_path.stat().st_size:
                    temporary_path.replace(thumbnail_path)
                    break
            else:
                temporary_path.unlink(missing_ok=True)
                raise HTTPException(status_code=422, detail="この動画からサムネイルを作成できません")
    return FileResponse(thumbnail_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})

@app.get("/api/hls/start")
def start_hls(path: str = Query(...)) -> Response:
    media_path = resolve_inside_root(path)
    if not media_path.is_file() or media_path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=404, detail="動画ファイルが見つかりません")
    key = _transcode_key(media_path)
    cache_dir = CACHE_ROOT / key
    cache_dir.mkdir(parents=True, exist_ok=True)
    duration = _media_duration(media_path, cache_dir)
    _start_transcode(key, media_path, cache_dir)
    _write_master_playlist(cache_dir)
    if not (cache_dir / "master.m3u8").is_file():
        return Response(status_code=202, headers={"Retry-After": "2", "Cache-Control": "no-store"})
    return {
        "master": f"/api/hls/{key}/master.m3u8",
        "id": key,
        "duration": duration,
        "complete": (cache_dir / "complete").exists(),
        "availableDuration": _playlist_duration(cache_dir / "v0" / "index.m3u8"),
    }


@app.get("/api/hls/{key}/{asset_path:path}")
def hls_asset(key: str, asset_path: str) -> Response:
    if not re.fullmatch(r"[a-f0-9]{24}", key):
        raise HTTPException(status_code=404, detail="ストリームが見つかりません")
    if asset_path != "master.m3u8" and not re.fullmatch(r"v[0-2]/(index\.m3u8|segment_\d{5}\.ts)", asset_path):
        raise HTTPException(status_code=404, detail="ストリームが見つかりません")
    file_path = CACHE_ROOT / key / asset_path
    try:
        file_path.resolve().relative_to((CACHE_ROOT / key).resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="ストリームが見つかりません")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="ストリームが見つかりません")
    mime_type = "application/vnd.apple.mpegurl" if file_path.suffix == ".m3u8" else "video/mp2t"
    return FileResponse(file_path, media_type=mime_type, headers={"Cache-Control": "no-cache"})

def parse_range(value: str, size: int) -> tuple[int, int]:
    match = RANGE_RE.fullmatch(value.strip())
    if not match or size <= 0:
        raise ValueError
    first, last = match.groups()
    if not first and not last:
        raise ValueError
    if not first:  # suffix byte range: bytes=-500
        suffix_length = int(last)
        if suffix_length <= 0:
            raise ValueError
        start = max(0, size - suffix_length)
        end = size - 1
    else:
        start = int(first)
        end = int(last) if last else size - 1
        if start >= size or end < start:
            raise ValueError
        end = min(end, size - 1)
    return start, end


def stream_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as media_file:
        media_file.seek(start)
        while remaining:
            chunk = media_file.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.api_route("/api/stream", methods=["GET", "HEAD"])
def stream(request: Request, path: str = Query(...)) -> Response:
    media_path = resolve_inside_root(path)
    if not media_path.is_file() or media_path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=404, detail="動画ファイルが見つかりません")
    size = media_path.stat().st_size
    mime_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    safe_filename = media_path.name.replace('"', "").replace("\r", "").replace("\n", "")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{safe_filename}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    range_value = request.headers.get("range")
    if range_value:
        try:
            start, end = parse_range(range_value, size)
        except ValueError:
            return Response(status_code=HTTP_416_RANGE_NOT_SATISFIABLE,
                            headers={**headers, "Content-Range": f"bytes */{size}"})
        headers.update({
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        })
        if request.method == "HEAD":
            return Response(status_code=HTTP_206_PARTIAL_CONTENT, media_type=mime_type, headers=headers)
        return StreamingResponse(stream_file(media_path, start, end), status_code=HTTP_206_PARTIAL_CONTENT,
                                 media_type=mime_type, headers=headers)
    headers["Content-Length"] = str(size)
    if request.method == "HEAD":
        return Response(status_code=200, media_type=mime_type, headers=headers)
    if size == 0:
        return Response(status_code=200, media_type=mime_type, headers=headers)
    return StreamingResponse(stream_file(media_path, 0, size - 1), media_type=mime_type, headers=headers)














