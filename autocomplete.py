# autocomplete.py
import logging
from discord import app_commands
import utils

PAGE_SIZE = 25
CHOICE_NAME_MAX = 95  # 避免顯示過長在 UI 斷裂


def _cf(s: str) -> str:
    return s.casefold() if isinstance(s, str) else str(s).casefold()


def _truncate_choice_name(s: str, limit: int = CHOICE_NAME_MAX) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


async def play_autocomplete(interaction, current: str):
    """
    規則：
    1) 空字串：依 id 升冪列出前 25 筆。
    2) 非空字串：
       - 若全是數字：優先顯示「id 精準/前綴」匹配，再列出標題/歌手包含。
       - 否則：標題/歌手包含 + id 包含。
    3) 總量上限 25；輸出格式「id - title - artist」。
    """
    if utils.songs_cache is None or not utils.songs_cache:
        logging.warning("⚠️ [autocomplete] songs_cache 尚未初始化或為空")
        return []

    cur = (current or "").strip()
    cur_cf = _cf(cur)
    songs = utils.songs_cache

    # 空輸入：單純依 id 升冪回前 25 筆
    if cur == "":
        base = sorted(songs, key=lambda x: x.get("id", 0))[:PAGE_SIZE]
        return [
            app_commands.Choice(
                name=_truncate_choice_name(f"{s['id']} - {s['title']} - {s['artist']}"),
                value=s["id"],
            )
            for s in base
        ]

    # 非空輸入：建立多個 bucket 以優先序合併
    buckets = []
    seen_ids = set()

    # 判斷是否純數字（id 導向）
    is_digits = cur.isdigit()

    def push_bucket(items):
        nonlocal buckets
        buckets.append(items)

    if is_digits:
        # A. id 精準匹配
        exact = []
        try:
            qid = int(cur)
            s = utils.songs_by_id.get(qid) if utils.songs_cache is not None else None
            if s:
                exact.append(s)
        except Exception:
            qid = None
        push_bucket(exact)

        # B. id 前綴匹配（如輸入 "12" → 12, 120, 121...）
        prefix = cur
        prefix_hits = [
            s for s in songs
            if str(s.get("id", "")).startswith(prefix) and (not exact or s["id"] != qid)
        ]
        prefix_hits.sort(key=lambda x: x.get("id", 0))
        push_bucket(prefix_hits)

        # C. 標題/歌手包含
        ta_hits = [
            s for s in songs
            if (cur_cf in _cf(s.get("title", "")) or cur_cf in _cf(s.get("artist", "")))
        ]
        ta_hits.sort(key=lambda x: x.get("id", 0))
        push_bucket(ta_hits)
    else:
        # 文字查詢：標題/歌手包含優先
        ta_hits = [
            s for s in songs
            if (cur_cf in _cf(s.get("title", "")) or cur_cf in _cf(s.get("artist", "")))
        ]
        ta_hits.sort(key=lambda x: x.get("id", 0))
        push_bucket(ta_hits)

        # 其次：id 字串包含（例如輸入 "15" → id 15, 115, 215...）
        id_sub_hits = [s for s in songs if cur in str(s.get("id", ""))]
        id_sub_hits.sort(key=lambda x: x.get("id", 0))
        push_bucket(id_sub_hits)

    # 依序合併各 bucket，去重，限制 25 筆
    merged = []
    for bucket in buckets:
        for s in bucket:
            sid = s.get("id")
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            merged.append(s)
            if len(merged) >= PAGE_SIZE:
                break
        if len(merged) >= PAGE_SIZE:
            break

    logging.info("🔍 autocomplete 匹配 %d → 顯示 %d（current=%r）",
                 sum(len(b) for b in buckets), len(merged), current)

    return [
        app_commands.Choice(
            name=_truncate_choice_name(f"{s['id']} - {s['title']} - {s['artist']}"),
            value=s["id"],
        )
        for s in merged
    ]


async def playlists_autocomplete(interaction, current: str):
    """
    歌單名稱自動補全（全域 + 個人）
    - 空字串：回前 25 個排序後的名字。
    - 含字串：大小寫不敏感 (casefold) 子字串匹配。
    - 先全域後個人，去重。
    """
    cur = (current or "").strip()
    cur_cf = _cf(cur)

    try:
        global_playlists = utils.load_global_playlists() or {}
    except Exception:
        logging.exception("讀取 global_playlists 失敗")
        global_playlists = {}

    try:
        user_playlists = utils.load_user_playlists(str(interaction.user.id)) or {}
    except Exception:
        logging.exception("讀取 user_playlists 失敗")
        user_playlists = {}

    # 先全域後個人（個人中去除與全域重覆）
    raw_names = list(global_playlists.keys()) + [
        n for n in user_playlists.keys() if n not in global_playlists
    ]

    if cur == "":
        names = sorted(raw_names)[:PAGE_SIZE]
    else:
        names = [n for n in raw_names if cur_cf in _cf(n)]
        names = sorted(names)[:PAGE_SIZE]

    return [app_commands.Choice(name=_truncate_choice_name(n), value=n) for n in names]
