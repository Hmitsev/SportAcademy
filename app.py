import os
import uuid
import hashlib
import base64
import json
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from pathlib import Path

import psycopg2
from psycopg2 import IntegrityError
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
import streamlit as st
import time
import secrets
from decimal import Decimal

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
PROFILE_DIR = UPLOAD_DIR / "profiles"
DOC_DIR = UPLOAD_DIR / "documents"
CLIENT_BG = APP_DIR / "Designer (25).png"
ADMIN_BG = APP_DIR / "Designer (26).png"
AUTODOC_LOGO = APP_DIR / "auto.dog.jpg"
AUTODOC_PARTS_URL = "https://www.autodoc.bg/?_gl=1*eouckk*_up*MQ..*_gs*MQ..&gclid=Cj0KCQiAh9vXBhDKARIsAMooYhfPS_LiTItsQeNDS0sJyvrEc_2SxL3II1WIqwrIbK1zcTlv00Gl1fMaAiDZEALw_wcB&gbraid=0AAAAAoWFwrdC3XBhKR6uCiRvUSaypUd-l"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

for folder in (DATA_DIR, UPLOAD_DIR, PROFILE_DIR, DOC_DIR):
    folder.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Сервиз за самообслужване", page_icon="🔧", layout="wide")

if not DATABASE_URL:
    st.error("Липсва DATABASE_URL в Render Environment.")
    st.stop()

@st.cache_resource
def get_pool():
    return ThreadedConnectionPool(
        1, 8, dsn=DATABASE_URL, sslmode="require", connect_timeout=10,
        keepalives=1, keepalives_idle=30, keepalives_interval=10,
        keepalives_count=5, application_name="garage_manager",
    )

@contextmanager
def db():
    db_pool = get_pool()
    conn = None
    broken = False
    try:
        conn = db_pool.getconn()
        if conn.closed:
            broken = True
            raise psycopg2.InterfaceError("Получена е затворена PostgreSQL връзка.")
        yield conn
        if not conn.closed:
            conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        if conn is not None and not conn.closed:
            try: conn.rollback()
            except Exception: pass
        raise
    except Exception:
        if conn is not None and not conn.closed:
            try: conn.rollback()
            except Exception: broken = True
        raise
    finally:
        if conn is not None:
            try: db_pool.putconn(conn, close=broken or bool(conn.closed))
            except Exception:
                try: conn.close()
                except Exception: pass

def fetchone(query, params=()):
    for attempt in range(2):
        try:
            with db() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(query, params); return cur.fetchone()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if attempt == 1: raise
            st.cache_resource.clear()

def fetchall(query, params=()):
    for attempt in range(2):
        try:
            with db() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(query, params); return cur.fetchall()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if attempt == 1: raise
            st.cache_resource.clear()

def execute(query, params=(), returning=False):
    for attempt in range(2):
        try:
            with db() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(query, params); return cur.fetchone() if returning else None
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if attempt == 1: raise
            st.cache_resource.clear()

def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
@st.cache_resource
def init_db():
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE, email TEXT, password_hash TEXT NOT NULL,
            profile_photo TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS reservations (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reservation_date DATE NOT NULL, start_time TIME NOT NULL, end_time TIME NOT NULL,
            status TEXT NOT NULL DEFAULT 'Очаква потвърждение', wants_parts BOOLEAN NOT NULL DEFAULT FALSE,
            parts_comment TEXT, vin TEXT, registration_doc TEXT, parts_status TEXT,
            admin_note TEXT, door_pin TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(reservation_date, start_time)
        )""",
        """CREATE TABLE IF NOT EXISTS technical_issues (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category TEXT NOT NULL, description TEXT NOT NULL, photo TEXT,
            status TEXT NOT NULL DEFAULT 'Ново', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS parts_offer TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS parts_offer_file TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS parts_offer_data BYTEA",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS parts_offer_name TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS parts_offer_mime TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS customer_parts_decision TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS customer_parts_comment TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS customer_parts_responded_at TIMESTAMPTZ",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS final_amount NUMERIC(12,2)",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'unpaid'",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS payment_method TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS test_payment BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS offer_items_json TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS selected_items_json TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS selected_items_total NUMERIC(12,2)",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS offer_sent_at TIMESTAMPTZ",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS customer_selection_at TIMESTAMPTZ",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS admin_selection_confirmed_at TIMESTAMPTZ",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS payment_due_type TEXT",
        """CREATE TABLE IF NOT EXISTS login_sessions(
            id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
            last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS idx_garage_sessions_token ON login_sessions(token_hash,last_seen)",
        """CREATE TABLE IF NOT EXISTS expenses(
            id BIGSERIAL PRIMARY KEY, expense_date DATE NOT NULL DEFAULT CURRENT_DATE,
            category TEXT NOT NULL, amount NUMERIC(12,2) NOT NULL CHECK(amount > 0),
            description TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS idx_reservation_day ON reservations(reservation_date, start_time)",
        "CREATE INDEX IF NOT EXISTS idx_reservation_user ON reservations(user_id)",
        """CREATE TABLE IF NOT EXISTS notifications(
            id BIGSERIAL PRIMARY KEY,
            recipient_role TEXT NOT NULL,
            user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            target_page TEXT,
            reservation_id BIGINT REFERENCES reservations(id) ON DELETE CASCADE,
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            read_at TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS idx_notifications_client ON notifications(user_id,is_read,created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_admin ON notifications(recipient_role,is_read,created_at DESC)",
    ]
    with db() as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            defaults = {
                "youtube_1": "", "youtube_2": "", "youtube_3": "",
                "help_oil": "", "help_filters": "", "help_brakes": "",
                "help_interior": "", "help_exterior": "", "help_spark_plugs": "",
                "autodoc_channel": "", "autodoc_parts": AUTODOC_PARTS_URL,
                "slot_minutes": "60", "open_hour": "09:00", "close_hour": "18:00", "reservation_fee": "20.00",
                "admin_pin_hash": hash_value(os.getenv("ADMIN_PIN", "2468")),
            }
            for key, value in defaults.items():
                cur.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING", (key, value))

def setting(key, fallback=""):
    row = fetchone("SELECT value FROM settings WHERE key=%s", (key,))
    return row["value"] if row else fallback

def set_setting(key, value):
    execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, str(value)))

def save_upload(uploaded, folder, prefix):
    if not uploaded:
        return None
    suffix = Path(uploaded.name).suffix.lower() or ".jpg"
    target = folder / f"{prefix}_{uuid.uuid4().hex}{suffix}"
    target.write_bytes(uploaded.getbuffer())
    return str(target)
@st.cache_data
def get_bg_css(background_file):

    if not background_file.exists():
        return ""

    encoded = base64.b64encode(
        background_file.read_bytes()
    ).decode()

    return (
        "background-image:"
        "linear-gradient(rgba(15,0,0,.86),rgba(15,0,0,.90)),"
        f"url(data:image/png;base64,{encoded});"
    )


def apply_style():
    background_file = ADMIN_BG if st.session_state.get("role") == "admin" else CLIENT_BG
    bg_css = get_bg_css(background_file)
    st.markdown(f"""
    <style>
    .stApp {{{bg_css} background-size:cover;background-position:center;background-attachment:fixed;}}
    .block-container {{max-width:1250px;padding-top:1.2rem;}}
    h1,h2,h3,p,label,.stMarkdown {{color:#fff;}}
    [data-testid="stSidebar"] {{background:#170707;}}
    div[data-testid="stMetric"] {{background:rgba(255,255,255,.08);border:1px solid #7c2020;border-radius:14px;padding:12px;}}
    .hero {{padding:22px;border-radius:18px;background:linear-gradient(120deg,rgba(150,0,0,.9),rgba(20,0,0,.82));border:1px solid #e63d3d;margin-bottom:18px;}}
    .notice {{padding:12px 15px;border-left:5px solid #ffcc00;background:rgba(75,55,0,.75);border-radius:8px;color:white;}}
    .slot-free {{padding:11px;text-align:center;border-radius:10px;background:#14532d;border:1px solid #22c55e;color:white;font-weight:700;margin-bottom:6px;}}
    .slot-busy {{padding:11px;text-align:center;border-radius:10px;background:#7f1d1d;border:1px solid #ef4444;color:white;font-weight:700;margin-bottom:6px;}}
    .stButton>button {{border-radius:10px;font-weight:700;}}

    /* Client navigation buttons */
    div[data-testid="stHorizontalBlock"] .stButton > button {{
        width:100%; min-height:54px; border-radius:14px;
        border:1px solid #ff4242;
        background:linear-gradient(135deg,#720000,#d01010 55%,#8b0000);
        color:white; font-weight:800;
        box-shadow:0 8px 20px rgba(180,0,0,.28);
    }}
    div[data-testid="stHorizontalBlock"] .stButton > button:hover {{
        border-color:#ff8585;
        background:linear-gradient(135deg,#970000,#ef2424 55%,#a00000);
        color:white; transform:translateY(-1px);
    }}
    div[data-testid="stHorizontalBlock"] .stButton > button[kind="primary"] {{
        border-color:#43e47c;
        background:linear-gradient(135deg,#086b30,#16a34a 55%,#08732f);
        color:white;
        box-shadow:0 8px 22px rgba(22,163,74,.38);
    }}
    </style>""", unsafe_allow_html=True)

def user_by_phone(phone):
    return fetchone("SELECT * FROM users WHERE phone=%s", (phone.strip(),))

def notify(recipient_role, title, message, user_id=None, target_page=None, reservation_id=None):
    execute("""INSERT INTO notifications(recipient_role,user_id,title,message,target_page,reservation_id)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (recipient_role,user_id,title,message,target_page,reservation_id))

def notification_center(role, user_id=None):
    if role == "client":
        rows=fetchall("""SELECT * FROM notifications WHERE recipient_role='client' AND user_id=%s
                          ORDER BY is_read ASC,created_at DESC LIMIT 30""",(user_id,))
    else:
        rows=fetchall("""SELECT * FROM notifications WHERE recipient_role='admin'
                          ORDER BY is_read ASC,created_at DESC LIMIT 30""")
    unread=[r for r in rows if not r["is_read"]]
    label=f"🔔 {len(unread)}" if unread else "🔔"
    with st.popover(label, use_container_width=True):
        st.markdown("### Известия")
        if not rows:
            st.caption("Няма известия.")
        for n in rows:
            icon="🔔" if not n["is_read"] else "✓"
            st.markdown(f"**{icon} {n['title']}**")
            st.write(n["message"])
            st.caption(n["created_at"].strftime("%d.%m.%Y %H:%M"))
            if not n["is_read"]:
                if st.button("Прочетено",key=f"read_notification_{role}_{n['id']}",use_container_width=True):
                    execute("UPDATE notifications SET is_read=TRUE,read_at=NOW() WHERE id=%s",(n["id"],))
                    if role=="client" and n.get("target_page"):
                        st.session_state.client_page=n["target_page"]
                    st.rerun()
            st.markdown("---")

def expire_stale_parts_requests():
    execute("""UPDATE reservations
               SET status='Анулирана', parts_status='Изтекла заявка'
               WHERE wants_parts=TRUE
                 AND payment_status<>'paid'
                 AND expires_at IS NOT NULL
                 AND expires_at < NOW()
                 AND parts_status IN ('Очаква преглед','Оферта изпратена','Избор от клиента','Изборът е потвърден')""")

def reservation_fee():
    try:
        return max(0.0, float(setting("reservation_fee", "20.00")))
    except Exception:
        return 20.0

def day_slots(selected_date):
    expire_stale_parts_requests()
    open_dt = datetime.combine(
        selected_date,
        datetime.strptime(setting("open_hour", "09:00"), "%H:%M").time(),
    )
    close_dt = datetime.combine(
        selected_date,
        datetime.strptime(setting("close_hour", "18:00"), "%H:%M").time(),
    )
    minutes = int(setting("slot_minutes", "60"))
    rows = fetchall(
        """SELECT start_time,end_time FROM reservations
           WHERE reservation_date=%s
             AND status NOT IN ('Отказана','Анулирана')""",
        (selected_date,),
    )

    reservations = [
        (
            datetime.combine(selected_date, row["start_time"]),
            datetime.combine(selected_date, row["end_time"]),
        )
        for row in rows
    ]

    slots = []
    current = open_dt
    while current + timedelta(minutes=minutes) <= close_dt:
        slot_end = current + timedelta(minutes=minutes)
        is_busy = any(
            current < reserved_end and slot_end > reserved_start
            for reserved_start, reserved_end in reservations
        )
        slots.append({"time": current.strftime("%H:%M"), "is_busy": is_busy})
        current = slot_end
    return slots

def session_hash(token):
    return hash_value(token)

def create_login_session(role, user_id=None):
    token = secrets.token_urlsafe(32)
    execute("INSERT INTO login_sessions(user_id,role,token_hash,last_seen) VALUES(%s,%s,%s,NOW())", (user_id,role,session_hash(token)))
    st.session_state.role = role
    if user_id: st.session_state.user_id = user_id
    st.session_state.login_token = token
    st.query_params["session"] = token

def restore_login_session():
    token = st.session_state.get("login_token") or st.query_params.get("session")
    if not token: return False
    row = fetchone("SELECT * FROM login_sessions WHERE token_hash=%s AND last_seen >= NOW()-INTERVAL '7 days'", (session_hash(token),))
    if not row:
        st.query_params.pop("session",None); return False
    execute("UPDATE login_sessions SET last_seen=NOW() WHERE id=%s",(row["id"],))
    st.session_state.login_token=token; st.session_state.role=row["role"]
    if row.get("user_id"): st.session_state.user_id=row["user_id"]
    return True

def keep_session_in_url():
    token=st.session_state.get("login_token") or st.query_params.get("session")
    if token and st.query_params.get("session") != token:
        st.query_params["session"] = token

def logout():
    token=st.session_state.get("login_token") or st.query_params.get("session")
    if token: execute("DELETE FROM login_sessions WHERE token_hash=%s",(session_hash(token),))
    st.session_state.clear(); st.query_params.clear(); st.rerun()

def register_view():
    st.subheader("Бърза регистрация")
    with st.form("register_form"):
        c1, c2 = st.columns(2)
        first = c1.text_input("Име *"); last = c2.text_input("Фамилия *")
        phone = c1.text_input("Телефон *"); email = c2.text_input("Имейл")
        password = c1.text_input("Парола *", type="password")
        password2 = c2.text_input("Повтори паролата *", type="password")
        submitted = st.form_submit_button("Създай профил", use_container_width=True)
    if submitted:
        if not all([first.strip(), last.strip(), phone.strip(), password]): st.error("Попълни всички задължителни полета.")
        elif password != password2: st.error("Паролите не съвпадат.")
        elif user_by_phone(phone): st.error("Вече има профил с този телефон.")
        else:
            row = execute("""
            INSERT INTO users(
                first_name,last_name,phone,email,
                password_hash,profile_photo
            )
            VALUES(%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                first.strip(),
                last.strip(),
                phone.strip(),
                email.strip(),
                hash_value(password),
                None
            ), True)
            create_login_session("client",row["id"]); st.rerun()

def login_view():

    st.subheader("Вход")

    tab_client, tab_admin = st.tabs(
        ["Клиент", "Администратор"]
    )

    with tab_client:

        with st.form("client_login_form"):

            phone = st.text_input(
                "Телефон",
                key="login_phone"
            )

            password = st.text_input(
                "Парола",
                type="password",
                key="login_pass"
            )

            login_pressed = st.form_submit_button(
                "Вход като клиент",
                use_container_width=True,
            )

        if login_pressed:

            user = user_by_phone(phone)

            if user and user["password_hash"] == hash_value(password):

                create_login_session("client",user["id"])
                st.rerun()

            st.error("Невалиден телефон или парола.")

    with tab_admin:

        with st.form("admin_login_form"):

            pin = st.text_input(
                "Администраторски PIN",
                type="password",
            )

            admin_login_pressed = st.form_submit_button(
                "Вход в админ панела",
                use_container_width=True,
            )

        if admin_login_pressed:

            if hash_value(pin) == setting("admin_pin_hash"):

                create_login_session("admin")
                st.rerun()

            st.error("Невалиден администраторски PIN.")

def video_section():
    st.markdown(
        """
        <h2 style="
            color:#ff2f2f;
            font-weight:800;
            margin-bottom:20px;
        ">
            Запознаване със сервиза
        </h2>
        """,
        unsafe_allow_html=True,
    )

    videos = [
        {
            "title": "Как работи Приложението",
            "setting_key": "youtube_1",
            "description": "Основни правила за работа с приложението.",
    
        },
        {
            "title": "Как да използвам Сервиза",
            "setting_key": "youtube_2",
            "description": "Основни правила за работа в сервиза.",
        },
        {
            "title": "Работа с подемник и Правила за Безопастност",
            "setting_key": "youtube_3",
            "description": "Инструкции за правилна и безопасна работа с подемника.",
        },
    ]

    columns = st.columns(3)

    for column, video in zip(columns, videos):
        with column:
            st.markdown(f"### {video['title']}")

            url = setting(video["setting_key"], "").strip()

            if url:
                st.video(url)
            else:
                st.info("Видеото ще бъде добавено от администратора.")
                st.caption(video["description"])

PART_BRANDS_BRAKES = ["ABE", "ATE", "BLUE PRINT", "BOSCH", "BRECK", "BREMBO", "DBA", "DELPHI", "FEBI", "FERODO", "FTE", "HERTH+BUSS JAKOPARTS", "JURID", "LPR", "MEYLE", "TEXTAR", "TRW", "VALEO", "ОЕ (оригинален продукт)", "Друго"]
PART_BRANDS_FILTERS = ["BLUE PRINT", "BOSCH", "FEBI", "FILTRON", "HANS PRIES", "HENGST", "HERTH+BUSS JAKOPARTS", "KNECHT", "MAGNETI MARELLI", "MANN-FILTER", "PURFLUX", "PURRO", "SOFIMA", "VAICO", "ОЕ (оригинален продукт)", "Друго"]
OIL_BRANDS = ["CASTROL", "CHEMPIOIL", "CHEMPIOIL MOTO", "COMMA", "ELF", "FEBI", "FUCHS OIL", "LIQUI MOLY", "MOBIL", "MOTUL", "RAVENOL", "REVLINE", "REVLINE MOTO", "VALVOLINE", "ZF", "ОЕ (оригинален продукт)", "Друго"]
OIL_SPECS = ["0W12", "0W16", "0W20", "0W30", "0W40", "0W8", "10W", "10W30", "10W40", "10W50", "10W60", "15W30", "15W40", "15W50", "15W60", "20W20", "20W40", "20W50", "20W60", "25W40", "5W16", "5W20", "5W30", "5W40", "5W50", "75W80", "75W90", "Друга / по спецификация на автомобила"]
BATTERY_BRANDS = ["4 RIDE", "4MAX", "BOSCH", "CARTECHNIC", "EXIDE", "FURYA", "VARTA", "YUASA", "ОЕ (оригинален продукт)", "Друго"]
FLUID_TYPES = ["Спирачна течност", "Антифриз", "Течност за чистачки", "AdBlue", "Добавка", "Почистващ агент", "Зимна химия", "Проникващ спрей", "Каша за ръце", "Обезмаслител", "Лепило", "Друга химия"]


def quantity_input(key):
    return st.number_input("Брой / количество", min_value=1, max_value=50, value=1, step=1, key=key)


def parts_order_builder():
    st.markdown("### Поръчка на части")
    st.caption("Отворете желаната категория, попълнете вариантите и натиснете „Добави в поръчката“.")
    selected = []

    def add_line(title, details, qty):
        selected.append(f"{title}: {details}; количество: {int(qty)}")
        st.success(f"Добавено: {title}")

    for title, axle_key in [("Дискове", "discs"), ("Накладки", "pads")]:
        with st.expander(title):
            axle = st.selectbox("Позиция", ["Предни", "Задни", "Предни и задни"], key=f"{axle_key}_axle")
            brands = st.multiselect("Желани марки за оферта", PART_BRANDS_BRAKES, key=f"{axle_key}_brands")
            comment = st.text_input("Коментар", key=f"{axle_key}_comment")
            qty = quantity_input(f"{axle_key}_qty")
            if st.checkbox("Добави в поръчката", key=f"{axle_key}_add"):
                add_line(title, f"{axle}; марки: {', '.join(brands) or 'без предпочитание'}; {comment or 'без коментар'}", qty)

    for title, key in [("Маслен филтър", "oil_filter"), ("Горивен филтър", "fuel_filter"), ("Въздушен филтър", "air_filter"), ("Кабинен филтър", "cabin_filter")]:
        with st.expander(title):
            brands = st.multiselect("Желани марки за оферта", PART_BRANDS_FILTERS, key=f"{key}_brands")
            comment = st.text_input("Коментар", key=f"{key}_comment")
            qty = quantity_input(f"{key}_qty")
            if st.checkbox("Добави в поръчката", key=f"{key}_add"):
                add_line(title, f"марки: {', '.join(brands) or 'без предпочитание'}; {comment or 'без коментар'}", qty)

    with st.expander("Масло"):
        brands = st.multiselect("Желани марки за оферта", OIL_BRANDS, key="oil_brands")
        spec = st.selectbox("Вискозитет / спецификация", OIL_SPECS, key="oil_spec")
        pack = st.selectbox("Предпочитана разфасовка", ["1 л", "4 л", "5 л", "20 л", "Друга"], key="oil_pack")
        litres = st.number_input("Необходимо количество в литри", min_value=0.5, max_value=100.0, value=5.0, step=0.5, key="oil_litres")
        comment = st.text_area("Коментар за маслото", key="oil_comment")
        qty = quantity_input("oil_qty")
        if st.checkbox("Добави в поръчката", key="oil_add"):
            add_line("Масло", f"марки: {', '.join(brands) or 'без предпочитание'}; {spec}; разфасовка {pack}; общо {litres:g} л; {comment or 'без коментар'}", qty)

    with st.expander("Течност / автохимия"):
        fluid = st.selectbox("Вид течност", FLUID_TYPES, key="fluid_type")
        pack = st.selectbox("Разфасовка", ["до 2 л", "до 5 л"], key="fluid_pack")
        oe = st.checkbox("ОЕ (оригинален продукт)", key="fluid_oe")
        comment = st.text_input("Коментар", key="fluid_comment")
        qty = quantity_input("fluid_qty")
        if st.checkbox("Добави в поръчката", key="fluid_add"):
            add_line("Течност", f"{fluid}; {pack}; {'ОЕ' if oe else 'aftermarket'}; {comment or 'без коментар'}", qty)

    with st.expander("Акумулатор"):
        brands = st.multiselect("Желани марки за оферта", BATTERY_BRANDS, key="battery_brands")
        capacity = st.number_input("Капацитет (Ah)", min_value=20.0, max_value=250.0, value=62.0, step=1.0, key="battery_capacity")
        comment = st.text_input("Допълнителни данни / размери / стартов ток", key="battery_comment")
        qty = quantity_input("battery_qty")
        if st.checkbox("Добави в поръчката", key="battery_add"):
            add_line("Акумулатор", f"марки: {', '.join(brands) or 'без предпочитание'}; {capacity:g} Ah; {comment or 'без коментар'}", qty)

    with st.expander("Гуми"):
        brand = st.text_input("Желана марка (може няколко, разделени със запетая)", key="tyre_brand")
        size = st.text_input("Размер", placeholder="Например 225/45 R17", key="tyre_size")
        oe = st.checkbox("ОЕ / оригинално вграждане", key="tyre_oe")
        qty = quantity_input("tyre_qty")
        if st.checkbox("Добави в поръчката", key="tyre_add"):
            add_line("Гуми", f"марки: {brand or 'без предпочитание'}; размер: {size or 'неуточнен'}; {'ОЕ' if oe else 'aftermarket'}", qty)

    with st.expander("Друго"):
        details = st.text_area("Опишете частта", key="other_part_details")
        oe = st.checkbox("ОЕ (оригинален продукт)", key="other_part_oe")
        qty = quantity_input("other_part_qty")
        if st.checkbox("Добави в поръчката", key="other_part_add"):
            add_line("Друго", f"{details or 'без описание'}; {'ОЕ' if oe else 'aftermarket'}", qty)
    return selected


def show_autodoc_logo(width=260):
    if AUTODOC_LOGO.exists():
        st.image(str(AUTODOC_LOGO), width=width)
    else:
        st.markdown("### AUTODOC")

def autodoc_help():
    show_autodoc_logo(750)
    st.caption("Изберете категория")
    categories = [
        ("Смяна на масло", "help_oil"), ("Смяна на филтри", "help_filters"),
        ("Смяна на дискове и накладки", "help_brakes"), ("Ремонт по интериор", "help_interior"),
        ("Ремонт по екстериор / купе", "help_exterior"), ("Смяна на свещи", "help_spark_plugs"),
    ]
    choice = st.radio("Категория", [x[0] for x in categories], horizontal=True)
    key = dict(categories)[choice]
    url = setting(key, "").strip()
    if url: st.video(url)
    else: st.info("За тази категория все още няма добавено видео.")
    st.markdown("### Не намирате подходящата секция?")
    st.write("Посетете канала на AUTODOC и потърсете конкретен ремонт за вашия автомобил.")
    channel = setting("autodoc_channel", "").strip()
    parts = setting("autodoc_parts", "").strip() or AUTODOC_PARTS_URL
    c1, c2 = st.columns(2)
    if channel: c1.link_button("▶️ Отвори канала на AUTODOC", channel, use_container_width=True)
    else: c1.info("Линкът към канала ще бъде добавен от администратора.")
    if parts: c2.link_button("🛒 Намери части в AUTODOC", parts, use_container_width=True)
    else: c2.info("Линкът към сайта за части ще бъде добавен от администратора.")

def new_reservation(user_id):
    st.subheader("Визуален календар за резервации")
    selected = st.date_input("Избери дата", min_value=date.today(), value=date.today() + timedelta(days=1))
    slots = day_slots(selected)
    slot_minutes = int(setting("slot_minutes", "60"))
    if st.session_state.get("slot_day") != selected.isoformat():
        st.session_state.slot_day = selected.isoformat()
        st.session_state.slot_start_index = None
        st.session_state.slot_end_index = None
        st.session_state.pop("pending_slot", None)
    a = st.session_state.get("slot_start_index")
    b = st.session_state.get("slot_end_index")
    selected_indexes = set(range(a, b + 1)) if a is not None and b is not None else set()
    dynamic_css = ["<style>"]
    for i, slot in enumerate(slots):
        if not slot["is_busy"]:
            dynamic_css.append(f'.st-key-res_slot_{i} button {{background:#14532d !important;border-color:#22c55e !important;color:white !important;min-height:78px !important;}}')
    for i in selected_indexes:
        dynamic_css.append(f'.st-key-res_slot_{i} button {{background:#d97706 !important;border-color:#fbbf24 !important;color:white !important;box-shadow:0 0 18px rgba(245,158,11,.55) !important;}}')
    dynamic_css.append("</style>")
    st.markdown("".join(dynamic_css), unsafe_allow_html=True)
    st.caption("🟢 Свободен • 🟠 Избран период • 🔴 Зает. Натиснете първия свободен час, после последния час от периода.")
    for row_start in range(0, len(slots), 4):
        for i, (col, slot) in enumerate(zip(st.columns(4), slots[row_start:row_start + 4]), start=row_start):
            start_time = slot["time"]
            end_time = (datetime.strptime(start_time, "%H:%M") + timedelta(minutes=slot_minutes)).strftime("%H:%M")
            if slot["is_busy"]:
                col.markdown(f'<div class="slot-busy">{start_time}–{end_time}<br>ЗАЕТ</div>', unsafe_allow_html=True)
            else:
                label = f"{start_time}–{end_time}\n{'ИЗБРАН' if i in selected_indexes else 'СВОБОДЕН'}"
                if col.button(label, key=f"res_slot_{i}", use_container_width=True):
                    if a is None or (a is not None and b is not None and a != b):
                        st.session_state.slot_start_index = i
                        st.session_state.slot_end_index = i
                    else:
                        lo, hi = sorted((a, i))
                        if any(slots[x]["is_busy"] for x in range(lo, hi + 1)):
                            st.error("Периодът преминава през зает час. Изберете друг краен час.")
                        else:
                            st.session_state.slot_start_index = lo
                            st.session_state.slot_end_index = hi
                    st.session_state.pop("pending_slot", None)
                    st.rerun()
    a = st.session_state.get("slot_start_index")
    b = st.session_state.get("slot_end_index")
    if a is None or b is None:
        st.info("Изберете свободен час. За по-дълга резервация натиснете и последния желан часови блок.")
        return
    start_value = slots[a]["time"]
    end_dt = datetime.combine(selected, datetime.strptime(slots[b]["time"], "%H:%M").time()) + timedelta(minutes=slot_minutes)
    end_value = end_dt.strftime("%H:%M")
    duration_slots = b - a + 1
    st.success(f"Избран период: {selected.isoformat()} от {start_value} до {end_value} ({duration_slots * slot_minutes // 60:g} ч.)")
    if st.button("Продължи към потвърждение", type="primary", use_container_width=True):
        st.session_state.pending_slot = (selected.isoformat(), start_value, end_value, duration_slots)
    if "pending_slot" not in st.session_state: return
    day_value, pending_start, pending_end, pending_duration = st.session_state.pending_slot
    st.markdown('<div class="notice">Заявка за части, които да ви очакват в сервиза, се подава минимум 1 ден предварително.</div>', unsafe_allow_html=True)
    st.markdown("### Желаете ли да закупите части, които да ви очакват в сервиза?")
    if "parts_purchase_choice" not in st.session_state:
        st.session_state.parts_purchase_choice = "reservation_only"
    choice_cols = st.columns(3)
    with choice_cols[0]:
        if st.button("Само резервация", key="choice_reservation_only", use_container_width=True,
                     type="primary" if st.session_state.parts_purchase_choice == "reservation_only" else "secondary"):
            st.session_state.parts_purchase_choice = "reservation_only"
            st.session_state.show_direct_payment = True
            st.rerun()
    with choice_cols[1]:
        if st.button("Искам да закупя от AUTODOC", key="choice_autodoc", use_container_width=True,
                     type="primary" if st.session_state.parts_purchase_choice == "autodoc" else "secondary"):
            st.session_state.parts_purchase_choice = "autodoc"; st.rerun()
    with choice_cols[2]:
        if st.button("Желая части да ме очакват в сервиза", key="choice_parts_waiting", use_container_width=True,
                     type="primary" if st.session_state.parts_purchase_choice == "parts_waiting" else "secondary"):
            st.session_state.parts_purchase_choice = "parts_waiting"; st.rerun()
    wants_parts_choice = st.session_state.parts_purchase_choice == "parts_waiting"
    autodoc_choice = st.session_state.parts_purchase_choice == "autodoc"
    if wants_parts_choice:
        st.success("Избрано: заявка за части към сервиза. Часът се пази временно и очаква обратна връзка от администрацията. На този етап не се взима плащане.")
    elif autodoc_choice:
        st.info("Тази опция е само информативна и препраща към сайта на AUTODOC. Тук не се създава и не се запазва резервация.")
        st.warning("AUTODOC изисква регистрация. Преди да запазите отделно час в сервиза, проверете кога ще пристигнат закупените части.")
        read_autodoc = st.checkbox("Прочетох информацията и разбирам, че трябва да знам датата на доставка.", key="autodoc_terms_read")
        autodoc_url = setting("autodoc_parts", "").strip() or AUTODOC_PARTS_URL
        if read_autodoc:
            st.link_button("Отиди директно в AUTODOC", autodoc_url, use_container_width=True)
        return
    else:
        st.info(f"Избрано: само резервация. След изпращане ще платите тестово таксата за часа: {reservation_fee():.2f} €.")
    parts_comment = ""; vin = ""; doc_path = None; valid_parts = True
    if wants_parts_choice:
        reservation_dt = datetime.combine(date.fromisoformat(day_value), datetime.strptime(pending_start, "%H:%M").time())
        if reservation_dt < datetime.now() + timedelta(days=1):
            st.error("За заявка с части изберете период поне 1 ден напред."); valid_parts = False
        parts_selected = parts_order_builder()
        parts_comment = "\n".join(parts_selected)
        vin = st.text_input("Номер на рама (VIN)", max_chars=17, help="Въведете 17-символен VIN или приложете снимка на талона.").upper().strip()
        add_doc = st.checkbox("📷 Добави снимка на талона")
        if add_doc:
            source = st.radio("Как желаете да добавите талона?", ["Снимай с камера", "Качи готов файл"], horizontal=True, key="registration_document_source")
            document_file = st.camera_input("Снимай талона", key="registration_doc_camera") if source == "Снимай с камера" else st.file_uploader("Качи снимка или PDF", type=["jpg","jpeg","png","pdf"], key="registration_doc_upload")
            if document_file: doc_path = save_upload(document_file, DOC_DIR, "talon"); st.success("Талонът е приложен успешно.")
    booking_fee = reservation_fee() * max(1, pending_duration)
    direct_payment = (not wants_parts_choice and not autodoc_choice and st.session_state.get("show_direct_payment"))
    if direct_payment:
        st.markdown(f"### Сума за плащане: {booking_fee:.2f} €")
        st.caption(f"{pending_duration} час(а) × {reservation_fee():.2f} €")
        st.warning("Тестов режим: плащането ще бъде отчетено като получено, без реална картова транзакция.")
    final_button_label = "💳 Потвърди часа и плати с карта" if direct_payment else "Потвърди и изпрати заявката"
    if st.button(final_button_label, use_container_width=True, disabled=not valid_parts, type="primary" if direct_payment else "secondary"):
        wants_parts = wants_parts_choice
        if wants_parts and not parts_comment.strip(): st.error("Добавете поне един продукт в поръчката."); return
        if wants_parts and len(vin) != 17 and not doc_path: st.error("Въведете 17-символен VIN или приложете талона."); return
        reservation_date = date.fromisoformat(day_value)
        start_time_db = datetime.strptime(pending_start, "%H:%M").time(); end_time_db = datetime.strptime(pending_end, "%H:%M").time()
        overlap = fetchone("""SELECT id FROM reservations WHERE reservation_date=%s AND status NOT IN ('Отказана','Анулирана') AND start_time < %s AND end_time > %s LIMIT 1""", (reservation_date,end_time_db,start_time_db))
        if overlap: st.error("Избраният период вече съдържа зает час."); st.session_state.pop("pending_slot",None); return
        try:
            created=execute("""INSERT INTO reservations(user_id,reservation_date,start_time,end_time,wants_parts,parts_comment,vin,registration_doc,parts_status,expires_at,payment_due_type,final_amount,payment_status,payment_method,paid_at,test_payment,status)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN NOW()+INTERVAL '3 hours' ELSE NULL END,%s,%s,%s,%s,CASE WHEN %s THEN NOW() ELSE NULL END,%s,%s)
                       RETURNING id""",
                    (user_id,reservation_date,start_time_db,end_time_db,wants_parts,parts_comment,vin,doc_path,
                     "Очаква преглед" if wants_parts else None,wants_parts,
                     "parts" if wants_parts else "reservation",
                     None if wants_parts else booking_fee,
                     "unpaid" if wants_parts else "paid",
                     None if wants_parts else "card",
                     not wants_parts,
                     not wants_parts,
                     "Очаква потвърждение" if wants_parts else "Потвърдена"),True)
            reservation_id=created["id"]
            notify("admin","Нова резервация",f"Нова резервация #{reservation_id} за {reservation_date} от {pending_start} до {pending_end}.",target_page="Резервации",reservation_id=reservation_id)
            for key in ["pending_slot","slot_start_index","slot_end_index","parts_purchase_choice","show_direct_payment"]: st.session_state.pop(key,None)
            st.session_state.client_flash=(f"Заявката за части и часът са изпратени. Часът е временно запазен за 3 часа и очаква обратна връзка от администрацията." if wants_parts else f"Тестовото плащане е отчетено. Часът за {day_value}, {pending_start}–{pending_end} е потвърден.")
            st.rerun()
        except IntegrityError:
            st.error("Началният час току-що е резервиран. Изберете друг период.")

def history(user_id):
    expire_stale_parts_requests()
    st.subheader("История на резервациите")
    rows=fetchall("""SELECT r.*, GREATEST(1, CEIL(EXTRACT(EPOCH FROM (r.end_time-r.start_time))/3600.0)) * %s AS duration_fee FROM reservations r WHERE user_id=%s ORDER BY reservation_date DESC,start_time DESC""",(reservation_fee(),user_id))
    if not rows: st.info("Все още няма резервации."); return
    for r in rows:
        day=r["reservation_date"].isoformat(); start_hour=r["start_time"].strftime("%H:%M"); end_hour=r["end_time"].strftime("%H:%M")
        status_icon = "🟠" if r["status"]=="Очаква потвърждение" else ("🟢" if r["status"]=="Потвърдена" else "⚪")
        with st.expander(f"{status_icon} {day} • {start_hour}–{end_hour} • {r['status']}"):
            if r["status"]=="Анулирана" and r.get("parts_status")=="Изтекла заявка":
                st.error("Заявката е изтекла поради липса на действие в рамките на 3 часа. Часът отново е свободен.")
                continue
            st.write("**Заявка за части:**","Да" if r["wants_parts"] else "Не")
            if r.get("parts_status"): st.write("**Статус части:**",r["parts_status"])
            if r.get("expires_at") and r.get("payment_status") != "paid" and r.get("parts_status") in ["Очаква преглед","Оферта изпратена","Избор от клиента","Изборът е потвърден"]:
                st.warning(f"Срок за действие: {r['expires_at']:%d.%m.%Y %H:%M}. След този час резервацията се освобождава автоматично.")
            offer_bytes=bytes(r["parts_offer_data"]) if r.get("parts_offer_data") else None
            if offer_bytes:
                with st.expander("📎 Преглед на офертата",expanded=False):
                    mime=r.get("parts_offer_mime") or "application/octet-stream"; name=r.get("parts_offer_name") or "oferta.pdf"
                    if mime.startswith("image/"): st.image(offer_bytes,caption=name,width=760)
                    elif mime=="application/pdf":
                        b64=base64.b64encode(offer_bytes).decode("utf-8")
                        pdf_data=f"data:application/pdf;base64,{b64}"
                        st.markdown(f"""<object data="{pdf_data}" type="application/pdf" width="100%" height="430" style="border:1px solid #555;border-radius:10px;">
                        <embed src="{pdf_data}" type="application/pdf" width="100%" height="430" />
                        <p style="color:white">Мобилният браузър не поддържа вграден PDF преглед. Използвайте бутона под него.</p>
                        </object>""",unsafe_allow_html=True)
                    st.download_button("📱 Отвори / изтегли офертата",offer_bytes,name,mime,key=f"offer_download_{r['id']}",use_container_width=True,type="primary")
            try: offer_items=json.loads(r.get("offer_items_json") or "[]")
            except Exception: offer_items=[]
            try: selected_items=json.loads(r.get("selected_items_json") or "[]")
            except Exception: selected_items=[]
            if r["wants_parts"] and r.get("parts_status")=="Очаква преглед":
                st.info("Заявката е получена. Администрацията подготвя оферта. На този етап не се дължи плащане.")
            elif r["wants_parts"] and r.get("parts_status")=="Оферта изпратена" and offer_items:
                st.success("🔔 Имате нова оферта. Маркирайте желаните артикули и изпратете избора за потвърждение.")
                chosen=[]
                for i,item in enumerate(offer_items):
                    qty=int(item.get("qty",1)); price=float(item.get("price",0)); article=str(item.get("article", "")); total=qty*price
                    if st.checkbox(f"{article} · {qty} бр. × {price:.2f} € = {total:.2f} €",key=f"choose_{r['id']}_{i}"):
                        chosen.append({"article":article,"qty":qty,"price":price,"total":total})
                no_parts=st.checkbox("Не желая нито един артикул от офертата",key=f"no_parts_{r['id']}")
                total=sum(x["total"] for x in chosen)
                st.markdown(f"### Общо от избора: {total:.2f} €")
                comment=st.text_area("Коментар към избора",key=f"comment_{r['id']}")
                if st.button("Изпрати избора към администрацията",key=f"submit_choice_{r['id']}",use_container_width=True,type="primary"):
                    if not chosen and not no_parts: st.error("Маркирайте артикул или изберете, че не желаете части.")
                    elif chosen and no_parts: st.error("Изберете артикули или отказ от всички части, но не и двете.")
                    else:
                        if no_parts:
                            execute("""UPDATE reservations SET selected_items_json='[]',selected_items_total=0,customer_parts_decision='Отказвам частите',customer_parts_comment=%s,customer_selection_at=NOW(),customer_parts_responded_at=NOW(),parts_status='Без избрани части',payment_due_type='reservation',final_amount=%s,expires_at=NULL WHERE id=%s AND user_id=%s""",(comment.strip() or None,float(r.get("duration_fee") or reservation_fee()),r["id"],user_id))
                            notify("admin","Клиентът отказа частите",f"Резервация #{r['id']}: клиентът не желае артикули от офертата.",target_page="Резервации",reservation_id=r["id"]); st.session_state.client_flash="Отказахте частите. Вече можете да платите само заявения час."
                        else:
                            execute("""UPDATE reservations SET selected_items_json=%s,selected_items_total=%s,customer_parts_decision='Изпратен избор',customer_parts_comment=%s,customer_selection_at=NOW(),customer_parts_responded_at=NOW(),parts_status='Избор от клиента',expires_at=NOW()+INTERVAL '3 hours' WHERE id=%s AND user_id=%s""",(json.dumps(chosen,ensure_ascii=False),total,comment.strip() or None,r["id"],user_id))
                            notify("admin","Нов избор на клиент",f"Клиентът изпрати избор по оферта за резервация #{r['id']}.",target_page="Резервации",reservation_id=r["id"]); st.session_state.client_flash="Изборът е изпратен. Очаква потвърждение от администрацията."
                        st.rerun()
            elif r.get("parts_status")=="Избор от клиента":
                st.warning("Изборът е изпратен и очаква потвърждение от администрацията. Плащане още не се изисква.")
            elif r.get("parts_status")=="Изборът е потвърден":
                st.success("🔔 Администрацията потвърди избраните артикули. Офертата и часът са готови за общо плащане.")
                for x in selected_items: st.write(f"• {x.get('article')} · {x.get('qty')} бр. × {float(x.get('price',0)):.2f} €")
                st.write(f"**Части:** {float(r.get('selected_items_total') or 0):.2f} €")
                st.write(f"**Такса за резервираното време:** {float(r.get('duration_fee') or reservation_fee()):.2f} €")
            elif r.get("parts_status")=="Без избрани части":
                st.info("Не са избрани части. Дължи се само таксата за заявения час.")
            if r.get("payment_status")=="paid":
                st.success(f"✅ Платено в тестов режим: {float(r.get('final_amount') or 0):.2f} €")
            else:
                can_pay = (not r["wants_parts"]) or r.get("parts_status") in ["Изборът е потвърден","Без избрани части"]
                if can_pay:
                    amount=float(r.get("final_amount") or (reservation_fee() if r.get("payment_due_type")=="reservation" else 0))
                    st.markdown(f"### Сума за плащане: {amount:.2f} €")
                    st.warning("Тестов режим: плащането се отчита като получено, без реална картова транзакция.")
                    if st.button("💳 Плати с карта",key=f"pay_{r['id']}",use_container_width=True,type="primary"):
                        execute("""UPDATE reservations SET payment_status='paid',payment_method='card',paid_at=NOW(),test_payment=TRUE,status='Потвърдена',expires_at=NULL WHERE id=%s AND user_id=%s""",(r["id"],user_id))
                        notify("admin","Получено плащане",f"Резервация #{r['id']}: отчетено е тестово плащане {amount:.2f} €.",target_page="Финанси",reservation_id=r["id"]); st.session_state.client_flash=f"Тестовото плащане от {amount:.2f} € е отчетено. Резервацията е потвърдена."
                        st.rerun()
            if r.get("admin_note"): st.write("**Съобщение от сервиза:**",r["admin_note"])
            if r.get("door_pin"): st.success(f"PIN за вход: {r['door_pin']}")

def technical_issue(user_id):

    st.subheader("Технически проблем")

    category = st.selectbox(
        "Категория",
        [
            "Подемник",
            "Инструменти",
            "Врата / smart lock",
            "Осветление",
            "Друго",
        ],
        key="issue_category",
    )

    description = st.text_area(
        "Опиши проблема *",
        key="issue_description",
    )

    add_photo = st.checkbox(
        "📷 Добави снимка към сигнала",
        key="issue_add_photo",
    )

    photo = None

    if add_photo:
        photo_source = st.radio(
            "Как желаете да добавите снимката?",
            ["Снимай с камера", "Качи готова снимка"],
            horizontal=True,
            key="issue_photo_source",
        )

        if photo_source == "Снимай с камера":
            photo = st.camera_input(
                "Снимай проблема",
                key="issue_camera",
            )
        else:
            photo = st.file_uploader(
                "Качи снимка на проблема",
                type=["jpg", "jpeg", "png", "webp"],
                key="issue_upload",
            )
        if photo is not None:
            st.success("Снимката е добавена успешно.")

            st.image(
                photo,
                caption="Преглед на снимката преди изпращане",
                use_container_width=True,
            )

    if st.button(
        "Изпрати сигнал",
        key="submit_issue",
        use_container_width=True,
        type="primary",
    ):

        if not description.strip():
            st.error("Добави описание на проблема.")
            return

        photo_path = None

        if add_photo and photo is None:
            st.error(
                "Маркирали сте добавяне на снимка, "
                "но няма заснета или качена снимка."
            )
            return

        if photo is not None:
            photo_path = save_upload(
                photo,
                DOC_DIR,
                "issue",
            )

        execute(
            """
            INSERT INTO technical_issues(
                user_id,
                category,
                description,
                photo
            )
            VALUES(%s,%s,%s,%s)
            """,
            (
                user_id,
                category,
                description.strip(),
                photo_path,
            ),
        )

        notify("admin","Нов технически сигнал",f"Нов технически сигнал: {category}.",target_page="Технически сигнали"); st.session_state.client_flash = (
            "Техническият сигнал е изпратен успешно."
        )

        for key in [
            "issue_description",
            "issue_add_photo",
            "issue_photo_source",
            "issue_camera",
            "issue_upload",
        ]:
            st.session_state.pop(key, None)

        st.rerun()


def client_panel():
    user = fetchone("SELECT * FROM users WHERE id=%s", (st.session_state.user_id,))

    if "client_page" not in st.session_state:
        st.session_state.client_page = "Начало"

    st.markdown('<div class="hero">', unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
    st.markdown(
        f"<p style='text-align:center;color:white;'>Здравей, {user['first_name']}.</p>",
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)

    nav_items = [
        ("🏠 Начало", "Начало"),
        ("🔧 AUTODOC", "Помощ от AUTODOC"),
        ("📅 Запази час", "Запази час"),
        ("🧾 Моите резервации", "Моите резервации"),
        ("🛠 Технически проблем", "Технически проблем"),
    ]
    nav_columns = st.columns(5)
    for column, (label, page_name) in zip(nav_columns, nav_items):
        with column:
            if st.button(
                label,
                key="client_nav_autodoc" if page_name == "Помощ от AUTODOC" else f"client_nav_{page_name}",
                use_container_width=True,
                type="primary" if st.session_state.client_page == page_name else "secondary",
            ):
                st.session_state.client_page = page_name
                st.rerun()

    with st.sidebar:
        st.write(f"**{user['first_name']} {user['last_name']}**")
        if st.button("🏠 Начало", key="sidebar_home", use_container_width=True):
            st.session_state.client_page = "Начало"
            st.rerun()
        if st.button("👤 Моят профил", key="sidebar_profile", use_container_width=True):
            st.session_state.client_page = "Моят профил"
            st.rerun()
        if st.button("Изход", use_container_width=True):
            logout()

    if st.session_state.get("client_flash"):
        st.success(st.session_state.pop("client_flash"))

    # Камбанката остава видима във всички секции на клиентския профил.
    notification_center("client",user["id"])
    page = st.session_state.client_page
    st.markdown("---")
    if page == "Начало":
        video_section()
    elif page == "Помощ от AUTODOC":
        autodoc_help()
    elif page == "Запази час":
        new_reservation(user["id"])
    elif page == "Моите резервации":
        history(user["id"])
    elif page == "Технически проблем":
        technical_issue(user["id"])
    else:
        st.subheader("Моят профил")
        st.write(f"**Име:** {user['first_name']} {user['last_name']}")
        st.write(f"**Телефон:** {user['phone']}")
        st.write(f"**Имейл:** {user['email'] or '—'}")

def admin_reservations():
    expire_stale_parts_requests()
    st.subheader("Мониторинг на резервациите")
    statuses=["Очаква потвърждение","Потвърдена","Отказана","Завършена"]
    selected=st.multiselect("Статус",statuses,default=statuses[:2])
    rows=fetchall("""SELECT r.*,u.first_name,u.last_name,u.phone, GREATEST(1, CEIL(EXTRACT(EPOCH FROM (r.end_time-r.start_time))/3600.0)) * %s AS duration_fee FROM reservations r JOIN users u ON u.id=r.user_id
                     WHERE r.status NOT IN ('Завършена','Отказана','Анулирана') ORDER BY r.reservation_date,r.start_time""",(reservation_fee(),))
    rows=[r for r in rows if not selected or r["status"] in selected]
    for r in rows:
        day=r["reservation_date"].isoformat(); start=r["start_time"].strftime("%H:%M"); end=r["end_time"].strftime("%H:%M")
        badge="🟠 ЧАКА ПОТВЪРЖДЕНИЕ" if r["status"]=="Очаква потвърждение" else "🟢 ПОТВЪРДЕНА"
        with st.expander(f"{badge} • #{r['id']} • {day} {start}–{end} • {r['first_name']} {r['last_name']}"):
            st.write(f"**Телефон:** {r['phone']}"); st.write("**Части:**","Да" if r["wants_parts"] else "Не")
            if r["wants_parts"]:
                st.write("**VIN:**",r.get("vin") or "—"); st.write("**Заявка:**",r.get("parts_comment") or "—")
                if r.get("expires_at"): st.warning(f"Краен срок за действие: {r['expires_at']:%d.%m.%Y %H:%M}")
            try: selected_items=json.loads(r.get("selected_items_json") or "[]")
            except Exception: selected_items=[]
            if selected_items:
                st.success("✅ ИЗБОР НА КЛИЕНТА")
                total=0
                for x in selected_items:
                    line=float(x.get("total",float(x.get("qty",1))*float(x.get("price",0)))); total+=line
                    st.write(f"• Артикул **{x.get('article')}** · {x.get('qty')} бр. × {float(x.get('price',0)):.2f} € = **{line:.2f} €**")
                st.markdown(f"### Общо избрано: {total:.2f} €")
                if r.get("customer_parts_comment"): st.write("**Коментар:**",r["customer_parts_comment"])
            elif r.get("parts_status")=="Без избрани части":
                st.info("Клиентът не е избрал части. Дължи се само таксата за часа.")
            c1,c2=st.columns(2)
            ns=c1.selectbox("Статус резервация",statuses,index=statuses.index(r["status"]),key=f"status_{r['id']}")
            c2.text_input("Статус части",value=r.get("parts_status") or "—",disabled=True,key=f"parts_state_{r['id']}")
            offer_upload=st.file_uploader("📎 Качи оферта (PDF или снимка)",type=["pdf","jpg","jpeg","png","webp"],key=f"offer_file_{r['id']}",disabled=not r["wants_parts"])
            try: existing=json.loads(r.get("offer_items_json") or "[]")
            except Exception: existing=[]
            offer_items=[]
            if r["wants_parts"] and r.get("parts_status") not in ["Избор от клиента","Изборът е потвърден","Без избрани части"]:
                st.markdown("#### Артикули от офертата")
                count=st.number_input("Брой артикули",1,30,max(1,len(existing)),1,key=f"count_{r['id']}")
                for i in range(int(count)):
                    cur=existing[i] if i<len(existing) else {}; a,b,c=st.columns([2,1,1])
                    art=a.text_input("Номер артикул",value=str(cur.get("article","")),key=f"art_{r['id']}_{i}")
                    qty=b.number_input("Брой",1,999,int(cur.get("qty",1) or 1),1,key=f"qty_{r['id']}_{i}")
                    price=c.number_input("Цена/бр. (€)",0.0,100000.0,float(cur.get("price",0) or 0),0.01,key=f"price_{r['id']}_{i}")
                    if art.strip() and price>0: offer_items.append({"article":art.strip(),"qty":int(qty),"price":float(price)})
                st.markdown(f"### Общо на всички предложени артикули: {sum(x['qty']*x['price'] for x in offer_items):.2f} €")
            note=st.text_area("Съобщение към клиента",value=r.get("admin_note") or "",key=f"note_{r['id']}")
            pin=st.text_input("PIN",value=r.get("door_pin") or "",key=f"pin_{r['id']}")
            if r["wants_parts"] and r.get("parts_status") in ["Очаква преглед","Оферта изпратена"]:
                if st.button("📨 Изпрати офертата към клиента",key=f"send_offer_{r['id']}",use_container_width=True,type="primary"):
                    if not (offer_upload or r.get("parts_offer_data")): st.error("Качете PDF или снимка с офертата.")
                    elif not offer_items: st.error("Въведете поне един артикул с номер, брой и цена.")
                    else:
                        data=r.get("parts_offer_data"); name=r.get("parts_offer_name"); mime=r.get("parts_offer_mime")
                        if offer_upload is not None: data=psycopg2.Binary(offer_upload.getvalue()); name=offer_upload.name; mime=offer_upload.type or "application/octet-stream"
                        execute("""UPDATE reservations SET parts_offer_data=%s,parts_offer_name=%s,parts_offer_mime=%s,offer_items_json=%s,parts_status='Оферта изпратена',offer_sent_at=NOW(),expires_at=NOW()+INTERVAL '3 hours',admin_note=%s WHERE id=%s""",(data,name,mime,json.dumps(offer_items,ensure_ascii=False),note.strip() or None,r["id"]))
                        notify("client","Нова оферта",f"Имате нова оферта за резервация #{r['id']}. Разполагате с 3 часа за избор.",user_id=r["user_id"],target_page="Моите резервации",reservation_id=r["id"]); st.session_state.admin_flash=f"Офертата за резервация #{r['id']} е изпратена. Клиентът има 3 часа за действие."; st.rerun()
            if r.get("parts_status")=="Избор от клиента":
                if st.button("✅ Потвърди избора на клиента",key=f"approve_choice_{r['id']}",use_container_width=True,type="primary"):
                    parts_total=float(r.get("selected_items_total") or 0)
                    total=parts_total + float(r.get("duration_fee") or reservation_fee())
                    execute("""UPDATE reservations SET parts_status='Изборът е потвърден',admin_selection_confirmed_at=NOW(),final_amount=%s,payment_due_type='parts_and_reservation',expires_at=NOW()+INTERVAL '3 hours',admin_note=%s WHERE id=%s""",(total,note.strip() or None,r["id"]))
                    notify("client","Изборът е потвърден",f"Избраните артикули за резервация #{r['id']} са потвърдени. Вече можете да платите.",user_id=r["user_id"],target_page="Моите резервации",reservation_id=r["id"]); st.session_state.admin_flash=f"Изборът на клиента за #{r['id']} е потвърден. Изпратено е известие за плащане."; st.rerun()
            if st.button("Запази статус / PIN / съобщение",key=f"save_meta_{r['id']}",use_container_width=True):
                execute("UPDATE reservations SET status=%s,admin_note=%s,door_pin=%s WHERE id=%s",(ns,note.strip() or None,pin.strip() or None,r["id"]))
                st.session_state.admin_flash=f"Резервация #{r['id']} е обновена."; st.rerun()

def admin_archive():
    st.subheader("Архив")
    a,b=st.columns(2); d1=a.date_input("От",date.today()-timedelta(days=90)); d2=b.date_input("До",date.today())
    rows=fetchall("""SELECT r.*,u.first_name,u.last_name,u.phone FROM reservations r JOIN users u ON u.id=r.user_id
                   WHERE reservation_date BETWEEN %s AND %s AND status IN ('Завършена','Отказана') ORDER BY reservation_date DESC,start_time DESC""",(d1,d2))
    st.metric("Архивирани",len(rows))
    for r in rows:
        with st.expander(
            f"{r['status']} • {r['reservation_date']} "
            f"{r['start_time'].strftime('%H:%M')}–{r['end_time'].strftime('%H:%M')} • "
            f"{r['first_name']} {r['last_name']}"
        ):
            st.write("**Телефон:**",r["phone"]); st.write("**Оферта:**",r.get("parts_offer") or "—"); st.write("**Решение:**",r.get("customer_parts_decision") or "—"); st.write("**Коментар:**",r.get("customer_parts_comment") or "—")


    st.markdown("### Решени технически сигнали")
    solved_issues = fetchall("""SELECT t.*,u.first_name,u.last_name FROM technical_issues t
                              JOIN users u ON u.id=t.user_id
                              WHERE t.status='Решено'
                              ORDER BY t.id DESC""")
    if not solved_issues:
        st.info("Няма решени технически сигнали.")
    for issue in solved_issues:
        with st.expander(f"Решено • #{issue['id']} • {issue['category']} • {issue['first_name']} {issue['last_name']}"):
            st.write(issue["description"])
            st.caption(f"Създадено: {issue['created_at'].strftime('%d.%m.%Y %H:%M')}")


def admin_statistics():
    st.subheader("Статистика")
    a,b=st.columns(2); d1=a.date_input("Период от",date.today()-timedelta(days=30),key="sf"); d2=b.date_input("Период до",date.today(),key="st")
    q=fetchone("""SELECT COUNT(*) total,COUNT(*) FILTER(WHERE status='Потвърдена') confirmed,
      COUNT(*) FILTER(WHERE status='Завършена') completed,COUNT(*) FILTER(WHERE status='Отказана') cancelled,
      COUNT(*) FILTER(WHERE wants_parts) parts,COUNT(*) FILTER(WHERE customer_parts_decision='Искам') accepted
      FROM reservations WHERE reservation_date BETWEEN %s AND %s""",(d1,d2))
    cols=st.columns(6)
    for col,label,key in zip(cols,["Общо","Потвърдени","Завършени","Отказани","С части","Приети оферти"],["total","confirmed","completed","cancelled","parts","accepted"]): col.metric(label,int(q[key] or 0))
    daily=fetchall("SELECT reservation_date,COUNT(*) total FROM reservations WHERE reservation_date BETWEEN %s AND %s GROUP BY reservation_date ORDER BY reservation_date",(d1,d2))
    if daily: st.line_chart({r["reservation_date"].isoformat():int(r["total"]) for r in daily})

def admin_finance():
    st.subheader("💰 Финанси")
    paid=float(fetchone("SELECT COALESCE(SUM(final_amount),0) total FROM reservations WHERE payment_status='paid'")["total"] or 0)
    due=float(fetchone("SELECT COALESCE(SUM(final_amount),0) total FROM reservations WHERE final_amount IS NOT NULL AND payment_status<>'paid' AND status NOT IN ('Отказана','Анулирана')")["total"] or 0)
    spent=float(fetchone("SELECT COALESCE(SUM(amount),0) total FROM expenses")["total"] or 0)
    c1,c2,c3,c4=st.columns(4); c1.metric("Постъпления",f"{paid:.2f} €"); c2.metric("Очаквани",f"{due:.2f} €"); c3.metric("Разходи",f"{spent:.2f} €"); c4.metric("Нетно",f"{paid-spent:.2f} €")
    with st.form("garage_expense",clear_on_submit=True):
        d=st.date_input("Дата",date.today()); cat=st.selectbox("Категория",["Инструменти","Консумативи","Ток","Вода","Други"])
        amount=st.number_input("Сума (€)",0.0,100000.0,0.0,0.01); desc=st.text_area("Описание"); save=st.form_submit_button("Добави разход",use_container_width=True)
    if save:
        if amount<=0: st.error("Сумата трябва да бъде по-голяма от 0.")
        else: execute("INSERT INTO expenses(expense_date,category,amount,description) VALUES(%s,%s,%s,%s)",(d,cat,amount,desc.strip() or None)); st.rerun()
    incomes=fetchall("""SELECT r.id,r.paid_at,r.final_amount,r.payment_due_type,r.test_payment,u.first_name,u.last_name,r.reservation_date
                        FROM reservations r JOIN users u ON u.id=r.user_id WHERE r.payment_status='paid' ORDER BY r.paid_at DESC""")
    expenses=fetchall("SELECT id,expense_date,category,amount,description,created_at FROM expenses ORDER BY expense_date DESC,id DESC")
    movements=[]
    for x in incomes:
        movements.append({"Дата":x.get("paid_at") or x.get("reservation_date"),"Тип":"Приход","Източник":f"Резервация #{x['id']} · {x['first_name']} {x['last_name']}","Основание":"Части + такса за час" if x.get("payment_due_type")=="parts_and_reservation" else ("Части" if x.get("payment_due_type")=="parts" else "Такса за час"),"Приход EUR":float(x.get("final_amount") or 0),"Разход EUR":0.0,"Тестово":"Да" if x.get("test_payment") else "Не"})
    for x in expenses:
        movements.append({"Дата":x.get("created_at") or x.get("expense_date"),"Тип":"Разход","Източник":x.get("category"),"Основание":x.get("description") or "Без описание","Приход EUR":0.0,"Разход EUR":float(x.get("amount") or 0),"Тестово":"Не"})
    movements.sort(key=lambda x:str(x["Дата"]),reverse=True)
    st.markdown("### Движения")
    if movements:
        st.dataframe(movements,use_container_width=True,hide_index=True)
        try:
            from io import BytesIO
            from openpyxl import Workbook
            wb=Workbook(); ws=wb.active; ws.title="Финанси"; headers=list(movements[0].keys()); ws.append(headers)
            for m in movements: ws.append([m[h] if not isinstance(m[h],(datetime,date)) else m[h].strftime("%d.%m.%Y %H:%M") for h in headers])
            for cell in ws[1]: cell.font=cell.font.copy(bold=True)
            for col in ws.columns: ws.column_dimensions[col[0].column_letter].width=min(45,max(12,max(len(str(c.value or "")) for c in col)+2))
            bio=BytesIO(); wb.save(bio)
            st.download_button("📥 Изтегли движенията в Excel",bio.getvalue(),f"finansi_{date.today().isoformat()}.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",use_container_width=True)
        except ImportError:
            st.error("За Excel експорта добавете openpyxl в requirements.txt.")
    else: st.info("Все още няма финансови движения.")

def admin_panel():

    st.subheader("Административен панел")

    with st.sidebar:
        page = st.radio(
            "Админ меню",
            ["Табло", "Резервации", "Архив", "Статистика", "Технически сигнали", "Видео съдържание", "Помощ AUTODOC", "Финанси", "Настройки"],
        )
        st.caption("🔒 Стабилна сесия: входът се пази до 7 дни")
        if st.button("Изход"):
            logout()

    if st.session_state.get("admin_flash"):
        st.success(st.session_state.pop("admin_flash"))

    # Камбанката остава видима във всички секции на Admin профила.
    notification_center("admin")
    if page == "Табло":
        pending = fetchone("SELECT COUNT(*) AS c FROM reservations WHERE status='Очаква потвърждение'")["c"]
        parts = fetchone("""SELECT COUNT(*) AS c FROM reservations
                          WHERE wants_parts=TRUE
                            AND status NOT IN ('Завършена','Отказана')
                            AND parts_status IN ('Очаква преглед','Можем да подготвим','Клиентът потвърди')""")["c"]
        issues = fetchone("SELECT COUNT(*) AS c FROM technical_issues WHERE status='Ново'")["c"]
        users = fetchone("SELECT COUNT(*) AS c FROM users")["c"]
        for col,label,value in zip(
            st.columns(4),
            ["Чакащи резервации","Заявки за части","Нови сигнали","Клиенти"],
            [pending,parts,issues,users],
        ):
            col.metric(label,value)
    elif page == "Резервации":
        admin_reservations()
    elif page == "Архив":
        admin_archive()
    elif page == "Статистика":
        admin_statistics()
    elif page == "Технически сигнали":

        rows = fetchall(
            """
            SELECT
                t.*,
                u.first_name,
                u.last_name
            FROM technical_issues t
            JOIN users u
                ON u.id = t.user_id
            WHERE t.status <> 'Решено'
            ORDER BY t.id DESC
            """
        )

        if not rows:
            st.info("Няма активни технически сигнали.")

        for r in rows:

            with st.expander(
                f"#{r['id']} • {r['category']} • "
                f"{r['first_name']} {r['last_name']} • "
                f"{r['status']}"
            ):

                st.write(r["description"])

                if r.get("photo"):

                    issue_photo_path = Path(r["photo"])

                    if issue_photo_path.exists():

                        st.markdown("**Снимка към сигнала:**")

                        suffix = issue_photo_path.suffix.lower()

                        if suffix in [
                            ".jpg",
                            ".jpeg",
                            ".png",
                            ".webp",
                        ]:
                            st.image(
                                str(issue_photo_path),
                                caption=f"Технически сигнал #{r['id']}",
                                use_container_width=True,
                            )

                            with open(
                                issue_photo_path,
                                "rb",
                            ) as issue_file:
                                st.download_button(
                                    "📥 Изтегли снимката",
                                    data=issue_file.read(),
                                    file_name=issue_photo_path.name,
                                    key=f"download_issue_{r['id']}",
                                    use_container_width=True,
                                )

                        else:
                            st.info(
                                f"Прикачен файл: "
                                f"{issue_photo_path.name}"
                            )

                    else:
                        st.warning(
                            "Към сигнала има записана снимка, "
                            "но файлът вече не е наличен на сървъра."
                        )

                else:
                    st.caption(
                        "Към този технически сигнал няма приложена снимка."
                    )

                status_options = [
                    "Ново",
                    "В работа",
                    "Решено",
                ]

                current_status = (
                    r["status"]
                    if r["status"] in status_options
                    else "Ново"
                )

                new = st.selectbox(
                    "Статус",
                    status_options,
                    index=status_options.index(current_status),
                    key=f"is_{r['id']}",
                )

                if st.button(
                    "Обнови",
                    key=f"iu_{r['id']}",
                    use_container_width=True,
                ):

                    execute(
                        """
                        UPDATE technical_issues
                        SET status=%s
                        WHERE id=%s
                        """,
                        (
                            new,
                            r["id"],
                        ),
                    )

                    if new == "Решено":
                        st.session_state.admin_flash = (
                            f"Потвърдено: сигнал #{r['id']} "
                            "е решен и преместен в Архив."
                        )
                    else:
                        st.session_state.admin_flash = (
                            f"Потвърдено: сигнал #{r['id']} "
                            f"е със статус „{new}“."
                        )

                    st.rerun()
    elif page == "Видео съдържание":
        with st.form("videos"):
            urls = [st.text_input(f"Видео {i}", value=setting(f"youtube_{i}")) for i in range(1,4)]
            if st.form_submit_button("Запази видеата", use_container_width=True):
                for i,url in enumerate(urls,1):
                    set_setting(f"youtube_{i}",url.strip())
                st.success("Потвърдено: видеата са обновени.")
    elif page == "Финанси":
        admin_finance()
    elif page == "Помощ AUTODOC":
        st.subheader("Видео помощ и линкове към AUTODOC")
        fields = [
            ("Смяна на масло", "help_oil"), ("Смяна на филтри", "help_filters"),
            ("Дискове и накладки", "help_brakes"), ("Ремонт по интериор", "help_interior"),
            ("Ремонт по екстериор / купе", "help_exterior"), ("Смяна на свещи", "help_spark_plugs"),
            ("Канал на AUTODOC", "autodoc_channel"), ("Сайт на AUTODOC за части", "autodoc_parts"),
        ]
        with st.form("autodoc_help_settings"):
            values = {key: st.text_input(label, value=setting(key, "")) for label, key in fields}
            if st.form_submit_button("Запази помощните линкове", use_container_width=True):
                for key, value in values.items(): set_setting(key, value.strip())
                st.success("Линковете за помощ са обновени.")
    else:
        with st.form("settings_form"):
            opening = st.text_input("Отваряне", value=setting("open_hour"))
            closing = st.text_input("Затваряне", value=setting("close_hour"))
            duration = st.number_input("Слот (минути)", 30, 240, int(setting("slot_minutes")), 30)
            fee = st.number_input("Такса за резервация (€)", 0.0, 1000.0, reservation_fee(), 0.01)
            new_pin = st.text_input("Нов администраторски PIN", type="password")
            if st.form_submit_button("Запази настройки"):
                datetime.strptime(opening,"%H:%M")
                datetime.strptime(closing,"%H:%M")
                set_setting("open_hour",opening)
                set_setting("close_hour",closing)
                set_setting("slot_minutes",duration)
                set_setting("reservation_fee",fee)
                if new_pin:
                    set_setting("admin_pin_hash",hash_value(new_pin))
                st.success("Потвърдено: настройките са запазени.")

try:
    init_db()
except Exception as exc:
    st.error("Неуспешна връзка с Neon. Провери DATABASE_URL в Render.")
    st.code(str(exc))
    st.stop()

if "role" not in st.session_state:
    try:
        restore_login_session()
    except Exception:
        st.cache_resource.clear()
        try: restore_login_session()
        except Exception: pass

apply_style()

if "role" not in st.session_state:

    st.markdown('<div class="hero">', unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1, 2, 1])

    with c2:

    st.markdown(
        "<p style='text-align:center;color:white;font-size:18px;'>"
        "Резервация • Части • Достъп • Самообслужване"
        "</p>",
        unsafe_allow_html=True,
    )

    st.markdown("</div>", unsafe_allow_html=True)

    tab1, tab2 = st.tabs(["Създай профил", "Вход"])

    with tab1:
        register_view()

    with tab2:
        login_view()

elif st.session_state.role == "client":
    keep_session_in_url()
    client_panel()

else:
    keep_session_in_url()
    admin_panel()
