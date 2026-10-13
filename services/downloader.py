import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Optional, Tuple
import aiohttp
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

logger = logging.getLogger(__name__)


@dataclass
class DownloadedTrack:
    audio_path: str
    cover_path: Optional[str]
    duration: int
    yt_id: str
    yt_url: str


class YouTubeDownloader:
    def __init__(self, temp_dir: str):
        self.temp_dir = temp_dir
        os.makedirs(self.temp_dir, exist_ok=True)

    async def download_track(
        self, title: str, artist: str, cover_url: Optional[str] = None, max_duration: int = 600
    ) -> Optional[DownloadedTrack]:
        """
        Searches and downloads the audio track from YouTube using yt-dlp.
        Runs asynchronously in a thread worker. Enforces max_duration limit.
        Returns None on download error (e.g. HTTP 403 Forbidden, restricted video) so caller can skip and retry.
        """
        query = f"ytsearch1:{artist} - {title} audio"
        logger.info("OpenSong: Searching and downloading track via YouTube: %s", query)

        try:
            download_result = await asyncio.to_thread(self._run_ytdlp, query, max_duration)
            if not download_result:
                return None

            audio_path, duration, yt_id = download_result

            # Download album cover if available
            cover_path = None
            if cover_url:
                cover_path = await self._download_cover(cover_url, yt_id)

            return DownloadedTrack(
                audio_path=audio_path,
                cover_path=cover_path,
                duration=duration,
                yt_id=yt_id,
                yt_url=f"https://www.youtube.com/watch?v={yt_id}" if yt_id else ""
            )
        except (DownloadError, ExtractorError) as e:
            logger.warning(
                "OpenSong: YouTube download error for '%s - %s' (HTTP 403 Forbidden / restricted video): %s",
                artist, title, e
            )
            return None
        except Exception as e:
            logger.error("OpenSong: Unexpected error downloading audio for %s - %s: %s", artist, title, e, exc_info=True)
            return None

    def _run_ytdlp(self, query: str, max_duration: int = 600) -> Optional[Tuple[str, int, str]]:
        outtmpl = os.path.join(self.temp_dir, "%(id)s.%(ext)s")

        def duration_filter(info_dict, *, incomplete):
            dur = info_dict.get("duration")
            if dur and max_duration > 0 and dur > max_duration:
                return f"Duration {dur}s exceeds maximum limit of {max_duration}s"
            return None

        ydl_opts = {
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "match_filter": duration_filter,
            "ignoreerrors": False,
        }

        # Search top 2 candidates so if candidate 1 fails with HTTP 403, candidate 2 is attempted
        search_query = query.replace("ytsearch1:", "ytsearch2:")
        candidate_ids = []
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "extract_flat": True}) as search_ydl:
                search_info = search_ydl.extract_info(search_query, download=False)
                if search_info:
                    entries = search_info.get("entries", [])
                    candidate_ids = [e["id"] for e in entries if e and e.get("id")]
        except Exception as e:
            logger.debug("OpenSong: Flat search extraction error: %s", e)

        if not candidate_ids:
            candidate_ids = [query]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for item in candidate_ids:
                try:
                    info = ydl.extract_info(item, download=True)
                    if not info:
                        continue

                    if "entries" in info and info["entries"]:
                        entry = info["entries"][0]
                    else:
                        entry = info

                    yt_id = entry.get("id", "song")
                    duration = int(entry.get("duration", 0) or 0)
                    target_mp3 = os.path.join(self.temp_dir, f"{yt_id}.mp3")

                    if os.path.exists(target_mp3):
                        return target_mp3, duration, yt_id

                    # Fallback check
                    filename = ydl.prepare_filename(entry)
                    candidate_mp3 = os.path.splitext(filename)[0] + ".mp3"
                    if os.path.exists(candidate_mp3):
                        return candidate_mp3, duration, yt_id
                except (DownloadError, ExtractorError) as e:
                    logger.warning("OpenSong: YouTube candidate '%s' download error (HTTP 403 / restricted): %s", item, e)
                    continue
                except Exception as e:
                    logger.warning("OpenSong: Candidate '%s' unexpected error: %s", item, e)
                    continue

        return None

    async def _download_cover(self, cover_url: str, file_prefix: str) -> Optional[str]:
        target_path = os.path.join(self.temp_dir, f"{file_prefix}_cover.jpg")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(cover_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(target_path, "wb") as f:
                            f.write(content)
                        return target_path
        except Exception as e:
            logger.warning("OpenSong: Failed to download cover image: %s", e)
        return None

    @staticmethod
    def cleanup_files(*paths: Optional[str]) -> None:
        """Safely removes temporary files."""
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception as e:
                    logger.warning("OpenSong: Could not remove temporary file %s: %s", p, e)

    def cleanup_temp_dir(self) -> None:
        """Clean all files in temp dir."""
        if os.path.exists(self.temp_dir):
            for filename in os.listdir(self.temp_dir):
                file_path = os.path.join(self.temp_dir, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception:
                    pass

