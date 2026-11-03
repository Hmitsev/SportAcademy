import os
import io
import base64
import hashlib
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
import streamlit as st

APP_DIR = Path(__file__).parent
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ACADEMY_SLUG = os.getenv("ACADEMY_SLUG", "dyulgerov-academy")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@academy.local").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
ASSETS = {
    "logo": APP_DIR / "лого.jpg",
    "background": APP_DIR / "беграуд.jpg",
    "coming": APP_DIR / "очаква се.jpg",
    "code": APP_DIR / "кодекс.jpg",
}

st.set_page_config(page_title="Dyulgerov Academy", page_icon="⚽", layout="wide", initial_sidebar_state="collapsed")
if not DATABASE_URL:
    st.error("Липсва DATABASE_URL в Render Environment.")
    st.stop()

@st.cache_resource
def pool():
    return ThreadedConnectionPool(1, 8, dsn=DATABASE_URL, sslmode="require")

@contextmanager
def db():
    conn = pool().getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool().putconn(conn)

def fetchone(q, p=()):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(q, p)
        return cur.fetchone()

def fetchall(q, p=()):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(q, p)
        return cur.fetchall()

def execute(q, p=(), returning=False):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(q, p)
        return cur.fetchone() if returning else None

def hash_password(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def image_data(path):
    if not path.exists(): return ""
    mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()

@st.cache_resource(show_spinner=False)
def init_db():
    statements = [
        """CREATE TABLE IF NOT EXISTS academies(
            id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
            sport TEXT NOT NULL DEFAULT 'football', city TEXT, logo_file TEXT,
            primary_color TEXT NOT NULL DEFAULT '#b48a32', secondary_color TEXT NOT NULL DEFAULT '#f7f2e8',
            default_fee NUMERIC(12,2) NOT NULL DEFAULT 50.00, currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
            active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS users(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT NOT NULL,
            phone TEXT, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'parent',
            account_status TEXT NOT NULL DEFAULT 'pending', active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(academy_id,email))""",
        """CREATE TABLE IF NOT EXISTS coaches(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            user_id BIGINT REFERENCES users(id) ON DELETE SET NULL, display_name TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE, UNIQUE(academy_id,display_name))""",
        """CREATE TABLE IF NOT EXISTS teams(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            name TEXT NOT NULL, birth_year INTEGER, coach_id BIGINT REFERENCES coaches(id) ON DELETE SET NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE, UNIQUE(academy_id,name))""",
        """CREATE TABLE IF NOT EXISTS players(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            first_name TEXT NOT NULL, last_name TEXT NOT NULL, birth_date DATE,
            team_id BIGINT REFERENCES teams(id) ON DELETE SET NULL,
            approval_status TEXT NOT NULL DEFAULT 'pending', active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS parent_players(
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            parent_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            relationship TEXT DEFAULT 'Родител', PRIMARY KEY(parent_user_id,player_id))""",
        """CREATE TABLE IF NOT EXISTS events(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            team_id BIGINT REFERENCES teams(id) ON DELETE CASCADE, event_type TEXT NOT NULL,
            title TEXT NOT NULL, opponent TEXT, starts_at TIMESTAMPTZ NOT NULL, ends_at TIMESTAMPTZ,
            location TEXT, description TEXT, status TEXT NOT NULL DEFAULT 'scheduled',
            created_by BIGINT REFERENCES users(id), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS team_messages(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            team_id BIGINT REFERENCES teams(id) ON DELETE CASCADE, sender_user_id BIGINT REFERENCES users(id),
            title TEXT NOT NULL, body TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS fee_templates(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            amount NUMERIC(12,2) NOT NULL DEFAULT 50.00, currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
            active BOOLEAN NOT NULL DEFAULT TRUE, UNIQUE(academy_id,team_id))""",
        """CREATE TABLE IF NOT EXISTS player_fees(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE, team_id BIGINT REFERENCES teams(id),
            fee_year INTEGER NOT NULL, fee_month INTEGER NOT NULL, standard_amount NUMERIC(12,2) NOT NULL,
            final_amount NUMERIC(12,2) NOT NULL, adjustment_reason TEXT, custom_status TEXT,
            status TEXT NOT NULL DEFAULT 'due', due_date DATE, paid_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(player_id,fee_year,fee_month))""",
        """CREATE TABLE IF NOT EXISTS public_posts(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            title TEXT NOT NULL, body TEXT, media_type TEXT NOT NULL DEFAULT 'image',
            media_url TEXT, image_data BYTEA, image_mime TEXT, published BOOLEAN NOT NULL DEFAULT TRUE,
            created_by BIGINT REFERENCES users(id), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS match_squads(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            squad_status TEXT NOT NULL DEFAULT 'not_in_group',
            selected_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(event_id,player_id))""",
        """CREATE INDEX IF NOT EXISTS idx_match_squads_event ON match_squads(academy_id,event_id,player_id)""",
        """CREATE TABLE IF NOT EXISTS attendance(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'present', note TEXT,
            marked_by BIGINT REFERENCES users(id), marked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(event_id,player_id))""",
        """CREATE INDEX IF NOT EXISTS idx_attendance_event ON attendance(academy_id,event_id,player_id)""",
        """CREATE INDEX IF NOT EXISTS idx_players_academy_team ON players(academy_id,team_id)""",
        """CREATE INDEX IF NOT EXISTS idx_events_team_date ON events(academy_id,team_id,starts_at)""",
        """CREATE INDEX IF NOT EXISTS idx_messages_team ON team_messages(academy_id,team_id,created_at DESC)""",
        """CREATE INDEX IF NOT EXISTS idx_fees_player ON player_fees(academy_id,player_id,fee_year,fee_month)""",
    ]
    with db() as conn, conn.cursor() as cur:
        for sql in statements: cur.execute(sql)
        cur.execute("""INSERT INTO academies(name,slug,city,logo_file,default_fee)
            VALUES('Dyulgerov Academy',%s,'Благоевград','лого.jpg',50.00)
            ON CONFLICT(slug) DO NOTHING""", (ACADEMY_SLUG,))
        cur.execute("SELECT id FROM academies WHERE slug=%s", (ACADEMY_SLUG,))
        academy_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO users(academy_id,first_name,last_name,email,password_hash,role,account_status)
            VALUES(%s,'Academy','Admin',%s,%s,'admin','approved')
            ON CONFLICT(academy_id,email) DO NOTHING""", (academy_id, ADMIN_EMAIL, hash_password(ADMIN_PASSWORD)))
        for i in range(1,16):
            cur.execute("INSERT INTO coaches(academy_id,display_name) VALUES(%s,%s) ON CONFLICT DO NOTHING", (academy_id,f"Треньор {i}"))
        for year in range(2012,2029):
            cur.execute("INSERT INTO teams(academy_id,name,birth_year) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (academy_id,f"Набор {year}",year))
        cur.execute("""INSERT INTO fee_templates(academy_id,team_id,amount)
            SELECT %s,id,50.00 FROM teams WHERE academy_id=%s ON CONFLICT DO NOTHING""", (academy_id,academy_id))
    return academy_id

ACADEMY_ID = init_db()

def apply_style():
    bg = image_data(ASSETS["background"])

    st.markdown(
        f"""
        <style>
        :root {{
            --gold: #b48a32;
            --gold-light: #d6b765;
            --cream: #f8f4ea;
            --text: #ffffff;
            --muted: #ddd4c2;
            --line: rgba(214, 183, 101, 0.48);
            --glass: rgba(8, 8, 8, 0.54);
            --glass-strong: rgba(5, 5, 5, 0.72);
        }}

        /* ОСНОВЕН ФОН */
        html,
        body,
        [data-testid="stAppViewContainer"],
        .stApp {{
            background-image:
                linear-gradient(
                    rgba(0, 0, 0, 0.48),
                    rgba(0, 0, 0, 0.68)
                ),
                url("{bg}") !important;

            background-size: cover !important;
            background-position: center center !important;
            background-repeat: no-repeat !important;
            background-attachment: fixed !important;
            color: var(--text) !important;
        }}

        /* ПРЕМАХВА СТАНДАРТНИТЕ ФОНОВЕ НА STREAMLIT */
        [data-testid="stAppViewContainer"] > .main {{
            background: transparent !important;
        }}

        [data-testid="stHeader"] {{
            background: rgba(5, 5, 5, 0.60) !important;
            border-bottom: 1px solid rgba(214, 183, 101, 0.22);
        }}

        [data-testid="stToolbar"] {{
            background: transparent !important;
        }}

        .block-container {{
            max-width: 1250px;
            padding-top: 1.4rem;
            padding-bottom: 4rem;
        }}

        /* ТЕКСТ */
        h1,
        h2,
        h3,
        h4,
        h5,
        h6,
        p,
        label,
        span,
        .stMarkdown,
        [data-testid="stCaptionContainer"] {{
            color: #ffffff !important;
        }}

        [data-testid="stCaptionContainer"] p {{
            color: #d7cfbf !important;
        }}

        /* SIDEBAR */
        section[data-testid="stSidebar"] {{
            background: rgba(6, 6, 6, 0.82) !important;
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border-right: 1px solid var(--line);
        }}

        section[data-testid="stSidebar"] > div {{
            background: transparent !important;
        }}

        section[data-testid="stSidebar"] label,
        section[data-testid="stSidebar"] p,
        section[data-testid="stSidebar"] span {{
            color: #ffffff !important;
        }}

        /* ФОРМИ И ПРОЗРАЧНИ ПАНЕЛИ */
        [data-testid="stForm"],
        .academy-card,
        div[data-testid="stVerticalBlockBorderWrapper"] {{
            background: rgba(8, 8, 8, 0.55) !important;
            backdrop-filter: blur(13px);
            -webkit-backdrop-filter: blur(13px);
            border: 1px solid var(--line) !important;
            border-radius: 18px !important;
            box-shadow: 0 15px 38px rgba(0, 0, 0, 0.38);
        }}

        [data-testid="stForm"] {{
            padding: 20px;
        }}

        /* МЕТРИКИ В ADMIN ТАБЛОТО */
        div[data-testid="stMetric"] {{
            background: rgba(8, 8, 8, 0.58) !important;
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 12px 30px rgba(0, 0, 0, 0.32);
        }}

        div[data-testid="stMetric"] label,
        div[data-testid="stMetric"] div {{
            color: #ffffff !important;
        }}

        /* HERO */
        .hero {{
            border-radius: 24px;
            padding: 28px;
            background: rgba(5, 5, 5, 0.56);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--line);
            box-shadow: 0 18px 50px rgba(0, 0, 0, 0.42);
            margin-bottom: 18px;
            text-align: center;
        }}

        .hero h1 {{
            color: #ffffff !important;
        }}

        /* ПОЛЕТА */
        div[data-baseweb="input"] > div,
        div[data-baseweb="select"] > div,
        div[data-baseweb="base-input"],
        textarea,
        input {{
            background-color: rgba(13, 14, 18, 0.82) !important;
            color: #ffffff !important;
            border-color: rgba(214, 183, 101, 0.34) !important;
        }}

        input::placeholder,
        textarea::placeholder {{
            color: rgba(255, 255, 255, 0.55) !important;
        }}

        /* SELECT И POPOVER */
        div[data-baseweb="popover"],
        ul[role="listbox"] {{
            background: #151515 !important;
            color: #ffffff !important;
        }}

        li[role="option"] {{
            color: #ffffff !important;
        }}

        /* БУТОНИ */
        div.stButton > button,
        div.stDownloadButton > button,
        button[kind="formSubmit"] {{
            background:
                linear-gradient(
                    135deg,
                    #956b20,
                    #d1ad57
                ) !important;

            color: #ffffff !important;
            border: 1px solid #e2c777 !important;
            border-radius: 12px !important;
            min-height: 44px;
            font-weight: 800 !important;
            box-shadow: 0 8px 20px rgba(0, 0, 0, 0.30);
        }}

        div.stButton > button:hover,
        div.stDownloadButton > button:hover,
        button[kind="formSubmit"]:hover {{
            border-color: #fff0b8 !important;
            box-shadow: 0 9px 24px rgba(180, 138, 50, 0.38);
            transform: translateY(-1px);
        }}

        div.stButton > button[kind="primary"] {{
            background:
                linear-gradient(
                    135deg,
                    rgba(18, 16, 12, 0.96),
                    rgba(93, 69, 25, 0.96)
                ) !important;

            border-color: #d9ba65 !important;
        }}

        /* TABS */
        button[data-baseweb="tab"] {{
            color: #ffffff !important;
        }}

        button[data-baseweb="tab"][aria-selected="true"] {{
            border-bottom-color: #d6b765 !important;
        }}

        /* EXPANDER */
        [data-testid="stExpander"] {{
            background: rgba(8, 8, 8, 0.55) !important;
            border: 1px solid var(--line) !important;
            border-radius: 14px !important;
        }}

        [data-testid="stExpander"] summary {{
            color: #ffffff !important;
        }}

        /* ALERT BOXES */
        [data-testid="stAlert"] {{
            background: rgba(12, 12, 12, 0.75) !important;
            border: 1px solid rgba(214, 183, 101, 0.42) !important;
            color: #ffffff !important;
        }}

        /* РОДИТЕЛСКО МЕНЮ */
        .st-key-parent_nav div[data-testid="stHorizontalBlock"] {{
            gap: 8px !important;
        }}

        .st-key-parent_nav div[data-testid="stButton"] > button {{
            min-height: 44px !important;
            font-size: 0.78rem !important;
        }}

        .muted {{
            color: #d6cebf !important;
            font-size: 0.92rem;
        }}

        .status {{
            display: inline-block;
            padding: 5px 10px;
            border-radius: 999px;
            background: rgba(180, 138, 50, 0.25);
            border: 1px solid #d9ba65;
            color: #ffffff;
            font-weight: 800;
        }}

        @media (max-width: 768px) {{
            .block-container {{
                padding: 0.8rem 0.7rem 3rem;
            }}

            .hero {{
                padding: 16px;
            }}

            .st-key-parent_nav div[data-testid="stButton"] > button {{
                font-size: 0.70rem !important;
                padding: 4px !important;
            }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

apply_style()

def academy(): return fetchone("SELECT * FROM academies WHERE id=%s",(ACADEMY_ID,))
def current_user():
    uid=st.session_state.get("user_id")
    return fetchone("SELECT * FROM users WHERE id=%s AND academy_id=%s",(uid,ACADEMY_ID)) if uid else None

def hero():
    with st.container():
        st.markdown('<div class="hero">',unsafe_allow_html=True)
        if ASSETS["logo"].exists(): st.image(str(ASSETS["logo"]),width=190)
        st.markdown("# DYULGEROV ACADEMY")
        st.caption("Където успехът е танц на таланта")
        st.markdown('</div>',unsafe_allow_html=True)

def public_home():
    # Стартовата страница показва само идентичността на академията.
    # Информацията от администратора е достъпна след вход в родителския профил.
    hero()


def parent_information():
    st.subheader("ℹ️ Информация от академията")
    posts = fetchall(
        "SELECT * FROM public_posts WHERE academy_id=%s AND published ORDER BY created_at DESC LIMIT 50",
        (ACADEMY_ID,),
    )
    if not posts:
        st.info("Все още няма публикувана информация от академията.")
        return
    for post in posts:
        with st.container(border=True):
            if post.get("image_data"):
                st.image(bytes(post["image_data"]), use_container_width=True)
            elif post.get("media_url"):
                if post["media_type"] == "video":
                    st.video(post["media_url"])
                else:
                    st.image(post["media_url"], use_container_width=True)
            st.subheader(post["title"])
            st.write(post.get("body") or "")
            st.caption(post["created_at"].strftime("%d.%m.%Y %H:%M"))

def register_parent():
    st.subheader("Регистрация на родител")
    child_count=st.number_input("Брой деца за добавяне сега",1,5,1)
    with st.form("register_parent_form"):
        c1,c2=st.columns(2); first=c1.text_input("Име"); last=c2.text_input("Фамилия")
        email=c1.text_input("Имейл").strip().lower(); phone=c2.text_input("Телефон")
        pw=c1.text_input("Парола",type="password"); pw2=c2.text_input("Повтори паролата",type="password")
        children=[]
        for i in range(int(child_count)):
            st.markdown(f"**Дете {i+1}**")
            a,b,c=st.columns(3)
            children.append((a.text_input("Име",key=f"cf{i}"),b.text_input("Фамилия",key=f"cl{i}"),c.date_input("Дата на раждане",date(2018,1,1),key=f"cb{i}")))
        submit=st.form_submit_button("Изпрати регистрация",use_container_width=True)
    if submit:
        if not all([first.strip(),last.strip(),email,pw]) or pw!=pw2 or len(pw)<8: st.error("Провери данните и паролата."); return
        if any(not x[0].strip() or not x[1].strip() for x in children): st.error("Попълни имената на всяко дете."); return
        try:
            parent=execute("""INSERT INTO users(academy_id,first_name,last_name,email,phone,password_hash,role,account_status)
                VALUES(%s,%s,%s,%s,%s,%s,'parent','pending') RETURNING id""",(ACADEMY_ID,first.strip(),last.strip(),email,phone.strip(),hash_password(pw)),True)
            for cf,cl,bd in children:
                player=execute("INSERT INTO players(academy_id,first_name,last_name,birth_date) VALUES(%s,%s,%s,%s) RETURNING id",(ACADEMY_ID,cf.strip(),cl.strip(),bd),True)
                execute("INSERT INTO parent_players(academy_id,parent_user_id,player_id) VALUES(%s,%s,%s)",(ACADEMY_ID,parent["id"],player["id"]))
            st.success("Регистрацията е изпратена. Администраторът трябва да одобри профила и да свърже децата с група.")
        except Exception as exc: st.error("Имейлът вече е използван или данните са невалидни.")

def login():
    with st.form("login_form"):
        email=st.text_input("Имейл").strip().lower(); pw=st.text_input("Парола",type="password")
        go=st.form_submit_button("Вход",use_container_width=True)
    if go:
        user=fetchone("SELECT * FROM users WHERE academy_id=%s AND lower(email)=lower(%s)",(ACADEMY_ID,email))
        if not user or user["password_hash"]!=hash_password(pw): st.error("Невалиден имейл или парола.")
        elif not user["active"]: st.error("Профилът е деактивиран.")
        elif user["account_status"]!="approved": st.warning("Профилът очаква одобрение от академията.")
        else: st.session_state.user_id=user["id"]; st.rerun()

def parent_nav():
    items=[("🏠 Начало","Начало"),("📅 Тренировки","Тренировки"),("⚽ Мачове","Мачове"),("💬 Съобщения","Съобщения"),("💰 Такси","Такси"),("💳 Плащане с карта","Карта"),("ℹ️ Информация","Информация"),("👤 Настройки","Настройки")]
    st.session_state.setdefault("parent_page","Начало")
    with st.container(key="parent_nav"):
        for start in range(0,len(items),2):
            cols=st.columns(2)
            for col,(label,page) in zip(cols,items[start:start+2]):
                if col.button(label,key=f"pnav_{page}",use_container_width=True,type="primary" if st.session_state.parent_page==page else "secondary"):
                    st.session_state.parent_page=page; st.rerun()

def parent_players(user):
    return fetchall("""SELECT p.*,t.name team_name,c.display_name coach_name FROM parent_players pp
        JOIN players p ON p.id=pp.player_id LEFT JOIN teams t ON t.id=p.team_id LEFT JOIN coaches c ON c.id=t.coach_id
        WHERE pp.academy_id=%s AND pp.parent_user_id=%s AND p.active ORDER BY p.first_name""",(ACADEMY_ID,user["id"]))

def parent_panel(user):
    hero(); parent_nav(); players=parent_players(user)
    if not players: st.info("Няма добавени деца към профила."); return
    labels={f"{p['first_name']} {p['last_name']}":p for p in players}; chosen=st.selectbox("Избрано дете",list(labels)); child=labels[chosen]
    st.caption(f"Група: {child.get('team_name') or 'Очаква свързване'} · Треньор: {child.get('coach_name') or 'Очаква свързване'}")
    page=st.session_state.parent_page
    if child["approval_status"]!="approved": st.warning("Детето очаква одобрение и свързване с група от администратор.")
    if page=="Начало":
        latest_info = fetchone(
            "SELECT * FROM public_posts WHERE academy_id=%s AND published ORDER BY created_at DESC LIMIT 1",
            (ACADEMY_ID,),
        )
        c1,c2,c3=st.columns(3)
        c1.metric("Актуална информация", "1" if latest_info else "0")
        c2.metric("Набор",child.get("team_name") or "—")
        c3.metric("Такса",f"{academy()['default_fee']:.2f} EUR")
        if latest_info:
            with st.container(border=True):
                st.subheader(latest_info["title"])
                st.write(latest_info.get("body") or "")
                st.caption(latest_info["created_at"].strftime("%d.%m.%Y %H:%M"))
        else:
            st.info("Няма нова информация от академията.")
    elif page in ("Тренировки","Мачове"):
        typ="training" if page=="Тренировки" else "match"
        rows=fetchall("SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND event_type=%s ORDER BY starts_at ASC LIMIT 40",(ACADEMY_ID,child.get("team_id"),typ)) if child.get("team_id") else []
        for r in rows:
            with st.container(border=True):
                st.write(f"**{r['title']}** · {r['starts_at']:%d.%m.%Y %H:%M} · {r.get('location') or 'Без място'}")
                if typ == "match":
                    squad = fetchone(
                        "SELECT squad_status FROM match_squads WHERE academy_id=%s AND event_id=%s AND player_id=%s",
                        (ACADEMY_ID, r["id"], child["id"]),
                    )
                    if squad:
                        if squad["squad_status"] == "in_group":
                            st.success("🟢 Детето е в групата за този мач.")
                        else:
                            st.warning("🔴 Детето не е в групата за този мач.")
                    else:
                        st.caption("Треньорът още не е публикувал група за този мач.")
        if not rows: st.info("Няма добавени записи.")
    elif page=="Съобщения":
        rows=fetchall("SELECT * FROM team_messages WHERE academy_id=%s AND team_id=%s ORDER BY created_at DESC",(ACADEMY_ID,child.get("team_id"))) if child.get("team_id") else []
        for r in rows:
            with st.container(border=True): st.subheader(r["title"]); st.write(r["body"]); st.caption(r["created_at"].strftime("%d.%m.%Y %H:%M"))
        if not rows: st.info("Няма съобщения.")
    elif page=="Такси":
        rows=fetchall("SELECT * FROM player_fees WHERE academy_id=%s AND player_id=%s ORDER BY fee_year DESC,fee_month DESC",(ACADEMY_ID,child["id"]))
        for r in rows: st.write(f"{r['fee_month']:02d}.{r['fee_year']} · **{r['final_amount']:.2f} EUR** · {r['status']} · {r.get('adjustment_reason') or ''}")
        if not rows: st.info("Няма начислени такси.")
    elif page=="Карта": st.warning("Плащането с карта е подготвено като модул и очаква избор на платежен доставчик и договор.")
    elif page=="Информация": parent_information()
    else:
        st.subheader("Профил и деца")
        with st.form("add_child_later"):
            a,b,c=st.columns(3); cf=a.text_input("Име на ново дете"); cl=b.text_input("Фамилия"); bd=c.date_input("Дата на раждане",date(2018,1,1))
            add=st.form_submit_button("Добави дете",use_container_width=True)
        if add and cf.strip() and cl.strip():
            p=execute("INSERT INTO players(academy_id,first_name,last_name,birth_date) VALUES(%s,%s,%s,%s) RETURNING id",(ACADEMY_ID,cf.strip(),cl.strip(),bd),True)
            execute("INSERT INTO parent_players(academy_id,parent_user_id,player_id) VALUES(%s,%s,%s)",(ACADEMY_ID,user["id"],p["id"])); st.success("Детето е добавено и очаква одобрение."); st.rerun()

def save_events_for_dates(user_id, team_id, event_type, title, dates, event_time, location, description):
    # Всяко ново публикуване става единствената актуална програма за този набор и тип.
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "DELETE FROM events WHERE academy_id=%s AND team_id=%s AND event_type=%s",
            (ACADEMY_ID, team_id, event_type),
        )
        created = 0
        for event_date in dates:
            cur.execute(
                """INSERT INTO events(academy_id,team_id,event_type,title,starts_at,location,description,created_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (ACADEMY_ID, team_id, event_type, title.strip(), datetime.combine(event_date, event_time),
                 location.strip(), description.strip(), user_id),
            )
            created += 1
    return created

def multi_date_event_form(form_key, teams, user_id, fixed_team_id=None):
    tmap = {t["name"]: t["id"] for t in teams}
    count = st.number_input("Брой календарни дати", 1, 15, 1, key=f"{form_key}_count")
    with st.form(form_key):
        typ = st.radio("Тип", ["training", "match"],
                       format_func=lambda x: "Тренировка" if x == "training" else "Мач",
                       horizontal=True, key=f"{form_key}_type")
        if fixed_team_id is None:
            team_name = st.selectbox("Набор", list(tmap), key=f"{form_key}_team")
            team_id = tmap[team_name]
        else:
            team_id = fixed_team_id
        title = st.text_input("Заглавие", key=f"{form_key}_title")
        event_dates = []
        for index in range(int(count)):
            event_dates.append(st.date_input(f"Дата {index + 1}", date.today() + timedelta(days=index), key=f"{form_key}_date_{index}"))
        event_time = st.time_input("Час", key=f"{form_key}_time")
        location = st.text_input("Място", key=f"{form_key}_location")
        description = st.text_area("Описание", key=f"{form_key}_description")
        save = st.form_submit_button("Публикувай всички дати", use_container_width=True)
    if save:
        if not title.strip():
            st.error("Въведи заглавие.")
        elif len(set(event_dates)) != len(event_dates):
            st.error("Има повторена дата. Избери различни календарни дни.")
        else:
            created = save_events_for_dates(user_id, team_id, typ, title, sorted(event_dates), event_time, location, description)
            st.success(f"Създадени са {created} събития.")
            st.rerun()


def coach_account_manager():
    coaches = fetchall("""SELECT c.*,u.email,u.account_status,u.active user_active
                          FROM coaches c LEFT JOIN users u ON u.id=c.user_id
                          WHERE c.academy_id=%s AND c.active ORDER BY c.id""", (ACADEMY_ID,))
    st.caption("Всеки треньор използва собствен имейл и парола. Един треньор може да води повече от един набор.")
    for coach in coaches:
        with st.expander(f"{coach['display_name']} · {coach.get('email') or 'без достъп'}"):
            name = st.text_input("Име на треньора", coach["display_name"], key=f"coach_name_{coach['id']}")
            email = st.text_input("Имейл за вход", coach.get("email") or "", key=f"coach_email_{coach['id']}").strip().lower()
            password = st.text_input("Нова / начална парола", type="password", key=f"coach_pw_{coach['id']}")
            c1, c2 = st.columns(2)
            if c1.button("Запази име", key=f"coach_save_name_{coach['id']}", use_container_width=True):
                execute("UPDATE coaches SET display_name=%s WHERE id=%s AND academy_id=%s", (name.strip(), coach["id"], ACADEMY_ID))
                st.rerun()
            if c2.button("Създай / обнови достъп", key=f"coach_access_{coach['id']}", use_container_width=True):
                if not email or len(password) < 8:
                    st.error("Въведи валиден имейл и парола с минимум 8 символа.")
                else:
                    existing = fetchone("SELECT id FROM users WHERE academy_id=%s AND lower(email)=lower(%s)", (ACADEMY_ID, email))
                    if existing:
                        user_id = existing["id"]
                        execute("""UPDATE users SET first_name=%s,last_name='Треньор',password_hash=%s,
                                   role='coach',account_status='approved',active=TRUE WHERE id=%s AND academy_id=%s""",
                                (name.strip(), hash_password(password), user_id, ACADEMY_ID))
                    else:
                        row = execute("""INSERT INTO users(academy_id,first_name,last_name,email,password_hash,role,account_status)
                                         VALUES(%s,%s,'Треньор',%s,%s,'coach','approved') RETURNING id""",
                                      (ACADEMY_ID, name.strip(), email, hash_password(password)), True)
                        user_id = row["id"]
                    execute("UPDATE coaches SET user_id=%s,display_name=%s WHERE id=%s AND academy_id=%s",
                            (user_id, name.strip(), coach["id"], ACADEMY_ID))
                    st.success("Треньорският достъп е готов.")
                    st.rerun()


def admin_panel(user):
    st.title("Административен панел")
    sections = ["Табло", "Регистрации", "Деца и групи", "Треньори и набори", "Роли и права",
                "Тренировки и мачове", "Съобщения", "Такси", "Информация"]
    sec = st.sidebar.radio("Админ меню", sections)

    if sec == "Табло":
        vals = [
            fetchone("SELECT COUNT(*) c FROM users WHERE academy_id=%s AND account_status='pending'", (ACADEMY_ID,))["c"],
            fetchone("SELECT COUNT(*) c FROM players WHERE academy_id=%s AND approval_status='pending'", (ACADEMY_ID,))["c"],
            fetchone("SELECT COUNT(*) c FROM teams WHERE academy_id=%s AND active", (ACADEMY_ID,))["c"],
        ]
        for col, label, value in zip(st.columns(3), ["Чакащи родители", "Чакащи деца", "Активни набори"], vals):
            col.metric(label, value)

    elif sec == "Регистрации":
        pending, archive = st.tabs(["🔴 Очакващи", "🟢 Архив потвърдени"])
        with pending:
            rows = fetchall("SELECT * FROM users WHERE academy_id=%s AND role='parent' AND account_status='pending' ORDER BY created_at", (ACADEMY_ID,))
            if not rows: st.info("Няма чакащи регистрации.")
            for r in rows:
                c1, c2, c3 = st.columns([5, 1, 1])
                c1.write(f"🔴 **{r['first_name']} {r['last_name']}** · {r['email']}")
                if c2.button("Одобри", key=f"au{r['id']}"):
                    execute("UPDATE users SET account_status='approved' WHERE id=%s AND academy_id=%s", (r["id"], ACADEMY_ID)); st.rerun()
                if c3.button("Откажи", key=f"ar{r['id']}"):
                    execute("UPDATE users SET account_status='rejected' WHERE id=%s AND academy_id=%s", (r["id"], ACADEMY_ID)); st.rerun()
        with archive:
            rows = fetchall("SELECT * FROM users WHERE academy_id=%s AND role='parent' AND account_status='approved' ORDER BY created_at DESC", (ACADEMY_ID,))
            if not rows: st.info("Архивът е празен.")
            for r in rows: st.write(f"🟢 **{r['first_name']} {r['last_name']}** · {r['email']}")

    elif sec == "Деца и групи":
        teams = fetchall("SELECT t.*,c.display_name FROM teams t LEFT JOIN coaches c ON c.id=t.coach_id WHERE t.academy_id=%s AND t.active ORDER BY birth_year", (ACADEMY_ID,))
        tmap = {f"{t['name']} · {t.get('display_name') or 'Без треньор'}": t for t in teams}
        pending_tab, archive_tab = st.tabs(["🔴 Очакващи деца", "🟢 Архив потвърдени"])
        with pending_tab:
            players = fetchall("SELECT p.*,t.name team_name FROM players p LEFT JOIN teams t ON t.id=p.team_id WHERE p.academy_id=%s AND p.active AND p.approval_status='pending' ORDER BY p.created_at", (ACADEMY_ID,))
            if not players: st.info("Няма деца, които чакат потвърждение.")
            for player in players:
                with st.expander(f"🔴 {player['first_name']} {player['last_name']} · Очаква потвърждение"):
                    choice = st.selectbox("Група и треньор", list(tmap), key=f"pending_tm{player['id']}")
                    if st.button("Потвърди и свържи", key=f"pending_mv{player['id']}", use_container_width=True):
                        execute("UPDATE players SET team_id=%s,approval_status='approved' WHERE id=%s AND academy_id=%s", (tmap[choice]["id"], player["id"], ACADEMY_ID)); st.rerun()
        with archive_tab:
            players = fetchall("""SELECT p.*,t.name team_name,c.display_name coach_name FROM players p
                                LEFT JOIN teams t ON t.id=p.team_id LEFT JOIN coaches c ON c.id=t.coach_id
                                WHERE p.academy_id=%s AND p.active AND p.approval_status='approved'
                                ORDER BY t.birth_year,p.first_name""", (ACADEMY_ID,))
            if not players: st.info("Няма потвърдени деца.")
            for player in players:
                with st.expander(f"🟢 {player['first_name']} {player['last_name']} · {player.get('team_name') or 'Без група'} · {player.get('coach_name') or 'Без треньор'}"):
                    choice = st.selectbox("Премести в друга група / при друг треньор", list(tmap), key=f"archive_tm{player['id']}")
                    if st.button("Запази промяната", key=f"archive_mv{player['id']}", use_container_width=True):
                        execute("UPDATE players SET team_id=%s WHERE id=%s AND academy_id=%s", (tmap[choice]["id"], player["id"], ACADEMY_ID)); st.rerun()

                    st.markdown("---")
                    st.markdown("#### 🗑️ Окончателно изтриване")
                    st.warning("Това действие е необратимо. Ще бъдат изтрити детето и свързаните с него такси, присъствия и връзки с родителски профили.")
                    delete_confirmation = st.text_input(
                        'За потвърждение напиши точно ИЗТРИЙ',
                        key=f"delete_confirm_{player['id']}",
                        placeholder="ИЗТРИЙ",
                    )
                    if st.button(
                        "🗑️ Изтрий окончателно",
                        key=f"delete_player_{player['id']}",
                        use_container_width=True,
                    ):
                        if delete_confirmation.strip() != "ИЗТРИЙ":
                            st.error('Окончателното изтриване не е изпълнено. Напиши точно „ИЗТРИЙ“.')
                        else:
                            with db() as conn, conn.cursor() as cur:
                                cur.execute("SELECT id FROM players WHERE id=%s AND academy_id=%s FOR UPDATE", (player["id"], ACADEMY_ID))
                                if not cur.fetchone():
                                    st.error("Детето вече не съществува или не принадлежи към тази академия.")
                                else:
                                    cur.execute("DELETE FROM attendance WHERE academy_id=%s AND player_id=%s", (ACADEMY_ID, player["id"]))
                                    cur.execute("DELETE FROM player_fees WHERE academy_id=%s AND player_id=%s", (ACADEMY_ID, player["id"]))
                                    cur.execute("DELETE FROM parent_players WHERE academy_id=%s AND player_id=%s", (ACADEMY_ID, player["id"]))
                                    cur.execute("DELETE FROM players WHERE academy_id=%s AND id=%s", (ACADEMY_ID, player["id"]))
                            st.success(f"{player['first_name']} {player['last_name']} е изтрит/а окончателно.")
                            st.rerun()

    elif sec == "Треньори и набори":
        coach_account_manager()
        st.divider()
        coaches = fetchall("SELECT * FROM coaches WHERE academy_id=%s AND active ORDER BY id", (ACADEMY_ID,))
        cmap = {c["display_name"]: c["id"] for c in coaches}
        for team in fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year", (ACADEMY_ID,)):
            a, b = st.columns([3, 2]); a.write(f"**{team['name']}**")
            names = ["Без треньор"] + list(cmap)
            current_row = fetchone("SELECT display_name FROM coaches WHERE id=%s", (team.get("coach_id"),)) if team.get("coach_id") else None
            current_name = current_row["display_name"] if current_row else "Без треньор"
            selected = b.selectbox("Треньор", names, index=names.index(current_name) if current_name in names else 0, key=f"tc{team['id']}")
            if st.button("Запази групата", key=f"ts{team['id']}"):
                execute("UPDATE teams SET coach_id=%s WHERE id=%s AND academy_id=%s", (None if selected == "Без треньор" else cmap[selected], team["id"], ACADEMY_ID)); st.rerun()

    elif sec == "Роли и права":
        st.caption("Admin има пълен достъп. Coach вижда само своите набори. Parent вижда само своите деца.")
        users = fetchall("SELECT * FROM users WHERE academy_id=%s ORDER BY role,first_name,last_name", (ACADEMY_ID,))
        for account in users:
            with st.expander(f"{account['first_name']} {account['last_name']} · {account['email']} · {account['role']}"):
                roles = ["parent", "coach", "admin"]
                role = st.selectbox("Роля", roles, index=roles.index(account["role"]) if account["role"] in roles else 0, key=f"role_{account['id']}")
                active = st.checkbox("Активен профил", value=account["active"], key=f"active_{account['id']}")
                if st.button("Запази права", key=f"rights_{account['id']}", use_container_width=True):
                    if account["id"] == user["id"] and (role != "admin" or not active):
                        st.error("Не можеш да премахнеш собствените си Admin права или да деактивираш текущия профил.")
                    else:
                        execute("UPDATE users SET role=%s,active=%s WHERE id=%s AND academy_id=%s", (role, active, account["id"], ACADEMY_ID)); st.rerun()

                st.markdown("#### 🔑 Смяна на парола")
                new_password = st.text_input(
                    "Нова парола",
                    type="password",
                    key=f"new_password_{account['id']}",
                    placeholder="Минимум 8 символа",
                )
                repeat_password = st.text_input(
                    "Повтори новата парола",
                    type="password",
                    key=f"repeat_password_{account['id']}",
                )
                if st.button("🔑 Смени паролата", key=f"change_password_{account['id']}", use_container_width=True):
                    if len(new_password) < 8:
                        st.error("Новата парола трябва да бъде минимум 8 символа.")
                    elif new_password != repeat_password:
                        st.error("Двете пароли не съвпадат.")
                    else:
                        execute(
                            "UPDATE users SET password_hash=%s WHERE id=%s AND academy_id=%s",
                            (hash_password(new_password), account["id"], ACADEMY_ID),
                        )
                        st.success(f"Паролата за {account['email']} е сменена успешно.")


                st.markdown("---")
                st.markdown("#### 🗑️ Окончателно изтриване на профила")
                if account["id"] == user["id"] or account["email"].strip().lower() == ADMIN_EMAIL:
                    st.info("Текущият и основният Admin профил са защитени и не могат да бъдат изтрити от този екран.")
                else:
                    st.warning(
                        "Действието е необратимо. Coach достъпът ще бъде освободен. "
                        "При Parent ще бъдат премахнати връзките с децата, а деца без друг родител ще бъдат изтрити заедно с таксите и присъствията им."
                    )
                    delete_user_confirmation = st.text_input(
                        'За потвърждение напиши точно ИЗТРИЙ',
                        key=f"delete_user_confirm_{account['id']}",
                        placeholder="ИЗТРИЙ",
                    )
                    if st.button(
                        "🗑️ Изтрий потребителския профил",
                        key=f"delete_user_{account['id']}",
                        use_container_width=True,
                    ):
                        if delete_user_confirmation.strip() != "ИЗТРИЙ":
                            st.error('Профилът не е изтрит. Напиши точно „ИЗТРИЙ“.')
                        else:
                            with db() as conn, conn.cursor() as cur:
                                cur.execute(
                                    "SELECT id,role,email FROM users WHERE id=%s AND academy_id=%s FOR UPDATE",
                                    (account["id"], ACADEMY_ID),
                                )
                                target = cur.fetchone()
                                if not target:
                                    st.error("Профилът вече не съществува.")
                                elif target[0] == user["id"] or target[2].strip().lower() == ADMIN_EMAIL:
                                    st.error("Основният Admin профил е защитен.")
                                else:
                                    target_id = target[0]
                                    cur.execute(
                                        "SELECT player_id FROM parent_players WHERE academy_id=%s AND parent_user_id=%s",
                                        (ACADEMY_ID, target_id),
                                    )
                                    linked_players = [row[0] for row in cur.fetchall()]

                                    cur.execute(
                                        "UPDATE coaches SET user_id=NULL WHERE academy_id=%s AND user_id=%s",
                                        (ACADEMY_ID, target_id),
                                    )
                                    cur.execute(
                                        "UPDATE events SET created_by=NULL WHERE academy_id=%s AND created_by=%s",
                                        (ACADEMY_ID, target_id),
                                    )
                                    cur.execute(
                                        "UPDATE team_messages SET sender_user_id=NULL WHERE academy_id=%s AND sender_user_id=%s",
                                        (ACADEMY_ID, target_id),
                                    )
                                    cur.execute(
                                        "UPDATE public_posts SET created_by=NULL WHERE academy_id=%s AND created_by=%s",
                                        (ACADEMY_ID, target_id),
                                    )
                                    cur.execute(
                                        "UPDATE attendance SET marked_by=NULL WHERE academy_id=%s AND marked_by=%s",
                                        (ACADEMY_ID, target_id),
                                    )
                                    cur.execute(
                                        "DELETE FROM parent_players WHERE academy_id=%s AND parent_user_id=%s",
                                        (ACADEMY_ID, target_id),
                                    )

                                    for player_id in linked_players:
                                        cur.execute(
                                            "SELECT COUNT(*) FROM parent_players WHERE academy_id=%s AND player_id=%s",
                                            (ACADEMY_ID, player_id),
                                        )
                                        if cur.fetchone()[0] == 0:
                                            cur.execute("DELETE FROM attendance WHERE academy_id=%s AND player_id=%s", (ACADEMY_ID, player_id))
                                            cur.execute("DELETE FROM player_fees WHERE academy_id=%s AND player_id=%s", (ACADEMY_ID, player_id))
                                            cur.execute("DELETE FROM players WHERE academy_id=%s AND id=%s", (ACADEMY_ID, player_id))

                                    cur.execute(
                                        "DELETE FROM users WHERE academy_id=%s AND id=%s",
                                        (ACADEMY_ID, target_id),
                                    )
                            st.success(f"Профилът {account['email']} е изтрит окончателно.")
                            st.rerun()

    elif sec == "Тренировки и мачове":
        teams = fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year", (ACADEMY_ID,))
        multi_date_event_form("admin_event_form", teams, user["id"])

    elif sec == "Съобщения":
        teams = fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year", (ACADEMY_ID,)); tmap = {t["name"]: t["id"] for t in teams}
        with st.form("msg_form"):
            team = st.selectbox("Набор", list(tmap)); title = st.text_input("Заглавие"); body = st.text_area("Съобщение"); send = st.form_submit_button("Изпрати към групата", use_container_width=True)
        if send and title.strip() and body.strip():
            execute("INSERT INTO team_messages(academy_id,team_id,sender_user_id,title,body) VALUES(%s,%s,%s,%s,%s)", (ACADEMY_ID,tmap[team],user["id"],title.strip(),body.strip())); st.rerun()

    elif sec == "Такси":
        teams = fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year", (ACADEMY_ID,)); tmap = {t["name"]: t for t in teams}; team_name = st.selectbox("Набор", list(tmap)); team = tmap[team_name]
        default = fetchone("SELECT amount FROM fee_templates WHERE academy_id=%s AND team_id=%s", (ACADEMY_ID,team["id"])); amount = st.number_input("Стандартна сума EUR",0.0,1000.0,float(default["amount"] if default else 50),.01); month = st.selectbox("Месец",range(1,13),index=date.today().month-1); year = st.number_input("Година",2024,2045,date.today().year)
        players = fetchall("SELECT * FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' ORDER BY first_name", (ACADEMY_ID,team["id"])); selected = {}
        for player in players:
            c1,c2,c3=st.columns([1,4,2]); use=c1.checkbox("",True,key=f"fu{player['id']}"); c2.write(f"{player['first_name']} {player['last_name']}"); custom=c3.number_input("Индивидуална",0.0,1000.0,float(amount),.01,key=f"fa{player['id']}"); selected[player["id"]]=(use,custom)
        reason=st.text_input("Основание за индивидуални корекции",placeholder="Отстъпка за второ/трето дете или форсмажор")
        if st.button("Начисли на маркираните деца",type="primary",use_container_width=True):
            execute("INSERT INTO fee_templates(academy_id,team_id,amount) VALUES(%s,%s,%s) ON CONFLICT(academy_id,team_id) DO UPDATE SET amount=EXCLUDED.amount",(ACADEMY_ID,team["id"],amount))
            for pid,(use,custom) in selected.items():
                if use: execute("""INSERT INTO player_fees(academy_id,player_id,team_id,fee_year,fee_month,standard_amount,final_amount,adjustment_reason,custom_status,due_date)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(player_id,fee_year,fee_month) DO UPDATE SET standard_amount=EXCLUDED.standard_amount,final_amount=EXCLUDED.final_amount,adjustment_reason=EXCLUDED.adjustment_reason,custom_status=EXCLUDED.custom_status""",(ACADEMY_ID,pid,team["id"],int(year),month,amount,custom,reason.strip() or None,"force_majeure" if custom!=amount else None,date(int(year),month,28)))
            st.success("Таксите са начислени."); st.rerun()

    else:
        with st.form("post_form"):
            title=st.text_input("Заглавие"); body=st.text_area("Текст"); typ=st.radio("Медия",["image","video"],format_func=lambda x:"Снимка" if x=="image" else "Видео линк",horizontal=True); url=st.text_input("URL за видео/снимка"); upload=st.file_uploader("Или качи снимка",type=["jpg","jpeg","png","webp"]); send=st.form_submit_button("Публикувай в родителска информация",use_container_width=True)
        if send and title.strip():
            data=upload.getvalue() if upload else None; mime=upload.type if upload else None
            with db() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM public_posts WHERE academy_id=%s", (ACADEMY_ID,))
                cur.execute(
                    """INSERT INTO public_posts(academy_id,title,body,media_type,media_url,image_data,image_mime,created_by)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (ACADEMY_ID,title.strip(),body,typ,url.strip() or None,data,mime,user["id"]),
                )
            st.success("Новата информация е публикувана и предишната е заменена.")
            st.rerun()


def coach_panel(user):
    coach = fetchone("SELECT * FROM coaches WHERE academy_id=%s AND user_id=%s AND active", (ACADEMY_ID, user["id"]))
    st.title("Треньорски панел")
    if not coach:
        st.warning("Този Coach профил още не е свързан с треньор. Admin трябва да го свърже от „Треньори и набори“.")
        return
    teams = fetchall("SELECT * FROM teams WHERE academy_id=%s AND coach_id=%s AND active ORDER BY birth_year", (ACADEMY_ID, coach["id"]))
    if not teams:
        st.info("Няма възложени набори към този треньор.")
        return
    st.sidebar.caption(coach["display_name"])
    sections = ["Табло", "Моите деца", "Тренировки и мачове", "Група за мача", "Присъствие", "Съобщения"]
    sec = st.sidebar.radio("Треньорско меню", sections)
    tmap = {t["name"]: t for t in teams}
    team_name = st.selectbox("Избран набор", list(tmap), key="coach_selected_team")
    team = tmap[team_name]

    if sec == "Табло":
        children = fetchone("SELECT COUNT(*) c FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' AND active", (ACADEMY_ID, team["id"]))["c"]
        events = fetchone("SELECT COUNT(*) c FROM events WHERE academy_id=%s AND team_id=%s AND starts_at>=NOW()", (ACADEMY_ID, team["id"]))["c"]
        c1,c2 = st.columns(2); c1.metric("Деца в групата", children); c2.metric("Предстоящи събития", events)
    elif sec == "Моите деца":
        players = fetchall("SELECT * FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' AND active ORDER BY first_name,last_name", (ACADEMY_ID, team["id"]))
        for player in players: st.write(f"🟢 **{player['first_name']} {player['last_name']}**")
        if not players: st.info("Няма потвърдени деца в този набор.")
    elif sec == "Тренировки и мачове":
        multi_date_event_form("coach_event_form", teams, user["id"], fixed_team_id=team["id"])
    elif sec == "Група за мача":
        matches = fetchall(
            "SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND event_type='match' ORDER BY starts_at ASC",
            (ACADEMY_ID, team["id"]),
        )
        if not matches:
            st.info("Няма публикувани мачове за този набор.")
        else:
            mmap = {f"{m['starts_at']:%d.%m.%Y %H:%M} · {m['title']}": m for m in matches}
            match_label = st.selectbox("Избери мач", list(mmap), key="squad_match")
            match = mmap[match_label]
            players = fetchall(
                "SELECT * FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' AND active ORDER BY first_name,last_name",
                (ACADEMY_ID, team["id"]),
            )
            existing = {
                row["player_id"]: row["squad_status"]
                for row in fetchall(
                    "SELECT player_id,squad_status FROM match_squads WHERE academy_id=%s AND event_id=%s",
                    (ACADEMY_ID, match["id"]),
                )
            }
            st.caption("Използването на този списък е по желание на треньора и според договорката с родителите.")
            with st.form(f"match_squad_{match['id']}"):
                choices = {}
                values = ["in_group", "not_in_group"]
                for player in players:
                    current = existing.get(player["id"], "not_in_group")
                    choices[player["id"]] = st.selectbox(
                        f"{player['first_name']} {player['last_name']}",
                        values,
                        index=values.index(current),
                        format_func=lambda x: "В групата" if x == "in_group" else "Не е в групата",
                        key=f"squad_{match['id']}_{player['id']}",
                    )
                publish_squad = st.form_submit_button("Изпрати групата към родителите", use_container_width=True)
            if publish_squad:
                with db() as conn, conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM match_squads WHERE academy_id=%s AND event_id=%s",
                        (ACADEMY_ID, match["id"]),
                    )
                    for player_id, squad_status in choices.items():
                        cur.execute(
                            """INSERT INTO match_squads(academy_id,event_id,player_id,squad_status,selected_by)
                               VALUES(%s,%s,%s,%s,%s)""",
                            (ACADEMY_ID, match["id"], player_id, squad_status, user["id"]),
                        )
                st.success("Групата за мача е изпратена към родителите.")
                st.rerun()

    elif sec == "Присъствие":
        events = fetchall("SELECT * FROM events WHERE academy_id=%s AND team_id=%s ORDER BY starts_at DESC LIMIT 40", (ACADEMY_ID, team["id"]))
        if not events: st.info("Първо създай тренировка или мач.")
        else:
            emap = {f"{e['starts_at']:%d.%m.%Y %H:%M} · {e['title']}": e for e in events}
            event_label = st.selectbox("Събитие", list(emap)); event = emap[event_label]
            players = fetchall("SELECT * FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' AND active ORDER BY first_name", (ACADEMY_ID, team["id"]))
            with st.form("attendance_form"):
                marks = {}
                for player in players:
                    marks[player["id"]] = st.selectbox(f"{player['first_name']} {player['last_name']}", ["present","absent","excused"], format_func=lambda x:{"present":"Присъства","absent":"Отсъства","excused":"Извинен"}[x], key=f"att_{event['id']}_{player['id']}")
                saved = st.form_submit_button("Запази присъствието", use_container_width=True)
            if saved:
                for player_id,status in marks.items():
                    execute("""INSERT INTO attendance(academy_id,event_id,player_id,status,marked_by) VALUES(%s,%s,%s,%s,%s)
                               ON CONFLICT(event_id,player_id) DO UPDATE SET status=EXCLUDED.status,marked_by=EXCLUDED.marked_by,marked_at=NOW()""",
                            (ACADEMY_ID,event["id"],player_id,status,user["id"]))
                st.success("Присъствието е запазено.")
    else:
        with st.form("coach_message_form"):
            title = st.text_input("Заглавие"); body = st.text_area("Съобщение към родителите"); send = st.form_submit_button("Изпрати към групата", use_container_width=True)
        if send and title.strip() and body.strip():
            execute("INSERT INTO team_messages(academy_id,team_id,sender_user_id,title,body) VALUES(%s,%s,%s,%s,%s)", (ACADEMY_ID,team["id"],user["id"],title.strip(),body.strip())); st.success("Съобщението е изпратено."); st.rerun()

def logout_button():
    if st.sidebar.button("Изход",use_container_width=True): st.session_state.clear(); st.rerun()

user = current_user()

if not user:
    public_home()

    tab1, tab2 = st.tabs([
        "Вход",
        "Регистрация на родител"
    ])

    with tab1:
        login()

    with tab2:
        register_parent()

    # ==================================================
    # КОДЕКС НА ЛЪВОВЕТЕ
    # ==================================================
    st.markdown("---")
    st.markdown(
        "<h2 style='text-align:center;'>Кодексът на лъвовете</h2>",
        unsafe_allow_html=True
    )

    if ASSETS["code"].exists():
        col_left, col_center, col_right = st.columns([1, 2.2, 1])

        with col_center:
            st.image(
                str(ASSETS["code"]),
                use_container_width=True
            )

else:
    logout_button()

    if user["role"] == "admin":
        admin_panel(user)
    elif user["role"] == "coach":
        coach_panel(user)
    else:
        parent_panel(user)
