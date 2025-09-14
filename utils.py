import os
import json
import logging
import asyncio
import aiohttp
import time
import discord
from dotenv import load_dotenv
from mutagen.mp3 import MP3

load_dotenv()

R2_PUBLIC_BASE = os.getenv("R2_PUBLIC_BASE")
R2_SONGS_JSON_URL = f"{R2_PUBLIC_BASE}/songs.json"
R2_GLOBAL_PLAYLISTS_URL = f"{R2_PUBLIC_BASE}/playlists/global_playlists.json"
R2_USER_PLAYLIST_URL_FMT = f"{R2_PUBLIC_BASE}/playlists/{{}}.json"

songs_cache = None
main_loop = None  # 主 event loop (for after_callback)

def set_main_loop(loop):
    global main_loop
    main_loop = loop
    logging.info(f"✅ [init_utils] 已設置主 event loop {main_loop}")

async def load_songs():
    """從 R2 載入 songs.json 並快取"""
    global songs_cache
    logging.info("🌐 正在從 R2 載入 songs.json ...")
    async with aiohttp.ClientSession() as session:
        async with session.get(R2_SONGS_JSON_URL) as resp:
            text = await resp.text()
            songs_cache = json.loads(text)
            logging.info(f"✅ songs.json 載入成功，共 {len(songs_cache)} 首")

async def reload_songs():
    await load_songs()
    logging.info(f"🔄 [reload] 歌曲清單重新載入，共 {len(songs_cache)} 首")

def get_song_info_by_id(song_id: int):
    if songs_cache is None:
        logging.warning("⚠️ [get_song_info_by_id] songs_cache 尚未初始化")
        return None
    return next((song for song in songs_cache if song.get("id") == song_id), None)

class GuildState:
    def __init__(self):
        self.queue = []
        self.is_playing = False
        self.vc = None
        self.current_mp3_seconds = None
        self._play_start_time = None

    async def start_playing(self, guild, text_channel, voice_channel):
        if self.is_playing or not self.queue:
            return
        self.is_playing = True

        song_id = self.queue[0]
        song_info = get_song_info_by_id(song_id)
        if not song_info:
            await text_channel.send(f"❌ 找不到歌曲 ID：{song_id}")
            self.queue.pop(0)
            self.is_playing = False
            await self.start_playing(guild, text_channel, voice_channel)
            return

        url = f"{R2_PUBLIC_BASE}/songs/{song_id}.mp3"
        logging.info(f"🔎 [mp3] 嘗試取得 mp3 時長：{url}")
        seconds = await get_mp3_duration_from_url(url)
        self.current_mp3_seconds = seconds
        logging.info(f"🕒 [mp3] 預期播放秒數：{seconds:.2f} 秒")
        logging.info(f"🔊 正在播放：{song_info['title']} - {song_info['artist']} (url={song_info['url']}) [id={song_id}]")

        self._play_start_time = time.time()
        try:
            if not self.vc or not self.vc.is_connected():
                self.vc = await voice_channel.connect()
            self.vc.play(
                discord.FFmpegPCMAudio(url),
                after=after_callback_factory(guild, text_channel)
            )
        except Exception as e:
            logging.exception("❌ 播放失敗：", exc_info=e)
            self.queue.pop(0)
            self.is_playing = False
            await self.start_playing(guild, text_channel, voice_channel)

guild_states = {}

def get_guild_state(guild):
    if guild.id not in guild_states:
        guild_states[guild.id] = GuildState()
    return guild_states[guild.id]

async def handle_after_play(guild, text_channel, error):
    state = get_guild_state(guild)
    if error:
        logging.error("🎵 播放出錯：", exc_info=error)
        await text_channel.send("⚠️ 播放發生錯誤，已跳過此曲")
    # 紀錄播放時間
    if state.current_mp3_seconds is not None and state._play_start_time:
        real_time = time.time() - state._play_start_time
        logging.info(f"🕒 [mp3] 播放結束，預期長度：{state.current_mp3_seconds:.2f} 秒，實際耗時：約 {real_time:.2f} 秒")
    if state.queue:
        state.queue.pop(0)
    state.is_playing = False
    state.current_mp3_seconds = None
    state._play_start_time = None
    await state.start_playing(guild, text_channel, state.vc.channel if state.vc else None)
    if not state.queue:
        logging.info(f"🎵 檢查播放條件：queue=[], flag=False, guild_id={guild.id}")
        if state.vc and state.vc.is_connected():
            await state.vc.disconnect(force=True)
            state.vc = None
            await text_channel.send("📤 無歌曲播放，自動離開語音（已清空佇列）")

def after_callback_factory(guild, channel):
    def callback(error):
        try:
            loop = main_loop
            if loop is None:
                loop = asyncio.get_event_loop_policy().get_event_loop()
            future = asyncio.run_coroutine_threadsafe(
                handle_after_play(guild, channel, error), loop)
            future.result()
        except Exception as e:
            logging.error("after callback failed", exc_info=e)
    return callback

async def get_mp3_duration_from_url(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.read()
        with open("temp.mp3", "wb") as f:
            f.write(data)
        audio = MP3("temp.mp3")
        seconds = audio.info.length
        os.remove("temp.mp3")
        return seconds
    except Exception as e:
        logging.warning(f"⚠️ 無法讀取 mp3 時長: {e}")
        return 0

def load_global_playlists():
    # 只支援同步 requests 方式
    import requests
    try:
        resp = requests.get(R2_GLOBAL_PLAYLISTS_URL)
        if resp.status_code == 200:
            return resp.json()
        else:
            logging.warning(f"⚠️ 讀取 global_playlists.json 失敗，status={resp.status_code}")
    except Exception as e:
        logging.warning(f"⚠️ 讀取 global_playlists.json 失敗: {e}")
    return {}

def load_user_playlists(user_id: str):
    # 只支援同步 requests 方式
    import requests
    url = R2_USER_PLAYLIST_URL_FMT.format(user_id)
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            return resp.json()
        else:
            logging.warning(f"⚠️ 讀取 {url} 失敗，status={resp.status_code}")
    except Exception as e:
        logging.warning(f"⚠️ 讀取 {url} 失敗: {e}")
    return {}
