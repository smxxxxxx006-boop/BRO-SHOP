import os
import json
import uuid
import logging
import time
import mimetypes
import hmac
import hashlib
from urllib.parse import parse_qsl, urlencode
from datetime import datetime, timezone

from flask import Flask, request, send_from_directory, abort, jsonify, Response
import telebot
from telebot import types

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bro_shop")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

# Render автоматически прокидывает RENDER_EXTERNAL_URL для web-сервисов.
# Можно переопределить вручную через WEBHOOK_URL, если нужно.
BASE_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Telegram-аккаунт менеджера (без @) — на него ведёт кнопка «Написать менеджеру».
SHOP_CONTACT = (os.environ.get("SHOP_CONTACT") or "trushnyibro").lstrip("@").strip()

# Пароль для входа в админ-панель (/admin). Задаётся в переменных окружения.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Telegram ID администратора(ов). Только этим пользователям бот присылает
# ссылку на админ-панель по команде /start. Можно указать несколько ID
# через запятую: "111111,222222".
ADMIN_CHAT_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_CHAT_ID", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

# Строка подключения к Neon (Postgres), например:
# postgresql://user:password@ep-xxx.eu-central-1.aws.neon.tech/neondb?sslmode=require
# Если не задана — проект работает по-старому, на JSON-файлах (удобно для локального запуска).
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
USE_DB = bool(DATABASE_URL)
if USE_DB:
    import psycopg2
    from psycopg2.extras import Json

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(APP_DIR, "products.json")
ORDERS_FILE = os.path.join(APP_DIR, "orders.json")
QUESTIONS_FILE = os.path.join(APP_DIR, "questions.json")
PROMOS_FILE = os.path.join(APP_DIR, "promos.json")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_BADGES = {"hit", "trend", "sale", "instock"}
ALLOWED_PROMO_TYPES = {"percent", "fixed"}
# Способ получения заказа (необязательное поле, фронтенд может его присылать).
DELIVERY_LABELS = {"pickup": "Самовывоз", "delivery": "Доставка"}
# Сколько секунд считаем initData из Telegram WebApp свежей (7 суток).
INIT_DATA_MAX_AGE = 7 * 24 * 3600

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 МБ на файл


# --- Вспомогательные функции для товаров и авторизации админа -----------

def require_admin():
    if not ADMIN_PASSWORD:
        abort(403, description="ADMIN_PASSWORD не задан на сервере")
    supplied = request.headers.get("X-Admin-Password", "")
    if supplied != ADMIN_PASSWORD:
        abort(401, description="Неверный пароль")


# --- Хранилище: Neon (Postgres) либо JSON-файлы ---------------------------

FILES = {
    "products": PRODUCTS_FILE,
    "orders": ORDERS_FILE,
    "questions": QUESTIONS_FILE,
    "promos": PROMOS_FILE,
}


def _file_load(table):
    path = FILES[table]
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _file_save(table, items):
    with open(FILES[table], "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def db_connect():
    """Подключение к Neon. Бесплатный Neon засыпает без трафика и просыпается
    за пару секунд, поэтому пробуем несколько раз."""
    last_err = None
    for _ in range(4):
        try:
            return psycopg2.connect(DATABASE_URL, connect_timeout=15)
        except psycopg2.OperationalError as e:
            last_err = e
            time.sleep(1.5)
    raise last_err


def _json(obj):
    return Json(obj, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def init_db():
    """Создаёт таблицы и один раз переносит в них данные из старых JSON-файлов
    (products.json, orders.json, ...) и из папки uploads/."""
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                for table in FILES:
                    cur.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} "
                        "(id INTEGER PRIMARY KEY, data JSONB NOT NULL)"
                    )
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS uploads ("
                    "filename TEXT PRIMARY KEY, content_type TEXT NOT NULL, "
                    "data BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
                )

                # Защита от двойного запуска миграции при нескольких воркерах
                cur.execute("SELECT pg_advisory_xact_lock(727001)")

                for table in FILES:
                    flag = f"seeded_{table}"
                    cur.execute("SELECT 1 FROM meta WHERE key=%s", (flag,))
                    if cur.fetchone():
                        continue
                    cur.execute(f"SELECT count(*) FROM {table}")
                    if cur.fetchone()[0] == 0:
                        for item in _file_load(table):
                            cur.execute(
                                f"INSERT INTO {table} (id, data) VALUES (%s, %s) "
                                "ON CONFLICT (id) DO NOTHING",
                                (int(item["id"]), _json(item)),
                            )
                        log.info("Таблица %s: перенесены данные из файла", table)
                    cur.execute("INSERT INTO meta (key, value) VALUES (%s, '1')", (flag,))

                cur.execute("SELECT 1 FROM meta WHERE key='seeded_uploads'")
                if not cur.fetchone():
                    if os.path.isdir(UPLOAD_DIR):
                        for fn in os.listdir(UPLOAD_DIR):
                            ext = os.path.splitext(fn)[1].lower()
                            full = os.path.join(UPLOAD_DIR, fn)
                            if ext in ALLOWED_IMAGE_EXT and os.path.isfile(full):
                                with open(full, "rb") as fh:
                                    cur.execute(
                                        "INSERT INTO uploads (filename, content_type, data) "
                                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                        (fn, image_content_type(fn), psycopg2.Binary(fh.read())),
                                    )
                    cur.execute("INSERT INTO meta (key, value) VALUES ('seeded_uploads', '1')")
    finally:
        conn.close()


def _db_load(table):
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT data FROM {table} ORDER BY id")
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _db_save(table, items):
    """Синхронизирует таблицу со списком items: меняются только изменённые
    записи, удалённые — убираются."""
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"LOCK TABLE {table} IN EXCLUSIVE MODE")
                cur.execute(f"SELECT id, data FROM {table}")
                existing = {r[0]: r[1] for r in cur.fetchall()}
                keep = set()
                for item in items:
                    iid = int(item["id"])
                    keep.add(iid)
                    if existing.get(iid) != item:
                        cur.execute(
                            f"INSERT INTO {table} (id, data) VALUES (%s, %s) "
                            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
                            (iid, _json(item)),
                        )
                for iid in set(existing) - keep:
                    cur.execute(f"DELETE FROM {table} WHERE id = %s", (iid,))
    finally:
        conn.close()


def _load(table):
    return _db_load(table) if USE_DB else _file_load(table)


def _save(table, items):
    if USE_DB:
        _db_save(table, items)
    else:
        _file_save(table, items)


def load_products():
    return _load("products")


def save_products(items):
    _save("products", items)


def load_orders():
    return _load("orders")


def save_orders(items):
    _save("orders", items)


def load_questions():
    return _load("questions")


def save_questions(items):
    _save("questions", items)


def load_promos():
    return _load("promos")


def save_promos(items):
    _save("promos", items)


def image_content_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif",
    }.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def fmt_price(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"


# --- Идентификация покупателя (CRM) ----------------------------------------

def normalize_username(raw):
    """'@Name ' -> 'name' (для поиска без учёта регистра и без @)."""
    return str(raw or "").strip().lstrip("@").strip().lower()


def clean_username(raw):
    """Username для хранения: без @ и пробелов, регистр сохраняется."""
    return str(raw or "").strip().lstrip("@").strip() or None


def verify_init_data(init_data):
    """Проверяет подпись Telegram WebApp initData (HMAC-SHA256 по токену бота).
    Возвращает dict пользователя (id, username, ...) или None, если подпись
    неверна / данные устарели. Это защищает от подмены Telegram ID в запросе."""
    if not init_data or not isinstance(init_data, str):
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            return None
        user = json.loads(pairs.get("user") or "null")
    except (TypeError, ValueError):
        return None
    return user if isinstance(user, dict) and user.get("id") else None


def extract_customer(data):
    """Определяет покупателя из тела запроса: (telegram_id, username).
    1) Если пришёл подписанный init_data — берём пользователя из него (надёжно).
    2) Иначе — из tg_user (старый формат, как отправляет текущий index.html)."""
    tg_user = verify_init_data(data.get("init_data")) or data.get("tg_user") or {}
    if not isinstance(tg_user, dict):
        tg_user = {}
    try:
        telegram_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        telegram_id = None
    return telegram_id, clean_username(tg_user.get("username"))


def order_telegram_id(order):
    """Telegram ID покупателя заказа (новое поле, для старых заказов — прежнее)."""
    return order.get("telegram_id") or order.get("customer_chat_id")


def order_username(order):
    return order.get("username") or order.get("customer_username")


def sanitize_delivery_method(raw):
    v = str(raw or "").strip().lower()
    return v if v in DELIVERY_LABELS else None


def sanitize_images(raw):
    if isinstance(raw, list):
        return [str(u).strip() for u in raw if isinstance(u, (str,)) and str(u).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def sanitize_brand(raw):
    """Бренд / подкатегория товара (необязательное поле)."""
    return str(raw or "").strip()[:60]


def sanitize_badge(raw):
    b = str(raw or "").strip().lower()
    return b if b in ALLOWED_BADGES else None


def sanitize_old_price(raw):
    if raw in (None, ""):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# --- Промокоды -----------------------------------------------------------

def sanitize_positive_int(raw):
    """Возвращает положительное целое либо None, если значение не задано/некорректно."""
    if raw in (None, ""):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def find_promo_by_code(code):
    code_norm = (code or "").strip().upper()
    if not code_norm:
        return None
    promos = load_promos()
    return next((p for p in promos if p.get("code") == code_norm), None)


def validate_promo_for_use(promo, subtotal, customer_chat_id=None):
    """Проверяет, что промокод можно применить к заказу на сумму subtotal.
    Возвращает текст ошибки либо None, если всё в порядке."""
    if not promo:
        return "Промокод не найден"
    if not promo.get("active", True):
        return "Промокод больше не активен"
    max_uses = promo.get("max_uses")
    if max_uses is not None and int(promo.get("used_count") or 0) >= int(max_uses):
        return "Лимит использований промокода исчерпан"
    min_total = promo.get("min_total")
    if min_total is not None and subtotal < int(min_total):
        return f"Промокод действует от суммы заказа {fmt_price(min_total)}"
    if customer_chat_id and has_customer_used_promo(promo.get("code"), customer_chat_id):
        return "Вы уже использовали этот промокод"
    return None


def has_customer_used_promo(promo_code, customer_chat_id):
    """Проверяет, применял ли этот покупатель данный промокод ранее
    (отменённые заказы не считаются использованием)."""
    if not promo_code or not customer_chat_id:
        return False
    orders = load_orders()
    return any(
        o.get("promo_code") == promo_code
        and o.get("customer_chat_id") == customer_chat_id
        and o.get("status") != "cancelled"
        for o in orders
    )


def compute_promo_discount(promo, subtotal):
    if promo.get("type") == "percent":
        discount = subtotal * float(promo.get("value") or 0) / 100.0
    else:
        discount = float(promo.get("value") or 0)
    discount = max(0.0, min(discount, subtotal))
    return int(round(discount))


# --- Telegram handlers -------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message):
    if not BASE_URL:
        bot.send_message(
            message.chat.id,
            "KOSHELEV: мини-апп ещё не сконфигурирован (не задан WEBHOOK_URL)."
        )
        return

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            text="👑 Открыть KOSHELEV",
            web_app=types.WebAppInfo(url=BASE_URL + "/")
        )
    )

    if SHOP_CONTACT:
        keyboard.add(
            types.InlineKeyboardButton(
                text="💬 Написать менеджеру",
                url=f"https://t.me/{SHOP_CONTACT}"
            )
        )

    is_admin = bool(message.from_user) and message.from_user.id in ADMIN_CHAT_IDS
    if is_admin:
        keyboard.add(
            types.InlineKeyboardButton(
                text="Панель администратора",
                web_app=types.WebAppInfo(url=BASE_URL + "/admin")
            )
        )

    bot.send_message(
        message.chat.id,
        "👑 *KOSHELEV — вейп-шоп*\n\n"
        "*Зашёл → выбрал → забрал.*\n"
        "Низкие цены, только оригинал, гарантия на покупку. "
        "Самовывоз и доставка по Москве день в день, без предоплаты.\n\n"
        "Нажмите кнопку ниже, чтобы открыть каталог, выбрать вкус и оформить заказ.\n\n"
        "🔞 _Продукция содержит никотин, который вызывает зависимость. Только для лиц старше 18 лет._",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )



@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message: types.Message):
    cmd_start(message)


# --- Flask routes --------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.route("/api/config", methods=["GET"])
def public_config():
    return jsonify({"contact": SHOP_CONTACT})


@app.route("/products.json", methods=["GET"])
def products():
    """Публичный каталог: скрытые товары покупателям не отдаются."""
    visible = [p for p in load_products() if not p.get("hidden")]
    resp = jsonify(visible)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def build_admin_order_markup(admin_id, customer_id, customer_username):
    """Inline-кнопка под уведомлением о заказе: открывает админку и сразу
    запускает поиск истории заказов этого покупателя.
    Приоритет — Telegram ID (он не меняется), иначе @username.
    Возвращает None, если открывать нечего (нет BASE_URL или нет идентификатора)."""
    if not BASE_URL:
        return None
    if customer_id:
        search = str(customer_id)
    elif customer_username:
        search = "@" + customer_username
    else:
        return None
    url = f"{BASE_URL}/admin?{urlencode({'customer': search})}"
    markup = types.InlineKeyboardMarkup()
    text = "🔍 Посмотреть историю заказов"
    # web_app-кнопки Telegram допускает только в личных чатах (положительный ID);
    # для групп/каналов отдаём обычную ссылку.
    if admin_id > 0:
        markup.add(types.InlineKeyboardButton(text=text, web_app=types.WebAppInfo(url=url)))
    else:
        markup.add(types.InlineKeyboardButton(text=text, url=url))
    return markup


@app.route("/api/order", methods=["POST"])
def create_order():
    data = request.get_json(force=True, silent=True) or {}

    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    comment = (data.get("comment") or "").strip()
    items = data.get("items") or []

    if not name or not phone:
        abort(400, description="Укажите имя и телефон")
    if not isinstance(items, list) or not items:
        abort(400, description="Корзина пуста")

    subtotal = sum(int(i.get("price") or 0) * int(i.get("qty") or 1) for i in items)

    # Telegram ID и username фиксируем на сервере (из подписанного initData,
    # а если его нет — из tg_user).
    customer_chat_id, customer_username = extract_customer(data)
    delivery_method = sanitize_delivery_method(data.get("delivery_method"))

    promo_code_raw = (data.get("promo_code") or "").strip()
    promo = None
    discount = 0
    if promo_code_raw:
        promo = find_promo_by_code(promo_code_raw)
        promo_err = validate_promo_for_use(promo, subtotal, customer_chat_id)
        if promo_err:
            abort(400, description=promo_err)
        discount = compute_promo_discount(promo, subtotal)

    total = subtotal - discount

    orders = load_orders()
    new_id = (max([o.get("id", 0) for o in orders], default=0) + 1)
    order = {
        "id": new_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "phone": phone,
        "comment": comment,
        "items": items,
        "subtotal": subtotal,
        "discount": discount,
        "promo_code": promo.get("code") if promo else None,
        "total": total,
        "delivery_method": delivery_method,
        # CRM: идентификатор клиента
        "telegram_id": customer_chat_id,
        "username": customer_username,
        # прежние поля оставлены — на них опираются index.html и admin.html
        "customer_chat_id": customer_chat_id,
        "customer_username": customer_username,
        "status": "new",
        "messages": [],
    }
    orders.append(order)
    save_orders(orders)

    if promo:
        promos = load_promos()
        for p in promos:
            if p.get("id") == promo.get("id"):
                p["used_count"] = int(p.get("used_count") or 0) + 1
        save_promos(promos)

    items_lines = "\n".join(
        f"· {i.get('name','—')} ({i.get('size') or '—'}) × {i.get('qty', 1)}"
        for i in items
    )

    discount_line = ""
    if discount > 0:
        discount_line = (
            f"Сумма товаров: {fmt_price(subtotal)}\n"
            f"Промокод: {promo.get('code')} (−{fmt_price(discount)})\n"
        )

    if customer_chat_id:
        try:
            bot.send_message(
                customer_chat_id,
                "✅ Заказ оформлен!\n\n"
                f"Номер заказа: KOSHELEV-{new_id}\n"
                f"{items_lines}\n\n"
                f"{discount_line}"
                f"Итого: {fmt_price(total)}\n\n"
                "Мы свяжемся с вами в этом чате, чтобы подтвердить заказ и "
                "обсудить доставку."
            )
        except Exception as e:
            log.warning("Не удалось отправить подтверждение покупателю: %s", e)

    buyer_id_label = str(customer_chat_id) if customer_chat_id else "ID не указан"
    buyer_username_label = f"@{customer_username}" if customer_username else "Юзернейм не указан"

    admin_text = (
        f"🛒 Новый заказ KOSHELEV-{new_id}\n\n"
        f"Клиент: {name}\n"
        f"Телефон: {phone}\n"
        f"Покупатель: {buyer_id_label} | {buyer_username_label}\n"
        + f"\n{items_lines}\n\n"
        f"{discount_line}"
        f"Итого: {fmt_price(total)}\n"
        + (f"\nКомментарий: {comment}\n" if comment else "")
        + "\nОткройте панель администратора (вкладка «Заказы»), чтобы "
          "написать клиенту и обсудить доставку."
    )
    for admin_id in ADMIN_CHAT_IDS:
        try:
            markup = build_admin_order_markup(admin_id, customer_chat_id, customer_username)
            try:
                bot.send_message(admin_id, admin_text, reply_markup=markup)
            except Exception as e:
                if markup is None:
                    raise
                # Кнопка не прошла (например, Telegram отклонил URL) —
                # само уведомление о заказе всё равно должно дойти.
                log.warning("Уведомление с кнопкой не отправилось (%s), шлю без кнопки", e)
                bot.send_message(admin_id, admin_text)
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)

    return jsonify({"ok": True, "order_id": new_id}), 201


@app.route("/api/promo/check", methods=["POST"])
def check_promo():
    """Проверка промокода из корзины (до оформления заказа)."""
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip()
    try:
        subtotal = int(float(data.get("subtotal") or 0))
    except (TypeError, ValueError):
        subtotal = 0
    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    if not code:
        abort(400, description="Введите промокод")

    promo = find_promo_by_code(code)
    err = validate_promo_for_use(promo, subtotal, customer_chat_id)
    if err:
        abort(400, description=err)

    discount = compute_promo_discount(promo, subtotal)
    return jsonify({
        "ok": True,
        "code": promo.get("code"),
        "type": promo.get("type"),
        "value": promo.get("value"),
        "discount": discount,
        "new_total": subtotal - discount,
    })


@app.route("/api/orders", methods=["GET"])
def customer_orders():
    """История заказов покупателя (по его Telegram ID)."""
    try:
        tg_id = int(request.args.get("tg_id", ""))
    except (TypeError, ValueError):
        abort(400, description="Некорректный tg_id")
    orders = load_orders()
    mine = [o for o in orders if o.get("customer_chat_id") == tg_id]
    mine.sort(key=lambda o: o.get("id", 0), reverse=True)
    return jsonify(mine)


@app.route("/api/orders/<int:oid>/cancel", methods=["POST"])
def customer_cancel_order(oid):
    """Покупатель отменяет свой заказ, если он ещё не взят в обработку."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        tg_id = int(data.get("tg_id"))
    except (TypeError, ValueError):
        abort(400, description="Некорректный tg_id")

    orders = load_orders()
    order = next((o for o in orders if o.get("id") == oid), None)
    if not order:
        abort(404, description="Заказ не найден")
    if order.get("customer_chat_id") != tg_id:
        abort(403, description="Это не ваш заказ")
    if order.get("status") != "new":
        abort(400, description="Заказ уже в обработке — отмена недоступна")

    order["status"] = "cancelled"
    save_orders(orders)

    for admin_id in ADMIN_CHAT_IDS:
        try:
            bot.send_message(admin_id, f"❌ Клиент отменил заказ KOSHELEV-{oid}")
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)

    return jsonify(order)


@app.route("/api/question", methods=["POST"])
def create_question():
    data = request.get_json(force=True, silent=True) or {}

    question = (data.get("question") or "").strip()
    product_name = (data.get("product_name") or "").strip()
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        product_id = None

    if not question:
        abort(400, description="Введите вопрос")

    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    customer_username = (tg_user.get("username") or "").strip() or None

    questions = load_questions()
    new_id = (max([q.get("id", 0) for q in questions], default=0) + 1)
    entry = {
        "id": new_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "product_id": product_id,
        "product_name": product_name,
        "question": question,
        "customer_chat_id": customer_chat_id,
        "customer_username": customer_username,
        "status": "new",
        "messages": [],
    }
    questions.append(entry)
    save_questions(questions)

    if customer_chat_id:
        try:
            bot.send_message(
                customer_chat_id,
                "✅ Ваш вопрос отправлен администратору.\n\n"
                f"Товар: {product_name}\n"
                f"Вопрос: {question}\n\n"
                "Мы ответим вам в этом чате."
            )
        except Exception as e:
            log.warning("Не удалось отправить подтверждение по вопросу: %s", e)

    admin_text = (
        f"❓ Новый вопрос по товару «{product_name}»\n\n"
        + (f"От: @{customer_username}\n\n" if customer_username else "\n")
        + f"{question}\n\n"
        "Откройте панель администратора (вкладка «Вопросы»), чтобы ответить клиенту."
    )
    for admin_id in ADMIN_CHAT_IDS:
        try:
            bot.send_message(admin_id, admin_text)
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)

    return jsonify({"ok": True, "question_id": new_id}), 201


@app.route("/uploads/<path:filename>", methods=["GET"])
def uploaded_file(filename):
    if USE_DB:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT content_type, data FROM uploads WHERE filename = %s", (filename,))
                row = cur.fetchone()
        finally:
            conn.close()
        if row:
            resp = Response(bytes(row[1]), mimetype=row[0])
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/admin", methods=["GET"])
def admin_page():
    return send_from_directory(APP_DIR, "admin.html")


@app.route("/healthz", methods=["GET"])
def healthz():
    return {"status": "ok", "storage": "neon" if USE_DB else "files"}


# --- Admin API -------------------------------------------------------

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    require_admin()
    return jsonify({"ok": True})


@app.route("/api/admin/products", methods=["GET"])
def admin_get_products():
    require_admin()
    return jsonify(load_products())


@app.route("/api/admin/products", methods=["POST"])
def admin_create_product():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    items = load_products()
    new_id = (max([p.get("id", 0) for p in items], default=0) + 1)
    sizes_raw = data.get("sizes", "")
    sizes = [s.strip() for s in sizes_raw.split(",") if s.strip()] if isinstance(sizes_raw, str) else (sizes_raw or [])
    try:
        price = int(float(data.get("price") or 0))
    except (TypeError, ValueError):
        price = 0
    images = sanitize_images(data.get("images") if "images" in data else data.get("image"))
    product = {
        "id": new_id,
        "name": (data.get("name") or "").strip(),
        "category": (data.get("category") or "").strip(),
        "brand": sanitize_brand(data.get("brand")),
        "price": price,
        "old_price": sanitize_old_price(data.get("old_price")),
        "badge": sanitize_badge(data.get("badge")),
        "sizes": sizes or ["Стандарт"],
        "swatch": int(data.get("swatch") or 0) % 6,
        "desc": (data.get("desc") or "").strip(),
        "images": images,
        "image": images[0] if images else None,
        "hidden": bool(data.get("hidden", False)),
    }
    items.append(product)
    save_products(items)
    return jsonify(product), 201


@app.route("/api/admin/products/<int:pid>", methods=["PUT"])
def admin_update_product(pid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    items = load_products()
    for p in items:
        if p.get("id") == pid:
            if "name" in data:
                p["name"] = (data.get("name") or "").strip()
            if "category" in data:
                p["category"] = (data.get("category") or "").strip()
            if "brand" in data:
                p["brand"] = sanitize_brand(data.get("brand"))
            if "price" in data:
                try:
                    p["price"] = int(float(data.get("price") or 0))
                except (TypeError, ValueError):
                    pass
            if "old_price" in data:
                p["old_price"] = sanitize_old_price(data.get("old_price"))
            if "badge" in data:
                p["badge"] = sanitize_badge(data.get("badge"))
            if "sizes" in data:
                sizes_raw = data.get("sizes", "")
                p["sizes"] = [s.strip() for s in sizes_raw.split(",") if s.strip()] if isinstance(sizes_raw, str) else (sizes_raw or p["sizes"])
            if "desc" in data:
                p["desc"] = (data.get("desc") or "").strip()
            if "swatch" in data:
                p["swatch"] = int(data.get("swatch") or 0) % 6
            if "hidden" in data:
                p["hidden"] = bool(data.get("hidden"))
            if "images" in data:
                images = sanitize_images(data.get("images"))
                p["images"] = images
                p["image"] = images[0] if images else None
            elif "image" in data:
                images = sanitize_images(data.get("image"))
                p["images"] = images
                p["image"] = images[0] if images else None
            save_products(items)
            return jsonify(p)
    abort(404, description="Товар не найден")


@app.route("/api/admin/products/<int:pid>", methods=["DELETE"])
def admin_delete_product(pid):
    require_admin()
    items = load_products()
    new_items = [p for p in items if p.get("id") != pid]
    if len(new_items) == len(items):
        abort(404, description="Товар не найден")
    save_products(new_items)
    return jsonify({"ok": True})


@app.route("/api/admin/upload", methods=["POST"])
def admin_upload():
    require_admin()
    file = request.files.get("file")
    if not file or not file.filename:
        abort(400, description="Файл не передан")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        abort(400, description="Недопустимый формат файла")
    filename = f"{uuid.uuid4().hex}{ext}"
    if USE_DB:
        data = file.read()
        conn = db_connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO uploads (filename, content_type, data) VALUES (%s, %s, %s)",
                        (filename, image_content_type(filename), psycopg2.Binary(data)),
                    )
        finally:
            conn.close()
    else:
        file.save(os.path.join(UPLOAD_DIR, filename))
    return jsonify({"url": f"/uploads/{filename}"}), 201


@app.route("/api/admin/promos", methods=["GET"])
def admin_get_promos():
    require_admin()
    promos = load_promos()
    promos.sort(key=lambda p: p.get("id", 0), reverse=True)
    return jsonify(promos)


@app.route("/api/admin/promos", methods=["POST"])
def admin_create_promo():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}

    code = (data.get("code") or "").strip().upper()
    if not code:
        abort(400, description="Укажите код промокода")

    promo_type = (data.get("type") or "").strip().lower()
    if promo_type not in ALLOWED_PROMO_TYPES:
        abort(400, description="Некорректный тип скидки")

    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        abort(400, description="Укажите значение скидки")
    if value <= 0:
        abort(400, description="Значение скидки должно быть больше нуля")
    if promo_type == "percent" and value > 100:
        abort(400, description="Скидка в процентах не может быть больше 100")

    promos = load_promos()
    if any(p.get("code") == code for p in promos):
        abort(400, description="Такой промокод уже существует")

    new_id = (max([p.get("id", 0) for p in promos], default=0) + 1)
    promo = {
        "id": new_id,
        "code": code,
        "type": promo_type,
        "value": value,
        "active": bool(data.get("active", True)),
        "max_uses": sanitize_positive_int(data.get("max_uses")),
        "used_count": 0,
        "min_total": sanitize_positive_int(data.get("min_total")),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    promos.append(promo)
    save_promos(promos)
    return jsonify(promo), 201


@app.route("/api/admin/promos/<int:pid>", methods=["PUT"])
def admin_update_promo(pid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    promos = load_promos()
    for p in promos:
        if p.get("id") == pid:
            if "code" in data:
                code = (data.get("code") or "").strip().upper()
                if not code:
                    abort(400, description="Укажите код промокода")
                if any(o.get("code") == code and o.get("id") != pid for o in promos):
                    abort(400, description="Такой промокод уже существует")
                p["code"] = code
            if "type" in data:
                promo_type = (data.get("type") or "").strip().lower()
                if promo_type not in ALLOWED_PROMO_TYPES:
                    abort(400, description="Некорректный тип скидки")
                p["type"] = promo_type
            if "value" in data:
                try:
                    value = float(data.get("value"))
                except (TypeError, ValueError):
                    abort(400, description="Укажите значение скидки")
                if value <= 0:
                    abort(400, description="Значение скидки должно быть больше нуля")
                if p.get("type") == "percent" and value > 100:
                    abort(400, description="Скидка в процентах не может быть больше 100")
                p["value"] = value
            if "active" in data:
                p["active"] = bool(data.get("active"))
            if "max_uses" in data:
                p["max_uses"] = sanitize_positive_int(data.get("max_uses"))
            if "min_total" in data:
                p["min_total"] = sanitize_positive_int(data.get("min_total"))
            save_promos(promos)
            return jsonify(p)
    abort(404, description="Промокод не найден")


@app.route("/api/admin/promos/<int:pid>", methods=["DELETE"])
def admin_delete_promo(pid):
    require_admin()
    promos = load_promos()
    new_promos = [p for p in promos if p.get("id") != pid]
    if len(new_promos) == len(promos):
        abort(404, description="Промокод не найден")
    save_promos(new_promos)
    return jsonify({"ok": True})


@app.route("/api/admin/orders", methods=["GET"])
def admin_get_orders():
    require_admin()
    orders = load_orders()
    orders.sort(key=lambda o: o.get("id", 0), reverse=True)
    return jsonify(orders)


@app.route("/api/admin/customer-orders", methods=["GET"])
def admin_customer_orders():
    """CRM: вся история заказов клиента по Telegram ID или @username.

    GET /api/admin/customer-orders?query=123456789
    GET /api/admin/customer-orders?query=@username
    Заголовок: X-Admin-Password.
    """
    require_admin()
    raw_query = (request.args.get("query") or "").strip()
    if not raw_query:
        abort(400, description="Укажите Telegram ID или @username")

    q_clean = raw_query.lstrip("@").strip()
    if not q_clean:
        abort(400, description="Укажите Telegram ID или @username")

    orders = load_orders()

    if not raw_query.startswith("@") and q_clean.lstrip("-").isdigit():
        search_type = "telegram_id"
        ids = {int(q_clean)}
    else:
        search_type = "username"
        uname = normalize_username(q_clean)
        # Находим ID по username, затем берём ВСЕ заказы этих ID —
        # так не теряются заказы, сделанные до смены username.
        ids = {
            order_telegram_id(o)
            for o in orders
            if order_telegram_id(o) and normalize_username(order_username(o)) == uname
        }

    def belongs(o):
        tid = order_telegram_id(o)
        if tid and tid in ids:
            return True
        if search_type == "username":
            return normalize_username(order_username(o)) == uname
        return False

    mine = [o for o in orders if belongs(o)]
    mine.sort(key=lambda o: o.get("id", 0), reverse=True)

    result_orders = []
    for o in mine:
        items = []
        for it in o.get("items") or []:
            try:
                qty = int(it.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            try:
                price = int(it.get("price") or 0)
            except (TypeError, ValueError):
                price = 0
            items.append({
                "id": it.get("id"),
                "name": it.get("name"),
                "size": it.get("size"),
                "qty": qty,
                "price": price,
                "sum": price * qty,
            })
        method = o.get("delivery_method")
        result_orders.append({
            "id": o.get("id"),
            "number": f"KOSHELEV-{o.get('id')}",
            "created_at": o.get("created_at"),
            "status": o.get("status"),
            "items": items,
            "items_count": sum(i["qty"] for i in items),
            "delivery_method": method,
            "delivery_label": DELIVERY_LABELS.get(method, "Не указан"),
            "subtotal": o.get("subtotal"),
            "discount": o.get("discount") or 0,
            "promo_code": o.get("promo_code"),
            "total": int(o.get("total") or 0),
            "name": o.get("name"),
            "phone": o.get("phone"),
            "comment": o.get("comment"),
        })

    active = [o for o in result_orders if o["status"] != "cancelled"]
    total_amount = sum(o["total"] for o in result_orders)
    active_amount = sum(o["total"] for o in active)
    dates = sorted(o["created_at"] for o in result_orders if o.get("created_at"))

    # Данные клиента: последние известные username / имя / телефон
    latest_username = next((order_username(o) for o in mine if order_username(o)), None)
    latest_id = next((order_telegram_id(o) for o in mine if order_telegram_id(o)), None)

    def uniq(values):
        seen, out = set(), []
        for v in values:
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    return jsonify({
        "query": raw_query,
        "search_type": search_type,
        "found": bool(mine),
        "customer": {
            "telegram_id": latest_id,
            "username": latest_username,
            "usernames": uniq(order_username(o) for o in mine),
            "names": uniq(o.get("name") for o in mine),
            "phones": uniq(o.get("phone") for o in mine),
        },
        "stats": {
            "orders_count": len(result_orders),
            "total_amount": total_amount,
            "cancelled_count": len(result_orders) - len(active),
            "active_orders_count": len(active),
            "total_amount_without_cancelled": active_amount,
            "average_order": int(round(active_amount / len(active))) if active else 0,
            "first_order_at": dates[0] if dates else None,
            "last_order_at": dates[-1] if dates else None,
        },
        "orders": result_orders,
    })


@app.route("/api/admin/orders/<int:oid>/status", methods=["PUT"])
def admin_update_order_status(oid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in {"new", "contacted", "done", "cancelled"}:
        abort(400, description="Некорректный статус")
    orders = load_orders()
    for o in orders:
        if o.get("id") == oid:
            o["status"] = status
            save_orders(orders)
            return jsonify(o)
    abort(404, description="Заказ не найден")


@app.route("/api/admin/orders/<int:oid>/message", methods=["POST"])
def admin_message_customer(oid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        abort(400, description="Введите текст сообщения")

    orders = load_orders()
    order = next((o for o in orders if o.get("id") == oid), None)
    if not order:
        abort(404, description="Заказ не найден")

    chat_id = order.get("customer_chat_id")
    if not chat_id:
        abort(400, description="У этого заказа нет чата с покупателем в Telegram")

    try:
        bot.send_message(
            chat_id,
            f"💬 Сообщение от KOSHELEV по заказу KOSHELEV-{oid}:\n\n{text}"
        )
    except Exception as e:
        abort(400, description=f"Не удалось отправить сообщение: {e}")

    order.setdefault("messages", []).append({
        "text": text,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    if order.get("status") == "new":
        order["status"] = "contacted"
    save_orders(orders)
    return jsonify(order)


@app.route("/api/admin/questions", methods=["GET"])
def admin_get_questions():
    require_admin()
    questions = load_questions()
    questions.sort(key=lambda q: q.get("id", 0), reverse=True)
    return jsonify(questions)


@app.route("/api/admin/questions/<int:qid>/message", methods=["POST"])
def admin_message_question(qid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        abort(400, description="Введите текст ответа")

    questions = load_questions()
    q = next((x for x in questions if x.get("id") == qid), None)
    if not q:
        abort(404, description="Вопрос не найден")

    chat_id = q.get("customer_chat_id")
    if not chat_id:
        abort(400, description="У этого вопроса нет чата с покупателем в Telegram")

    try:
        bot.send_message(
            chat_id,
            f"💬 Ответ от KOSHELEV по товару «{q.get('product_name','')}»:\n\n{text}"
        )
    except Exception as e:
        abort(400, description=f"Не удалось отправить ответ: {e}")

    q.setdefault("messages", []).append({
        "text": text,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    q["status"] = "answered"
    save_questions(questions)
    return jsonify(q)


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "OK", 200


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
def handle_api_errors(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(err, "description", str(err))}), err.code
    return err


def setup_webhook():
    if not BASE_URL:
        log.warning("WEBHOOK_URL / RENDER_EXTERNAL_URL не заданы — вебхук не установлен.")
        return
    url = f"{BASE_URL}/webhook/{BOT_TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=url)
    log.info("Webhook установлен: %s", url)


# Раньше в поле «категория» лежали бренды (Elf Bar, Geek Vape ...). Один раз
# переносим их в новое поле «бренд», а категорию делаем общей — «Девайсы».
KNOWN_BRAND_CATEGORIES = {"Elf Bar", "Geek Vape", "Vaporesso", "Smoant", "Voopoo", "Lost Vape", "Rincoe"}
DEFAULT_DEVICE_CATEGORY = "Девайсы"


def migrate_brand_field():
    items = load_products()
    changed = False
    for p in items:
        if "brand" in p:
            continue
        cat = (p.get("category") or "").strip()
        if cat in KNOWN_BRAND_CATEGORIES:
            p["brand"] = cat
            p["category"] = DEFAULT_DEVICE_CATEGORY
        else:
            p["brand"] = ""
        changed = True
    if changed:
        save_products(items)
        log.info("Товары: добавлено поле brand (миграция)")


def migrate_order_identity():
    """Старым заказам добавляем поля telegram_id / username (копия прежних
    customer_chat_id / customer_username), чтобы CRM-поиск видел всю историю."""
    orders = load_orders()
    changed = False
    for o in orders:
        if "telegram_id" in o and "username" in o:
            continue
        o["telegram_id"] = o.get("customer_chat_id")
        o["username"] = clean_username(o.get("customer_username"))
        o.setdefault("delivery_method", None)
        changed = True
    if changed:
        save_orders(orders)
        log.info("Заказы: добавлены поля telegram_id/username (миграция)")


if USE_DB:
    init_db()
    log.info("Хранилище: Neon (Postgres)")
else:
    log.warning("DATABASE_URL не задан — данные хранятся в JSON-файлах и пропадут при редеплое на Render.")

migrate_brand_field()
migrate_order_identity()

setup_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
