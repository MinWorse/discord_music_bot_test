# main.py
import os
import logging
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import utils
from autocomplete import play_autocomplete, playlists_autocomplete

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_TOKEN_DEV")
if not TOKEN:
    raise RuntimeError("環境變數缺少 DISCORD_TOKEN 或 DISCORD_TOKEN_DEV")

INTENTS = discord.Intents.default()
INTENTS.message_content = False
INTENTS.voice_states = True  # 明確開啟語音狀態

bot = commands.Bot(command_prefix="!", intents=INTENTS)


@bot.event
async def on_ready():
    # 拿正在運行的 event loop，供 after-callback 使用
    utils.set_main_loop(asyncio.get_running_loop())
    await utils.load_songs()
    logging.info("✅ 登入成功：%s", bot.user)
    logging.info("🚩 on_ready: songs_cache 載入結果 type=%s, count=%s",
                 type(utils.songs_cache), len(utils.songs_cache or []))
    try:
        await bot.tree.sync()
        logging.info("✅ Slash 指令同步成功")
    except Exception as e:
        logging.exception("❌ 指令同步失敗：%s", e)


@bot.tree.command(name="play")
@app_commands.describe(song="請選擇歌曲")
@app_commands.autocomplete(song=play_autocomplete)
async def play(interaction: discord.Interaction, song: int):
    guild_id = interaction.guild_id
    user_id = interaction.user.id
    logging.info(f"📝 使用者輸入 /play {song}（guild_id={guild_id}, user_id={user_id}）")

    await interaction.response.defer()
    voice = interaction.user.voice
    if not voice or not voice.channel:
        await interaction.followup.send("⚠️ 請先加入語音頻道！")
        return

    songinfo = utils.get_song_info_by_id(song)
    if songinfo is None:
        await interaction.followup.send("❌ 查無此歌曲編號！")
        return

    state = utils.get_guild_state(interaction.guild)
    state.queue.append(song)
    logging.info(f"➕ 加入歌曲至佇列：{songinfo['title']}（guild_id={guild_id}）")
    await interaction.followup.send(
        f"✅ 已加入播放佇列：{songinfo['id']} - {songinfo['title']} - {songinfo['artist']}。"
    )

    if not state.is_playing:
        await state.start_playing(interaction.guild, interaction.channel, voice.channel)


@bot.tree.command(name="disconnect")
async def disconnect(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    user_id = interaction.user.id
    logging.info(f"📝 使用者輸入 /disconnect（guild_id={guild_id}, user_id={user_id}）")
    await interaction.response.send_message("📴 已中斷連線，請稍候清除播放資源...")

    async def cleanup():
        logging.info(f"🔧 [disconnect] 背景處理開始（guild_id={guild_id}）")
        state = utils.get_guild_state(interaction.guild)
        logging.info(f"🔧 [disconnect] 原始佇列長度：{len(state.queue)}，是否有 vc：{state.vc is not None}")
        state.queue.clear()
        state.is_playing = False
        logging.info(f"🔧 [disconnect] 已清空佇列與播放狀態")
        if state.vc:
            logging.info(f"🔧 [disconnect] 正在呼叫 vc.disconnect()...")
            try:
                await state.vc.disconnect()
            finally:
                state.vc = None
        logging.info("✅ [disconnect] 語音斷線成功")
        try:
            await interaction.channel.send("播放資源已釋放完畢，可再次使用 `/play` 播放新歌曲。")
        except Exception:
            logging.exception("⚠️ 送出完成訊息失敗（可能頻道權限/訊息狀態）")
        logging.info(f"✅ [disconnect] 背景處理結束，guild_id={guild_id}")

    bot.loop.create_task(cleanup())


@bot.tree.command(name="stop")
async def stop(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    user_id = interaction.user.id
    logging.info(f"📝 使用者輸入 /stop（guild_id={guild_id}, user_id={user_id}）")
    state = utils.get_guild_state(interaction.guild)
    if state.vc and state.vc.is_playing():
        # 不清空佇列；停止當前歌曲 → after-callback 會自動接下一首
        state.vc.stop()
        await interaction.response.send_message("⏹️ 已停止當前歌曲。若佇列中仍有歌曲，將立即播放下一首。")
    else:
        await interaction.response.send_message("⚠️ 沒有播放中的歌曲")


@bot.tree.command(name="skip")
async def skip(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    user_id = interaction.user.id
    logging.info(f"📝 使用者輸入 /skip（guild_id={guild_id}, user_id={user_id}）")
    state = utils.get_guild_state(interaction.guild)
    if state.vc and state.vc.is_playing():
        state.vc.stop()
        await interaction.response.send_message("⏭️ 用戶手動跳過歌曲")
    else:
        await interaction.response.send_message("⚠️ 沒有播放中的歌曲")


@bot.tree.command(name="reload")
async def reload(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    user_id = interaction.user.id
    logging.info(f"📝 使用者輸入 /reload（guild_id={guild_id}, user_id={user_id}）")
    await interaction.response.defer()
    await utils.reload_songs()
    await interaction.followup.send(f"✅ 歌曲清單已重新載入（共 {len(utils.songs_cache)} 首）")


@bot.tree.command(name="show_playlist")
@app_commands.describe(name="請選擇歌單名稱")
@app_commands.autocomplete(name=playlists_autocomplete)
async def show_playlist(interaction: discord.Interaction, name: str):
    user_id = str(interaction.user.id)
    global_playlists = utils.load_global_playlists()
    user_playlists = utils.load_user_playlists(user_id)

    # 查詢順序：全域 > 個人
    if name in global_playlists:
        song_ids = global_playlists[name]
        title = f""
    elif name in user_playlists:
        song_ids = user_playlists[name]
        title = f"（個人歌單）"
    else:
        await interaction.response.send_message("❌ 查無此歌單名稱", ephemeral=True)
        return

    if not song_ids:
        await interaction.response.send_message(f"⚠️ 此歌單「{name}」沒有任何歌曲", ephemeral=True)
        return

    # 分段顯示（每 20 首一段）
    PAGE_SIZE = 20
    chunks = [song_ids[i:i + PAGE_SIZE] for i in range(0, len(song_ids), PAGE_SIZE)]

    await interaction.response.send_message(f"{title} 共 {len(song_ids)} 首，分 {len(chunks)} 頁：", ephemeral=False)
    for idx, chunk in enumerate(chunks):
        lines = []
        for sid in chunk:
            song = utils.get_song_info_by_id(sid)
            if song:
                lines.append(f"{song['id']} - {song['title']} - {song['artist']}")
            else:
                lines.append(f"{sid} - [未找到]")
        msg = f"{title}第 {idx + 1} 頁 / 共 {len(chunks)} 頁\n" + "\n".join(lines)
        await interaction.channel.send(msg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')
    logging.info("🎯 準備連線 Discord")
    bot.run(TOKEN)
