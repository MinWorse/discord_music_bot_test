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

R2_PUBLIC_BASE = os.getenv("R2_PUBLIC_BASE")
R2_SONGS_JSON_URL = f"{R2_PUBLIC_BASE}/songs.json"
R2_GLOBAL_PLAYLISTS_URL = f"{R2_PUBLIC_BASE}/playlists/global_playlists.json"
R2_USER_PLAYLIST_URL_FMT = f"{R2_PUBLIC_BASE}/playlists/{{}}.json"

# 快取
songs_cache = None          # list[dict]
songs_by_id = {}            # dict[int, dict]  ← O(1) 查找
main_loop = None            # 主 event loop (for after_callback)


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
        async with session.get(R2_SONGS_JSON_URL) as resp:
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


class GuildState:
    def __init__(self):
        self.queue = []                       # list[int]
        self.is_playing = False
        self.vc: discord.VoiceClient | None = None
        self.current_mp3_seconds = None       # float | None
        self._play_start_time = None          # float | None

    async def _ensure_voice_connection(self, voice_channel: discord.VoiceChannel | None, text_channel: discord.TextChannel):
        """確保已連上正確的語音頻道；必要時移動。"""
        # 如果呼叫端沒有給 voice_channel，但已經有 vc，就沿用現有頻道
        if voice_channel is None and self.vc and self.vc.channel:
            voice_channel = self.vc.channel

        if voice_channel is None:
            await text_channel.send("⚠️ 找不到可連線的語音頻道，請先加入語音再 /play。")
            raise RuntimeError("No voice channel to connect")

        if self.vc and self.vc.is_connected():
            # 已連到別的頻道就移動過去
            if self.vc.channel.id != voice_channel.id:
                await self.vc.move_to(voice_channel)
        else:
            self.vc = await voice_channel.connect()

    async def start_playing(self, guild: discord.Guild, text_channel: discord.TextChannel, voice_channel: discord.VoiceChannel | None):
        """從 queue[0] 開始播放；若失敗會彈出該曲並嘗試下一首。"""
        if self.is_playing or not self.queue:
            return
        self.is_playing = True

        song_id = self.queue[0]
        song_info = get_song_info_by_id(song_id)
        if not song_info:
            await text_channel.send(f"❌ 找不到歌曲 ID：{song_id}")
            # 彈出壞項後嘗試下一首
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

        # 讀取時長（非必要，但可留作紀錄）
        url = f"{R2_PUBLIC_BASE}/songs/{song_id}.mp3"
        logging.info("🔎 [mp3] 嘗試取得 mp3 時長：%s", url)
        seconds = await get_mp3_duration_from_url(url)
        self.current_mp3_seconds = seconds if seconds and seconds > 0 else None
        if self.current_mp3_seconds:
            logging.info("🕒 [mp3] 預期播放秒數：%.2f 秒", self.current_mp3_seconds)

        logging.info(
            "🔊 正在播放：%s - %s (src=%s) [id=%s]",
            song_info.get("title"), song_info.get("artist"), song_info.get("url"), song_id
        )

        self._play_start_time = time.time()
        try:
            self.vc.play(
                discord.FFmpegPCMAudio(url),
                after=after_callback_factory(guild, text_channel)
            )
        except Exception:
            logging.exception("❌ 播放啟動失敗 (id=%s)；跳過本曲", song_id)
            # 彈出並嘗試下一首
            if self.queue:
                self.queue.pop(0)
            self.is_playing = False
            if self.queue:
                await self.start_playing(guild, text_channel, voice_channel)


guild_states = {}  # dict[int, GuildState]


def get_guild_state(guild: discord.Guild) -> GuildState:
    state = guild_states.get(guild.id)
    if state is None:
        state = GuildState()
        guild_states[guild.id] = state
    return state


async def handle_after_play(guild: discord.Guild, text_channel: discord.TextChannel, error: Exception | None):
    state = get_guild_state(guild)

    if error:
        logging.error("🎵 播放出錯（FFmpeg after callback）", exc_info=error)
        try:
            await text_channel.send("⚠️ 播放發生錯誤，已跳過此曲")
        except Exception:
            logging.exception("⚠️ 報錯訊息無法送出（可能頻道權限/刪除）")

    # 紀錄實際播放時間
    if state.current_mp3_seconds is not None and state._play_start_time:
        real_time = time.time() - state._play_start_time
        logging.info("🕒 [mp3] 播放結束，預期長度：%.2f 秒，實際耗時：約 %.2f 秒",
                     state.current_mp3_seconds, real_time)

    # 前曲彈出
    if state.queue:
        state.queue.pop(0)

    # 重置播放旗標
    state.is_playing = False
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
                # 嘗試抓一個正在運行的 loop；如無則放棄（避免死鎖）
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
            # 在 FFmpeg 的 after thread 上等待結果可接受（避免吞錯）
            fut.result()
        except Exception:
            logging.exception("after callback failed")
    return callback


async def get_mp3_duration_from_url(url: str) -> float:
    """
    嘗試以 Range 抓前 512KiB 讓 mutagen 估長度；失敗再抓整檔。
    估不到就回 0。
    """
    tmp_path = None
    async with aiohttp.ClientSession() as session:
        try:
            # 先嘗試部分下載
            headers = {"Range": "bytes=0-524287"}  # 512KiB
            async with session.get(url, headers=headers) as resp:
                if resp.status in (200, 206):
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                        tmp_path = tmp.name
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            f.write(chunk)
                    try:
                        audio = MP3(tmp_path)
                        if audio and audio.info and getattr(audio.info, "length", None):
                            return float(audio.info.length)
                    except Exception:
                        # 若部分檔案解析失敗，稍後試完整抓取
                        pass

            # 退而求其次：完整下載
            async with session.get(url) as resp2:
                resp2.raise_for_status()
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp2:
                    tmp2_path = tmp2.name
                with open(tmp2_path, "wb") as f:
                    async for chunk in resp2.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                try:
                    audio = MP3(tmp2_path)
                    if audio and audio.info and getattr(audio.info, "length", None):
                        return float(audio.info.length)
                except Exception:
                    logging.warning("⚠️ mutagen 無法解析 mp3 長度（完整下載亦失敗）")
                finally:
                    try:
                        os.remove(tmp2_path)
                    except Exception:
                        pass

        except Exception as e:
            logging.warning("⚠️ 無法讀取 mp3 時長: %s", e)
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
    return 0.0


# Playlist 載入（同步 requests）——保留簡單版；需要的時候可改 async+快取。
def load_global_playlists() -> dict:
    import requests
    try:
        resp = requests.get(R2_GLOBAL_PLAYLISTS_URL, timeout=10)
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
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logging.warning("⚠️ 讀取 %s 失敗，status=%s", url, resp.status_code)
    except Exception as e:
        logging.warning("⚠️ 讀取 %s 失敗: %s", url, e)
    return {}
