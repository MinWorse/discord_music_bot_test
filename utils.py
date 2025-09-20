# utils.py
import os
import json
import logging
import asyncio
import aiohttp
import time
import tempfile
import discord
from dotenv import load_dotenv
from mutagen.mp3 import MP3

load_dotenv()

# === R2 路徑 ===
R2_PUBLIC_BASE = os.getenv("R2_PUBLIC_BASE")
R2_SONGS_JSON_URL = f"{R2_PUBLIC_BASE}/songs.json"
R2_GLOBAL_PLAYLISTS_URL = f"{R2_PUBLIC_BASE}/playlists/global_playlists.json"
R2_USER_PLAYLIST_URL_FMT = f"{R2_PUBLIC_BASE}/playlists/{{}}.json"

# === 讀檔/HTTP 預設 ===
DEFAULT_HTTP_HEADERS = {
    "Accept-Encoding": "identity",
    "User-Agent": "YeeMusicBot/1.0",
}

# 預設用 /dev/shm（記憶體）作為暫存；想改用 /tmp：在 .env 設 PREFETCH_USE_SHM=0
PREFETCH_USE_SHM = os.getenv("PREFETCH_USE_SHM", "1").lower() in ("1", "true", "yes")

def format_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.2f} {unit}" if unit != "B" else f"{int(x)} {unit}"
        x /= 1024.0

# === 全域快取 ===
songs_cache = None           # list[dict]
songs_by_id = {}             # dict[int, dict]  ← O(1) 查找
main_loop = None             # 主 event loop (for after_callback)

def set_main_loop(loop: asyncio.AbstractEventLoop):
    """由 main.py 在 on_ready() 設置正在運行的 loop。"""
    global main_loop
    main_loop = loop
    logging.info("✅ [init_utils] 已設置主 event loop %s", main_loop)

def _build_index():
    """依 songs_cache 建 songs_by_id 索引。"""
    global songs_by_id
    songs_by_id = {}
    if not songs_cache:
        return
    dup = 0
    for s in songs_cache:
        sid = s.get("id")
        if isinstance(sid, int):
            if sid in songs_by_id:
                dup += 1
            songs_by_id[sid] = s
    if dup:
        logging.warning("⚠️ songs.json 中有重覆 id（%d 筆），已以最後一次為準。", dup)

async def load_songs():
    """從 R2 載入 songs.json 並快取 + 建立索引。"""
    global songs_cache
    logging.info("🌐 正在從 R2 載入 songs.json ...")
    async with aiohttp.ClientSession() as session:
        async with session.get(R2_SONGS_JSON_URL, headers=DEFAULT_HTTP_HEADERS) as resp:
            resp.raise_for_status()
            songs_cache = await resp.json(content_type=None)
            _build_index()
            logging.info("✅ songs.json 載入成功，共 %d 首（索引 %d 筆）",
                         len(songs_cache), len(songs_by_id))

async def reload_songs():
    await load_songs()
    logging.info("🔄 [reload] 歌曲清單重新載入，共 %d 首", len(songs_cache or []))

def get_song_info_by_id(song_id: int):
    """O(1) 取歌；快取尚未載入時回 None。"""
    if songs_cache is None:
        logging.warning("⚠️ [get_song_info_by_id] songs_cache 尚未初始化")
        return None
    try:
        return songs_by_id.get(int(song_id))
    except Exception:
        return None

# === 下載與時長 ===

async def download_to_temp(url: str, *, chunk_size=1 << 16, max_retries=3, use_shm=PREFETCH_USE_SHM) -> str:
    """
    將 url 完整下載到暫存檔，成功回傳檔案路徑；失敗拋出例外。
    會嘗試斷點續傳；預設優先用 /dev/shm（記憶體磁碟）以加速與減少 SSD 壓力。
    """
    tmpdir = "/dev/shm" if (use_shm and os.path.isdir("/dev/shm")) else None
    fd, tmp_path = tempfile.mkstemp(prefix="bot_", suffix=".mp3", dir=tmpdir)
    os.close(fd)
    downloaded = 0

    for attempt in range(1, max_retries + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=30)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                headers = dict(DEFAULT_HTTP_HEADERS)
                mode = "wb"
                if downloaded > 0:
                    headers["Range"] = f"bytes={downloaded}-"
                    mode = "ab"
                async with sess.get(url, headers=headers) as resp:
                    if resp.status not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status}")
                    with open(tmp_path, mode) as f:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            f.write(chunk)
                            downloaded += len(chunk)
            break
        except Exception:
            if attempt == max_retries:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                raise
            await asyncio.sleep(1.5 * attempt)

    return tmp_path

def get_mp3_duration_from_path(path: str) -> float:
    try:
        audio = MP3(path)
        if audio and audio.info and getattr(audio.info, "length", None):
            return float(audio.info.length)
    except Exception:
        pass
    return 0.0

# === Guild 狀態 ===

class GuildState:
    def __init__(self):
        self.queue = []                      # list[int]
        self.is_playing = False
        self.is_paused = False
        self.vc: discord.VoiceClient | None = None
        self.current_mp3_seconds = None      # float | None
        self._play_start_time = None         # float | None
        self._current_tmp_path: str | None = None  # 目前這首歌的本地暫存檔

    async def _ensure_voice_connection(self, voice_channel: discord.VoiceChannel | None, text_channel: discord.TextChannel):
        """確保已連上正確的語音頻道；必要時移動。"""
        if voice_channel is None and self.vc and self.vc.channel:
            voice_channel = self.vc.channel
        if voice_channel is None:
            await text_channel.send("⚠️ 找不到可連線的語音頻道，請先加入語音再 /play。")
            raise RuntimeError("No voice channel to connect")
        if self.vc and self.vc.is_connected():
            if self.vc.channel.id != voice_channel.id:
                await self.vc.move_to(voice_channel)
        else:
            self.vc = await voice_channel.connect()

    def _cleanup_tmp(self):
        """刪除目前這首歌的暫存檔。"""
        if not self._current_tmp_path:
            return
        try:
            os.remove(self._current_tmp_path)
            logging.info("🧹 已刪除暫存檔：%s", self._current_tmp_path)
        except FileNotFoundError:
            pass
        except Exception:
            logging.exception("⚠️ 暫存檔刪除失敗")
        finally:
            self._current_tmp_path = None

    async def start_playing(self, guild: discord.Guild, text_channel: discord.TextChannel, voice_channel: discord.VoiceChannel | None):
        """從 queue[0] 開始播放；若失敗會彈出該曲並嘗試下一首。"""
        if self.is_playing or not self.queue:
            return
        self.is_playing = True
        self.is_paused = False

        song_id = self.queue[0]
        song_info = get_song_info_by_id(song_id)
        if not song_info:
            await text_channel.send(f"❌ 找不到歌曲 ID：{song_id}")
            self.queue.pop(0)
            self.is_playing = False
            if self.queue:
                await self.start_playing(guild, text_channel, voice_channel)
            return

        # 確保語音連線
        try:
            await self._ensure_voice_connection(voice_channel, text_channel)
        except Exception:
            logging.exception("❌ 語音連線/移動失敗，略過本曲 (id=%s)", song_id)
            self.queue.pop(0)
            self.is_playing = False
            if self.queue:
                await self.start_playing(guild, text_channel, voice_channel)
            return

        # === 核心：先完整下載再播放（含頻道提示 + 計時 + 檔案大小） ===
        url = f"{R2_PUBLIC_BASE}/songs/{song_id}.mp3"
        title = song_info.get("title")
        artist = song_info.get("artist")

        # 對使用者提示：開始預取
        try:
            await text_channel.send(f"⏳ 正在預取：**{title} - {artist}** …")
        except Exception:
            logging.exception("⚠️ 無法送出預取提示訊息")

        logging.info("🔎 [mp3] 預取並讀取時長：%s", url)
        t0 = time.perf_counter()
        try:
            self._current_tmp_path = await download_to_temp(url)
            elapsed = time.perf_counter() - t0
            size_bytes = os.path.getsize(self._current_tmp_path) if self._current_tmp_path else 0
            logging.info("⬇️ 預取完成：path=%s, size=%s, time=%.2fs",
                         self._current_tmp_path, format_bytes(size_bytes), elapsed)
            try:
                await text_channel.send(f"✅ 預取完成（{elapsed:.2f}s，{format_bytes(size_bytes)}），開始播放：**{title} - {artist}**")
            except Exception:
                logging.exception("⚠️ 無法送出預取完成訊息")
        except Exception:
            logging.exception("❌ 預取 mp3 失敗 (id=%s)；跳過本曲", song_id)
            if self.queue:
                self.queue.pop(0)
            self.is_playing = False
            self._cleanup_tmp()
            if self.queue:
                await self.start_playing(guild, text_channel, voice_channel)
            return

        # 用本地檔取得時長
        seconds = get_mp3_duration_from_path(self._current_tmp_path)
        self.current_mp3_seconds = seconds if seconds and seconds > 0 else None
        if self.current_mp3_seconds:
            logging.info("🕒 [mp3] 預期播放秒數：%.2f 秒", self.current_mp3_seconds)

        logging.info(
            "🔊 正在播放：%s - %s (src=%s) [id=%s]",
            title, artist, song_info.get("url"), song_id
        )
        self._play_start_time = time.time()

        try:
            self.vc.play(
                discord.FFmpegPCMAudio(
                    self._current_tmp_path,   # 本地檔播放
                    options="-vn"
                ),
                after=after_callback_factory(guild, text_channel)
            )
        except Exception:
            logging.exception("❌ 播放啟動失敗 (id=%s)；跳過本曲", song_id)
            if self.queue:
                self.queue.pop(0)
            self.is_playing = False
            self._cleanup_tmp()
            if self.queue:
                await self.start_playing(guild, text_channel, voice_channel)

# === guild 狀態管理 ===

guild_states = {}  # dict[int, GuildState]

def get_guild_state(guild: discord.Guild) -> GuildState:
    state = guild_states.get(guild.id)
    if state is None:
        state = GuildState()
        guild_states[guild.id] = state
    return state

# === 播放結束回呼 ===

async def handle_after_play(guild: discord.Guild, text_channel: discord.TextChannel, error: Exception | None):
    state = get_guild_state(guild)

    if error:
        logging.error("🎵 播放出錯（FFmpeg after callback）", exc_info=error)
        try:
            await text_channel.send("⚠️ 播放發生錯誤，已跳過此曲")
        except Exception:
            logging.exception("⚠️ 報錯訊息無法送出（可能頻道權限/刪除）")

    # 紀錄實際播放時間（Pause 仍計入總耗時；若需純播放時間可加 pause 時長累計）
    if state.current_mp3_seconds is not None and state._play_start_time:
        real_time = time.time() - state._play_start_time
        logging.info("🕒 [mp3] 播放結束，預期長度：%.2f 秒，實際耗時：約 %.2f 秒",
                     state.current_mp3_seconds, real_time)

    # 刪暫存檔（包含 stop/skip/disconnect 觸發的 after）
    state._cleanup_tmp()

    # 前曲彈出
    if state.queue:
        state.queue.pop(0)

    # 重置播放旗標
    state.is_playing = False
    state.is_paused = False
    state.current_mp3_seconds = None
    state._play_start_time = None

    # 若還有下一首就接續播放
    if state.queue:
        await state.start_playing(guild, text_channel, state.vc.channel if state.vc else None)
        return

    # 佇列空了：自動離開語音
    logging.info("🎵 檢查播放條件：queue=[], guild_id=%s", guild.id)
    if state.vc and state.vc.is_connected():
        try:
            await state.vc.disconnect()
        finally:
            state.vc = None
        try:
            await text_channel.send("📤 無歌曲播放，自動離開語音（已清空佇列）")
        except Exception:
            logging.exception("⚠️ 自動離線訊息無法送出（可能頻道權限/刪除）")

def after_callback_factory(guild: discord.Guild, channel: discord.TextChannel):
    """把 FFmpeg 的非 async thread 回呼轉回主 loop。"""
    def callback(error: Exception | None):
        try:
            loop = main_loop
            if loop is None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
            if loop is None:
                logging.error("❌ after-callback 找不到可用的 event loop，略過 handle_after_play。")
                return
            fut = asyncio.run_coroutine_threadsafe(
                handle_after_play(guild, channel, error),
                loop
            )
            fut.result()
        except Exception:
            logging.exception("after callback failed")
    return callback

# === Playlists（同步 requests） ===

def load_global_playlists() -> dict:
    import requests
    try:
        resp = requests.get(R2_GLOBAL_PLAYLISTS_URL, timeout=10, headers=DEFAULT_HTTP_HEADERS)
        if resp.status_code == 200:
            return resp.json()
        logging.warning("⚠️ 讀取 global_playlists.json 失敗，status=%s", resp.status_code)
    except Exception as e:
        logging.warning("⚠️ 讀取 global_playlists.json 失敗: %s", e)
    return {}

def load_user_playlists(user_id: str) -> dict:
    import requests
    url = R2_USER_PLAYLIST_URL_FMT.format(user_id)
    try:
        resp = requests.get(url, timeout=10, headers=DEFAULT_HTTP_HEADERS)
        if resp.status_code == 200:
            return resp.json()
        logging.warning("⚠️ 讀取 %s 失敗，status=%s", url, resp.status_code)
    except Exception as e:
        logging.warning("⚠️ 讀取 %s 失敗: %s", url, e)
    return {}