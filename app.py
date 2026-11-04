import os
import io
import base64
import hashlib
import secrets
import pandas as pd
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

def avatar_html(image_bytes=None, mime_type=None, size=74):
    if image_bytes:
        mime_type = mime_type or "image/jpeg"
        encoded = base64.b64encode(bytes(image_bytes)).decode("utf-8")
        return f"""<div style="width:{size}px;height:{size}px;border-radius:50%;overflow:hidden;border:2px solid rgba(214,183,101,.85);background:#11151c;flex:0 0 {size}px"><img src="data:{mime_type};base64,{encoded}" style="width:100%;height:100%;object-fit:cover"></div>"""
    return f"""<div style="width:{size}px;height:{size}px;border-radius:50%;border:2px solid rgba(214,183,101,.65);background:linear-gradient(145deg,#11151c,#242a35);color:#9aa3b2;display:flex;align-items:center;justify-content:center;font-size:{int(size*.52)}px;flex:0 0 {size}px">👤</div>"""

def valid_profile_image(uploaded_file):
    if not uploaded_file: return None, None
    image_bytes = uploaded_file.getvalue()
    if len(image_bytes) > 5 * 1024 * 1024:
        st.error("Снимката е прекалено голяма. Максималният размер е 5 MB.")
        return None, None
    return image_bytes, uploaded_file.type

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
        """CREATE TABLE IF NOT EXISTS fan_chat_messages(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            sender_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL, image_data BYTEA, image_mime TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """ALTER TABLE fan_chat_messages ADD COLUMN IF NOT EXISTS image_data BYTEA""",
        """ALTER TABLE fan_chat_messages ADD COLUMN IF NOT EXISTS image_mime TEXT""",
        """CREATE INDEX IF NOT EXISTS idx_fan_chat_team ON fan_chat_messages(academy_id,team_id,created_at DESC)""",
        """CREATE TABLE IF NOT EXISTS feedback(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            parent_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            player_id BIGINT REFERENCES players(id) ON DELETE SET NULL,
            coach_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            feedback_type TEXT NOT NULL DEFAULT 'signal', rating INTEGER,
            subject TEXT NOT NULL, body TEXT NOT NULL,
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS notifications(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            notification_type TEXT NOT NULL, title TEXT NOT NULL, body TEXT,
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(academy_id,user_id,is_read,created_at DESC)""",
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
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS payment_method TEXT""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS payment_note TEXT""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS bank_transfer_date DATE""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS payment_recorded_by BIGINT REFERENCES users(id) ON DELETE SET NULL""",
        """CREATE TABLE IF NOT EXISTS polls(id BIGSERIAL PRIMARY KEY,academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,poll_type TEXT NOT NULL,title TEXT NOT NULL,question TEXT NOT NULL,team_id BIGINT REFERENCES teams(id) ON DELETE CASCADE,event_id BIGINT REFERENCES events(id) ON DELETE CASCADE,active BOOLEAN NOT NULL DEFAULT TRUE,created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS poll_responses(id BIGSERIAL PRIMARY KEY,academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,poll_id BIGINT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,player_id BIGINT REFERENCES players(id) ON DELETE CASCADE,answer TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_poll_response ON poll_responses(poll_id,user_id,COALESCE(player_id,0))""",
        """CREATE TABLE IF NOT EXISTS expenses(
            id BIGSERIAL PRIMARY KEY,
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            expense_date DATE NOT NULL DEFAULT CURRENT_DATE,
            category TEXT NOT NULL,
            amount NUMERIC(12,2) NOT NULL CHECK(amount >= 0),
            payment_method TEXT NOT NULL DEFAULT 'cash',
            description TEXT,
            created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE INDEX IF NOT EXISTS idx_expenses_academy_date ON expenses(academy_id,expense_date DESC)""",
        """CREATE TABLE IF NOT EXISTS login_sessions(
            id BIGSERIAL PRIMARY KEY,
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE INDEX IF NOT EXISTS idx_login_sessions_token ON login_sessions(token_hash,last_seen)""",
        """CREATE TABLE IF NOT EXISTS player_evaluations(
            id BIGSERIAL PRIMARY KEY,
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            coach_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
            evaluation_year INTEGER NOT NULL CHECK(evaluation_year BETWEEN 2020 AND 2100),
            evaluation_month INTEGER NOT NULL CHECK(evaluation_month BETWEEN 1 AND 12),
            technique INTEGER NOT NULL CHECK(technique BETWEEN 1 AND 5),
            tactics INTEGER NOT NULL CHECK(tactics BETWEEN 1 AND 5),
            physical INTEGER NOT NULL CHECK(physical BETWEEN 1 AND 5),
            teamwork INTEGER NOT NULL CHECK(teamwork BETWEEN 1 AND 5),
            discipline INTEGER NOT NULL CHECK(discipline BETWEEN 1 AND 5),
            effort INTEGER NOT NULL CHECK(effort BETWEEN 1 AND 5),
            coach_note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(player_id,evaluation_year,evaluation_month))""",
        """CREATE INDEX IF NOT EXISTS idx_player_evaluations_period ON player_evaluations(academy_id,player_id,evaluation_year DESC,evaluation_month DESC)""",
        """ALTER TABLE players ADD COLUMN IF NOT EXISTS profile_image BYTEA""",
        """ALTER TABLE players ADD COLUMN IF NOT EXISTS profile_image_mime TEXT""",
        """ALTER TABLE coaches ADD COLUMN IF NOT EXISTS profile_image BYTEA""",
        """ALTER TABLE coaches ADD COLUMN IF NOT EXISTS profile_image_mime TEXT""",
        """ALTER TABLE academies ADD COLUMN IF NOT EXISTS parent_help_video_url TEXT""",
        """ALTER TABLE players ADD COLUMN IF NOT EXISTS position TEXT""",
        """ALTER TABLE teams ADD COLUMN IF NOT EXISTS standings_url TEXT""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS confirmation_status TEXT NOT NULL DEFAULT 'unreported'""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS parent_reported_at TIMESTAMPTZ""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS confirmed_by BIGINT REFERENCES users(id) ON DELETE SET NULL""",
        """CREATE TABLE IF NOT EXISTS match_reports(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            opponent_name TEXT NOT NULL DEFAULT 'Друг отбор', home_score INTEGER NOT NULL DEFAULT 0 CHECK(home_score>=0),
            away_score INTEGER NOT NULL DEFAULT 0 CHECK(away_score>=0), standings_url TEXT, confirmed BOOLEAN NOT NULL DEFAULT FALSE,
            confirmed_by BIGINT REFERENCES users(id) ON DELETE SET NULL, confirmed_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(event_id))""",
        """CREATE TABLE IF NOT EXISTS match_lineups(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            pitch_slot TEXT NOT NULL, updated_by BIGINT REFERENCES users(id) ON DELETE SET NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(event_id,player_id), UNIQUE(event_id,pitch_slot))""",
        """CREATE TABLE IF NOT EXISTS match_player_stats(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            goals INTEGER NOT NULL DEFAULT 0 CHECK(goals>=0), match_rating INTEGER CHECK(match_rating BETWEEN 1 AND 5), note TEXT,
            updated_by BIGINT REFERENCES users(id) ON DELETE SET NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(event_id,player_id))""",
        """ALTER TABLE player_evaluations ADD COLUMN IF NOT EXISTS match_bonus NUMERIC(4,2) NOT NULL DEFAULT 0""",
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
            --glass: rgba(8, 8, 8, 0.70);
            --glass-strong: rgba(5, 5, 5, 0.84);
        }}

        /* ОСНОВЕН ФОН */
        html,
        body,
        [data-testid="stAppViewContainer"],
        .stApp {{
            background-image:
                linear-gradient(
                    rgba(0, 0, 0, 0.68),
                    rgba(0, 0, 0, 0.82)
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

       /* ==================================================
       PREMIUM TABS
       ================================================== */

        div[data-baseweb="tab-list"] {{
            gap: 14px !important;
            margin-top: 18px !important;
            margin-bottom: 24px !important;
        }}

        button[data-baseweb="tab"] {{
            background: rgba(18, 15, 10, 0.90) !important;
            border: 1px solid rgba(214, 183, 101, 0.70) !important;
            border-radius: 14px !important;

            min-height: 54px !important;
            padding: 12px 28px !important;

            color: #ffffff !important;
            font-size: 1rem !important;
            font-weight: 800 !important;

            box-shadow: 0 8px 20px rgba(0, 0, 0, 0.36) !important;
        }}

        button[data-baseweb="tab"] p {{
            color: #ffffff !important;
            font-size: 1rem !important;
            font-weight: 800 !important;
        }}

        button[data-baseweb="tab"]:hover {{
            background: rgba(112, 81, 27, 0.92) !important;
            border-color: #efd27a !important;
        }}

        button[data-baseweb="tab"][aria-selected="true"] {{
            background: linear-gradient(
                135deg,
                #966b20 0%,
                #d6b454 52%,
                #a77824 100%
            ) !important;

            border: 1px solid #f2d67e !important;

            box-shadow:
                0 10px 26px rgba(0, 0, 0, 0.42),
                0 0 18px rgba(180, 138, 50, 0.30) !important;
        }}

        button[data-baseweb="tab"][aria-selected="true"] p {{
            color: #ffffff !important;
            font-weight: 900 !important;
        }}

        div[data-baseweb="tab-highlight"] {{
            display: none !important;
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

        .st-key-parent_nav {{
            margin-top: 72px !important;
        }}
        
        [data-testid="stSidebar"] [data-testid="stRadio"] {{
            margin-top: 18px !important;
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
def session_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_login_session(user_id):
    token = secrets.token_urlsafe(32)
    execute(
        "INSERT INTO login_sessions(academy_id,user_id,token_hash,last_seen) VALUES(%s,%s,%s,NOW())",
        (ACADEMY_ID, user_id, session_token_hash(token)),
    )
    st.session_state.user_id = user_id
    st.session_state.login_token = token
    st.query_params["session"] = token


def current_user():
    uid = st.session_state.get("user_id")
    token = st.session_state.get("login_token") or st.query_params.get("session")
    if not token:
        return fetchone("SELECT * FROM users WHERE id=%s AND academy_id=%s", (uid, ACADEMY_ID)) if uid else None

    session = fetchone(
        """SELECT ls.user_id,u.* FROM login_sessions ls
           JOIN users u ON u.id=ls.user_id
           WHERE ls.academy_id=%s AND ls.token_hash=%s
             AND ls.last_seen >= NOW() - INTERVAL '5 minutes'
             AND u.active AND u.account_status='approved'""",
        (ACADEMY_ID, session_token_hash(token)),
    )
    if not session:
        execute("DELETE FROM login_sessions WHERE token_hash=%s", (session_token_hash(token),))
        st.session_state.pop("user_id", None)
        st.session_state.pop("login_token", None)
        st.query_params.pop("session", None)
        return None

    execute("UPDATE login_sessions SET last_seen=NOW() WHERE token_hash=%s", (session_token_hash(token),))
    st.session_state.user_id = session["id"]
    st.session_state.login_token = token
    return session

def create_notification(user_id, notification_type, title, body=""):
    execute(
        "INSERT INTO notifications(academy_id,user_id,notification_type,title,body) VALUES(%s,%s,%s,%s,%s)",
        (ACADEMY_ID, user_id, notification_type, title, body),
    )


def unread_notification_count(user_id):
    row = fetchone(
        "SELECT COUNT(*) c FROM notifications WHERE academy_id=%s AND user_id=%s AND NOT is_read",
        (ACADEMY_ID, user_id),
    )
    return row["c"] if row else 0


def notification_center(user):
    st.subheader("🔔 Известия")
    rows = fetchall(
        "SELECT * FROM notifications WHERE academy_id=%s AND user_id=%s ORDER BY created_at DESC LIMIT 50",
        (ACADEMY_ID, user["id"]),
    )
    if not rows:
        st.info("Няма известия.")
        return
    if st.button("Маркирай всички като прочетени", key=f"read_all_{user['id']}"):
        execute("UPDATE notifications SET is_read=TRUE WHERE academy_id=%s AND user_id=%s", (ACADEMY_ID, user["id"]))
        st.rerun()
    for row in rows:
        icon = "🔴" if not row["is_read"] else "⚪"
        with st.container(border=True):
            st.write(f"{icon} **{row['title']}**")
            if row.get("body"): st.write(row["body"])
            st.caption(row["created_at"].strftime("%d.%m.%Y %H:%M"))


def notify_team_parents(team_id, notification_type, title, body=""):
    parents = fetchall(
        """SELECT DISTINCT pp.parent_user_id user_id FROM parent_players pp
           JOIN players p ON p.id=pp.player_id
           WHERE pp.academy_id=%s AND p.team_id=%s AND p.active""",
        (ACADEMY_ID, team_id),
    )
    for parent in parents:
        create_notification(parent["user_id"], notification_type, title, body)


def notify_admins(notification_type, title, body=""):
    admins = fetchall(
        "SELECT id FROM users WHERE academy_id=%s AND role='admin' AND active AND account_status='approved'",
        (ACADEMY_ID,),
    )
    for admin in admins:
        create_notification(admin["id"], notification_type, title, body)


def hero():
    with st.container():
        st.markdown('<div class="hero">', unsafe_allow_html=True)
        st.markdown("# DYULGEROV ACADEMY")
        st.caption("Където успехът е танц на таланта")
        st.markdown('</div>', unsafe_allow_html=True)

def public_home():
    hero()
    video_url = academy().get("parent_help_video_url")
    with st.expander("▶️ Как се използва приложението от родители", expanded=False):
        st.caption("Кратко видео за регистрация, вход и използване на родителския профил.")
        if video_url: st.video(video_url)
        else: st.info("Видео инструкцията все още не е добавена от администратора.")


def parent_information(user=None):
    st.subheader("ℹ️ Информационен архив")
    if user:
        for poll in fetchall("SELECT * FROM polls WHERE academy_id=%s AND poll_type='global' AND active ORDER BY created_at DESC",(ACADEMY_ID,)):render_poll(poll,user)
    posts = fetchall(
        "SELECT * FROM public_posts WHERE academy_id=%s AND published ORDER BY created_at DESC LIMIT 100",
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
                if post["media_type"] == "video": st.video(post["media_url"])
                else: st.image(post["media_url"], use_container_width=True)
            st.subheader(post["title"])
            st.write(post.get("body") or "")
            st.caption(post["created_at"].strftime("%d.%m.%Y %H:%M"))


def fan_chat(user, team_id):
    # Поетапно пазим само последните 5 дни. Изтриването се изпълнява при всяко отваряне/изпращане в чата.
    execute(
        "DELETE FROM fan_chat_messages WHERE academy_id=%s AND team_id=%s AND created_at < NOW() - INTERVAL '5 days'",
        (ACADEMY_ID, team_id),
    )

    st.subheader("🗣️ ФЕН ЧАТ")
    st.caption("Чатът е само за родителите в същия набор. Съобщенията се пазят 5 дни.")

    media_enabled = st.checkbox(
        "📷 Разреши снимка към това съобщение",
        value=False,
        key=f"fan_chat_media_enabled_{team_id}",
    )

    with st.form(f"fan_chat_form_{team_id}", clear_on_submit=True):
        body = st.text_area("Ново съобщение", max_chars=1000)
        image = None
        if media_enabled:
            image = st.file_uploader(
                "📷 Камера / файл със снимка",
                type=["jpg", "jpeg", "png", "webp"],
                accept_multiple_files=False,
                key=f"fan_chat_image_{team_id}",
            )
        send = st.form_submit_button("Изпрати в групата", use_container_width=True)

    if send:
        image_bytes = image.getvalue() if image else None
        image_mime = image.type if image else None
        if not body.strip() and not image_bytes:
            st.error("Напиши съобщение или добави снимка.")
        else:
            # За да не позволяваме прекалено големи снимки в PostgreSQL.
            if image_bytes and len(image_bytes) > 5 * 1024 * 1024:
                st.error("Снимката е прекалено голяма. Максимум 5 MB.")
            else:
                execute(
                    """INSERT INTO fan_chat_messages(academy_id,team_id,sender_user_id,body,image_data,image_mime)
                       VALUES(%s,%s,%s,%s,%s,%s)""",
                    (ACADEMY_ID, team_id, user["id"], body.strip(), image_bytes, image_mime),
                )
                st.rerun()

    rows = fetchall(
        """SELECT f.*,u.first_name,u.last_name FROM fan_chat_messages f
           JOIN users u ON u.id=f.sender_user_id
           WHERE f.academy_id=%s AND f.team_id=%s
             AND f.created_at >= NOW() - INTERVAL '5 days'
           ORDER BY f.created_at DESC LIMIT 100""",
        (ACADEMY_ID, team_id),
    )
    if not rows:
        st.info("Няма съобщения във ФЕН ЧАТ от последните 5 дни.")
    for row in rows:
        with st.container(border=True):
            st.write(f"**{row['first_name']} {row['last_name']}**")
            if row.get("body"):
                st.write(row["body"])
            if row.get("image_data"):
                st.image(bytes(row["image_data"]), use_container_width=True)
            st.caption(row["created_at"].strftime("%d.%m.%Y %H:%M"))


def render_poll(poll,user,child=None):
    pid=child["id"] if child else None
    old=fetchone("SELECT answer FROM poll_responses WHERE poll_id=%s AND user_id=%s AND player_id IS NOT DISTINCT FROM %s",(poll["id"],user["id"],pid))
    with st.container(border=True):
        if poll.get("poll_type") == "match_attendance": st.write("**📊 Потвърждение за участие**")
        else:
            st.write(f"**📊 {poll['title']}**")
            if poll.get("question"): st.write(poll["question"])
        vals=["yes","no","maybe"]
        ans=st.radio("Избор",vals,index=vals.index(old["answer"]) if old and old["answer"] in vals else 0,format_func=lambda x:{"yes":"Да","no":"Не","maybe":"Не съм сигурен/а"}[x],horizontal=True,key=f"poll_{poll['id']}_{pid or 0}")
        if st.button("Запази отговора",key=f"poll_save_{poll['id']}_{pid or 0}",use_container_width=True):
            execute("""INSERT INTO poll_responses(academy_id,poll_id,user_id,player_id,answer) VALUES(%s,%s,%s,%s,%s) ON CONFLICT (poll_id,user_id,(COALESCE(player_id,0))) DO UPDATE SET answer=EXCLUDED.answer,created_at=NOW()""",(ACADEMY_ID,poll["id"],user["id"],pid,ans)); st.success("Отговорът е записан.")

def payment_rows(team_id=None):
    extra=" AND pf.team_id=%s" if team_id else ""; params=(ACADEMY_ID,team_id) if team_id else (ACADEMY_ID,)
    return fetchall(f"""SELECT pf.id fee_id,t.name team_name,p.first_name,p.last_name,pf.fee_month,pf.fee_year,pf.final_amount,pf.status,pf.payment_method,pf.bank_transfer_date,pf.payment_note,pf.paid_at,pf.confirmation_status,pf.parent_reported_at FROM player_fees pf JOIN players p ON p.id=pf.player_id LEFT JOIN teams t ON t.id=pf.team_id WHERE pf.academy_id=%s {extra} ORDER BY t.birth_year,p.last_name,p.first_name,pf.fee_year DESC,pf.fee_month DESC""",params)

def payment_excel(rows):
    names={"cash":"В брой","bank":"По банка","card":"Карта","other":"Друго"}; data=[]
    for r in rows:data.append({"Набор":r.get("team_name") or "","Дете":f"{r['first_name']} {r['last_name']}","Месец":r["fee_month"],"Година":r["fee_year"],"Сума EUR":float(r["final_amount"]),"Статус":"Платена" if r["status"]=="paid" else "Дължима","Плащане":names.get(r.get("payment_method"),r.get("payment_method") or ""),"Дата превод":r.get("bank_transfer_date"),"Бележка":r.get("payment_note") or "","Платено на":r.get("paid_at")})
    b=io.BytesIO()
    with pd.ExcelWriter(b, engine="openpyxl") as w:
        df = pd.DataFrame(data)
    
        # Excel не поддържа datetime стойности с timezone.
        # Премахваме timezone само от Excel копието на данните.
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                try:
                    df[col] = df[col].dt.tz_localize(None)
                except (TypeError, AttributeError):
                    pass
    
            elif df[col].dtype == "object":
                df[col] = df[col].apply(
                    lambda x: x.replace(tzinfo=None)
                    if isinstance(x, datetime) and x.tzinfo is not None
                    else x
                )
    
        df.to_excel(
            w,
            index=False,
            sheet_name="Плащания",
        )
    
        ws = w.book["Плащания"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    
        for c in ws.columns:
            ws.column_dimensions[c[0].column_letter].width = min(
                max(len(str(x.value or "")) for x in c) + 2,
                32,
            )
    
    return b.getvalue()

def payment_panel(user,team_ids=None,fixed_team_id=None):
    teams=fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year",(ACADEMY_ID,))
    teams=[t for t in teams if team_ids is None or t["id"] in team_ids]
    if not teams: st.info("Няма набори."); return
    team_map={t["name"]:t for t in teams}
    default_team=next((t for t in teams if t["id"]==fixed_team_id),teams[0])
    team_name=st.selectbox("Набор",list(team_map),index=list(team_map).index(default_team["name"]),key=f"fee_team_{user['id']}")
    team=team_map[team_name]
    now=date.today(); c1,c2=st.columns(2)
    month=c1.selectbox("Месец",range(1,13),index=now.month-1,key=f"fee_month_{user['id']}_{team['id']}")
    year=int(c2.number_input("Година",2024,2045,now.year,key=f"fee_year_{user['id']}_{team['id']}"))
    rows=[r for r in payment_rows(team["id"]) if int(r["fee_month"])==month and int(r["fee_year"])==year]
    if not rows: st.info("Няма начислени такси за избрания набор и месец.")
    for r in rows:
        state=r.get("confirmation_status") or ("confirmed" if r["status"]=="paid" else "unreported")
        icon={"confirmed":"🟢","pending":"🟠","unreported":"🔴"}.get(state,"🔴")
        label={"confirmed":"ПОТВЪРДЕНО","pending":"ЧАКА ADMIN","unreported":"ДЪЛЖИМА"}.get(state,"ДЪЛЖИМА")
        with st.expander(f"{icon} {r['first_name']} {r['last_name']} · {r['final_amount']:.2f} EUR · {label}"):
            if state=="confirmed":
                st.success("Плащането е потвърдено и е заключено за треньора.")
            elif state=="pending":
                st.warning("Плащането е заявено и чака потвърждение от администратор.")
            else:
                method=st.selectbox("Начин",["cash","bank","card","other"],format_func=lambda x:{"cash":"В брой","bank":"По банка","card":"Карта","other":"Друго"}[x],key=f"pm_{user['id']}_{r['fee_id']}")
                note=st.text_input("Бележка",key=f"pn_{user['id']}_{r['fee_id']}")
                if st.button("Изпрати за потвърждение",key=f"pp_{user['id']}_{r['fee_id']}",use_container_width=True):
                    execute("UPDATE player_fees SET confirmation_status='pending',payment_method=%s,payment_note=%s,payment_recorded_by=%s,parent_reported_at=NOW() WHERE id=%s AND academy_id=%s AND status<>'paid'",(method,note.strip() or None,user["id"],r["fee_id"],ACADEMY_ID)); notify_admins("payment_pending","Плащане чака потвърждение",f"{r['first_name']} {r['last_name']} · {month:02d}.{year}"); st.rerun()
    st.download_button("📥 Excel: всички набори и деца",payment_excel(payment_rows()),"academy_payments.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",use_container_width=True)

def admin_payment_confirmation_panel():
    rows=fetchall("""SELECT pf.id fee_id,pf.*,p.first_name,p.last_name,t.name team_name FROM player_fees pf JOIN players p ON p.id=pf.player_id LEFT JOIN teams t ON t.id=pf.team_id WHERE pf.academy_id=%s AND (pf.confirmation_status='pending' OR pf.status='paid') ORDER BY pf.fee_year DESC,pf.fee_month DESC,t.birth_year,p.first_name""",(ACADEMY_ID,))
    st.subheader("✅ Потвърждение и връщане на плащания")
    for r in rows:
        icon="🟠" if r.get("confirmation_status")=="pending" else "🟢"
        with st.expander(f"{icon} {r['first_name']} {r['last_name']} · {r.get('team_name') or ''} · {r['fee_month']:02d}.{r['fee_year']} · {r['final_amount']:.2f} EUR"):
            st.write(f"Начин: **{r.get('payment_method') or 'не е посочен'}**")
            if r.get("payment_note"): st.write(r["payment_note"])
            a,b=st.columns(2)
            if r.get("confirmation_status")=="pending" and a.button("✅ Потвърди",key=f"admin_confirm_{r['fee_id']}",use_container_width=True):
                execute("UPDATE player_fees SET status='paid',confirmation_status='confirmed',confirmed_by=%s,paid_at=NOW() WHERE id=%s AND academy_id=%s",(st.session_state.user_id,r["fee_id"],ACADEMY_ID)); st.rerun()
            if b.button("↩️ Върни за корекция",key=f"admin_return_{r['fee_id']}",use_container_width=True):
                execute("UPDATE player_fees SET status='due',confirmation_status='unreported',confirmed_by=NULL,paid_at=NULL,parent_reported_at=NULL WHERE id=%s AND academy_id=%s",(r["fee_id"],ACADEMY_ID)); st.rerun()

def financial_summary_rows():
    income = fetchall(
        """SELECT COALESCE(payment_method,'other') payment_method,
                  COALESCE(SUM(final_amount),0) total
           FROM player_fees
           WHERE academy_id=%s AND status='paid'
           GROUP BY COALESCE(payment_method,'other')""",
        (ACADEMY_ID,),
    )
    expenses = fetchall(
        """SELECT payment_method,COALESCE(SUM(amount),0) total
           FROM expenses WHERE academy_id=%s GROUP BY payment_method""",
        (ACADEMY_ID,),
    )
    income_map = {r["payment_method"]: float(r["total"]) for r in income}
    expense_map = {r["payment_method"]: float(r["total"]) for r in expenses}
    return income_map, expense_map


def financial_report_excel():
    income_map, expense_map = financial_summary_rows()
    methods = [("bank","По банка"),("cash","В брой"),("card","Карта"),("other","Друго")]
    rows=[]
    for key,label in methods:
        inc=income_map.get(key,0.0); exp=expense_map.get(key,0.0)
        rows.append({"Начин":label,"Приход EUR":inc,"Разход EUR":exp,"Нетно EUR":inc-exp})
    total_income=sum(income_map.values()); total_expense=sum(expense_map.values())
    rows.append({"Начин":"ТОТАЛ","Приход EUR":total_income,"Разход EUR":total_expense,"Нетно EUR":total_income-total_expense})
    details = fetchall(
        "SELECT expense_date,category,amount,payment_method,description,created_at FROM expenses WHERE academy_id=%s ORDER BY expense_date DESC,created_at DESC",
        (ACADEMY_ID,),
    )
    method_names={"cash":"В брой","bank":"По банка","card":"Карта","other":"Друго"}
    detail_rows=[{"Дата":r["expense_date"],"Категория":r["category"],"Сума EUR":float(r["amount"]),"Начин":method_names.get(r["payment_method"],r["payment_method"]),"Описание":r.get("description") or ""} for r in details]
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer,index=False,sheet_name="Финансова справка")
        pd.DataFrame(detail_rows).to_excel(writer,index=False,sheet_name="Разходи")
        for ws in writer.book.worksheets:
            ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width=min(max(len(str(c.value or "")) for c in col)+2,35)
    return out.getvalue()


def financial_dashboard():
    income_map, expense_map = financial_summary_rows()
    bank=income_map.get("bank",0.0); cash=income_map.get("cash",0.0); card=income_map.get("card",0.0)
    expenses_total=sum(expense_map.values()); total_income=sum(income_map.values()); net=total_income-expenses_total
    st.subheader("Финансово табло")
    cols=st.columns(5)
    cols[0].metric("По банка",f"{bank:.2f} EUR")
    cols[1].metric("В брой",f"{cash:.2f} EUR")
    cols[2].metric("Карта",f"{card:.2f} EUR")
    cols[3].metric("Разход",f"{expenses_total:.2f} EUR")
    cols[4].metric("Тотал",f"{net:.2f} EUR",help="Общи приходи минус общи разходи")
    st.download_button(
        "📥 Изтегли само финансовата справка",
        financial_report_excel(),
        "academy_financial_report.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        key="download_financial_summary",
    )


def expense_panel(user):
    st.subheader("Нов разход")
    with st.form("expense_form", clear_on_submit=True):
        expense_date=st.date_input("Дата",date.today())
        category=st.selectbox("Категория",["Наем","Консумативи","Други"])
        amount=st.number_input("Сума (€)",0.0,1000000.0,0.0,0.01)
        payment_method=st.selectbox("Начин",["cash","bank","card"],format_func=lambda x:{"cash":"В брой","bank":"По банка","card":"Карта"}[x])
        description=st.text_area("Описание")
        save=st.form_submit_button("Добави разход",use_container_width=True)
    if save:
        if amount <= 0:
            st.error("Сумата трябва да е по-голяма от 0.")
        else:
            execute(
                "INSERT INTO expenses(academy_id,expense_date,category,amount,payment_method,description,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (ACADEMY_ID,expense_date,category,amount,payment_method,description.strip() or None,user["id"]),
            )
            st.success("Разходът е добавен.")
            st.rerun()
    st.subheader("Архив на разходите")
    rows=fetchall("SELECT * FROM expenses WHERE academy_id=%s ORDER BY expense_date DESC,created_at DESC",(ACADEMY_ID,))
    names={"cash":"В брой","bank":"По банка","card":"Карта"}
    for row in rows:
        with st.container(border=True):
            c1,c2=st.columns([5,1])
            c1.write(f"**{row['expense_date']:%d.%m.%Y} · {row['category']} · {row['amount']:.2f} EUR · {names.get(row['payment_method'],row['payment_method'])}**")
            if row.get("description"): c1.write(row["description"])
            if c2.button("🗑️",key=f"delete_expense_{row['id']}"):
                execute("DELETE FROM expenses WHERE academy_id=%s AND id=%s",(ACADEMY_ID,row["id"])); st.rerun()
    if not rows: st.info("Няма въведени разходи.")


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
        else:
            create_login_session(user["id"])
            st.rerun()

def parent_nav():
    items=[("🏠 Начало","Начало"),("📅 Тренировки","Тренировки"),("⚽ Мачове","Мачове"),("💬 Съобщение от треньор","Съобщения"),("💰 Такси и плащане","Такси"),("ℹ️ Информация","Информация"),("📝 Обратна връзка","Обратна връзка"),("🗣️ ФЕН ЧАТ","ФЕН ЧАТ"),("👤 Настройки","Настройки")]
    st.session_state.setdefault("parent_page","Начало")
    with st.container(key="parent_nav"):
        for start in range(0,len(items),2):
            cols=st.columns(2)
            for col,(label,page) in zip(cols,items[start:start+2]):
                if col.button(label,key=f"pnav_{page}",use_container_width=True,type="primary" if st.session_state.parent_page==page else "secondary"):
                    st.session_state.parent_page=page; st.rerun()

def parent_players(user):
    return fetchall("""SELECT p.*,t.name team_name,c.display_name coach_name,c.profile_image coach_image,c.profile_image_mime coach_image_mime FROM parent_players pp
        JOIN players p ON p.id=pp.player_id LEFT JOIN teams t ON t.id=p.team_id LEFT JOIN coaches c ON c.id=t.coach_id
        WHERE pp.academy_id=%s AND pp.parent_user_id=%s AND p.active ORDER BY p.first_name""",(ACADEMY_ID,user["id"]))

def evaluation_average(row):
    return sum(int(row[x]) for x in ("technique","tactics","physical","teamwork","discipline","effort"))/6

def evaluation_status(score):
    if score >= 4.50: return "🟢 Отлично развитие"
    if score >= 3.75: return "🟢 Много добро развитие"
    if score >= 3.00: return "🔵 Добро развитие"
    if score >= 2.00: return "🟠 Нужда от развитие"
    return "🔴 Изисква допълнително внимание"

def star_rating(value):
    value=max(1,min(5,int(value))); return "★"*value+"☆"*(5-value)

def month_bounds(year,month):
    start=datetime(int(year),int(month),1)
    end=datetime(int(year)+1,1,1) if int(month)==12 else datetime(int(year),int(month)+1,1)
    return start,end

def player_attendance_stats(player_id,team_id,year,month):
    if not team_id:return {"trainings":0,"present":0,"absent":0,"excused":0,"percent":None}
    start,end=month_bounds(year,month)
    r=fetchone("""SELECT COUNT(DISTINCT e.id) trainings,
        COUNT(DISTINCT e.id) FILTER(WHERE a.status='present') present,
        COUNT(DISTINCT e.id) FILTER(WHERE a.status='absent') absent,
        COUNT(DISTINCT e.id) FILTER(WHERE a.status='excused') excused
        FROM events e LEFT JOIN attendance a ON a.event_id=e.id AND a.player_id=%s AND a.academy_id=%s
        WHERE e.academy_id=%s AND e.team_id=%s AND e.event_type='training'
        AND e.starts_at >= %s AND e.starts_at < %s AND e.status <> 'cancelled'""",
        (player_id,ACADEMY_ID,ACADEMY_ID,team_id,start,end)) or {}
    total=int(r.get("trainings") or 0);present=int(r.get("present") or 0)
    return {"trainings":total,"present":present,"absent":int(r.get("absent") or 0),"excused":int(r.get("excused") or 0),"percent":present/total*100 if total else None}

def render_attendance_stats(player_id,team_id,year,month):
    a=player_attendance_stats(player_id,team_id,year,month);st.markdown("#### 📅 Автоматична посещаемост")
    if not a["trainings"]:st.info("За избрания месец все още няма достатъчно данни за проведени тренировки.");return
    c=st.columns(5);c[0].metric("Тренировки",a["trainings"]);c[1].metric("Присъствия",a["present"]);c[2].metric("Отсъствия",a["absent"]);c[3].metric("Извинени",a["excused"]);c[4].metric("Посещаемост",f"{a['percent']:.1f}%")

def render_parent_player_development(user,child):
    rows=fetchall("""SELECT pe.* FROM player_evaluations pe JOIN parent_players pp ON pp.player_id=pe.player_id
        WHERE pe.academy_id=%s AND pp.academy_id=%s AND pp.parent_user_id=%s AND pe.player_id=%s
        ORDER BY pe.evaluation_year DESC,pe.evaluation_month DESC""",(ACADEMY_ID,ACADEMY_ID,user["id"],child["id"]))
    st.divider();st.subheader("📊 Развитие на футболиста")
    if not rows:st.info("Все още няма публикувана месечна оценка от треньора.");return
    labels=[("⚽ Техника","technique"),("🧠 Тактика","tactics"),("🏃 Физическа подготовка","physical"),("🤝 Работа в екип","teamwork"),("💪 Дисциплина","discipline"),("⭐ Старание","effort")]
    available_years=sorted({int(row["evaluation_year"]) for row in rows},reverse=True)
    period_options=["Всички оценки"]+[f"{year} година" for year in available_years]
    selected_period=st.selectbox("Период за обобщената статистика",period_options,key=f"development_period_{child['id']}")
    period_rows=rows if selected_period=="Всички оценки" else [row for row in rows if int(row["evaluation_year"])==int(selected_period.split()[0])]
    period_label="целия наличен период" if selected_period=="Всички оценки" else selected_period
    period_averages={field:sum(float(row[field]) for row in period_rows)/len(period_rows) for _,field in labels}
    period_total=sum(period_averages.values())/len(period_averages)
    match_rating_row=fetchone("SELECT AVG(mps.match_rating)::numeric avg_rating FROM match_player_stats mps JOIN events e ON e.id=mps.event_id WHERE mps.academy_id=%s AND mps.player_id=%s AND mps.match_rating IS NOT NULL",(ACADEMY_ID,child["id"]))
    if match_rating_row and match_rating_row.get("avg_rating") is not None:
        match_avg=float(match_rating_row["avg_rating"]); period_total=period_total*0.80+match_avg*0.20
        st.caption(f"Общата оценка включва 80% месечно развитие и 20% оценки от мачове. Средна мачова оценка: {match_avg:.1f}/5")
    st.markdown(f"### 📈 Обобщено развитие за {period_label}")
    metric_cols=st.columns(3)
    for index,(label,field) in enumerate(labels): metric_cols[index%3].metric(label,f"{period_averages[field]:.1f} / 5")
    st.progress(min(max(period_total/5,0.0),1.0)); st.markdown(f"### Общо за периода: {period_total:.1f} / 5"); st.write(f"**{evaluation_status(period_total)}**")
    trend_rows=sorted(period_rows,key=lambda item:(item["evaluation_year"],item["evaluation_month"]))
    if len(trend_rows)>1:
        trend_data=pd.DataFrame({"Период":[f"{i['evaluation_month']:02d}.{i['evaluation_year']}" for i in trend_rows],"Общо развитие":[evaluation_average(i) for i in trend_rows]})
        st.line_chart(trend_data.set_index("Период"),height=280)
    st.divider()
    latest=rows[0];score=evaluation_average(latest)
    with st.container(border=True):
        st.markdown(f"### Последна оценка: {latest['evaluation_month']:02d}.{latest['evaluation_year']}")
        for label,field in labels:st.write(f"**{label}:** {star_rating(latest[field])}")
        st.markdown(f"### Общо развитие: {score:.1f} / 5");st.write(f"**{evaluation_status(score)}**")
        st.caption("Статусът показва развитието за периода, а не определя детето като добър или слаб футболист.")
        if latest.get("coach_note"):st.markdown("#### 📝 Бележка от треньора");st.write(latest["coach_note"])
        render_attendance_stats(child["id"],child.get("team_id"),latest["evaluation_year"],latest["evaluation_month"])
    st.markdown("### История на развитието")
    for item in reversed(rows):
        score=evaluation_average(item)
        with st.expander(f"{item['evaluation_month']:02d}.{item['evaluation_year']} → {score:.1f}/5"):
            for label,field in labels:st.write(f"**{label}:** {star_rating(item[field])}")
            st.write(f"**{evaluation_status(score)}**")
            if item.get("coach_note"):st.write(f"📝 {item['coach_note']}")
            render_attendance_stats(child["id"],child.get("team_id"),item["evaluation_year"],item["evaluation_month"])

def coach_player_statistics(user,team):
    players=fetchall("""SELECT p.* FROM players p JOIN teams t ON t.id=p.team_id JOIN coaches c ON c.id=t.coach_id
        WHERE p.academy_id=%s AND p.team_id=%s AND p.approval_status='approved' AND p.active
        AND c.academy_id=%s AND c.user_id=%s AND c.active ORDER BY p.first_name,p.last_name""",(ACADEMY_ID,team["id"],ACADEMY_ID,user["id"]))
    if not players:st.info("Няма потвърдени футболисти в този набор.");return
    pmap={f"{x['first_name']} {x['last_name']}":x for x in players};label=st.selectbox("Футболист",list(pmap),key=f"eval_player_{team['id']}");player=pmap[label];now=date.today()
    c1,c2=st.columns(2);month=c1.selectbox("Месец",range(1,13),index=now.month-1,key=f"eval_month_{team['id']}_{player['id']}");year=int(c2.number_input("Година",2020,2100,now.year,key=f"eval_year_{team['id']}_{player['id']}"))
    old=fetchone("""SELECT pe.* FROM player_evaluations pe JOIN teams t ON t.id=pe.team_id JOIN coaches c ON c.id=t.coach_id
        WHERE pe.academy_id=%s AND pe.player_id=%s AND pe.team_id=%s AND pe.evaluation_year=%s AND pe.evaluation_month=%s AND c.user_id=%s AND c.active""",(ACADEMY_ID,player["id"],team["id"],year,month,user["id"]))
    if old:st.info("За този месец има запазена оценка. Промените ще обновят съществуващия запис.")
    criteria=[("⚽ Техника","technique"),("🧠 Тактика","tactics"),("🏃 Физическа подготовка","physical"),("🤝 Работа в екип","teamwork"),("💪 Дисциплина","discipline"),("⭐ Старание / отношение към тренировките","effort")];vals={}
    for label,field in criteria:vals[field]=st.select_slider(label,options=[1,2,3,4,5],value=int(old[field]) if old else 3,format_func=star_rating,key=f"eval_{field}_{team['id']}_{player['id']}_{year}_{month}")
    note=st.text_area("📝 Бележка от треньора",value=(old.get("coach_note") or "") if old else "",key=f"eval_note_{team['id']}_{player['id']}_{year}_{month}")
    score=sum(vals.values())/6;st.markdown(f"### Общо развитие: {score:.1f} / 5");st.write(f"**{evaluation_status(score)}**");st.caption("Статусът показва развитието за периода, а не определя детето като добър или слаб футболист.");render_attendance_stats(player["id"],team["id"],year,month)
    if st.button("💾 Запази месечната оценка",key=f"save_eval_{team['id']}_{player['id']}_{year}_{month}",use_container_width=True,type="primary"):
        saved=execute("""INSERT INTO player_evaluations(academy_id,player_id,team_id,coach_user_id,evaluation_year,evaluation_month,technique,tactics,physical,teamwork,discipline,effort,coach_note)
            SELECT %s,p.id,p.team_id,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s FROM players p JOIN teams t ON t.id=p.team_id JOIN coaches c ON c.id=t.coach_id
            WHERE p.id=%s AND p.academy_id=%s AND p.team_id=%s AND p.approval_status='approved' AND p.active AND c.academy_id=%s AND c.user_id=%s AND c.active
            ON CONFLICT(player_id,evaluation_year,evaluation_month) DO UPDATE SET academy_id=EXCLUDED.academy_id,team_id=EXCLUDED.team_id,coach_user_id=EXCLUDED.coach_user_id,technique=EXCLUDED.technique,tactics=EXCLUDED.tactics,physical=EXCLUDED.physical,teamwork=EXCLUDED.teamwork,discipline=EXCLUDED.discipline,effort=EXCLUDED.effort,coach_note=EXCLUDED.coach_note,updated_at=NOW() RETURNING id""",
            (ACADEMY_ID,user["id"],year,month,vals["technique"],vals["tactics"],vals["physical"],vals["teamwork"],vals["discipline"],vals["effort"],note.strip() or None,player["id"],ACADEMY_ID,team["id"],ACADEMY_ID,user["id"]),True)
        if saved:st.success(f"Месечната оценка е запазена. Общо развитие: {score:.1f} / 5");st.rerun()
        else:st.error("Оценката не е запазена, защото футболистът не принадлежи към твой активен набор.")

POSITION_OPTIONS=["Не е избрана","Вратар","Десен защитник","Централен защитник","Ляв защитник","Дефанзивен халф","Централен халф","Атакуващ халф","Дясно крило","Ляво крило","Нападател"]
PITCH_SLOTS=[("GK","Вратар"),("LB","Ляв защитник"),("CB1","Централен защитник 1"),("CB2","Централен защитник 2"),("RB","Десен защитник"),("DM","Дефанзивен халф"),("CM","Централен халф"),("AM","Атакуващ халф"),("LW","Ляво крило"),("RW","Дясно крило"),("ST","Нападател")]

def player_card(player,position_edit=False,key_prefix="player"):
    c1,c2=st.columns([5,1])
    with c1:
        st.write(f"**{player['first_name']} {player['last_name']}**")
        st.caption(player.get("position") or "Позиция не е избрана")
        if position_edit:
            current=player.get("position") or "Не е избрана"
            pos=st.selectbox("Позиция",POSITION_OPTIONS,index=POSITION_OPTIONS.index(current) if current in POSITION_OPTIONS else 0,key=f"{key_prefix}_position_{player['id']}",label_visibility="collapsed")
            if pos!=current:
                execute("UPDATE players SET position=%s WHERE id=%s AND academy_id=%s",(None if pos=="Не е избрана" else pos,player["id"],ACADEMY_ID)); st.rerun()
    with c2: st.markdown(avatar_html(player.get("profile_image"),player.get("profile_image_mime"),64),unsafe_allow_html=True)

def match_day_panel(user,team):
    matches=fetchall(
        "SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND event_type='match' ORDER BY starts_at DESC",
        (ACADEMY_ID,team["id"]),
    )
    if not matches:
        st.info("Няма публикувани мачове за този набор.")
        return
    match_map={f"{m['starts_at']:%d.%m.%Y %H:%M} · {m['title']}":m for m in matches}
    match_label=st.selectbox("Избери мач",list(match_map),key=f"matchday_match_{team['id']}")
    match=match_map[match_label]
    squad=fetchall(
        """SELECT p.* FROM match_squads ms JOIN players p ON p.id=ms.player_id
           WHERE ms.academy_id=%s AND ms.event_id=%s AND ms.squad_status='in_group'
           ORDER BY p.first_name,p.last_name""",
        (ACADEMY_ID,match["id"]),
    )
    if not squad:
        st.warning("Първо избери и изпрати групата за този мач.")
        return
    st.markdown("## 🧩 Match Day")
    st.caption("Кратък отзив от треньора за избраните деца. Оценките не се показват тук като статистика, но участват в общото развитие на детето.")
    old_stats={r["player_id"]:r for r in fetchall(
        "SELECT * FROM match_player_stats WHERE academy_id=%s AND event_id=%s",
        (ACADEMY_ID,match["id"]),
    )}
    with st.form(f"matchday_feedback_{match['id']}"):
        feedback={}
        for player in squad:
            old=old_stats.get(player["id"],{})
            with st.container(border=True):
                photo_col,info_col,rating_col=st.columns([1,5,2])
                with photo_col:
                    st.markdown(avatar_html(player.get("profile_image"),player.get("profile_image_mime"),58),unsafe_allow_html=True)
                with info_col:
                    st.write(f"**{player['first_name']} {player['last_name']}**")
                    note=st.text_input(
                        "Кратък отзив",
                        value=old.get("note") or "",
                        key=f"match_note_{match['id']}_{player['id']}",
                        placeholder="Напр. силен мач, добра дисциплина...",
                        label_visibility="collapsed",
                    )
                with rating_col:
                    rating=st.select_slider(
                        "Оценка",
                        options=[1,2,3,4,5],
                        value=int(old.get("match_rating") or 3),
                        format_func=star_rating,
                        key=f"match_rating_{match['id']}_{player['id']}",
                    )
                feedback[player["id"]]=(rating,note)
        save_feedback=st.form_submit_button("💾 Запази отзивите",use_container_width=True)
    if save_feedback:
        for player_id,(rating,note) in feedback.items():
            execute(
                """INSERT INTO match_player_stats(academy_id,event_id,player_id,goals,match_rating,note,updated_by)
                   VALUES(%s,%s,%s,0,%s,%s,%s)
                   ON CONFLICT(event_id,player_id) DO UPDATE SET
                     match_rating=EXCLUDED.match_rating,note=EXCLUDED.note,
                     updated_by=EXCLUDED.updated_by,updated_at=NOW()""",
                (ACADEMY_ID,match["id"],player_id,rating,note.strip() or None,user["id"]),
            )
        st.success("Отзивите са запазени и участват в общата статистика на децата.")
        st.rerun()
    report=fetchone(
        "SELECT * FROM match_reports WHERE academy_id=%s AND event_id=%s",
        (ACADEMY_ID,match["id"]),
    ) or {}
    st.markdown("### 🏆 Резултат от мача")
    with st.form(f"matchday_result_{match['id']}"):
        saved_opponent=report.get("opponent_name") or ""
        opponent=st.text_input(
            "Име на другия отбор",
            value="" if saved_opponent == "Друг отбор" else saved_opponent,
            placeholder="Въведи име на отбора",
        )
        home_col,away_col=st.columns(2)
        home_score=home_col.number_input("The Lions",0,30,int(report.get("home_score") or 0))
        away_score=away_col.number_input("Другият отбор",0,30,int(report.get("away_score") or 0))
        confirm=st.form_submit_button("✅ Потвърди резултата",use_container_width=True,type="primary")
    if confirm:
        if not opponent.strip():
            st.error("Моля, въведи името на другия отбор.")
        else:
            execute(
                """INSERT INTO match_reports(
                   academy_id,event_id,team_id,opponent_name,home_score,away_score,
                   standings_url,confirmed,confirmed_by,confirmed_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,TRUE,%s,NOW())
               ON CONFLICT(event_id) DO UPDATE SET
                   opponent_name=EXCLUDED.opponent_name,home_score=EXCLUDED.home_score,
                   away_score=EXCLUDED.away_score,standings_url=EXCLUDED.standings_url,
                   confirmed=TRUE,confirmed_by=EXCLUDED.confirmed_by,
                   confirmed_at=NOW(),updated_at=NOW()""",
            (ACADEMY_ID,match["id"],team["id"],opponent.strip() or "Друг отбор",
             home_score,away_score,team.get("standings_url"),user["id"]),
            )
            notify_team_parents(
            team["id"],"match_result","Публикуван е резултат",
            f"The Lions {home_score}:{away_score} {opponent.strip() or 'Друг отбор'}",
            )
            st.success("Резултатът е записан и вече се вижда в „Резултати“.")
            st.rerun()


def results_statistics_panel(team):
    st.markdown("## 🏆 Резултати")
    st.markdown("### ⚙️ Настройка на класирането")
    standings_url=st.text_input(
        "Линк към актуалното класиране",
        value=team.get("standings_url") or "",
        placeholder="Напр. страница на БФС или друг сайт",
        key=f"standings_url_{team['id']}",
    )
    save_col,open_col=st.columns(2)
    if save_col.button("💾 Запази линка",key=f"save_standings_{team['id']}",use_container_width=True):
        execute(
            "UPDATE teams SET standings_url=%s WHERE academy_id=%s AND id=%s",
            (standings_url.strip() or None,ACADEMY_ID,team["id"]),
        )
        st.success("Линкът към класирането е запазен.")
        st.rerun()
    if standings_url.strip():
        open_col.link_button("🔗 Отвори класирането",standings_url.strip(),use_container_width=True)
    else:
        open_col.info("Няма добавен линк.")
    st.divider()
    rows=fetchall(
        """SELECT mr.*,e.title,e.starts_at FROM match_reports mr
           JOIN events e ON e.id=mr.event_id
           WHERE mr.academy_id=%s AND mr.team_id=%s AND mr.confirmed
           ORDER BY e.starts_at DESC""",
        (ACADEMY_ID,team["id"]),
    )
    if not rows:
        st.info("Няма потвърдени резултати.")
        return
    wins=sum(1 for r in rows if r["home_score"]>r["away_score"])
    draws=sum(1 for r in rows if r["home_score"]==r["away_score"])
    losses=len(rows)-wins-draws
    for col,label,value in zip(st.columns(4),["Мачове","Победи","Равни","Загуби"],[len(rows),wins,draws,losses]):
        col.metric(label,value)
    for row in rows:
        status="Победа" if row["home_score"]>row["away_score"] else "Равен мач" if row["home_score"]==row["away_score"] else "Загуба"
        status_icon="🟢" if status=="Победа" else "🔵" if status=="Равен мач" else "🔴"
        with st.container(border=True):
            st.markdown(f"### {status_icon} The Lions {row['home_score']} : {row['away_score']} {row['opponent_name']}")
            st.caption(f"{row['starts_at']:%d.%m.%Y %H:%M} · {row['title']} · {status}")


def parent_panel(user):
    parent_nav(); players=parent_players(user)
    if not players: st.info("Няма добавени деца към профила."); return
    labels={f"{p['first_name']} {p['last_name']}":p for p in players}; chosen=st.selectbox("Избрано дете",list(labels)); child=labels[chosen]
    st.caption(f"Група: {child.get('team_name') or 'Очаква свързване'} · Треньор: {child.get('coach_name') or 'Очаква свързване'}")
    page=st.session_state.parent_page
    if child["approval_status"]!="approved": st.warning("Детето очаква одобрение и свързване с група от администратор.")
    if page=="Начало":
        unread = unread_notification_count(user["id"])
        if unread > 0:
            if st.button(f"🔔 {unread} нови известия", key="home_notification_bell", type="primary"):
                st.session_state.parent_page = "Известия"
                st.rerun()
        latest_info = fetchone(
            "SELECT * FROM public_posts WHERE academy_id=%s AND published ORDER BY created_at DESC LIMIT 1",
            (ACADEMY_ID,),
        )
        c1, c2 = st.columns([1.6, 1])

        with c1:
            st.markdown(
                f"""
                <div style="
                    min-height:140px;
                    padding:14px;
                    border-radius:18px;
                    background:rgba(8,8,8,.72);
                    border:1px solid rgba(214,183,101,.48);
                    display:flex;
                    align-items:center;
                    justify-content:center;
                    gap:14px;
                ">
                    {avatar_html(
                        child.get('coach_image'),
                        child.get('coach_image_mime'),
                        78
                    )}
                    <div>
                        <div style="font-size:.8rem;color:#c8c1b5">
                            Треньор
                        </div>
                        <div style="font-size:1.02rem;font-weight:800">
                            {child.get('coach_name') or 'Очаква свързване'}
                        </div>
                        <div style="font-size:.82rem;color:#c8c1b5;margin-top:4px">
                            {child.get('team_name') or 'Без набор'}
                        </div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        
        with c2:
            st.metric(
                "Такса",
                f"{academy()['default_fee']:.2f} EUR"
            )
        if latest_info:
            with st.container(border=True):
                st.subheader(latest_info["title"])
                st.write(latest_info.get("body") or "")
                st.caption(latest_info["created_at"].strftime("%d.%m.%Y %H:%M"))
        else:
            st.info("Няма нова информация от академията.")
    elif page == "Тренировки":
        rows=fetchall(
            "SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND event_type='training' ORDER BY starts_at ASC LIMIT 40",
            (ACADEMY_ID,child.get("team_id")),
        ) if child.get("team_id") else []
        for r in rows:
            with st.container(border=True):
                st.markdown(
                    f"<div class='schedule-line' style='color:#ff2020!important;font-weight:900!important;font-size:1.12rem!important;text-shadow:0 0 2px #000,0 0 8px rgba(255,0,0,.50);'>{r['title']} · {r['starts_at']:%d.%m.%Y %H:%M} · {r.get('location') or 'Без място'}</div>",
                    unsafe_allow_html=True,
                )
                if r.get("description"): st.info(f"ℹ️ {r['description']}")
        if not rows: st.info("Няма добавени тренировки.")
        render_parent_player_development(user,child)
    elif page == "Мачове":
        match_view=st.radio(
            "Мачове",
            ["⚽ Предстоящи мачове","🏆 Класиране и резултати"],
            horizontal=True,
            key=f"parent_match_view_{child['id']}",
            label_visibility="collapsed",
        )
        if match_view == "⚽ Предстоящи мачове":
            rows=fetchall(
                """SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND event_type='match'
                   AND starts_at >= NOW() AND status <> 'cancelled'
                   ORDER BY starts_at ASC LIMIT 40""",
                (ACADEMY_ID,child.get("team_id")),
            ) if child.get("team_id") else []
            for r in rows:
                with st.container(border=True):
                    st.markdown(
                        f"<div class='schedule-line' style='color:#ff2020!important;font-weight:900!important;font-size:1.12rem!important;text-shadow:0 0 2px #000,0 0 8px rgba(255,0,0,.50);'>{r['title']} · {r['starts_at']:%d.%m.%Y %H:%M} · {r.get('location') or 'Без място'}</div>",
                        unsafe_allow_html=True,
                    )
                    if r.get("description"): st.info(f"ℹ️ {r['description']}")
                    squad=fetchone(
                        "SELECT squad_status FROM match_squads WHERE academy_id=%s AND event_id=%s AND player_id=%s",
                        (ACADEMY_ID,r["id"],child["id"]),
                    )
                    if squad:
                        if squad["squad_status"] == "in_group": st.success("🟢 Детето е в групата за този мач.")
                        else: st.warning("🔴 Детето не е в групата за този мач.")
                    else: st.caption("Треньорът още не е публикувал група за този мач.")
                    poll=fetchone(
                        "SELECT * FROM polls WHERE academy_id=%s AND event_id=%s AND active ORDER BY created_at DESC LIMIT 1",
                        (ACADEMY_ID,r["id"]),
                    )
                    if poll and squad and squad["squad_status"] == "in_group": render_poll(poll,user,child)
            if not rows: st.info("Няма предстоящи мачове.")
        else:
            team_info=fetchone(
                "SELECT standings_url FROM teams WHERE academy_id=%s AND id=%s",
                (ACADEMY_ID,child.get("team_id")),
            ) if child.get("team_id") else None
            st.markdown("## 🏆 Класиране и резултати")
            if team_info and team_info.get("standings_url"):
                st.link_button("🔗 Отвори актуалното класиране",team_info["standings_url"],use_container_width=True)
            else:
                st.info("Все още няма добавен линк към класирането.")
            results=fetchall(
                """SELECT mr.*,e.title,e.starts_at,e.location FROM match_reports mr
                   JOIN events e ON e.id=mr.event_id
                   WHERE mr.academy_id=%s AND mr.team_id=%s AND mr.confirmed
                   ORDER BY e.starts_at DESC""",
                (ACADEMY_ID,child.get("team_id")),
            ) if child.get("team_id") else []
            if results:
                wins=sum(1 for r in results if r["home_score"]>r["away_score"])
                draws=sum(1 for r in results if r["home_score"]==r["away_score"])
                losses=len(results)-wins-draws
                for col,label,value in zip(st.columns(4),["Мачове","Победи","Равни","Загуби"],[len(results),wins,draws,losses]): col.metric(label,value)
                st.markdown("### Последни резултати")
                for r in results:
                    status="Победа" if r["home_score"]>r["away_score"] else "Равен мач" if r["home_score"]==r["away_score"] else "Загуба"
                    icon="🟢" if status=="Победа" else "🔵" if status=="Равен мач" else "🔴"
                    with st.container(border=True):
                        st.markdown(f"### {icon} The Lions {r['home_score']} : {r['away_score']} {r['opponent_name']}")
                        st.caption(f"{r['starts_at']:%d.%m.%Y %H:%M} · {r['title']} · {status}")
            else:
                st.info("Все още няма потвърдени резултати от изминали мачове.")
    elif page=="Съобщения":
        rows=fetchall("SELECT * FROM team_messages WHERE academy_id=%s AND team_id=%s ORDER BY created_at DESC",(ACADEMY_ID,child.get("team_id"))) if child.get("team_id") else []
        for r in rows:
            with st.container(border=True): st.subheader(r["title"]); st.write(r["body"]); st.caption(r["created_at"].strftime("%d.%m.%Y %H:%M"))
        if not rows: st.info("Няма съобщения.")
    elif page=="Такси":
        rows=fetchall("SELECT * FROM player_fees WHERE academy_id=%s AND player_id=%s ORDER BY fee_year DESC,fee_month DESC",(ACADEMY_ID,child["id"]))
        for r in rows:
            paid=r["status"]=="paid"; pending=r.get("confirmation_status")=="pending"; icon="🟢" if paid else "🟠" if pending else "🔴"; label="ПЛАТЕНА" if paid else "ЧАКА ПОТВЪРЖДЕНИЕ" if pending else "ДЪЛЖИМА"
            with st.expander(f"{icon} {r['fee_month']:02d}.{r['fee_year']} · {r['final_amount']:.2f} EUR · {label}"):
                st.write(f"**{icon} {label}**")
                if not paid:
                    method=st.radio("Начин на плащане",["cash","bank","card"],format_func=lambda x:{"cash":"В брой","bank":"По банка","card":"С карта"}[x],horizontal=True,key=f"choice_{r['id']}")
                    if method=="bank":
                        d=st.date_input("Дата на превода",date.today(),key=f"bd_{r['id']}")
                        if st.button("Изпрати информацията",key=f"bs_{r['id']}",use_container_width=True):execute("UPDATE player_fees SET payment_method='bank',bank_transfer_date=%s,payment_note='Заявено от родител',confirmation_status='pending',parent_reported_at=NOW() WHERE id=%s",(d,r["id"]));notify_admins("bank_payment","Заявен банков превод",f"{child['first_name']} {child['last_name']} · {d:%d.%m.%Y}");st.success("Изпратено. Администраторът трябва да потвърди плащането.")
                    elif method=="cash":st.info("Плащането в брой се потвърждава от треньор или администратор.")
                    else:st.info("Плащането с карта предстои.")
        if not rows:st.info("Няма начислени такси.")
    elif page=="Информация":
        parent_information(user)
    elif page=="Известия":
        notification_center(user)
    elif page=="ФЕН ЧАТ":
        if child.get("team_id"):
            fan_chat(user, child["team_id"])
        else:
            st.warning("Детето още не е свързано с набор.")
    elif page=="Обратна връзка":
        st.subheader("📝 Обратна връзка към треньора")
        coach_user = fetchone(
            """SELECT c.user_id, c.display_name FROM teams t JOIN coaches c ON c.id=t.coach_id
               WHERE t.academy_id=%s AND t.id=%s""",
            (ACADEMY_ID, child.get("team_id")),
        ) if child.get("team_id") else None
        if not coach_user or not coach_user.get("user_id"):
            st.warning("За този набор още няма активен треньорски профил.")
        else:
            with st.form(f"feedback_{child['id']}"):
                feedback_type = st.selectbox("Тип", ["signal", "rating", "suggestion"], format_func=lambda x:{"signal":"Сигнал","rating":"Оценка","suggestion":"Предложение"}[x])
                rating = st.slider("Оценка", 1, 5, 5) if feedback_type == "rating" else None
                subject = st.text_input("Тема")
                body = st.text_area("Съобщение")
                send_feedback = st.form_submit_button("Изпрати само към треньора", use_container_width=True)
            if send_feedback:
                if not subject.strip() or not body.strip():
                    st.error("Попълни тема и съобщение.")
                else:
                    execute(
                        """INSERT INTO feedback(academy_id,parent_user_id,player_id,coach_user_id,feedback_type,rating,subject,body)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (ACADEMY_ID,user["id"],child["id"],coach_user["user_id"],feedback_type,rating,subject.strip(),body.strip()),
                    )
                    create_notification(coach_user["user_id"], "feedback", "Нова обратна връзка от родител", subject.strip())
                    st.success("Обратната връзка е изпратена само към треньора.")
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
            ci1,ci2=st.columns([1,5])
            with ci1: st.markdown(avatar_html(coach.get("profile_image"),coach.get("profile_image_mime"),82),unsafe_allow_html=True)
            with ci2:
                coach_image=st.file_uploader("Снимка на треньора",type=["jpg","jpeg","png","webp"],key=f"coach_image_{coach['id']}")
                if st.button("Запази снимката",key=f"save_coach_image_{coach['id']}"):
                    ib,im=valid_profile_image(coach_image)
                    if ib: execute("UPDATE coaches SET profile_image=%s,profile_image_mime=%s WHERE id=%s AND academy_id=%s",(ib,im,coach["id"],ACADEMY_ID)); st.rerun()
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
    count = unread_notification_count(user["id"])
    st.title("Административен панел")
    if count > 0:
        with st.expander(f"🔔 {count} нови известия", expanded=False):
            notification_center(user)
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
            archive_options={"Всички набори":None,**{t["name"]:t["id"] for t in teams}}
            archive_label=st.selectbox("Покажи деца от набор",list(archive_options),key="admin_archive_team_filter")
            archive_team_id=archive_options[archive_label]
            if archive_team_id is None:
                players=fetchall("""SELECT p.*,t.name team_name,c.display_name coach_name FROM players p LEFT JOIN teams t ON t.id=p.team_id LEFT JOIN coaches c ON c.id=t.coach_id WHERE p.academy_id=%s AND p.active AND p.approval_status='approved' ORDER BY t.birth_year,p.first_name,p.last_name""",(ACADEMY_ID,))
            else:
                players=fetchall("""SELECT p.*,t.name team_name,c.display_name coach_name FROM players p LEFT JOIN teams t ON t.id=p.team_id LEFT JOIN coaches c ON c.id=t.coach_id WHERE p.academy_id=%s AND p.team_id=%s AND p.active AND p.approval_status='approved' ORDER BY p.first_name,p.last_name""",(ACADEMY_ID,archive_team_id))
            if not players: st.info("Няма потвърдени деца.")
            for player in players:
                with st.expander(f"🟢 {player['first_name']} {player['last_name']} · {player.get('team_name') or 'Без група'} · {player.get('coach_name') or 'Без треньор'}"):
                    pi1,pi2=st.columns([1,5])
                    with pi1: st.markdown(avatar_html(player.get("profile_image"),player.get("profile_image_mime"),82),unsafe_allow_html=True)
                    with pi2:
                        player_image=st.file_uploader("Профилна снимка",type=["jpg","jpeg","png","webp"],key=f"player_image_{player['id']}")
                        if st.button("Запази снимката",key=f"save_player_image_{player['id']}"):
                            ib,im=valid_profile_image(player_image)
                            if ib: execute("UPDATE players SET profile_image=%s,profile_image_mime=%s WHERE id=%s AND academy_id=%s",(ib,im,player["id"],ACADEMY_ID)); st.rerun()
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
                                    cur.execute("DELETE FROM player_evaluations WHERE academy_id=%s AND player_id=%s", (ACADEMY_ID, player["id"]))
                                    cur.execute("DELETE FROM match_squads WHERE academy_id=%s AND player_id=%s", (ACADEMY_ID, player["id"]))
                                    cur.execute("DELETE FROM poll_responses WHERE academy_id=%s AND player_id=%s", (ACADEMY_ID, player["id"]))
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
            execute("INSERT INTO team_messages(academy_id,team_id,sender_user_id,title,body) VALUES(%s,%s,%s,%s,%s)", (ACADEMY_ID,tmap[team],user["id"],title.strip(),body.strip()))
            notify_team_parents(tmap[team], "message", title.strip(), body.strip())
            st.success("Съобщението е добавено в архива.")
            st.rerun()
        st.divider(); st.subheader("Архив на съобщенията")
        archive_team = st.selectbox("Покажи архив за набор", list(tmap), key="admin_message_archive_team")
        rows = fetchall("SELECT * FROM team_messages WHERE academy_id=%s AND team_id=%s ORDER BY created_at DESC", (ACADEMY_ID,tmap[archive_team]))
        for row in rows:
            with st.container(border=True):
                st.write(f"**{row['title']}**"); st.write(row['body']); st.caption(row['created_at'].strftime('%d.%m.%Y %H:%M'))
                if st.button("🗑️ Изтрий това съобщение", key=f"delete_team_message_{row['id']}"):
                    execute("DELETE FROM team_messages WHERE academy_id=%s AND id=%s", (ACADEMY_ID,row['id']))
                    st.rerun()
        if not rows: st.info("Архивът е празен.")

    elif sec == "Такси":
        st.session_state.setdefault("admin_fee_page", "Начисляване")
        b1,b2,b3=st.columns(3)
        if b1.button("💰 Начисляване",use_container_width=True,type="primary" if st.session_state.admin_fee_page=="Начисляване" else "secondary"):
            st.session_state.admin_fee_page="Начисляване"; st.rerun()
        if b2.button("📊 Справка и плащания",use_container_width=True,type="primary" if st.session_state.admin_fee_page=="Справка" else "secondary"):
            st.session_state.admin_fee_page="Справка"; st.rerun()
        if b3.button("🧾 Разход",use_container_width=True,type="primary" if st.session_state.admin_fee_page=="Разход" else "secondary"):
            st.session_state.admin_fee_page="Разход"; st.rerun()

        fee_page=st.session_state.admin_fee_page
        if fee_page == "Начисляване":
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
        elif fee_page == "Справка":
            payment_panel(user)
            st.divider()
            financial_dashboard()
        else:
            expense_panel(user)

    elif sec == "Информация":
        st.subheader("🎬 Видео инструкция за родители")
        current_help_video=academy().get("parent_help_video_url") or ""
        help_video_url=st.text_input("Линк към видео инструкция",value=current_help_video,key="parent_help_video_url")
        vh1,vh2=st.columns(2)
        if vh1.button("Запази видео инструкцията",use_container_width=True): execute("UPDATE academies SET parent_help_video_url=%s WHERE id=%s",(help_video_url.strip() or None,ACADEMY_ID)); st.rerun()
        if vh2.button("Премахни видеото",use_container_width=True): execute("UPDATE academies SET parent_help_video_url=NULL WHERE id=%s",(ACADEMY_ID,)); st.rerun()
        if current_help_video: st.video(current_help_video)
        st.divider()
        with st.form("post_form"):
            title=st.text_input("Заглавие"); body=st.text_area("Текст"); typ=st.radio("Медия",["image","video"],format_func=lambda x:"Снимка" if x=="image" else "Видео линк",horizontal=True); url=st.text_input("URL за видео/снимка"); upload=st.file_uploader("Или качи снимка",type=["jpg","jpeg","png","webp"]); add_poll=st.checkbox("📊 Добави глобална анкета"); poll_question=st.text_input("Въпрос на анкетата") if add_poll else ""; send=st.form_submit_button("Добави в информационния архив",use_container_width=True)
        if send and title.strip():
            data=upload.getvalue() if upload else None; mime=upload.type if upload else None
            execute(
                """INSERT INTO public_posts(academy_id,title,body,media_type,media_url,image_data,image_mime,created_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (ACADEMY_ID,title.strip(),body,typ,url.strip() or None,data,mime,user["id"]),
            )
            if add_poll and poll_question.strip():execute("INSERT INTO polls(academy_id,poll_type,title,question,created_by) VALUES(%s,'global',%s,%s,%s)",(ACADEMY_ID,title.strip(),poll_question.strip(),user["id"]))
            parents = fetchall("SELECT id FROM users WHERE academy_id=%s AND role='parent' AND active AND account_status='approved'", (ACADEMY_ID,))
            for parent in parents: create_notification(parent["id"], "information", title.strip(), body.strip())
            st.success("Информацията е добавена в архива.")
            st.rerun()
        st.divider(); st.subheader("Информационен архив")
        posts = fetchall("SELECT * FROM public_posts WHERE academy_id=%s ORDER BY created_at DESC", (ACADEMY_ID,))
        for post in posts:
            with st.container(border=True):
                st.write(f"**{post['title']}**"); st.write(post.get('body') or ''); st.caption(post['created_at'].strftime('%d.%m.%Y %H:%M'))
                if st.button("🗑️ Изтрий тази информация", key=f"delete_info_{post['id']}"):
                    execute("DELETE FROM public_posts WHERE academy_id=%s AND id=%s", (ACADEMY_ID,post['id']))
                    st.rerun()
        if not posts: st.info("Информационният архив е празен.")
    else:
        notification_center(user)


def coach_panel(user):
    coach = fetchone("SELECT * FROM coaches WHERE academy_id=%s AND user_id=%s AND active", (ACADEMY_ID, user["id"]))
    st.title("Треньорски панел")
    coach_unread = unread_notification_count(user["id"])
    if coach_unread > 0:
        with st.expander(f"🔔 {coach_unread} нови известия", expanded=False):
            notification_center(user)
    if not coach:
        st.warning("Този Coach профил още не е свързан с треньор. Admin трябва да го свърже от „Треньори и набори“.")
        return
    teams = fetchall("SELECT * FROM teams WHERE academy_id=%s AND coach_id=%s AND active ORDER BY birth_year", (ACADEMY_ID, coach["id"]))
    if not teams:
        st.info("Няма възложени набори към този треньор.")
        return
    st.sidebar.caption(coach["display_name"])
    sections = ["Табло", "Тренировки и мачове", "Група за мача", "Присъствие", "Такси", "Съобщения", "Обратна връзка"]
    sec = st.sidebar.radio("Треньорско меню", sections)
    tmap = {t["name"]: t for t in teams}
    team_name = st.selectbox("Избран набор", list(tmap), key="coach_selected_team")
    team = tmap[team_name]

    if sec == "Табло":
        players=fetchall("SELECT * FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' AND active ORDER BY first_name,last_name",(ACADEMY_ID,team["id"]))
        events=fetchone("SELECT COUNT(*) c FROM events WHERE academy_id=%s AND team_id=%s AND starts_at>=NOW()",(ACADEMY_ID,team["id"]))["c"]
        c1,c2=st.columns(2); c1.metric("Деца в групата",len(players)); c2.metric("Предстоящи събития",events)
        st.divider(); st.subheader("⚽ Играч")
        for player in players:
            with st.container(border=True): player_card(player,position_edit=True,key_prefix=f"coach_{team['id']}")
        if not players: st.info("Няма потвърдени деца в този набор.")
    elif sec == "Тренировки и мачове":
        multi_date_event_form("coach_event_form", teams, user["id"], fixed_team_id=team["id"])
    elif sec == "Група за мача":
        squad_section=st.radio("Избор",["⚽ Група за мача","🧩 Match Day","📊 Статистика","🏆 Резултати"],horizontal=True,key=f"coach_squad_inner_{team['id']}",label_visibility="collapsed")
        if squad_section == "⚽ Група за мача":
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
                st.caption("Маркирай само футболистите, които са в групата за избрания мач.")
                with st.form(f"match_squad_{match['id']}"):
                    choices={}
                    st.markdown("#### Футболисти")
                    for player in players:
                        current=existing.get(player["id"],"not_in_group")=="in_group"
                        name_col,check_col=st.columns([8,1])
                        name_col.write(f"**{player['first_name']} {player['last_name']}**")
                        choices[player["id"]]=check_col.checkbox("В групата",value=current,key=f"squad_check_{match['id']}_{player['id']}",label_visibility="collapsed")
                    existing_poll=fetchone("SELECT id FROM polls WHERE academy_id=%s AND event_id=%s AND poll_type='match_attendance' AND active ORDER BY created_at DESC LIMIT 1",(ACADEMY_ID,match["id"]))
                    create_match_poll=st.checkbox("📊 Изпрати анкета за присъствие",value=bool(existing_poll),key=f"match_poll_{match['id']}")
                    publish_squad=st.form_submit_button("Изпрати групата към родителите",use_container_width=True)
                if publish_squad:
                    with db() as conn, conn.cursor() as cur:
                        cur.execute("DELETE FROM match_squads WHERE academy_id=%s AND event_id=%s",(ACADEMY_ID,match["id"]))
                        for player_id,is_selected in choices.items():
                            cur.execute("INSERT INTO match_squads(academy_id,event_id,player_id,squad_status,selected_by) VALUES(%s,%s,%s,%s,%s)",(ACADEMY_ID,match["id"],player_id,"in_group" if is_selected else "not_in_group",user["id"]))
                    if create_match_poll:
                        old_poll=fetchone("SELECT id FROM polls WHERE academy_id=%s AND event_id=%s AND poll_type='match_attendance' ORDER BY created_at DESC LIMIT 1",(ACADEMY_ID,match["id"]))
                        question=f"Ще присъства ли детето на мача на {match['starts_at']:%d.%m.%Y от %H:%M}?"
                        if old_poll: execute("UPDATE polls SET active=TRUE,title=%s,question=%s,team_id=%s WHERE id=%s AND academy_id=%s",("Присъствие на мача",question,team["id"],old_poll["id"],ACADEMY_ID))
                        else: execute("INSERT INTO polls(academy_id,poll_type,title,question,team_id,event_id,created_by) VALUES(%s,'match_attendance',%s,%s,%s,%s,%s)",(ACADEMY_ID,"Присъствие на мача",question,team["id"],match["id"],user["id"]))
                    else: execute("UPDATE polls SET active=FALSE WHERE academy_id=%s AND event_id=%s AND poll_type='match_attendance'",(ACADEMY_ID,match["id"]))
                    notify_team_parents(team["id"],"match_squad","Публикувана е група за мача",match_label)
                    st.success("Групата за мача е изпратена към родителите."); st.rerun()
        elif squad_section == "🧩 Match Day":
            match_day_panel(user,team)
        elif squad_section == "🏆 Резултати":
            results_statistics_panel(team)
        else:
            coach_player_statistics(user,team)
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
    elif sec == "Такси":
        payment_panel(user,[t["id"] for t in teams],fixed_team_id=team["id"])
    elif sec == "Съобщения":
        with st.form("coach_message_form"):
            title = st.text_input("Заглавие"); body = st.text_area("Съобщение към родителите"); send = st.form_submit_button("Изпрати към групата", use_container_width=True)
        if send and title.strip() and body.strip():
            execute("INSERT INTO team_messages(academy_id,team_id,sender_user_id,title,body) VALUES(%s,%s,%s,%s,%s)", (ACADEMY_ID,team["id"],user["id"],title.strip(),body.strip()))
            notify_team_parents(team["id"], "message", title.strip(), body.strip())
            st.success("Съобщението е добавено в архива.")
            st.rerun()
        st.divider(); st.subheader("Архив на съобщенията")
        rows = fetchall("SELECT * FROM team_messages WHERE academy_id=%s AND team_id=%s ORDER BY created_at DESC", (ACADEMY_ID,team["id"]))
        for row in rows:
            with st.container(border=True):
                st.write(f"**{row['title']}**"); st.write(row['body']); st.caption(row['created_at'].strftime('%d.%m.%Y %H:%M'))
                if row.get("sender_user_id") == user["id"] and st.button("🗑️ Изтрий", key=f"coach_delete_message_{row['id']}"):
                    execute("DELETE FROM team_messages WHERE academy_id=%s AND id=%s", (ACADEMY_ID,row['id']))
                    st.rerun()

    elif sec == "Обратна връзка":
        st.subheader("📝 Обратна връзка от родители")
        rows = fetchall(
            """SELECT f.*,u.first_name,u.last_name,p.first_name player_first,p.last_name player_last
               FROM feedback f JOIN users u ON u.id=f.parent_user_id
               LEFT JOIN players p ON p.id=f.player_id
               WHERE f.academy_id=%s AND f.coach_user_id=%s ORDER BY f.created_at DESC""",
            (ACADEMY_ID, user["id"]),
        )
        if not rows: st.info("Няма изпратена обратна връзка.")
        for row in rows:
            with st.container(border=True):
                st.write(f"**{row['subject']}** · {row['feedback_type']}")
                if row.get("rating"): st.write(f"Оценка: {row['rating']}/5")
                st.write(row["body"])
                st.caption(f"За: {row.get('player_first') or ''} {row.get('player_last') or ''} · {row['created_at']:%d.%m.%Y %H:%M}")
        execute("UPDATE feedback SET is_read=TRUE WHERE academy_id=%s AND coach_user_id=%s", (ACADEMY_ID,user["id"]))
    else:
        notification_center(user)

def logout_button():
    if st.sidebar.button("Изход",use_container_width=True):
        token = st.session_state.get("login_token") or st.query_params.get("session")
        if token:
            execute("DELETE FROM login_sessions WHERE token_hash=%s", (session_token_hash(token),))
        st.session_state.clear()
        st.query_params.clear()
        st.rerun()

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
