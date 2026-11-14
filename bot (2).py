import logging
import os
import asyncio
import httpx
import sqlite3
import io
from typing import Tuple
from PIL import Image, ImageDraw, ImageFont

from telegram.request import HTTPXRequest

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------
# Конфигурация
# ------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:10809")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watermark_bot.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния диалога
WAITING_TEXT = 1

POSITIONS = {
    "top_left": "\u2196\ufe0f Top-Left",
    "top_center": "\u2191 Top-Center",
    "top_right": "\u2197\ufe0f Top-Right",
    "middle_left": "\u2190 Middle-Left",
    "center": "\u2022 Center",
    "middle_right": "\u2192 Middle-Right",
    "bottom_left": "\u2199\ufe0f Bottom-Left",
    "bottom_center": "\u2193 Bottom-Center",
    "bottom_right": "\u2198\ufe0f Bottom-Right",
}
POSITION_ORDER = list(POSITIONS.keys())

COLORS = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "yellow": (255, 215, 0),
    "cyan": (0, 200, 255),
}
COLOR_ORDER = list(COLORS.keys())

DEFAULT_SETTINGS = {
    "font_size": 48,
    "opacity": 60,
    "color": "white",
    "position": "bottom_right",
    "rotation": 0,
}

FONT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]

# ------------------------------------------------------------------
# База данных
# ------------------------------------------------------------------

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init() -> None:
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            font_size INTEGER NOT NULL,
            opacity INTEGER NOT NULL,
            color TEXT NOT NULL,
            position TEXT NOT NULL,
            rotation INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

def db_load_settings(user_id: int) -> dict:
    conn = db_connect()
    row = conn.execute(
        "SELECT font_size, opacity, color, position, rotation FROM user_settings WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return DEFAULT_SETTINGS.copy()

def db_save_settings(user_id: int, settings: dict) -> None:
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO user_settings (user_id, font_size, opacity, color, position, rotation)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            font_size=excluded.font_size,
            opacity=excluded.opacity,
            color=excluded.color,
            position=excluded.position,
            rotation=excluded.rotation
        """,
        (
            user_id,
            settings["font_size"],
            settings["opacity"],
            settings["color"],
            settings["position"],
            settings["rotation"],
        ),
    )
    conn.commit()
    conn.close()

def db_delete_settings(user_id: int) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# ------------------------------------------------------------------
# Логика обработки изображений
# ------------------------------------------------------------------

def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    logger.warning(
        "Ни один .ttf шрифт не найден. Использую стандартный шрифт PIL."
    )
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

def compute_position(
    canvas_size: Tuple[int, int], text_size: Tuple[int, int], position: str, margin: int = 20
) -> Tuple[int, int]:
    cw, ch = canvas_size
    tw, th = text_size

    x_map = {"left": margin, "center": (cw - tw) // 2, "right": cw - tw - margin}
    y_map = {"top": margin, "middle": (ch - th) // 2, "bottom": ch - th - margin}

    mapping = {
        "top_left": ("left", "top"),
        "top_center": ("center", "top"),
        "top_right": ("right", "top"),
        "middle_left": ("left", "middle"),
        "center": ("center", "middle"),
        "middle_right": ("right", "middle"),
        "bottom_left": ("left", "bottom"),
        "bottom_center": ("center", "bottom"),
        "bottom_right": ("right", "bottom"),
    }
    x_key, y_key = mapping.get(position, ("right", "bottom"))
    return x_map[x_key], y_map[y_key]

def apply_watermark(image_bytes: bytes, text: str, settings: dict) -> bytes:
    base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    font = load_font(settings["font_size"])
    color = COLORS.get(settings["color"], (255, 255, 255))
    alpha = int(255 * (settings["opacity"] / 100))

    scratch_draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    bbox = scratch_draw.textbbox((0, 0), text, font=font, stroke_width=2)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad = 14
    tile = Image.new("RGBA", (text_w + pad * 2, text_h + pad * 2), (0, 0, 0, 0))
    tile_draw = ImageDraw.Draw(tile)
    # Outline gives it a clean "designed" edge instead of flat text
    stroke_color = (0, 0, 0) if color != (0, 0, 0) else (255, 255, 255)
    tile_draw.text(
        (pad - bbox[0], pad - bbox[1]), text, font=font,
        fill=(*color, alpha),
        stroke_width=2, stroke_fill=(*stroke_color, int(alpha * 0.6)),
    )

    rotation = settings.get("rotation", 0)
    tile = tile.rotate(rotation, expand=True, resample=Image.BICUBIC)

    tile_w, tile_h = tile.size
    # Wider gaps = calmer pattern, not crowded/"crawling"
    gap_x, gap_y = int(tile_w * 1.1), int(tile_h * 2.0)
    step_x, step_y = tile_w + gap_x, tile_h + gap_y

    canvas_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    y, row = -tile_h, 0
    while y < base.height + tile_h:
        x = -tile_w if row % 2 == 0 else -tile_w - step_x // 2
        while x < base.width + tile_w:
            canvas_layer.paste(tile, (x, y), tile)
            x += step_x
        y += step_y
        row += 1

    watermarked = Image.alpha_composite(base, canvas_layer)

    out = io.BytesIO()
    watermarked.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()

# ------------------------------------------------------------------
# Интерфейс и клавиатуры
# ------------------------------------------------------------------

def settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("A- Размер", callback_data="font_size_down"),
            InlineKeyboardButton(f"Шрифт: {settings['font_size']}", callback_data="noop"),
            InlineKeyboardButton("Размер +A", callback_data="font_size_up"),
        ],
        [
            InlineKeyboardButton("\U0001F4A7 - Прозр.", callback_data="opacity_down"),
            InlineKeyboardButton(f"Прозр: {settings['opacity']}%", callback_data="noop"),
            InlineKeyboardButton("Прозр. +\U0001F4A7", callback_data="opacity_up"),
        ],
        [
            InlineKeyboardButton("\U0001F504 Поворот -", callback_data="rotation_down"),
            InlineKeyboardButton(f"Угол: {settings['rotation']}\u00b0", callback_data="noop"),
            InlineKeyboardButton("Поворот +\U0001F504", callback_data="rotation_up"),
        ],
        [
            InlineKeyboardButton(f"Цвет: {settings['color'].upper()}", callback_data="toggle_color")
        ],
    ]
    return InlineKeyboardMarkup(rows)

# ------------------------------------------------------------------
# Обработчики команд Telegram
# ------------------------------------------------------------------

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo_file = await update.message.photo[-1].get_file()
    context.user_data["photo_bytes"] = await photo_file.download_as_bytearray()

    await update.message.reply_text("Теперь отправь мне текст, который ты хочешь наложить на фото:")
    return WAITING_TEXT


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    photo_bytes = context.user_data.get("photo_bytes")

    if not photo_bytes:
        await update.message.reply_text("Произошла ошибка. Пожалуйста, отправь фото заново.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    settings = db_load_settings(user_id)

    status_msg = await update.message.reply_text("Обрабатываю изображение...")

    try:
        loop = asyncio.get_running_loop()
        result_bytes = await loop.run_in_executor(
            None, apply_watermark, bytes(photo_bytes), text, settings
        )

        await update.message.reply_photo(photo=io.BytesIO(result_bytes))
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Ошибка при создании водяного знака: {e}")
        await status_msg.edit_text("Не удалось обработать изображение.")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>Привет! Я наношу водяные знаки на фото.</b>\n\n"
        "📸 Просто пришли мне картинку — а дальше текст для водяного знака.\n"
        "⚙️ /settings — настроить размер, прозрачность, поворот и цвет\n"
        "♻️ /resetsettings — сбросить настройки к значениям по умолчанию",
        parse_mode="HTML",
    )


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    settings = db_load_settings(user_id)
    keyboard = settings_keyboard(settings)
    await update.message.reply_text(
        "🎨 <b>Настройки водяного знака</b>\n"
        "Нажимай на кнопки, чтобы подстроить под себя:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def reset_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_delete_settings(update.effective_user.id)
    await update.message.reply_text(
        "Настройки сброшены к значениям по умолчанию (шрифт 48, непрозрачность 60%)."
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "noop":
        return

    user_id = update.effective_user.id
    settings = db_load_settings(user_id)

    if query.data == "font_size_up":
        settings["font_size"] = min(200, settings["font_size"] + 4)
    elif query.data == "font_size_down":
        settings["font_size"] = max(10, settings["font_size"] - 4)
    elif query.data == "opacity_up":
        settings["opacity"] = min(100, settings["opacity"] + 10)
    elif query.data == "opacity_down":
        settings["opacity"] = max(10, settings["opacity"] - 10)
    elif query.data == "rotation_up":
        settings["rotation"] = (settings["rotation"] + 15) % 360
    elif query.data == "rotation_down":
        settings["rotation"] = (settings["rotation"] - 15) % 360
    elif query.data == "toggle_color":
        idx = COLOR_ORDER.index(settings["color"])
        settings["color"] = COLOR_ORDER[(idx + 1) % len(COLOR_ORDER)]
    elif query.data == "toggle_position":
        idx = POSITION_ORDER.index(settings["position"])
        settings["position"] = POSITION_ORDER[(idx + 1) % len(POSITION_ORDER)]

    db_save_settings(user_id, settings)
    await query.edit_message_reply_markup(reply_markup=settings_keyboard(settings))


# ------------------------------------------------------------------
# Запуск бота
# ------------------------------------------------------------------

def main() -> None:
    db_init()
    
    # Создаём и фиксируем Event Loop для Python 3.12+
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if PROXY_URL:
        logger.info(f"Запуск через прокси: {PROXY_URL}")
        
        # HTTPXRequest принимает параметры тайм-аутов напрямую
        request_client = HTTPXRequest(
            proxy_url=PROXY_URL,
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=30.0
        )
        
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .request(request_client)
            .get_updates_request(request_client)
            .build()
        )
    else:
        logger.info("Запуск без прокси")
        application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, photo_handler)],
        states={
            WAITING_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CommandHandler("resetsettings", reset_settings))
    application.add_handler(CallbackQueryHandler(settings_callback))
    application.add_handler(conv_handler)

    print("Бот запущен. Ожидание сообщений...")
    application.run_polling()


if __name__ == "__main__":
    main()