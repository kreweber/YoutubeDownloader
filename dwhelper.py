import os
import re
import json
import time
import base64
import hashlib
import random
import string
import urllib.parse
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any, Union, List
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from yt_dlp import YoutubeDL
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

DEFAULT_SAVE_FOLDER = "downloaded_videos"
USER_AGENT_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

USER_AGENT_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.2 Mobile/15E148 Safari/604.1"
)

COMMON_HEADERS = {
    "User-Agent": USER_AGENT_DESKTOP,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

TIKTOK_USER_AGENTS = [
    USER_AGENT_MOBILE,
    USER_AGENT_DESKTOP,
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36"
]

def make_session():
    s = requests.Session()
    retries = Retry(
        total=6,
        backoff_factor=1.3,
        status_forcelist=[429, 500, 502, 503, 504, 520, 522],
        allowed_methods=["GET", "POST", "HEAD", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(COMMON_HEADERS)
    return s


def random_id(length=12):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def get_timestamp_ms():
    return int(time.time() * 1000)


def compute_md5(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def create_folder_if_needed(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def clean_filename(dirty_name):
    name = re.sub(r'[<>:"/\\|?*]', '_', dirty_name)
    name = re.sub(r'\s+', '_', name.strip())
    name = re.sub(r'__+', '_', name)
    return name[:200].rstrip('_')


@dataclass
class VideoData:
    original_url: str
    title: str = "video"
    username: str = "unknown"
    duration_sec: Optional[float] = None
    preview_url: Optional[str] = None
    download_url: Optional[str] = None
    source_name: str = "unknown"
    downloaded_ok: bool = False
    fail_reason: Optional[str] = None

    def suggested_filename(self):
        parts = [clean_filename(self.username), clean_filename(self.title)]
        clean_parts = [p for p in parts if p]
        if not clean_parts:
            return f"video_{get_timestamp_ms()}.mp4"
        return "_".join(clean_parts) + ".mp4"


class PlatformHandler:
    def __init__(self):
        self.session = make_session()

    def can_handle(self, url: str) -> bool:
        raise NotImplementedError

    def extract_info(self, url: str) -> VideoData:
        raise NotImplementedError

    def perform_download(self, video: VideoData, save_path: str) -> Tuple[bool, str]:
        raise NotImplementedError


class YtDlpHandler(PlatformHandler):
    def __init__(self):
        super().__init__()
        if not YT_DLP_AVAILABLE:
            raise RuntimeError("yt-dlp is not installed")

    def can_handle(self, url: str) -> bool:
        return True

    def extract_info(self, url: str) -> VideoData:
        try:
            with YoutubeDL({"quiet": True}) as ydl:
                info_dict = ydl.extract_info(url, download=False)
                return VideoData(
                    original_url=url,
                    title=info_dict.get("title", "no_title"),
                    username=info_dict.get("uploader", "unknown"),
                    duration_sec=info_dict.get("duration"),
                    preview_url=info_dict.get("thumbnail"),
                    source_name=info_dict.get("extractor", "yt-dlp"),
                    downloaded_ok=True
                )
        except Exception as e:
            return VideoData(
                original_url=url,
                fail_reason=f"yt-dlp extract error: {str(e)}"
            )

    def perform_download(self, video: VideoData, save_folder: str) -> Tuple[bool, str]:
        create_folder_if_needed(save_folder)
        out_path = os.path.join(save_folder, video.suggested_filename())

        options = {
            "outtmpl": out_path,
            "continuedl": True,
            "retries": 12,
            "fragment_retries": 12,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "noplaylist": True,
            "quiet": False,
            "no_warnings": False
        }

        try:
            with YoutubeDL(options) as ydl:
                ydl.download([video.original_url])
            if os.path.exists(out_path) and os.path.getsize(out_path) > 300_000:
                return True, out_path
            return False, "file missing or too small"
        except Exception as e:
            return False, str(e)


class TikTokHandler(PlatformHandler):
    def can_handle(self, url: str) -> bool:
        patterns = [
            r'tiktok\.com/@[^/]+/video/',
            r'tiktok\.com/t/',
            r'vm\.tiktok\.com/',
            r'vt\.tiktok\.com/'
        ]
        return any(re.search(p, url, re.IGNORECASE) for p in patterns)

    def extract_info(self, url: str) -> VideoData:
        headers = COMMON_HEADERS.copy()
        headers["User-Agent"] = random.choice(TIKTOK_USER_AGENTS)
        headers["Referer"] = "https://www.tiktok.com/"

        for attempt in range(1, 4):
            try:
                service_urls = [
                    "https://ssstik.io/abc?url=dl",
                    "https://tiktokio.com/api/ajaxSearch",
                    "https://tikwm.com/api/"
                ]

                for service in service_urls:
                    if "ssstik" in service:
                        payload = {
                            "id": url,
                            "locale": "en",
                            "tt": random_id(9)
                        }
                        r = self.session.post(service, data=payload, headers=headers, timeout=18)
                    elif "tikwm" in service:
                        params = {"url": url, "hd": "1"}
                        r = self.session.get(service, params=params, headers=headers, timeout=18)
                    else:
                        continue

                    if r.status_code != 200:
                        continue

                    text = r.text.lower()

                    video_links = re.findall(
                        r'(https?://[^"\']+\.mp4[^"\']*)',
                        text + json.dumps(r.json() if r.text.strip().startswith('{') else {})
                    )

                    for link in video_links:
                        if "watermark" not in link.lower() and len(link) > 50:
                            return VideoData(
                                original_url=url,
                                download_url=link,
                                title=f"tiktok_{get_timestamp_ms()}",
                                source_name="tiktok_direct",
                                downloaded_ok=True
                            )

                time.sleep(random.uniform(1.4, 3.1))
            except:
                continue

        return VideoData(original_url=url, fail_reason="all tiktok methods failed")


class InstagramHandler(PlatformHandler):
    def can_handle(self, url: str) -> bool:
        return bool(re.search(r'(instagram\.com|instagr\.am)/(p|reel)/', url, re.I))

    def extract_info(self, url: str) -> VideoData:
        clean_url = url.split('?')[0].rstrip('/')
        attempts = [
            clean_url + "/?__a=1&__d=dis",
            clean_url + "/embed",
            clean_url + "/?__a=1"
        ]

        for attempt_url in attempts:
            try:
                r = self.session.get(attempt_url, timeout=14)
                if r.status_code != 200:
                    continue

                if "__a=1" in attempt_url and r.text.strip():
                    try:
                        data = r.json()
                        if "graphql" in data:
                            media = data["graphql"]["shortcode_media"]
                            if media.get("video_url"):
                                return VideoData(
                                    original_url=url,
                                    title=media.get("title", f"ig_{get_timestamp_ms()}"),
                                    username=media["owner"].get("username", "unknown"),
                                    download_url=media["video_url"],
                                    source_name="instagram_graphql",
                                    downloaded_ok=True
                                )
                    except:
                        pass

                time.sleep(0.8)
            except:
                continue

        return VideoData(original_url=url, fail_reason="instagram methods exhausted")


class AllInOneDownloader:
    def __init__(self):
        self.handlers = []
        if YT_DLP_AVAILABLE:
            self.handlers.append(YtDlpHandler())
        self.handlers.extend([
            TikTokHandler(),
            InstagramHandler(),
        ])

    def try_download(self, url: str, destination_folder: str = DEFAULT_SAVE_FOLDER) -> Tuple[bool, str]:
        print(f"\nОбрабатываем → {url}")

        for handler in self.handlers:
            if not handler.can_handle(url):
                continue

            print(f"  Пробуем: {handler.__class__.__name__}")

            video_info = handler.extract_info(url)

            if video_info.downloaded_ok and video_info.download_url:
                print("  → найдена прямая ссылка, скачиваем...")
                return self._fast_direct_download(video_info, destination_folder)

            print("  → пытаемся скачать стандартным способом...")
            success, path = handler.perform_download(video_info, destination_folder)

            if success:
                print(f"  Готово! → {path}")
                return True, path

        print("  Специализированные обработчики не справились...")

        if YT_DLP_AVAILABLE:
            print("  Последняя надежда — полный yt-dlp...")
            fallback = YtDlpHandler()
            return fallback.perform_download(VideoData(original_url=url), destination_folder)

        return False, "не удалось подобрать подходящий способ"


    def _fast_direct_download(self, video: VideoData, folder: str) -> Tuple[bool, str]:
        if not video.download_url:
            return False, "нет прямой ссылки"

        create_folder_if_needed(folder)
        target_file = os.path.join(folder, video.suggested_filename())

        try:
            print(f"   Скачивание: {target_file}")
            with self.session.get(video.download_url, stream=True, timeout=90) as response:
                response.raise_for_status()
                with open(target_file, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024*512):
                        if chunk:
                            f.write(chunk)

            size_mb = os.path.getsize(target_file) / (1024 * 1024)
            if size_mb < 0.3:
                os.unlink(target_file)
                return False, f"файл слишком маленький ({size_mb:.2f} МБ)"

            return True, target_file

        except Exception as e:
            return False, f"прямая загрузка не удалась: {str(e)}"


def main_loop():
    downloader = AllInOneDownloader()

    print("═" * 78)
    print("     Многофункциональный загрузчик видео — январь 2026     ")
    print("═" * 78)

    while True:
        link = input("\nСсылка (или exit/q): ").strip()

        if link.lower() in ("exit", "q", "quit", ""):
            print("\nДо новых встреч!\n")
            break

        if not link.startswith(("http://", "https://")):
            print("Похоже это не ссылка...")
            continue

        ok, result = downloader.try_download(link)

        if ok:
            print("\n" + "═" * 30 + " УСПЕШНО " + "═" * 30)
            print(f"Файл сохранён:\n→ {result}")
        else:
            print("\n" + "═" * 30 + " ОШИБКА " + "═" * 30)
            print(f"Не получилось...\nПричина: {result}")

        print("─" * 78)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем. Хорошего дня! :)")
