import sqlite3
from datetime import datetime, timedelta

DB_PATH = "bot.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            joined_at TEXT,
            insta_used INTEGER DEFAULT 0,
            yt_used INTEGER DEFAULT 0,
            pin_used INTEGER DEFAULT 0,
            week_start TEXT,
            referrals INTEGER DEFAULT 0,
            referred_by INTEGER,
            ref_month TEXT,
            sub_until TEXT
        )
    """)
    conn.commit()
    conn.close()


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def month_str():
    return datetime.now().strftime("%Y-%m")


def get_user(user_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def ensure_user(user_id: int, referred_by: int = None):
    conn = get_conn()
    row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (user_id, joined_at, week_start, ref_month, referred_by) "
            "VALUES (?,?,?,?,?)",
            (user_id, datetime.now().isoformat(), today_str(), month_str(), referred_by),
        )
        conn.commit()
        if referred_by and referred_by != user_id:
            _reset_referral_month_if_needed(conn, referred_by)
            conn.execute(
                "UPDATE users SET referrals = referrals + 1 WHERE user_id=?",
                (referred_by,),
            )
            conn.commit()
    conn.close()


def _reset_week_if_needed(conn, user_id):
    row = conn.execute("SELECT week_start FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row or not row["week_start"]:
        conn.execute("UPDATE users SET week_start=? WHERE user_id=?", (today_str(), user_id))
        conn.commit()
        return
    start = datetime.strptime(row["week_start"], "%Y-%m-%d")
    if datetime.now() - start >= timedelta(days=7):
        conn.execute(
            "UPDATE users SET insta_used=0, yt_used=0, pin_used=0, week_start=? WHERE user_id=?",
            (today_str(), user_id),
        )
        conn.commit()


def _reset_referral_month_if_needed(conn, user_id):
    row = conn.execute("SELECT ref_month FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row["ref_month"] != month_str():
        conn.execute(
            "UPDATE users SET referrals=0, ref_month=? WHERE user_id=?",
            (month_str(), user_id),
        )
        conn.commit()


def has_unlimited(user_id: int, referral_needed: int) -> bool:
    conn = get_conn()
    _reset_referral_month_if_needed(conn, user_id)
    row = conn.execute(
        "SELECT referrals, sub_until FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    if not row:
        return False
    if row["referrals"] >= referral_needed:
        return True
    if row["sub_until"]:
        try:
            if datetime.fromisoformat(row["sub_until"]) > datetime.now():
                return True
        except ValueError:
            pass
    return False


def can_download(user_id: int, platform: str, weekly_insta: int, weekly_yt: int, weekly_pin: int, referral_needed: int):
    """Returns (allowed: bool, reason: str|None)"""
    if has_unlimited(user_id, referral_needed):
        return True, None
    conn = get_conn()
    _reset_week_if_needed(conn, user_id)
    row = conn.execute(
        "SELECT insta_used, yt_used, pin_used FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    if platform == "instagram":
        if row["insta_used"] >= weekly_insta:
            return False, f"سقف هفتگی اینستا ({weekly_insta} ویدیو) تموم شده"
    elif platform == "youtube":
        if row["yt_used"] >= weekly_yt:
            return False, f"سقف هفتگی یوتیوب ({weekly_yt} ویدیو) تموم شده"
    else:
        if row["pin_used"] >= weekly_pin:
            return False, f"سقف هفتگی پینترست ({weekly_pin} ویدیو) تموم شده"
    return True, None


def increment_usage(user_id: int, platform: str):
    col = {"instagram": "insta_used", "youtube": "yt_used", "pinterest": "pin_used"}[platform]
    conn = get_conn()
    conn.execute(f"UPDATE users SET {col} = {col} + 1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def set_subscription(user_id: int, days: int):
    conn = get_conn()
    row = conn.execute("SELECT sub_until FROM users WHERE user_id=?", (user_id,)).fetchone()
    now = datetime.now()
    base = now
    if row and row["sub_until"]:
        try:
            existing = datetime.fromisoformat(row["sub_until"])
            if existing > now:
                base = existing  # extend from existing expiry if still active
        except ValueError:
            pass
    until = base + timedelta(days=days)
    conn.execute("UPDATE users SET sub_until=? WHERE user_id=?", (until.isoformat(), user_id))
    conn.commit()
    conn.close()
    return until


def get_referrals(user_id: int) -> int:
    conn = get_conn()
    _reset_referral_month_if_needed(conn, user_id)
    row = conn.execute("SELECT referrals FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row["referrals"] if row else 0


def get_usage_summary(user_id: int, weekly_insta: int, weekly_yt: int, weekly_pin: int):
    conn = get_conn()
    _reset_week_if_needed(conn, user_id)
    row = conn.execute(
        "SELECT insta_used, yt_used, pin_used, sub_until FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "insta_left": max(0, weekly_insta - row["insta_used"]),
        "yt_left": max(0, weekly_yt - row["yt_used"]),
        "pin_left": max(0, weekly_pin - row["pin_used"]),
        "sub_until": row["sub_until"],
    }
