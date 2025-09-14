import logging
from discord import app_commands

import utils

PAGE_SIZE = 25

async def play_autocomplete(interaction, current):
    # 使用 utils.songs_cache 作為唯一資料來源
    if utils.songs_cache is None:
        logging.warning("⚠️ [autocomplete] songs_cache 尚未初始化")
        return [
            app_commands.Choice(name="⚠️ 歌曲清單尚未載入", value=-1)
        ]

    current_lower = current.strip().lower()
    matches = []
    for song in utils.songs_cache:
        # 支援 id、歌名、歌手
        if not current_lower or \
           current_lower in str(song['id']) or \
           current_lower in song['title'].lower() or \
           current_lower in song['artist'].lower():
            matches.append(song)
    logging.info(f"🔍 autocomplete 匹配到 {len(matches)} 首（current='{current}'）")

    # 預設排序 id（升冪）
    matches.sort(key=lambda x: x['id'])
    limited = matches[:PAGE_SIZE]
    logging.info(f"🔍 autocomplete 顯示 {len(limited)} 首（最多{PAGE_SIZE}首）")

    # 格式 id-歌名-歌手
    return [
        app_commands.Choice(
            name=f"{song['id']} - {song['title']} - {song['artist']}",
            value=song['id']
        )
        for song in limited
    ]


async def playlists_autocomplete(interaction, current):
    """歌單名稱自動補全：含全域歌單和使用者歌單"""
    user_id = str(interaction.user.id)
    results = []

    # 取全域歌單
    global_playlists = utils.load_global_playlists()
    for name in global_playlists.keys():
        if current.lower() in name.lower():
            results.append(name)

    # 取個人歌單
    user_playlists = utils.load_user_playlists(user_id)
    for name in user_playlists.keys():
        if current.lower() in name.lower() and name not in results:
            results.append(name)

    # 最多顯示 25 筆
    results = results[:25]
    return [app_commands.Choice(name=name, value=name) for name in results]
