import asyncio
import logging
import os
import re
import subprocess
import uuid

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv
from yt_dlp import YoutubeDL

import db

load_dotenv()

# ---------------------------------------------------------------------------
# تنظیمات (همه از Environment Variables / Secrets خونده میشه)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

# کانال‌های جوین اجباری: با کاما جدا میشن. هرکدوم می‌تونه @username یا آیدی عددی (-100...) باشه
FORCE_CHANNELS = [c.strip() for c in os.getenv("FORCE_CHANNELS", "").split(",") if c.strip()]
# متنی که روی دکمه‌های جوین نشون داده میشه (به‌جای آیدی واقعی کانال)
FORCE_CHANNEL_LABEL = os.getenv("FORCE_CHANNEL_LABEL", "عضویت در کانال پشتیبانی")

CARD_NUMBER = os.getenv("CARD_NUMBER", "")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")

SUB_PRICE_2M = os.getenv("SUB_PRICE_2M", "200000")
SUB_PRICE_6M = os.getenv("SUB_PRICE_6M", "500000")
SUB_PRICE_1Y = os.getenv("SUB_PRICE_1Y", "700000")

REFERRAL_NEEDED = int(os.getenv("REFERRAL_NEEDED", "5"))
WEEKLY_INSTA = int(os.getenv("WEEKLY_INSTA", "2"))
WEEKLY_YT = int(os.getenv("WEEKLY_YT", "1"))
WEEKLY_PIN = int(os.getenv("WEEKLY_PIN", "3"))

# سقف واقعی آپلود ویدیو با توکن ربات تلگرام: ۵۰ مگابایت
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

SUB_PLANS = [
    {"key": "2m", "label": "۲ ماهه", "days": 60, "price": SUB_PRICE_2M},
    {"key": "6m", "label": "۶ ماهه", "days": 180, "price": SUB_PRICE_6M},
    {"key": "1y", "label": "۱ ساله", "days": 365, "price": SUB_PRICE_1Y},
]
PLAN_BY_KEY = {p["key"]: p for p in SUB_PLANS}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# داده‌های موقت در حافظه (بین ریست‌های ربات پاک میشن، اشکالی نداره چون کوتاه‌مدتن)
pending_download: dict[str, dict] = {}      # token -> {"user_id":.., "url":.., "platform":..}
pending_plan_choice: dict[int, str] = {}    # user_id -> plan_key (منتظر رسید پرداخت)

YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)")
INSTAGRAM_RE = re.compile(r"instagram\.com")
PINTEREST_RE = re.compile(r"(pinterest\.[a-z.]+|pin\.it)")


# ---------------------------------------------------------------------------
# جوین اجباری
# ---------------------------------------------------------------------------
async def check_membership(user_id: int) -> tuple[list[str], list[str]]:
    """
    برمی‌گردونه (کانال‌هایی که عضو نیست, کانال‌هایی که خطای دسترسی داشتن).
    خطای دسترسی معمولاً یعنی ربات تو اون کانال ادمین نیست.
    """
    not_joined = []
    access_errors = []
    for channel in FORCE_CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(channel)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logger.warning("Membership check failed for %s: %s", channel, e)
            access_errors.append(channel)
        except Exception as e:
            logger.warning("Unexpected error checking %s: %s", channel, e)
            access_errors.append(channel)
    return not_joined, access_errors


def _channel_join_url(channel: str) -> str:
    if channel.startswith("@"):
        return f"https://t.me/{channel.lstrip('@')}"
    # آیدی عددی (کانال خصوصی) - باید لینک دعوت جداگونه ست بشه، فعلا یه فالبک ساده
    return f"https://t.me/{channel}"


def join_keyboard(missing: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i, ch in enumerate(missing, start=1):
        label = FORCE_CHANNEL_LABEL if len(missing) == 1 else f"{FORCE_CHANNEL_LABEL} {i}"
        rows.append([InlineKeyboardButton(text=f"🔒 {label}", url=_channel_join_url(ch))])
    rows.append([InlineKeyboardButton(text="✅ عضو شدم", callback_data="check_join")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def enforce_join(message: Message) -> bool:
    if not FORCE_CHANNELS:
        return True
    missing, errors = await check_membership(message.from_user.id)

    if errors and ADMIN_ID:
        # به ادمین خبر بده که یه کانال قابل‌چک نیست (احتمالاً ربات توش ادمین نیست)
        await bot.send_message(
            ADMIN_ID,
            "⚠️ ربات نتونست عضویت کاربر رو تو این کانال(ها) چک کنه:\n"
            + "\n".join(errors)
            + "\n\nاحتمالاً ربات تو این کانال ادمین نیست. لطفاً ربات رو ادمین کانال کن.",
        )

    if missing or errors:
        await message.answer(
            "برای استفاده از ربات، اول باید عضو کانال زیر بشی 👇\n"
            "بعد از عضویت، رو دکمه «✅ عضو شدم» بزن.",
            reply_markup=join_keyboard(missing + errors),
        )
        return False
    return True


@router.callback_query(F.data == "check_join")
async def cb_check_join(call: CallbackQuery):
    missing, errors = await check_membership(call.from_user.id)
    if missing or errors:
        await call.answer("هنوز عضو نشدی، یا هنوز داخل کانال ثبت نشدی. چند ثانیه صبر کن و دوباره امتحان کن.", show_alert=True)
        return
    await call.message.edit_text(
        "عضویت تایید شد ✅\n\n"
        "حالا می‌تونی از ربات استفاده کنی. یه لینک از اینستاگرام، یوتیوب یا پینترست بفرست 🎬"
    )


# ---------------------------------------------------------------------------
# استارت / معرفی کامل
# ---------------------------------------------------------------------------
WELCOME_TEXT = (
    "سلام 👋🎬\n\n"
    "به ربات دانلودر خوش اومدی! از اینستاگرام، یوتیوب و پینترست ویدیو دانلود کن، سریع و بدون واترمارک ⚡️\n\n"
    "📦 <b>سهمیه رایگان هفتگی:</b>\n"
    f"📸 اینستاگرام: {WEEKLY_INSTA} ویدیو\n"
    f"▶️ یوتیوب: {WEEKLY_YT} ویدیو (با انتخاب کیفیت 360/480/720)\n"
    f"📌 پینترست: {WEEKLY_PIN} ویدیو\n\n"
    "🎁 <b>چطور نامحدود دانلود کنی؟</b>\n"
    f"۱. {REFERRAL_NEEDED} نفر رو با لینک دعوتت وارد ربات کن → یک ماه کامل نامحدود می‌شی (هر ماه دوباره با {REFERRAL_NEEDED} دعوت تمدید میشه)\n"
    "۲. یا یکی از پلن‌های اشتراک رو بخر (/buy) → کاملاً نامحدود تا پایان مدت اشتراک\n\n"
    "دستورات:\n"
    "/invite - لینک دعوت و وضعیت رفرال\n"
    "/buy - خرید اشتراک نامحدود\n"
    "/status - سهمیه باقی‌مونده‌ات"
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    args = message.text.split(maxsplit=1)
    referred_by = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referred_by = int(args[1].replace("ref_", ""))
        except ValueError:
            referred_by = None

    db.ensure_user(message.from_user.id, referred_by)

    if not await enforce_join(message):
        return

    await message.answer(WELCOME_TEXT)


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not await enforce_join(message):
        return
    db.ensure_user(message.from_user.id)
    summary = db.get_usage_summary(message.from_user.id, WEEKLY_INSTA, WEEKLY_YT, WEEKLY_PIN)
    unlimited = db.has_unlimited(message.from_user.id, REFERRAL_NEEDED)
    if unlimited:
        await message.answer("🎉 تو الان دسترسی <b>نامحدود</b> داری، هر چقدر بخوای دانلود کن!")
        return
    await message.answer(
        "📊 <b>سهمیه باقی‌مونده این هفته:</b>\n"
        f"📸 اینستاگرام: {summary['insta_left']}\n"
        f"▶️ یوتیوب: {summary['yt_left']}\n"
        f"📌 پینترست: {summary['pin_left']}"
    )


@router.message(Command("invite"))
async def cmd_invite(message: Message):
    if not await enforce_join(message):
        return
    db.ensure_user(message.from_user.id)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    count = db.get_referrals(message.from_user.id)
    remaining = max(0, REFERRAL_NEEDED - count)
    await message.answer(
        "👥 <b>لینک دعوت اختصاصی تو:</b>\n"
        f"{link}\n\n"
        f"تعداد دعوت این ماه: <b>{count}</b> از {REFERRAL_NEEDED}\n"
        + (
            f"فقط <b>{remaining} نفر</b> دیگه دعوت کن تا این ماه کامل نامحدود بشی 🎉"
            if remaining > 0
            else "تبریک 🎉 این ماه نامحدودی! ماه بعد دوباره با همین تعداد تمدید میشه."
        )
    )


# ---------------------------------------------------------------------------
# خرید اشتراک
# ---------------------------------------------------------------------------
def plans_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{p['label']} - {int(p['price']):,} تومان", callback_data=f"plan_{p['key']}")]
        for p in SUB_PLANS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("buy"))
async def cmd_buy(message: Message):
    if not await enforce_join(message):
        return
    await message.answer(
        "💎 <b>پلن‌های اشتراک نامحدود</b>\n"
        "با خرید اشتراک، محدودیت هفتگی از هر سه پلتفرم (اینستا، یوتیوب، پینترست) برداشته میشه، تا آخر مدت اشتراک 🎉\n\n"
        "یکی از پلن‌ها رو انتخاب کن 👇",
        reply_markup=plans_keyboard(),
    )


@router.callback_query(F.data.startswith("plan_"))
async def cb_choose_plan(call: CallbackQuery):
    plan_key = call.data.split("_", 1)[1]
    plan = PLAN_BY_KEY.get(plan_key)
    if not plan:
        await call.answer("این پلن پیدا نشد", show_alert=True)
        return
    pending_plan_choice[call.from_user.id] = plan_key
    await call.message.edit_text(
        f"✅ پلن انتخابی: <b>{plan['label']}</b> - {int(plan['price']):,} تومان\n\n"
        f"💳 مبلغ رو به شماره کارت زیر واریز کن:\n"
        f"<code>{CARD_NUMBER}</code>\n"
        f"به نام: {CARD_HOLDER}\n\n"
        "📸 بعد از واریز، عکس رسید رو همینجا برام بفرست تا برای بررسی به ادمین ارسال بشه."
    )


@router.message(F.photo)
async def receipt_photo(message: Message):
    if not await enforce_join(message):
        return

    plan_key = pending_plan_choice.get(message.from_user.id)
    if not plan_key:
        await message.answer(
            "برای خرید اشتراک اول یکی از پلن‌ها رو با /buy انتخاب کن، بعد رسید رو بفرست 🙏"
        )
        return

    plan = PLAN_BY_KEY[plan_key]
    caption = (
        f"🧾 رسید پرداخت جدید\n"
        f"پلن: {plan['label']} ({int(plan['price']):,} تومان)\n"
        f"از کاربر: {message.from_user.full_name} (@{message.from_user.username})\n"
        f"آیدی عددی: {message.from_user.id}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید و فعال‌سازی", callback_data=f"approve_{message.from_user.id}_{plan_key}"),
        InlineKeyboardButton(text="❌ رد", callback_data=f"reject_{message.from_user.id}"),
    ]])
    if ADMIN_ID:
        await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=caption, reply_markup=kb)
    await message.answer("رسیدت برای بررسی ارسال شد، منتظر تایید بمون ⏳")


@router.callback_query(F.data.startswith("approve_"))
async def cb_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    _, user_id_str, plan_key = call.data.split("_", 2)
    user_id = int(user_id_str)
    plan = PLAN_BY_KEY.get(plan_key)
    if not plan:
        await call.answer("پلن نامعتبر", show_alert=True)
        return
    until = db.set_subscription(user_id, plan["days"])
    pending_plan_choice.pop(user_id, None)
    new_caption = (call.message.caption or "") + "\n\n✅ تایید شد"
    await call.message.edit_caption(caption=new_caption)
    await bot.send_message(
        user_id,
        f"🎉 اشتراک <b>{plan['label']}</b> فعال شد!\n"
        f"تا تاریخ {until.strftime('%Y-%m-%d')} کاملاً نامحدود می‌تونی از هر پلتفرم دانلود کنی."
    )


@router.callback_query(F.data.startswith("reject_"))
async def cb_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    user_id = int(call.data.split("_", 1)[1])
    new_caption = (call.message.caption or "") + "\n\n❌ رد شد"
    await call.message.edit_caption(caption=new_caption)
    await bot.send_message(user_id, "رسید پرداختت تایید نشد 🙏 لطفاً با پشتیبانی هماهنگ کن یا دوباره رسید درست رو بفرست.")


# ---------------------------------------------------------------------------
# ابزار ادمین برای عیب‌یابی کانال‌های جوین اجباری
# ---------------------------------------------------------------------------
@router.message(Command("checkchannels"))
async def cmd_checkchannels(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not FORCE_CHANNELS:
        await message.answer("هیچ کانالی برای جوین اجباری تنظیم نشده (FORCE_CHANNELS خالیه).")
        return
    lines = []
    me = await bot.get_me()
    for ch in FORCE_CHANNELS:
        try:
            member = await bot.get_chat_member(ch, me.id)
            status = member.status
            ok = status in ("administrator", "creator")
            lines.append(f"{ch} → وضعیت ربات: {status} {'✅' if ok else '⚠️ (باید ادمین باشه)'}")
        except Exception as e:
            lines.append(f"{ch} → ❌ خطا: {e}\n(احتمالاً یوزرنیم اشتباهه یا ربات اصلاً عضو نیست)")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# دانلود
# ---------------------------------------------------------------------------
def quality_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="360p", callback_data=f"q_{token}_360"),
        InlineKeyboardButton(text="480p", callback_data=f"q_{token}_480"),
        InlineKeyboardButton(text="720p", callback_data=f"q_{token}_720"),
    ]])


def _compress_to_fit(path: str, max_bytes: int = MAX_UPLOAD_BYTES) -> str:
    """اگه فایل بزرگ‌تر از سقف مجاز تلگرام بود، با ffmpeg فشرده‌ش می‌کنه."""
    if os.path.getsize(path) <= max_bytes:
        return path

    base, _ = os.path.splitext(path)
    attempts = [(720, 26), (480, 28), (480, 32), (360, 32), (360, 36), (240, 36)]

    last_out = path
    for height, crf in attempts:
        out = f"{base}_c{height}_{crf}.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", path,
            "-vf", f"scale=-2:{height}",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
            "-c:a", "aac", "-b:a", "96k",
            out,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=600)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("ffmpeg failed: %s", e)
            break
        if result.returncode == 0 and os.path.exists(out):
            last_out = out
            if os.path.getsize(out) <= max_bytes:
                if os.path.exists(path) and path != out:
                    os.remove(path)
                return out
    return last_out


def _build_instagram_caption(info: dict) -> str:
    description = (info.get("description") or "").strip()
    uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or "نامشخص"
    like_count = info.get("like_count")
    comment_count = info.get("comment_count")

    if len(description) > 600:
        description = description[:600] + "…"

    parts = [f"👤 پیج: @{uploader}"]
    if like_count is not None:
        parts.append(f"❤️ لایک: {like_count:,}")
    if comment_count is not None:
        parts.append(f"💬 کامنت: {comment_count:,}")
    if description:
        parts.append(f"📝 کپشن:\n{description}")

    caption = "\n".join(parts)
    # تلگرام کپشن رو به ۱۰۲۴ کاراکتر محدود می‌کنه
    if len(caption) > 1024:
        caption = caption[:1000] + "…"
    return caption


async def download_and_send(user_id: int, url: str, platform: str, height: int = None):
    os.makedirs("downloads", exist_ok=True)
    outtmpl = "downloads/%(id)s.%(ext)s"

    if platform == "youtube":
        fmt = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
    else:
        # اول سعی کن فایلی زیر سقف مجاز پیدا کنی، وگرنه بهترین کیفیت رو بگیر و بعداً فشرده کن
        fmt = f"best[filesize<{MAX_UPLOAD_BYTES}]/best"

    ydl_opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
    }

    loop = asyncio.get_event_loop()

    def run_download():
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            # وقتی merge میشه، پسوند نهایی معمولاً mp4 هست
            if not os.path.exists(filepath):
                alt = os.path.splitext(filepath)[0] + ".mp4"
                if os.path.exists(alt):
                    filepath = alt
            return filepath, info

    try:
        filepath, info = await loop.run_in_executor(None, run_download)
    except Exception as e:
        logger.error("Download failed: %s", e)
        await bot.send_message(
            user_id,
            "⚠️ دانلود ناموفق بود. ممکنه لینک خصوصی/حذف‌شده باشه یا سایت مقصد موقتاً مشکل داشته باشه.\n"
            "دوباره امتحان کن یا لینک دیگه‌ای بفرست."
        )
        return

    if not os.path.exists(filepath):
        await bot.send_message(user_id, "⚠️ فایل دانلود شده پیدا نشد، دوباره امتحان کن.")
        return

    # اگه بزرگ‌تر از سقف تلگرامه، فشرده‌ش کن
    if os.path.getsize(filepath) > MAX_UPLOAD_BYTES:
        await bot.send_message(user_id, "⏳ حجم فایل بالاست، در حال فشرده‌سازی...")
        filepath = await loop.run_in_executor(None, _compress_to_fit, filepath)

    if os.path.getsize(filepath) > MAX_UPLOAD_BYTES:
        await bot.send_message(
            user_id,
            "❌ حتی بعد از فشرده‌سازی هم حجم فایل بیشتر از سقف مجاز تلگرامه (۵۰ مگابایت).\n"
            "این معمولاً برای ویدیوهای طولانیه؛ کیفیت پایین‌تر یا لینک کوتاه‌تر امتحان کن."
        )
        if os.path.exists(filepath):
            os.remove(filepath)
        return

    caption = "✅ آماده شد"
    if platform == "instagram":
        try:
            caption = _build_instagram_caption(info)
        except Exception as e:
            logger.warning("Could not build instagram caption: %s", e)

    try:
        await bot.send_video(user_id, FSInputFile(filepath), caption=caption)
        db.increment_usage(user_id, platform)
    except Exception as e:
        logger.error("Send video failed: %s", e)
        await bot.send_message(user_id, f"⚠️ ارسال ویدیو ناموفق بود: {e}")
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


@router.message(F.text.regexp(r"https?://\S+"))
async def handle_link(message: Message):
    if not await enforce_join(message):
        return

    url = message.text.strip()
    user_id = message.from_user.id
    db.ensure_user(user_id)

    if YOUTUBE_RE.search(url):
        platform = "youtube"
    elif INSTAGRAM_RE.search(url):
        platform = "instagram"
    elif PINTEREST_RE.search(url):
        platform = "pinterest"
    else:
        await message.answer("این لینک رو نمی‌شناسم 🤔 فقط لینک اینستاگرام، یوتیوب یا پینترست بفرست.")
        return

    allowed, reason = db.can_download(user_id, platform, WEEKLY_INSTA, WEEKLY_YT, WEEKLY_PIN, REFERRAL_NEEDED)
    if not allowed:
        await message.answer(
            f"🚫 {reason}\n"
            f"برای دانلود نامحدود، {REFERRAL_NEEDED} نفر دعوت کن (/invite) یا اشتراک بخر (/buy)."
        )
        return

    if platform == "youtube":
        token = uuid.uuid4().hex[:8]
        pending_download[token] = {"user_id": user_id, "url": url, "platform": "youtube"}
        await message.answer("🎬 کیفیت مورد نظر رو انتخاب کن:", reply_markup=quality_keyboard(token))
    else:
        label = "اینستاگرام 📸" if platform == "instagram" else "پینترست 📌"
        await message.answer(f"⏳ در حال دانلود از {label}...")
        await download_and_send(user_id, url, platform)


@router.callback_query(F.data.startswith("q_"))
async def cb_quality(call: CallbackQuery):
    _, token, height = call.data.split("_")
    data = pending_download.pop(token, None)
    if not data:
        await call.answer("این درخواست منقضی شده، دوباره لینک رو بفرست.", show_alert=True)
        return
    await call.message.edit_text(f"⏳ در حال دانلود با کیفیت {height}p...")
    await download_and_send(data["user_id"], data["url"], "youtube", int(height))


# ---------------------------------------------------------------------------
async def main():
    db.init_db()
    logger.info("Bot starting, polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
