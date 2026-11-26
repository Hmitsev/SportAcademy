import os
import io
import base64
import hashlib
import secrets
import time
import json
import pandas as pd
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pywebpush import webpush, WebPushException

APP_DIR = Path(__file__).parent
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ACADEMY_SLUG = os.getenv("ACADEMY_SLUG", "dyulgerov-academy")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@academy.local").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip().strip('"').strip("'")
# IMPORTANT: this is a 32-byte P-256 private scalar encoded as base64url, not PEM.
# pywebpush/py_vapid reads this raw representation directly, avoiding Render multiline PEM issues.
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip().strip('"').strip("'")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:hmitsevapps@gmail.com").strip()

ASSETS = {
    "logo": APP_DIR / "лого.jpg",
    "background": APP_DIR / "беграуд.jpg",
    "coming": APP_DIR / "очаква се.jpg",
    "code": APP_DIR / "кодекс.jpg",
    "match_template": APP_DIR / "Designer.jpg",
    "birthday_template": APP_DIR / "чрд.jpg",
}

st.set_page_config(page_title="Dyulgerov Academy", page_icon="⚽", layout="wide", initial_sidebar_state="collapsed")
if not DATABASE_URL:
    st.error("Липсва DATABASE_URL в Render Environment.")
    st.stop()


def generate_match_poster(opponent):
    """Create a ready JPEG poster from Designer.jpg with the opponent name."""
    template_path=ASSETS["match_template"]
    if not template_path.exists():
        raise FileNotFoundError("Липсва шаблонът Designer.jpg в GitHub.")
    image=Image.open(template_path).convert("RGB")
    draw=ImageDraw.Draw(image)
    # The black opponent field in the supplied 1024x1536 template.
    left,top,right,bottom=82,925,942,1084
    max_width=right-left-70
    font_paths=[
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    font_path=next((fp for fp in font_paths if Path(fp).exists()),None)
    if not font_path:
        raise FileNotFoundError("Липсва системен Unicode шрифт за генериране на плаката.")
    text=opponent.strip().upper()
    font_size=72
    while font_size>=34:
        font=ImageFont.truetype(font_path,font_size)
        box=draw.textbbox((0,0),text,font=font,stroke_width=2)
        if box[2]-box[0] <= max_width: break
        font_size-=2
    # For very long names, use two centered lines.
    lines=[text]
    if draw.textbbox((0,0),text,font=font)[2] > max_width:
        words=text.split(); lines=[]; line=""
        for word in words:
            trial=(line+" "+word).strip()
            if not line or draw.textbbox((0,0),trial,font=font)[2] <= max_width: line=trial
            else: lines.append(line); line=word
        if line: lines.append(line)
        lines=lines[:2]
    line_height=font_size+10
    total_height=len(lines)*line_height-10
    y=top+(bottom-top-total_height)//2
    for line in lines:
        box=draw.textbbox((0,0),line,font=font,stroke_width=2)
        x=left+(right-left-(box[2]-box[0]))//2
        draw.text((x,y),line,font=font,fill=(245,204,91),stroke_width=2,stroke_fill=(40,25,0))
        y+=line_height
    buffer=io.BytesIO()
    image.save(buffer,format="JPEG",quality=88,optimize=True)
    return buffer.getvalue()

@st.cache_resource
def pool():
    return ThreadedConnectionPool(
        1,
        8,
        dsn=DATABASE_URL,
        sslmode="require",
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
        application_name="academy_manager",
    )

@contextmanager
def db():
    db_pool = pool()
    conn = None
    broken = False

    try:
        conn = db_pool.getconn()

        # Не използваме затворена или прекъсната PostgreSQL връзка.
        if conn.closed:
            broken = True
            raise psycopg2.InterfaceError("Получена е затворена PostgreSQL връзка.")
        try:
            with conn.cursor() as health_cursor:
                health_cursor.execute("SELECT 1")
                health_cursor.fetchone()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True
            raise

        yield conn

        if not conn.closed:
            conn.commit()

    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True

        if conn is not None and not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass

        raise

    except Exception:
        if conn is not None and not conn.closed:
            try:
                conn.rollback()
            except Exception:
                broken = True

        raise

    finally:
        if conn is not None:
            try:
                db_pool.putconn(
                    conn,
                    close=broken or bool(conn.closed),
                )
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


def reset_db_pool():
    """Close every old PostgreSQL connection, then discard only the cached pool."""
    try:
        cached_pool = pool()
        try:
            cached_pool.closeall()
        except Exception:
            pass
    finally:
        try:
            pool.clear()
        except Exception:
            pass


def _retry_db_call(operation, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            return operation()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            last_error = exc
            reset_db_pool()
            if attempt < attempts - 1:
                time.sleep(0.75 * (attempt + 1))
    raise last_error


def fetchone(q, p=()):
    def operation():
        with db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(q, p)
                return cur.fetchone()
    return _retry_db_call(operation)


def fetchall(q, p=()):
    def operation():
        with db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(q, p)
                return cur.fetchall()
    return _retry_db_call(operation)


def execute(q, p=(), returning=False):
    def operation():
        with db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(q, p)
                return cur.fetchone() if returning else None
    return _retry_db_call(operation)

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
    """Normalize profile photos before PostgreSQL storage to reduce transfer usage."""
    if not uploaded_file:
        return None, None
    raw_bytes = uploaded_file.getvalue()
    if len(raw_bytes) > 8 * 1024 * 1024:
        st.error("Снимката е прекалено голяма. Максималният входен размер е 8 MB.")
        return None, None
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        image.load()
        if image.mode not in ("RGB", "L"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            if "A" in image.getbands():
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image.convert("RGB"))
            image = background
        else:
            image = image.convert("RGB")
        image.thumbnail((600, 600), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=75, optimize=True, progressive=True)
        compressed = output.getvalue()
        if len(compressed) > 450 * 1024:
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=62, optimize=True, progressive=True)
            compressed = output.getvalue()
        return compressed, "image/jpeg"
    except Exception:
        st.error("Файлът не може да бъде обработен като валидна снимка.")
        return None, None


def set_flash(message,kind="success"):
    st.session_state["action_flash"]={"message":message,"kind":kind}

def show_flash():
    flash=st.session_state.pop("action_flash",None)
    if not flash:return
    {"success":st.success,"info":st.info,"warning":st.warning,"error":st.error}.get(flash.get("kind"),st.success)(flash.get("message") or "Действието е изпълнено.")

@st.cache_resource(show_spinner=False)
def init_db():
    statements = [
        """CREATE TABLE IF NOT EXISTS academies(
            id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
            sport TEXT NOT NULL DEFAULT 'football', city TEXT, logo_file TEXT,
            primary_color TEXT NOT NULL DEFAULT '#b48a32', secondary_color TEXT NOT NULL DEFAULT '#f7f2e8',
            default_fee NUMERIC(12,2) NOT NULL DEFAULT 35.00, currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
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
            amount NUMERIC(12,2) NOT NULL DEFAULT 35.00, currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
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
        """ALTER TABLE feedback ADD COLUMN IF NOT EXISTS read_at TIMESTAMPTZ""",
        """CREATE INDEX IF NOT EXISTS idx_feedback_read_cleanup ON feedback(academy_id,is_read,read_at)""",
        """UPDATE feedback SET read_at=NOW() WHERE is_read=TRUE AND read_at IS NULL""",
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
        """ALTER TABLE events ADD COLUMN IF NOT EXISTS target_all_players BOOLEAN NOT NULL DEFAULT TRUE""",
        """CREATE TABLE IF NOT EXISTS event_players(academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,PRIMARY KEY(event_id,player_id))""",
        """CREATE INDEX IF NOT EXISTS idx_event_players_player ON event_players(academy_id,player_id,event_id)""",
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
        """CREATE TABLE IF NOT EXISTS coach_teams(
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            coach_id BIGINT NOT NULL REFERENCES coaches(id) ON DELETE CASCADE,
            team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(coach_id,team_id))""",
        "CREATE INDEX IF NOT EXISTS idx_coach_teams_team ON coach_teams(academy_id,team_id,coach_id)",
        """INSERT INTO coach_teams(academy_id,coach_id,team_id)
            SELECT academy_id,coach_id,id FROM teams WHERE coach_id IS NOT NULL
            ON CONFLICT(coach_id,team_id) DO NOTHING""",
        """CREATE TABLE IF NOT EXISTS specialist_profiles(
            id BIGSERIAL PRIMARY KEY,
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL,
            specialist_type TEXT NOT NULL DEFAULT 'conditioning' CHECK(specialist_type IN ('conditioning','goalkeeper','medical','psychologist','other')),
            qualification TEXT,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS specialist_teams(
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            specialist_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            assigned_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(specialist_user_id,team_id))""",
        "CREATE INDEX IF NOT EXISTS idx_specialist_teams_user ON specialist_teams(academy_id,specialist_user_id,team_id)",
        """CREATE TABLE IF NOT EXISTS specialist_player_notes(
            id BIGSERIAL PRIMARY KEY,
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            specialist_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            observation_date DATE NOT NULL DEFAULT CURRENT_DATE,
            technique INTEGER NOT NULL CHECK(technique BETWEEN 1 AND 5),
            teamwork INTEGER NOT NULL CHECK(teamwork BETWEEN 1 AND 5),
            effort INTEGER NOT NULL CHECK(effort BETWEEN 1 AND 5),
            discipline INTEGER NOT NULL CHECK(discipline BETWEEN 1 AND 5),
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(specialist_user_id,player_id,observation_date))""",
        "CREATE INDEX IF NOT EXISTS idx_specialist_notes_history ON specialist_player_notes(academy_id,specialist_user_id,player_id,observation_date DESC)",
        """CREATE TABLE IF NOT EXISTS specialist_messages(
            id BIGSERIAL PRIMARY KEY,
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            specialist_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sender_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
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
        """ALTER TABLE coaches ADD COLUMN IF NOT EXISTS birth_date DATE""",
        """ALTER TABLE specialist_profiles DROP CONSTRAINT IF EXISTS specialist_profiles_specialist_type_check""",
        """ALTER TABLE specialist_profiles ADD CONSTRAINT specialist_profiles_specialist_type_check CHECK(specialist_type IN ('conditioning','goalkeeper','medical','psychologist','other'))""",
        """ALTER TABLE specialist_profiles ADD COLUMN IF NOT EXISTS profile_image BYTEA""",
        """ALTER TABLE specialist_profiles ADD COLUMN IF NOT EXISTS profile_image_mime TEXT""",
        """ALTER TABLE specialist_profiles ADD COLUMN IF NOT EXISTS birth_date DATE""",
        """ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_image BYTEA""",
        """ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_image_mime TEXT""",
        """ALTER TABLE academies ALTER COLUMN default_fee SET DEFAULT 35.00""",
        """ALTER TABLE fee_templates ALTER COLUMN amount SET DEFAULT 35.00""",
        """UPDATE academies SET default_fee=35.00 WHERE default_fee=50.00""",
        """UPDATE fee_templates SET amount=35.00 WHERE amount=50.00""",
        """ALTER TABLE academies ADD COLUMN IF NOT EXISTS parent_help_video_url TEXT""",
        """ALTER TABLE academies ADD COLUMN IF NOT EXISTS lions_anthem_video_url TEXT""",
        """ALTER TABLE players ADD COLUMN IF NOT EXISTS position TEXT""",
        """ALTER TABLE teams ADD COLUMN IF NOT EXISTS standings_url TEXT""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS confirmation_status TEXT NOT NULL DEFAULT 'unreported'""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS parent_reported_at TIMESTAMPTZ""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS payment_reported_date DATE""",
        """ALTER TABLE player_fees ADD COLUMN IF NOT EXISTS confirmed_by BIGINT REFERENCES users(id) ON DELETE SET NULL""",
        """CREATE TABLE IF NOT EXISTS match_reports(
            id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, team_id BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            opponent_name TEXT NOT NULL DEFAULT 'Друг отбор', home_score INTEGER NOT NULL DEFAULT 0 CHECK(home_score>=0),
            away_score INTEGER NOT NULL DEFAULT 0 CHECK(away_score>=0), standings_url TEXT, confirmed BOOLEAN NOT NULL DEFAULT FALSE,
            confirmed_by BIGINT REFERENCES users(id) ON DELETE SET NULL, confirmed_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(event_id))""",
        """ALTER TABLE match_reports ADD COLUMN IF NOT EXISTS outcome TEXT CHECK(outcome IN ('win','draw','loss'))""",
        """ALTER TABLE match_reports ADD COLUMN IF NOT EXISTS has_exact_score BOOLEAN NOT NULL DEFAULT TRUE""",
        """ALTER TABLE match_reports ADD COLUMN IF NOT EXISTS result_mode TEXT NOT NULL DEFAULT 'detailed' CHECK(result_mode IN ('simple','detailed'))""",
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
        """CREATE TABLE IF NOT EXISTS event_attachments(
            id BIGSERIAL PRIMARY KEY,
            academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
            event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            file_name TEXT NOT NULL,
            file_mime TEXT,
            file_data BYTEA NOT NULL,
            uploaded_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE INDEX IF NOT EXISTS idx_event_attachments_event ON event_attachments(academy_id,event_id,created_at DESC)""",
        """ALTER TABLE player_evaluations ADD COLUMN IF NOT EXISTS match_bonus NUMERIC(4,2) NOT NULL DEFAULT 0""",
        """CREATE INDEX IF NOT EXISTS idx_fees_player ON player_fees(academy_id,player_id,fee_year,fee_month)""",
      """CREATE TABLE IF NOT EXISTS shop_products(
          id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
          name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'Екипировка', description TEXT,
          price NUMERIC(12,2) NOT NULL CHECK(price>=0), size_info TEXT, stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK(stock_quantity>=0),
          min_stock INTEGER NOT NULL DEFAULT 0 CHECK(min_stock>=0), image_data BYTEA, image_mime TEXT,
          active BOOLEAN NOT NULL DEFAULT TRUE, created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
      """CREATE INDEX IF NOT EXISTS idx_shop_products_active ON shop_products(academy_id,active,category,name)""",
      """CREATE TABLE IF NOT EXISTS shop_product_variants(
          id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
          product_id BIGINT NOT NULL REFERENCES shop_products(id) ON DELETE CASCADE,
          model_name TEXT NOT NULL DEFAULT 'Стандартен модел', size_name TEXT NOT NULL DEFAULT 'Универсален',
          sku TEXT, quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity>=0), min_quantity INTEGER NOT NULL DEFAULT 0 CHECK(min_quantity>=0),
          active BOOLEAN NOT NULL DEFAULT TRUE, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(product_id,model_name,size_name))""",
      """ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS variant_id BIGINT REFERENCES shop_product_variants(id) ON DELETE SET NULL""",
      """CREATE TABLE IF NOT EXISTS shop_orders(
          id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
          product_id BIGINT NOT NULL REFERENCES shop_products(id) ON DELETE RESTRICT,
          parent_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
          quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity>0), unit_price NUMERIC(12,2) NOT NULL,
          selected_size TEXT, payment_method TEXT NOT NULL CHECK(payment_method IN ('bank','card')),
          payment_status TEXT NOT NULL DEFAULT 'pending' CHECK(payment_status IN ('pending','paid','rejected')),
          fulfilment_status TEXT NOT NULL DEFAULT 'new' CHECK(fulfilment_status IN ('new','available','ordered','ready','delivered','cancelled')),
          admin_comment TEXT, parent_note TEXT, confirmed_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
          paid_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
      """CREATE INDEX IF NOT EXISTS idx_shop_orders_status ON shop_orders(academy_id,payment_status,fulfilment_status,created_at DESC)""",
      """ALTER TABLE shop_orders ALTER COLUMN parent_user_id DROP NOT NULL""",
      """ALTER TABLE shop_orders ALTER COLUMN player_id DROP NOT NULL""",
      """ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS requester_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL""",
      """ALTER TABLE shop_orders ADD COLUMN IF NOT EXISTS requester_name TEXT""",
      """ALTER TABLE shop_orders DROP CONSTRAINT IF EXISTS shop_orders_payment_method_check""",
      """ALTER TABLE shop_orders ADD CONSTRAINT shop_orders_payment_method_check CHECK(payment_method IN ('cash','bank','card'))""",
      """ALTER TABLE shop_orders DROP CONSTRAINT IF EXISTS shop_orders_fulfilment_status_check""",
      """ALTER TABLE shop_orders ADD CONSTRAINT shop_orders_fulfilment_status_check CHECK(fulfilment_status IN ('new','reviewed','available','ordered','ready','delivered','cancelled'))""",
      """CREATE TABLE IF NOT EXISTS club_inventory(
          id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
          item_name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'Други', quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity>=0),
          condition_status TEXT NOT NULL DEFAULT 'good' CHECK(condition_status IN ('new','good','worn','damaged','retired')),
          purchase_date DATE, replacement_due DATE, note TEXT, active BOOLEAN NOT NULL DEFAULT TRUE,
          created_by BIGINT REFERENCES users(id) ON DELETE SET NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
      """CREATE TABLE IF NOT EXISTS training_resources(
          id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
          title TEXT NOT NULL, resource_type TEXT NOT NULL DEFAULT 'link' CHECK(resource_type IN ('video','link','image')),
          url TEXT, description TEXT, image_data BYTEA, image_mime TEXT, active BOOLEAN NOT NULL DEFAULT TRUE,
          created_by BIGINT REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
      """CREATE TABLE IF NOT EXISTS coach_inbox_messages(
          id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
          coach_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          admin_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
          is_read BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
      """ALTER TABLE coach_inbox_messages ADD COLUMN IF NOT EXISTS read_at TIMESTAMPTZ""",
      """CREATE INDEX IF NOT EXISTS idx_coach_inbox_read_cleanup ON coach_inbox_messages(academy_id,is_read,read_at)""",
      """UPDATE coach_inbox_messages SET read_at=NOW() WHERE is_read=TRUE AND read_at IS NULL""",
      """CREATE TABLE IF NOT EXISTS staff_direct_messages(
          id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
          sender_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
          file_name TEXT, file_mime TEXT, file_data BYTEA, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
      """CREATE TABLE IF NOT EXISTS staff_direct_message_recipients(
          message_id BIGINT NOT NULL REFERENCES staff_direct_messages(id) ON DELETE CASCADE,
          recipient_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, is_read BOOLEAN NOT NULL DEFAULT FALSE,
          PRIMARY KEY(message_id,recipient_user_id))""",
      """CREATE INDEX IF NOT EXISTS idx_staff_direct_recipient ON staff_direct_message_recipients(recipient_user_id,is_read,message_id)""",
      """CREATE TABLE IF NOT EXISTS staff_chat_messages(
          id BIGSERIAL PRIMARY KEY, academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
          sender_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL, body TEXT,
          file_name TEXT, file_mime TEXT, file_data BYTEA, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
      """CREATE INDEX IF NOT EXISTS idx_staff_chat_created ON staff_chat_messages(academy_id,created_at DESC)""",
      """CREATE TABLE IF NOT EXISTS push_subscriptions(
          id BIGSERIAL PRIMARY KEY,academy_id BIGINT NOT NULL REFERENCES academies(id) ON DELETE CASCADE,
          user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,endpoint TEXT NOT NULL UNIQUE,
          p256dh TEXT NOT NULL,auth TEXT NOT NULL,user_agent TEXT,active BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
      """CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions(academy_id,user_id,active)""",
    ]
    with db() as conn, conn.cursor() as cur:
        for sql in statements: cur.execute(sql)
        cur.execute("""INSERT INTO academies(name,slug,city,logo_file,default_fee)
            VALUES('Dyulgerov Academy',%s,'Благоевград','лого.jpg',35.00)
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
            SELECT %s,id,35.00 FROM teams WHERE academy_id=%s ON CONFLICT DO NOTHING""", (academy_id,academy_id))
    return academy_id

try:
    ACADEMY_ID = _retry_db_call(init_db, attempts=3)

except (psycopg2.OperationalError, psycopg2.InterfaceError):
    st.markdown("## 🔄 Връзката беше прекъсната")
    st.info(
        "Временна връзка с базата данни беше прекъсната. "
        "Натиснете „Опитай отново“ за ново автоматично свързване."
    )

    if st.button(
        "🔄 Опитай отново",
        key="db_startup_retry",
        use_container_width=True,
        type="primary",
    ):
        reset_db_pool()
        st.rerun()

    st.stop()
# --- ONE-TIME NOTIFICATION CLEAN START ---------------------------------------
# Clears only old in-app bell notifications once before parent rollout.
# Push subscriptions, messages, chats, schedules and all future notifications stay intact.
NOTIFICATION_CLEAN_START_KEY = "notification_clean_start_parent_rollout_2026_11_23_v1"

def run_one_time_notification_clean_start():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS app_migrations(
            migration_key TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (NOTIFICATION_CLEAN_START_KEY,))
        cur.execute("SELECT 1 FROM app_migrations WHERE migration_key=%s", (NOTIFICATION_CLEAN_START_KEY,))
        if cur.fetchone():
            return False
        cur.execute("DELETE FROM notifications WHERE academy_id=%s", (ACADEMY_ID,))
        cur.execute("INSERT INTO app_migrations(migration_key) VALUES(%s)", (NOTIFICATION_CLEAN_START_KEY,))
        return True

try:
    _retry_db_call(run_one_time_notification_clean_start, attempts=3)
except (psycopg2.OperationalError, psycopg2.InterfaceError):
    pass

# --- ONE-TIME MATCH TEST DATA RESET -----------------------------------------
# Deletes only match-related test data that existed before the cutoff below.
# The migration marker makes this safe to keep in the code: it runs once only.
MATCH_RESET_KEY = "match_test_reset_before_2026_11_05_12_51_v1"
MATCH_RESET_CUTOFF = "2026-11-05 12:51:00+02"

def run_one_time_match_test_reset():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS app_migrations(
            migration_key TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        # Transaction-level advisory lock prevents two Render workers/sessions
        # from executing the same destructive migration at the same time.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (MATCH_RESET_KEY,))
        cur.execute("SELECT 1 FROM app_migrations WHERE migration_key=%s", (MATCH_RESET_KEY,))
        if cur.fetchone():
            return False

        cur.execute("""SELECT id FROM events
                       WHERE academy_id=%s AND event_type='match'
                         AND created_at <= %s::timestamptz""",
                    (ACADEMY_ID, MATCH_RESET_CUTOFF))
        event_ids = [r[0] for r in cur.fetchall()]

        if event_ids:
            # Remove match notifications generated by the old test flow.
            cur.execute("""DELETE FROM notifications
                           WHERE academy_id=%s
                             AND notification_type IN ('match','match_squad','match_result')
                             AND created_at <= %s::timestamptz""",
                        (ACADEMY_ID, MATCH_RESET_CUTOFF))
            # Poll responses are removed by polls' ON DELETE CASCADE.
            # Squads/reports/lineups/stats/attachments are removed by events' ON DELETE CASCADE.
            cur.execute("DELETE FROM events WHERE academy_id=%s AND id = ANY(%s)",
                        (ACADEMY_ID, event_ids))

        cur.execute("INSERT INTO app_migrations(migration_key) VALUES(%s)", (MATCH_RESET_KEY,))
        return True

try:
    _retry_db_call(run_one_time_match_test_reset, attempts=3)

except (psycopg2.OperationalError, psycopg2.InterfaceError):
    st.markdown("## 🔄 Връзката беше прекъсната")
    st.info(
        "Временна връзка с базата данни беше прекъсната. "
        "Натиснете „Опитай отново“ за ново автоматично свързване."
    )

    if st.button(
        "🔄 Опитай отново",
        key="db_reset_retry",
        use_container_width=True,
        type="primary",
    ):
        reset_db_pool()
        st.rerun()

    st.stop()
# ---------------------------------------------------------------------------
# ============================================================
# ONE-TIME FINAL TEST DATA RESET
# Нулира тестови плащания + тестови мачове.
# Изпълнява се САМО ВЕДНЪЖ.
# ============================================================

FINAL_TEST_RESET_KEY = "final_test_reset_payments_matches_2026_11_07_v1"


def run_final_test_data_reset():
    with db() as conn, conn.cursor() as cur:

        # Таблицата вече съществува, но я подсигуряваме.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_migrations(
                migration_key TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Защита от едновременно стартиране от две сесии.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (FINAL_TEST_RESET_KEY,)
        )

        # Ако вече е изпълнено, НЕ правим нищо.
        cur.execute(
            "SELECT 1 FROM app_migrations WHERE migration_key=%s",
            (FINAL_TEST_RESET_KEY,)
        )

        if cur.fetchone():
            return False

        # ====================================================
        # 1. НУЛИРАНЕ НА ПЛАЩАНИЯТА
        # Начислените такси остават, но стават ДЪЛЖИМИ.
        # ====================================================

        cur.execute("""
            UPDATE player_fees
            SET
                status = 'due',
                payment_method = NULL,
                payment_note = NULL,
                bank_transfer_date = NULL,
                payment_recorded_by = NULL,
                confirmation_status = 'unreported',
                parent_reported_at = NULL,
                payment_reported_date = NULL,
                confirmed_by = NULL,
                paid_at = NULL
            WHERE academy_id=%s
        """, (ACADEMY_ID,))

        # ====================================================
        # 2. НАМИРАМЕ ВСИЧКИ ТЕСТОВИ МАЧОВЕ
        # Само event_type='match'.
        # Тренировките НЕ се засягат.
        # ====================================================

        cur.execute("""
            SELECT id
            FROM events
            WHERE academy_id=%s
              AND event_type='match'
        """, (ACADEMY_ID,))

        match_event_ids = [row[0] for row in cur.fetchall()]

        if match_event_ids:

            # Изчистваме известията, генерирани от тестовите мачове.
            cur.execute("""
                DELETE FROM notifications
                WHERE academy_id=%s
                  AND notification_type IN (
                      'match',
                      'match_squad',
                      'match_result'
                  )
            """, (ACADEMY_ID,))

            # Изтриваме самите match events.
            #
            # Свързаните:
            # - match_reports
            # - match_squads
            # - match_lineups
            # - match_player_stats
            # - event_attachments
            #
            # се изтриват чрез ON DELETE CASCADE.
            cur.execute("""
                DELETE FROM events
                WHERE academy_id=%s
                  AND id = ANY(%s)
            """, (ACADEMY_ID, match_event_ids))

        # ====================================================
        # 3. ЗАПИСВАМЕ, ЧЕ RESET-ЪТ Е ИЗПЪЛНЕН
        # ====================================================

        cur.execute("""
            INSERT INTO app_migrations(migration_key)
            VALUES(%s)
        """, (FINAL_TEST_RESET_KEY,))

        return True


try:
    _retry_db_call(run_final_test_data_reset, attempts=3)

except (psycopg2.OperationalError, psycopg2.InterfaceError):

    st.markdown("## 🔄 Връзката беше прекъсната")

    st.info(
        "Натиснете „Опитай отново“ за ново автоматично свързване."
    )

    if st.button(
        "🔄 Опитай отново",
        key="final_test_reset_retry",
        use_container_width=True,
        type="primary",
    ):
        reset_db_pool()
        st.rerun()

    st.stop()
# ONE-TIME CLEAN ROLLOUT RESET
CLEAN_ROLLOUT_RESET_KEY="academy_clean_rollout_2026_11_24_v2"
def run_clean_rollout_reset():
    with db() as conn,conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS app_migrations(migration_key TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",(CLEAN_ROLLOUT_RESET_KEY,));cur.execute("SELECT 1 FROM app_migrations WHERE migration_key=%s",(CLEAN_ROLLOUT_RESET_KEY,))
        if cur.fetchone():return False
        for table in ("public_posts","player_fees","expenses","shop_orders","feedback","coach_inbox_messages","specialist_messages","staff_direct_messages","staff_chat_messages","fan_chat_messages","notifications"):
            cur.execute(f"DELETE FROM {table} WHERE academy_id=%s",(ACADEMY_ID,))
        cur.execute("DELETE FROM polls WHERE academy_id=%s AND poll_type='global'",(ACADEMY_ID,))
        cur.execute("INSERT INTO app_migrations(migration_key) VALUES(%s)",(CLEAN_ROLLOUT_RESET_KEY,));return True
try:_retry_db_call(run_clean_rollout_reset,attempts=3)
except (psycopg2.OperationalError,psycopg2.InterfaceError):pass

# --- ONE-TIME TRAINING CUTOFF: DECEMBER 22, 2026 ---------------------------
TRAINING_DECEMBER_CUTOFF_KEY = "training_cutoff_after_2026_12_22_v1"
def run_one_time_training_december_cutoff():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS app_migrations(
            migration_key TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (TRAINING_DECEMBER_CUTOFF_KEY,))
        cur.execute("SELECT 1 FROM app_migrations WHERE migration_key=%s", (TRAINING_DECEMBER_CUTOFF_KEY,))
        if cur.fetchone():
            return False
        cur.execute("""DELETE FROM events
            WHERE academy_id=%s AND event_type='training'
              AND starts_at >= TIMESTAMPTZ '2026-12-23 00:00:00+02'
              AND starts_at < TIMESTAMPTZ '2027-01-01 00:00:00+02'""", (ACADEMY_ID,))
        cur.execute("INSERT INTO app_migrations(migration_key) VALUES(%s)", (TRAINING_DECEMBER_CUTOFF_KEY,))
        return True
try:
    _retry_db_call(run_one_time_training_december_cutoff, attempts=3)
except (psycopg2.OperationalError, psycopg2.InterfaceError):
    pass

# --- ONE-TIME RESET: TEAM 2017 MATCH HISTORY AND COACH MESSAGES ------------
# Clears only completed match history and coach-to-parent messages for birth year 2017.
# Upcoming matches, training schedules, players, fees and all other teams remain intact.
TEAM_2017_CLEAN_RESET_KEY = "team_2017_matches_messages_reset_2026_11_24_v1"
def run_one_time_team_2017_clean_reset():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS app_migrations(
            migration_key TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (TEAM_2017_CLEAN_RESET_KEY,))
        cur.execute("SELECT 1 FROM app_migrations WHERE migration_key=%s", (TEAM_2017_CLEAN_RESET_KEY,))
        if cur.fetchone():
            return False

        cur.execute("SELECT id FROM teams WHERE academy_id=%s AND birth_year=2017", (ACADEMY_ID,))
        team_ids = [row[0] for row in cur.fetchall()]
        if team_ids:
            # Remove the visible coach-message archive for parents of team 2017.
            cur.execute("DELETE FROM team_messages WHERE academy_id=%s AND team_id=ANY(%s)", (ACADEMY_ID, team_ids))

            # Remove bell entries generated by those coach messages for parents linked to team 2017.
            cur.execute("""DELETE FROM notifications n
                WHERE n.academy_id=%s AND n.notification_type='message'
                  AND n.user_id IN (
                      SELECT DISTINCT pp.parent_user_id
                      FROM parent_players pp
                      JOIN players p ON p.id=pp.player_id AND p.academy_id=pp.academy_id
                      WHERE pp.academy_id=%s AND p.team_id=ANY(%s)
                  )""", (ACADEMY_ID, ACADEMY_ID, team_ids))

            # Remove only completed/archived matches. Related reports, squads,
            # lineups, stats, polls and attachments are removed by ON DELETE CASCADE.
            cur.execute("""SELECT DISTINCT e.id
                FROM events e
                JOIN match_reports mr ON mr.event_id=e.id AND mr.academy_id=e.academy_id
                WHERE e.academy_id=%s AND e.team_id=ANY(%s)
                  AND e.event_type='match' AND mr.confirmed""", (ACADEMY_ID, team_ids))
            completed_match_ids = [row[0] for row in cur.fetchall()]
            if completed_match_ids:
                cur.execute("""DELETE FROM notifications
                    WHERE academy_id=%s
                      AND notification_type IN ('match_result','match_squad','match')
                      AND user_id IN (
                          SELECT DISTINCT pp.parent_user_id
                          FROM parent_players pp
                          JOIN players p ON p.id=pp.player_id AND p.academy_id=pp.academy_id
                          WHERE pp.academy_id=%s AND p.team_id=ANY(%s)
                      )""", (ACADEMY_ID, ACADEMY_ID, team_ids))
                cur.execute("DELETE FROM events WHERE academy_id=%s AND id=ANY(%s)", (ACADEMY_ID, completed_match_ids))

        cur.execute("INSERT INTO app_migrations(migration_key) VALUES(%s)", (TEAM_2017_CLEAN_RESET_KEY,))
        return True
try:
    _retry_db_call(run_one_time_team_2017_clean_reset, attempts=3)
except (psycopg2.OperationalError, psycopg2.InterfaceError):
    pass

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
            margin-top: 56px !important;
            max-width: 820px !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }}
        .st-key-parent_nav div[data-testid="stHorizontalBlock"] {{
            gap: 6px !important;
        }}
        .st-key-parent_nav div[data-testid="stButton"] > button {{
            min-height: 40px !important;
            padding: 6px 10px !important;
            font-size: 0.76rem !important;
        }}
        
        [data-testid="stSidebar"] [data-testid="stRadio"] {{
            margin-top: 18px !important;
        }}
        @media (max-width: 768px) {{
            /* Компактно меню за родителския профил на телефон */
            .st-key-parent_nav {{margin-top:22px !important;max-width:100% !important;}}
            .st-key-parent_nav div[data-testid="stHorizontalBlock"] {{display:flex !important;flex-direction:row !important;flex-wrap:nowrap !important;gap:7px !important;margin-bottom:7px !important;}}
            .st-key-parent_nav div[data-testid="stColumn"] {{flex:1 1 50% !important;width:50% !important;min-width:0 !important;}}
            .st-key-parent_nav div[data-testid="stButton"] > button {{width:100% !important;min-height:42px !important;height:42px !important;padding:4px 7px !important;border-radius:11px !important;font-size:.72rem !important;font-weight:800 !important;line-height:1.08 !important;white-space:normal !important;box-shadow:0 5px 12px rgba(0,0,0,.24) !important;}}
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

@st.cache_data(ttl=300,show_spinner=False)
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
             AND ls.last_seen >= NOW() - INTERVAL '7 days'
             AND u.active AND u.account_status='approved'""",
        (ACADEMY_ID, session_token_hash(token)),
    )
    if not session:
        try:
            execute("DELETE FROM login_sessions WHERE token_hash=%s", (session_token_hash(token),))
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            pass
        st.session_state.pop("user_id", None)
        st.session_state.pop("login_token", None)
        st.query_params.pop("session", None)
        return None

    now_ts=datetime.now().timestamp()
    if now_ts-float(st.session_state.get("session_touch_ts",0)) > 300:
        execute("UPDATE login_sessions SET last_seen=NOW() WHERE token_hash=%s", (session_token_hash(token),))
        st.session_state.session_touch_ts=now_ts
    st.session_state.user_id = session["id"]
    st.session_state.login_token = token
    if st.query_params.get("session") != token:
        st.query_params["session"] = token
    return session

def browser_push_ready():
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_SUBJECT)

def vapid_private_key_shape_ok():
    try:
        padding="="*((4-len(VAPID_PRIVATE_KEY)%4)%4)
        raw=base64.urlsafe_b64decode(VAPID_PRIVATE_KEY+padding)
        return len(raw)==32
    except Exception:
        return False

def vapid_key_fingerprint():
    return hashlib.sha256(VAPID_PUBLIC_KEY.encode("utf-8")).hexdigest() if VAPID_PUBLIC_KEY else ""

def deactivate_subscriptions_from_old_vapid_key():
    if not browser_push_ready(): return
    execute("""CREATE TABLE IF NOT EXISTS push_config_state(academy_id BIGINT PRIMARY KEY REFERENCES academies(id) ON DELETE CASCADE,public_key_fingerprint TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    current=vapid_key_fingerprint()
    old=fetchone("SELECT public_key_fingerprint FROM push_config_state WHERE academy_id=%s",(ACADEMY_ID,))
    if old and old.get("public_key_fingerprint")!=current:
        execute("UPDATE push_subscriptions SET active=FALSE,updated_at=NOW() WHERE academy_id=%s AND active",(ACADEMY_ID,))
    execute("""INSERT INTO push_config_state(academy_id,public_key_fingerprint,updated_at) VALUES(%s,%s,NOW()) ON CONFLICT(academy_id) DO UPDATE SET public_key_fingerprint=EXCLUDED.public_key_fingerprint,updated_at=NOW()""",(ACADEMY_ID,current))

deactivate_subscriptions_from_old_vapid_key()

def save_push_subscription(user_id, subscription, user_agent=None):
    if not isinstance(subscription, dict): return False
    keys=subscription.get("keys") or {}
    if not subscription.get("endpoint") or not keys.get("p256dh") or not keys.get("auth"): return False
    execute("""INSERT INTO push_subscriptions(academy_id,user_id,endpoint,p256dh,auth,user_agent,active,updated_at)
               VALUES(%s,%s,%s,%s,%s,%s,TRUE,NOW())
               ON CONFLICT(endpoint) DO UPDATE SET user_id=EXCLUDED.user_id,p256dh=EXCLUDED.p256dh,
               auth=EXCLUDED.auth,user_agent=EXCLUDED.user_agent,active=TRUE,updated_at=NOW()""",
            (ACADEMY_ID,user_id,subscription["endpoint"],keys["p256dh"],keys["auth"],user_agent))
    return True

def send_browser_push(user_id,title,body="",return_details=False):
    if not browser_push_ready() or not user_id:
        r={"sent":0,"errors":["Липсва VAPID конфигурация или user_id."],"attempted":0};return r if return_details else 0
    if not vapid_private_key_shape_ok():
        r={"sent":0,"errors":["VAPID_PRIVATE_KEY не е валиден 32-byte P-256 raw base64url ключ. Подмени го с новата стойност от PRIVATE_RENDER_KEYS.txt."],"attempted":0};return r if return_details else 0
    rows=fetchall("SELECT id,endpoint,p256dh,auth FROM push_subscriptions WHERE academy_id=%s AND user_id=%s AND active",(ACADEMY_ID,user_id));payload=json.dumps({"title":title,"body":body or "Имате ново известие от Dyulgerov Academy.","url":"/"},ensure_ascii=False);sent=0;errors=[]
    for row in rows:
        try:
            webpush(subscription_info={"endpoint":row["endpoint"],"keys":{"p256dh":row["p256dh"],"auth":row["auth"]}},data=payload,vapid_private_key=VAPID_PRIVATE_KEY,vapid_claims={"sub":VAPID_SUBJECT},ttl=300);sent+=1
        except WebPushException as exc:
            response=getattr(exc,"response",None);status=getattr(response,"status_code",None);text=(getattr(response,"text",None) or "").strip() if response is not None else "";host=row["endpoint"].split("/")[2] if "://" in row["endpoint"] else "push endpoint";errors.append(f"{host}: HTTP {status or '?'} · {text or str(exc)}"[:1200])
            if status in (404,410):execute("UPDATE push_subscriptions SET active=FALSE,updated_at=NOW() WHERE id=%s",(row["id"],))
        except Exception as exc:errors.append(f"{type(exc).__name__}: {exc}"[:1200])
    r={"sent":sent,"errors":errors,"attempted":len(rows)};return r if return_details else sent

def create_notification(user_id, notification_type, title, body=""):
    execute("INSERT INTO notifications(academy_id,user_id,notification_type,title,body) VALUES(%s,%s,%s,%s,%s)",(ACADEMY_ID,user_id,notification_type,title,body))
    send_browser_push(user_id,title,body)

def unread_notification_count(user_id):
    row = fetchone(
        "SELECT COUNT(*) c FROM notifications WHERE academy_id=%s AND user_id=%s AND NOT is_read",
        (ACADEMY_ID, user_id),
    )
    return row["c"] if row else 0


def notification_center(user):
    st.subheader("🔔 Известия")
    rows = fetchall(
        "SELECT * FROM notifications WHERE academy_id=%s AND user_id=%s AND NOT is_read ORDER BY created_at DESC LIMIT 50",
        (ACADEMY_ID, user["id"]),
    )
    if not rows:
        st.info("Няма нови известия.")
        return
    if st.button("✅ Маркирай всички като прочетени", key=f"read_all_{user['id']}", use_container_width=True):
        execute("DELETE FROM notifications WHERE academy_id=%s AND user_id=%s", (ACADEMY_ID, user["id"]))
        set_flash("✅ Известията са прочетени и премахнати от камбанката.")
        st.rerun()
    for row in rows:
        with st.container(border=True):
            st.write(f"🔴 **{row['title']}**")
            if row.get("body"): st.write(row["body"])
            st.caption(row["created_at"].strftime("%d.%m.%Y %H:%M"))


def compact_notification_bell(user,key_prefix="notifications"):
    unread=unread_notification_count(user["id"])
    label=f"🔔 {unread}" if unread else "🔔"
    with st.popover(label,use_container_width=True):
        st.markdown("### Известия")
        rows=fetchall("SELECT * FROM notifications WHERE academy_id=%s AND user_id=%s AND NOT is_read ORDER BY created_at DESC LIMIT 50",(ACADEMY_ID,user["id"]))
        if not rows:st.caption("Няма нови известия.")
        else:
            if st.button("✅ Маркирай всички като прочетени",key=f"{key_prefix}_read_all_{user['id']}",use_container_width=True):
                execute("DELETE FROM notifications WHERE academy_id=%s AND user_id=%s",(ACADEMY_ID,user["id"]));st.rerun()
            for row in rows:
                st.write(f"**{row['title']}**")
                if row.get("body"):st.caption(row["body"])

def notify_team_parents(team_id, notification_type, title, body=""):
    parents = fetchall(
        """SELECT DISTINCT u.id AS user_id
           FROM users u
           JOIN parent_players pp ON pp.parent_user_id=u.id AND pp.academy_id=u.academy_id
           JOIN players p ON p.id=pp.player_id AND p.academy_id=pp.academy_id
           WHERE u.academy_id=%s AND u.role='parent' AND u.active
             AND u.account_status='approved' AND p.team_id=%s
             AND p.active AND p.approval_status='approved'""",
        (ACADEMY_ID, team_id),
    )
    for parent in parents:
        create_notification(parent["user_id"], notification_type, title, body)
    return len(parents)


def notify_selected_players_parents(player_ids,notification_type,title,body=""):
    if not player_ids:return 0
    rows=fetchall("""SELECT DISTINCT u.id user_id FROM users u JOIN parent_players pp ON pp.parent_user_id=u.id AND pp.academy_id=u.academy_id WHERE u.academy_id=%s AND u.role='parent' AND u.active AND u.account_status='approved' AND pp.player_id=ANY(%s)""",(ACADEMY_ID,player_ids))
    for row in rows:create_notification(row["user_id"],notification_type,title,body)
    return len(rows)

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
        st.markdown('</div>', unsafe_allow_html=True)

def normalize_video_url(url):
    url=(url or "").strip()
    if "youtube.com/shorts/" in url:
        video_id=url.split("youtube.com/shorts/",1)[1].split("?",1)[0].split("/",1)[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    if "youtu.be/" in url:
        video_id=url.split("youtu.be/",1)[1].split("?",1)[0].split("/",1)[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    return url

def public_home():
    hero()
    video_url = academy().get("parent_help_video_url")
    anthem_video_url = academy().get("lions_anthem_video_url")

    motto_col, anthem_col = st.columns([2.3, 1.2], vertical_alignment="center")
    with motto_col:
        st.caption("Където успехът е танц на таланта")
    with anthem_col:
        with st.expander("🦁 Химнът на лъвовете 🎶", expanded=False):
            st.caption("Официалният химн на Dyulgerov Academy.")
            if anthem_video_url: st.video(normalize_video_url(anthem_video_url))
            else: st.info("Химнът все още не е добавен.")

    help_col, _ = st.columns([1.2, 2.8])
    with help_col:
        with st.expander("▶️ Как се използва приложението", expanded=False):
            st.caption("Кратко видео за родителския профил.")
            if video_url: st.video(video_url)
            else: st.info("Видео инструкцията все още не е добавена.")

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
    # Поетапно пазим само последните 10 дни. Изтриването се изпълнява при всяко отваряне/изпращане в чата.
    execute(
        "DELETE FROM fan_chat_messages WHERE academy_id=%s AND team_id=%s AND created_at < NOW() - INTERVAL '10 days'",
        (ACADEMY_ID, team_id),
    )

    st.subheader("🗣️ ФЕН ЧАТ")
    st.caption("Чатът е само за родителите в същия набор. Съобщенията се пазят 10 дни.")

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
                fan_recipients=fetchall(
                    """SELECT DISTINCT u.id AS user_id
                       FROM users u
                       JOIN parent_players pp ON pp.parent_user_id=u.id AND pp.academy_id=u.academy_id
                       JOIN players p ON p.id=pp.player_id AND p.academy_id=pp.academy_id
                       WHERE u.academy_id=%s AND u.role='parent' AND u.active
                         AND u.account_status='approved' AND p.team_id=%s
                         AND p.active AND p.approval_status='approved' AND u.id<>%s""",
                    (ACADEMY_ID,team_id,user["id"]),
                )
                sender_name=f"{user['first_name']} {user['last_name']}".strip()
                preview=(body.strip()[:120] if body.strip() else "Изпратена е снимка.")
                for recipient in fan_recipients:
                    create_notification(recipient["user_id"],"fan_chat",f"Ново съобщение във ФЕН ЧАТ · {sender_name}",preview)
                st.rerun()

    rows = fetchall(
        """SELECT f.*,u.first_name,u.last_name,
                  COALESCE(pc.child_labels,'') AS child_labels
           FROM fan_chat_messages f
           JOIN users u ON u.id=f.sender_user_id
           LEFT JOIN LATERAL (
               SELECT STRING_AGG(DISTINCT (p.first_name || ' (' || t.birth_year::text || ')'), ' / ')
                      AS child_labels
               FROM parent_players pp
               JOIN players p ON p.id=pp.player_id AND p.academy_id=pp.academy_id
               JOIN teams t ON t.id=p.team_id AND t.academy_id=p.academy_id
               WHERE pp.academy_id=f.academy_id
                 AND pp.parent_user_id=f.sender_user_id
                 AND p.team_id=f.team_id
                 AND p.active
           ) pc ON TRUE
           WHERE f.academy_id=%s AND f.team_id=%s
             AND f.created_at >= NOW() - INTERVAL '10 days'
           ORDER BY f.created_at DESC LIMIT 100""",
        (ACADEMY_ID, team_id),
    )
    if not rows:
        st.info("Няма съобщения във ФЕН ЧАТ от последните 10 дни.")
    for row in rows:
        with st.container(border=True):
            sender_label = row['first_name']
            if row.get('child_labels'):
                sender_label += f" / {row['child_labels']}"
            st.write(f"**{sender_label}**")
            if row.get("body"):
                st.write(row["body"])
            if row.get("image_data"):
                st.image(bytes(row["image_data"]), use_container_width=True)
            st.caption(row["created_at"].strftime("%d.%m.%Y %H:%M"))


def render_poll(poll,user,child=None):
    pid=child["id"] if child else None
    old=fetchone("SELECT answer,created_at FROM poll_responses WHERE poll_id=%s AND user_id=%s AND player_id IS NOT DISTINCT FROM %s",(poll["id"],user["id"],pid))
    answer_names={"yes":"Да","no":"Не","maybe":"Не съм сигурен/а"}
    with st.container(border=True):
        if poll.get("poll_type") == "match_attendance": st.write("**📊 Потвърждение за участие**")
        else:
            st.write(f"**📊 {poll['title']}**")
            if poll.get("question"): st.write(poll["question"])
        if old and old.get("answer") in answer_names:
            st.success(f"✅ Вие избрахте: **{answer_names[old['answer']]}**")
            if old.get("created_at"):
                st.caption(f"Последно записан отговор: {old['created_at']:%d.%m.%Y %H:%M}. Можете да промените избора си, докато анкетата е активна.")
        vals=["yes","no","maybe"]
        ans=st.radio("Избор",vals,index=vals.index(old["answer"]) if old and old["answer"] in vals else 0,format_func=lambda x:answer_names[x],horizontal=True,key=f"poll_{poll['id']}_{pid or 0}")
        button_label="🔄 Промени отговора" if old else "✅ Запази отговора"
        if st.button(button_label,key=f"poll_save_{poll['id']}_{pid or 0}",use_container_width=True,type="primary"):
            execute("""INSERT INTO poll_responses(academy_id,poll_id,user_id,player_id,answer) VALUES(%s,%s,%s,%s,%s) ON CONFLICT (poll_id,user_id,(COALESCE(player_id,0))) DO UPDATE SET answer=EXCLUDED.answer,created_at=NOW()""",(ACADEMY_ID,poll["id"],user["id"],pid,ans))
            set_flash(f"✅ Отговорът е записан: {answer_names[ans]}. Можете да го промените, докато анкетата е активна.")
            st.rerun()

def payment_rows(team_id=None):
    extra=" AND pf.team_id=%s" if team_id else ""; params=(ACADEMY_ID,team_id) if team_id else (ACADEMY_ID,)
    return fetchall(f"""SELECT pf.id fee_id,t.name team_name,p.first_name,p.last_name,pf.fee_month,pf.fee_year,pf.final_amount,pf.status,pf.payment_method,pf.bank_transfer_date,pf.payment_note,pf.paid_at,pf.confirmation_status,pf.parent_reported_at,pf.payment_reported_date FROM player_fees pf JOIN players p ON p.id=pf.player_id LEFT JOIN teams t ON t.id=pf.team_id WHERE pf.academy_id=%s {extra} ORDER BY t.birth_year,p.last_name,p.first_name,pf.fee_year DESC,pf.fee_month DESC""",params)

def payment_excel(rows):
    names={"cash":"В брой","bank":"По банка","card":"Карта","other":"Друго"}; data=[]
    for r in rows:data.append({"Набор":r.get("team_name") or "","Дете":f"{r['first_name']} {r['last_name']}","Месец":r["fee_month"],"Година":r["fee_year"],"Сума €":float(r["final_amount"]),"Статус":"Платена" if r["status"]=="paid" else "Дължима","Плащане":names.get(r.get("payment_method"),r.get("payment_method") or ""),"Дата превод":r.get("bank_transfer_date"),"Бележка":r.get("payment_note") or "","Платено на":r.get("paid_at")})
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

def payment_method_label(method):
    return {"cash":"В брой","bank":"По банка","card":"Карта","other":"Друго"}.get(method,method or "Не е посочен")


def confirm_fee_payment(fee_id,user_id,method=None,payment_date=None):
    execute(
        """UPDATE player_fees SET status='paid',confirmation_status='confirmed',
           confirmed_by=%s,paid_at=COALESCE(%s::date,CURRENT_DATE),
           payment_method=COALESCE(%s,payment_method),
           payment_reported_date=COALESCE(%s,payment_reported_date)
           WHERE id=%s AND academy_id=%s AND status<>'paid'""",
        (user_id,payment_date,method,payment_date,fee_id,ACADEMY_ID),
    )


def payment_panel(user,team_ids=None,fixed_team_id=None):
    teams=fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year",(ACADEMY_ID,))
    teams=[t for t in teams if team_ids is None or t["id"] in team_ids]
    if not teams: st.info("Няма набори."); return
    team_map={t["name"]:t for t in teams}
    if fixed_team_id is not None:
        team=next((t for t in teams if t["id"]==fixed_team_id),teams[0])
        st.caption(f"Активен набор: {team['name']}")
    else:
        team_name=st.selectbox("Набор",list(team_map),key=f"fee_team_{user['id']}")
        team=team_map[team_name]
    now=date.today(); c1,c2=st.columns(2)
    month=c1.selectbox("Месец",range(1,13),index=now.month-1,key=f"fee_month_{user['id']}_{team['id']}")
    year=int(c2.number_input("Година",2024,2045,now.year,key=f"fee_year_{user['id']}_{team['id']}"))
    rows=[r for r in payment_rows(team["id"]) if int(r["fee_month"])==month and int(r["fee_year"])==year]
    status_filter=st.radio("Покажи",["Всички","Дължими","Чакащи потвърждение","Платени"],horizontal=True,key=f"fee_status_filter_{user['id']}_{team['id']}")
    if status_filter=="Дължими": rows=[r for r in rows if r["status"]!="paid" and (r.get("confirmation_status") or "unreported")=="unreported"]
    elif status_filter=="Чакащи потвърждение": rows=[r for r in rows if (r.get("confirmation_status") or "")=="pending"]
    elif status_filter=="Платени": rows=[r for r in rows if r["status"]=="paid"]
    if not rows: st.info("Няма плащания за избраните набор, период и статус.")
    is_admin=user.get("role")=="admin"
    is_coach=user.get("role")=="coach"
    for r in rows:
        state=r.get("confirmation_status") or ("confirmed" if r["status"]=="paid" else "unreported")
        method=r.get("payment_method")
        icon={"confirmed":"🟢","pending":"🟠","unreported":"🔴"}.get(state,"🔴")
        label={"confirmed":"ПЛАТЕНО","pending":"ЧАКА ПОТВЪРЖДЕНИЕ","unreported":"ДЪЛЖИМА"}.get(state,"ДЪЛЖИМА")
        with st.expander(f"{icon} {r['first_name']} {r['last_name']} · {r['final_amount']:.2f} € · {label}"):
            st.write(f"Метод: **{payment_method_label(method)}**")
            if r.get("payment_reported_date"): st.write(f"Посочена дата: **{r['payment_reported_date']:%d.%m.%Y}**")
            if r.get("payment_note"): st.caption(r["payment_note"])
            if state=="confirmed" or r["status"]=="paid":
                st.success(f"🟢 Платено · {payment_method_label(method)}")
                if r.get("paid_at"): st.caption(f"Отразено на {r['paid_at']:%d.%m.%Y}")
            elif state=="pending":
                if method=="bank":
                    st.warning("🏦 Банковото плащане чака потвърждение от Admin.")
                    if is_admin:
                        a,b=st.columns(2)
                        if a.button("✅ Потвърди банковото плащане",key=f"admin_bank_confirm_{r['fee_id']}",use_container_width=True):
                            confirm_fee_payment(r["fee_id"],user["id"],"bank",r.get("payment_reported_date"))
                            set_flash("✅ Банковото плащане е потвърдено и е включено във финансовата статистика.");st.rerun()
                        if b.button("↩️ Върни за корекция",key=f"admin_bank_return_{r['fee_id']}",use_container_width=True):
                            execute("UPDATE player_fees SET status='due',confirmation_status='unreported',confirmed_by=NULL,paid_at=NULL,parent_reported_at=NULL,payment_reported_date=NULL WHERE id=%s AND academy_id=%s",(r["fee_id"],ACADEMY_ID))
                            set_flash("↩️ Банковото плащане е върнато за корекция.","warning");st.rerun()
                    elif is_coach:
                        st.info("Треньорът вижда статуса, но няма право да потвърждава банково плащане.")
                elif method=="cash":
                    st.warning("💶 Плащането в брой чака потвърждение от Admin или Coach.")
                    if is_admin or is_coach:
                        a,b=st.columns(2)
                        if a.button("✅ Потвърди получената сума в брой",key=f"cash_confirm_{user['id']}_{r['fee_id']}",use_container_width=True):
                            confirm_fee_payment(r["fee_id"],user["id"],"cash",r.get("payment_reported_date"))
                            set_flash("✅ Плащането в брой е потвърдено и е включено във финансовата статистика.");st.rerun()
                        if is_admin and b.button("↩️ Върни за корекция",key=f"admin_cash_return_{r['fee_id']}",use_container_width=True):
                            execute("UPDATE player_fees SET status='due',confirmation_status='unreported',confirmed_by=NULL,paid_at=NULL,parent_reported_at=NULL,payment_reported_date=NULL WHERE id=%s AND academy_id=%s",(r["fee_id"],ACADEMY_ID))
                            set_flash("↩️ Плащането в брой е върнато за корекция.","warning");st.rerun()
                else:
                    st.warning("Плащането чака потвърждение от Admin.")
            else:
                if is_admin:
                    st.info("Admin може директно да отрази получена сума в брой, когато родителят не е подал заявка.")
                    cash_date=st.date_input("Дата на плащането в брой",date.today(),key=f"admin_direct_cash_date_{r['fee_id']}")
                    if st.button("💶 Отрази директно като платено в брой",key=f"admin_direct_cash_{r['fee_id']}",use_container_width=True,type="primary"):
                        confirm_fee_payment(r["fee_id"],user["id"],"cash",cash_date)
                        set_flash("✅ Плащането в брой е отразено и е включено във финансовата статистика.");st.rerun()
                elif is_coach:
                    st.info("🔴 Таксата е дължима. Ако сумата е получена в брой, треньорът може да я отрази директно.")
                    cash_date=st.date_input("Дата на плащането в брой",date.today(),key=f"coach_direct_cash_date_{r['fee_id']}")
                    if st.button("💶 Отрази като платено в брой",key=f"coach_direct_cash_{r['fee_id']}",use_container_width=True,type="primary"):
                        confirm_fee_payment(r["fee_id"],user["id"],"cash",cash_date)
                        set_flash("✅ Плащането в брой е отразено и се вижда в Admin и Parent панела.");st.rerun()
    st.download_button("📥 Excel: всички набори и деца",payment_excel(payment_rows()),"academy_payments.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",use_container_width=True)


def admin_payment_confirmation_panel():
    st.info("Потвържденията са налични директно в картите на плащанията по-горе.")

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
    shop_income = fetchall("""SELECT payment_method,COALESCE(SUM(unit_price*quantity),0) total FROM shop_orders WHERE academy_id=%s AND payment_status='paid' GROUP BY payment_method""",(ACADEMY_ID,))
    income_map = {r["payment_method"]: float(r["total"]) for r in income}
    shop_income_map = {r["payment_method"]: float(r["total"]) for r in shop_income}
    expense_map = {r["payment_method"]: float(r["total"]) for r in expenses}
    return income_map, expense_map, shop_income_map


def financial_report_excel():
    income_map, expense_map, shop_income_map = financial_summary_rows()
    methods = [("bank","По банка"),("cash","В брой"),("card","Карта"),("other","Друго")]
    rows=[]
    for key,label in methods:
        inc=income_map.get(key,0.0)+shop_income_map.get(key,0.0); exp=expense_map.get(key,0.0)
        rows.append({"Начин":label,"Приход €":inc,"Разход €":exp,"Нетно €":inc-exp})
    total_income=sum(income_map.values())+sum(shop_income_map.values()); total_expense=sum(expense_map.values())
    rows.append({"Начин":"ТОТАЛ","Приход €":total_income,"Разход €":total_expense,"Нетно €":total_income-total_expense})
    details = fetchall(
        "SELECT expense_date,category,amount,payment_method,description,created_at FROM expenses WHERE academy_id=%s ORDER BY expense_date DESC,created_at DESC",
        (ACADEMY_ID,),
    )
    method_names={"cash":"В брой","bank":"По банка","card":"Карта","other":"Друго"}
    detail_rows=[{"Дата":r["expense_date"],"Категория":r["category"],"Сума €":float(r["amount"]),"Начин":method_names.get(r["payment_method"],r["payment_method"]),"Описание":r.get("description") or ""} for r in details]
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
    income_map, expense_map, shop_income_map = financial_summary_rows()
    bank=income_map.get("bank",0.0); cash=income_map.get("cash",0.0); card=income_map.get("card",0.0); shop_total=sum(shop_income_map.values())
    expenses_total=sum(expense_map.values()); total_income=sum(income_map.values())+shop_total; net=total_income-expenses_total
    st.subheader("Финансово табло")
    cols=st.columns(6)
    cols[0].metric("По банка",f"{bank:.2f} €")
    cols[1].metric("В брой",f"{cash:.2f} €")
    cols[2].metric("Карта",f"{card:.2f} €")
    cols[3].metric("Приход екип/артикул",f"{shop_total:.2f} €")
    cols[4].metric("Разход",f"{expenses_total:.2f} €")
    cols[5].metric("Тотал",f"{net:.2f} €",help="Такси + платени артикули - разходи")
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
        category=st.selectbox("Категория",["Наем","Консумативи","Топки","Спортна екипировка","Тренировъчни пособия","Транспорт","Турнири и такси","Реклама","Други"])
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
            c1.write(f"**{row['expense_date']:%d.%m.%Y} · {row['category']} · {row['amount']:.2f} € · {names.get(row['payment_method'],row['payment_method'])}**")
            if row.get("description"): c1.write(row["description"])
            if c2.button("🗑️",key=f"delete_expense_{row['id']}"):
                execute("DELETE FROM expenses WHERE academy_id=%s AND id=%s",(ACADEMY_ID,row["id"])); st.rerun()
    if not rows: st.info("Няма въведени разходи.")


def product_catalog_parent(user,child=None):
    st.subheader("🛍️ Екипировка / Аксесоари")
    st.info("ℹ️ Закупуването е основно чрез предварителна заявка. Първо заявявате желания артикул, модел, размер и количество. Академията събира заявките и Ви уведомява при промяна на статуса.")
    rows=fetchall("SELECT * FROM shop_products WHERE academy_id=%s AND active ORDER BY category,name",(ACADEMY_ID,))
    visible_count=0
    for item in rows:
        variants=fetchall("SELECT * FROM shop_product_variants WHERE academy_id=%s AND product_id=%s AND active ORDER BY model_name,size_name",(ACADEMY_ID,item['id']))
        if not variants: continue
        visible_count+=1
        with st.container(border=True):
            photo,info=st.columns([1,3])
            with photo:
                if item.get('image_data'): st.image(bytes(item['image_data']),use_container_width=True)
                else: st.info("Няма снимка")
            with info:
                st.markdown(f"### {item['name']}");st.markdown(f"## {item['price']:.2f} €");st.caption(item['category'])
                if item.get('description'): st.write(item['description'])
                vmap={f"{v['model_name']} · размер {v['size_name']}":v for v in variants}
                variant=vmap[st.selectbox("Модел и размер",list(vmap),key=f"shop_variant_{user['id']}_{item['id']}")]
                qty=st.number_input("Количество",1,20,1,key=f"shop_qty_{user['id']}_{item['id']}")
                method=st.radio("Начин на плащане",['cash','bank','card'],format_func=lambda x:{'cash':'💶 В брой','bank':'🏦 По банка','card':'💳 С карта'}[x],horizontal=True,key=f"shop_method_{user['id']}_{item['id']}")
                if method=='cash': st.caption("Плащането в брой се потвърждава от Admin след получаване на сумата.")
                elif method=='bank': st.caption("След банков превод Admin проверява и потвърждава плащането.")
                else: st.caption("Плащането с карта предстои да бъде активирано.")
                note=st.text_input("Бележка към заявката",key=f"shop_note_{user['id']}_{item['id']}")
                disabled=method=='card'
                if user.get("role") == "admin":
                    if st.button(
                        "📝 Изпрати предварителна заявка",
                        key=f"shop_order_{user['id']}_{item['id']}",
                        use_container_width=True,
                        type="primary",
                        disabled=disabled
                    ):
                        requester = f"{user['first_name']} {user['last_name']}"
                        player_id = child['id'] if child else None
                        parent_id = user['id'] if user.get('role') == 'parent' else None
                
                        execute(
                            """INSERT INTO shop_orders(
                                academy_id,
                                product_id,
                                variant_id,
                                parent_user_id,
                                player_id,
                                requester_user_id,
                                requester_name,
                                quantity,
                                unit_price,
                                selected_size,
                                payment_method,
                                payment_status,
                                fulfilment_status,
                                parent_note
                            )
                            VALUES(
                                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                'pending','new',%s
                            )""",
                            (
                                ACADEMY_ID,
                                item['id'],
                                variant['id'],
                                parent_id,
                                player_id,
                                user['id'],
                                requester,
                                qty,
                                item['price'],
                                f"{variant['model_name']} / {variant['size_name']}",
                                method,
                                note.strip() or None
                            )
                        )
                
                        notify_admins(
                            'shop_preorder',
                            'Нова предварителна заявка',
                            f"{requester} · {item['name']} · {qty} бр."
                        )
                
                        set_flash(
                            "✅ Предварителната заявка е изпратена. "
                            "Ще получите известие при промяна на статуса."
                        )
                        st.rerun()
                
                else:
                    st.button(
                        "🔒 Изпрати заявка · предстои",
                        key=f"shop_locked_{user['id']}_{item['id']}",
                        use_container_width=True,
                        disabled=True,
                        help="Предварителните заявки предстои да бъдат активирани."
                    )
                
                    st.caption(
                        "🔒 Функцията за изпращане на предварителна заявка "
                        "предстои да бъде активирана."
                    )
    if not visible_count: st.info("В момента няма публикувани артикули.")
    st.divider();st.subheader("Моите заявки")
    mine=fetchall("""SELECT o.*,p.name product_name FROM shop_orders o JOIN shop_products p ON p.id=o.product_id WHERE o.academy_id=%s AND (o.requester_user_id=%s OR o.parent_user_id=%s) ORDER BY o.created_at DESC""",(ACADEMY_ID,user['id'],user['id']))
    status_names={'new':'Нова заявка','reviewed':'Заявката е прегледана','available':'Има наличност','ordered':'Поръчано от доставчик','ready':'Готово за получаване','delivered':'Получено','cancelled':'Отказано'}
    pay_names={'pending':'Чака потвърждение','paid':'Платено','rejected':'Отказано'}
    for o in mine:
        with st.container(border=True):
            st.write(f"**{o['product_name']} · {o.get('selected_size') or ''} · {o['quantity']} бр.**")
            st.write(f"Статус: **{status_names.get(o['fulfilment_status'],o['fulfilment_status'])}** · Плащане: **{pay_names.get(o['payment_status'],o['payment_status'])}**")
            if o.get('admin_comment'): st.info(o['admin_comment'])
    if not mine: st.caption("Все още няма изпратени заявки.")

def shop_orders_excel(rows):
    status_names={'new':'Нова заявка','reviewed':'Прегледана','available':'Има наличност','ordered':'Поръчано от доставчик','ready':'Готово за получаване','delivered':'Получено','cancelled':'Отказано'}
    data=[]
    for r in rows:data.append({'№':r['id'],'Заявител':r.get('requester_name') or f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip(),'Дете':f"{r.get('player_first') or ''} {r.get('player_last') or ''}".strip(),'Артикул':r['product_name'],'Модел / размер':r.get('selected_size') or '','Количество':r['quantity'],'Ед. цена €':float(r['unit_price']),'Общо €':float(r['unit_price'])*r['quantity'],'Плащане':{'cash':'В брой','bank':'По банка','card':'Карта'}.get(r['payment_method'],r['payment_method']),'Статус':status_names.get(r['fulfilment_status'],r['fulfilment_status']),'Бележка':r.get('parent_note') or '','Коментар Admin':r.get('admin_comment') or '','Дата':r['created_at'].replace(tzinfo=None) if getattr(r['created_at'],'tzinfo',None) else r['created_at']})
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine='openpyxl') as writer:
        pd.DataFrame(data).to_excel(writer,index=False,sheet_name='Заявки')
        ws=writer.book['Заявки'];ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=min(max(len(str(c.value or '')) for c in col)+2,38)
    return out.getvalue()

def club_inventory_excel(rows):
    condition_names={"new":"Ново","good":"Добро","worn":"Амортизирано","damaged":"Повредено","retired":"Бракувано"}
    data=[]
    for r in rows:
        quantity=int(r.get("quantity") or 0)
        data.append({
            "№":r["id"],"Артикул":r["item_name"],"Категория":r.get("category") or "",
            "Количество":quantity,"Статус наличност":"Има наличност" if quantity>0 else "Изчерпан",
            "Състояние":condition_names.get(r.get("condition_status"),r.get("condition_status") or ""),
            "Дата на закупуване":r.get("purchase_date"),"Планирана подмяна":r.get("replacement_due"),
            "Бележка":r.get("note") or "","Активен":"Да" if r.get("active") else "Не",
            "Последна актуализация":r.get("updated_at").replace(tzinfo=None) if r.get("updated_at") and getattr(r.get("updated_at"),"tzinfo",None) else r.get("updated_at"),
        })
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine="openpyxl") as writer:
        pd.DataFrame(data).to_excel(writer,index=False,sheet_name="Клубен асортимент")
        ws=writer.book["Клубен асортимент"];ws.freeze_panes="A2";ws.auto_filter.ref=ws.dimensions
        for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=min(max(len(str(cell.value or "")) for cell in col)+2,40)
    return out.getvalue()

def admin_shop_panel(user):
    st.subheader("🛍️ Екипировка / Аксесоари")
    st.info("Този модул е онлайн магазинът за родители и складовата наличност. Разходите се въвеждат само от отделния бутон „Разход“.")
    catalog,stock,orders,club=st.tabs(["🖼️ Витрина за родители","📦 Модели, размери и наличност","🧾 Поръчки","🏟️ Клубен асортимент"])
    with catalog:
        st.caption("Тук определяш какво родителите виждат в магазина: снимка, име, описание и продажна цена.")
        with st.form("new_shop_product",clear_on_submit=True):
            c1,c2=st.columns(2);name=c1.text_input("Име на артикула");category=c2.selectbox("Категория",["Екипировка","Аксесоар","Облекло","Фен артикули","Друго"])
            description=st.text_area("Описание за родителския панел");price=st.number_input("Продажна цена (€)",0.0,10000.0,0.0,.01)
            image=st.file_uploader("Основна снимка",type=["jpg","jpeg","png","webp"]);published=st.checkbox("Публикувай за родители",True)
            save=st.form_submit_button("Добави във витрината",use_container_width=True)
        if save:
            if not name.strip() or price<=0: st.error("Въведи име и продажна цена над 0.")
            else:
                execute("""INSERT INTO shop_products(academy_id,name,category,description,price,stock_quantity,min_stock,image_data,image_mime,active,created_by)
                           VALUES(%s,%s,%s,%s,%s,0,0,%s,%s,%s,%s)""",(ACADEMY_ID,name.strip(),category,description.strip() or None,price,image.getvalue() if image else None,image.type if image else None,published,user['id']))
                set_flash("✅ Артикулът е добавен. Добави моделите, размерите и бройките от следващия под-бутон.");st.rerun()
        products=fetchall("SELECT * FROM shop_products WHERE academy_id=%s ORDER BY active DESC,category,name",(ACADEMY_ID,))
        for x in products:
            with st.expander(f"{'🟢' if x['active'] else '⚪'} {x['name']} · {x['price']:.2f} €"):
                c1,c2=st.columns(2);price_edit=c1.number_input("Цена (€)",0.0,10000.0,float(x['price']),.01,key=f"product_price_{x['id']}");active=c2.checkbox("Видим за родители",x['active'],key=f"product_active_{x['id']}")
                description_edit=st.text_area("Описание",value=x.get('description') or '',key=f"product_desc_{x['id']}")
                if st.button("Запази витрината",key=f"save_product_{x['id']}",use_container_width=True):
                    execute("UPDATE shop_products SET price=%s,description=%s,active=%s,updated_at=NOW() WHERE id=%s AND academy_id=%s",(price_edit,description_edit.strip() or None,active,x['id'],ACADEMY_ID));st.rerun()
    with stock:
        products=fetchall("SELECT * FROM shop_products WHERE academy_id=%s ORDER BY category,name",(ACADEMY_ID,))
        if not products: st.info("Първо добави артикул във витрината.")
        else:
            pmap={f"{p['name']} · {p['price']:.2f} €":p for p in products};product=pmap[st.selectbox("Артикул",list(pmap),key="variant_product")]
            with st.form(f"add_variant_{product['id']}",clear_on_submit=True):
                a,b,c,d=st.columns(4);model=a.text_input("Модел",placeholder="Напр. Home 2026");size=b.text_input("Размер",placeholder="128, S, M, 5...");sku=c.text_input("Код/SKU");qty=d.number_input("Бройки",0,100000,0)
                minimum=st.number_input("Минимална наличност за предупреждение",0,100000,0);add=st.form_submit_button("Добави модел/размер",use_container_width=True)
            if add and model.strip() and size.strip():
                execute("""INSERT INTO shop_product_variants(academy_id,product_id,model_name,size_name,sku,quantity,min_quantity)
                           VALUES(%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(product_id,model_name,size_name) DO UPDATE SET sku=EXCLUDED.sku,quantity=EXCLUDED.quantity,min_quantity=EXCLUDED.min_quantity,active=TRUE,updated_at=NOW()""",(ACADEMY_ID,product['id'],model.strip(),size.strip(),sku.strip() or None,qty,minimum));st.rerun()
            availability=st.radio("Филтър наличност",["Всички","Налични","Изчерпани","Под минималната наличност"],horizontal=True)
            variants=fetchall("SELECT * FROM shop_product_variants WHERE academy_id=%s AND product_id=%s ORDER BY model_name,size_name",(ACADEMY_ID,product['id']))
            for v in variants:
                if availability=="Налични" and v['quantity']<=0: continue
                if availability=="Изчерпани" and v['quantity']!=0: continue
                if availability=="Под минималната наличност" and v['quantity']>v['min_quantity']: continue
                with st.container(border=True):
                    a,b,c,d=st.columns([3,2,2,1]);a.write(f"**{v['model_name']}** · размер **{v['size_name']}**");new_qty=b.number_input("Бройки",0,100000,int(v['quantity']),key=f"variant_qty_{v['id']}");new_min=c.number_input("Минимум",0,100000,int(v['min_quantity']),key=f"variant_min_{v['id']}");active=d.checkbox("Активен",v['active'],key=f"variant_active_{v['id']}")
                    if new_qty<=new_min: st.warning("Този модел/размер е за зареждане.")
                    if st.button("Запази наличността",key=f"save_variant_{v['id']}",use_container_width=True):execute("UPDATE shop_product_variants SET quantity=%s,min_quantity=%s,active=%s,updated_at=NOW() WHERE id=%s AND academy_id=%s",(new_qty,new_min,active,v['id'],ACADEMY_ID));st.rerun()
    with orders:
        flt=st.selectbox("Филтър",["Всички","Чакащи плащане","Платени","За поръчване","Готови","Приключени"])
        all_orders=fetchall("""SELECT o.*,p.name product_name,u.first_name,u.last_name,pl.first_name player_first,pl.last_name player_last
                              FROM shop_orders o JOIN shop_products p ON p.id=o.product_id LEFT JOIN users u ON u.id=COALESCE(o.requester_user_id,o.parent_user_id) LEFT JOIN players pl ON pl.id=o.player_id
                              WHERE o.academy_id=%s ORDER BY o.created_at DESC""",(ACADEMY_ID,))
        export_status=st.selectbox(
            "Статус за Excel",
            ["Всички","new","reviewed","available","ordered","ready","delivered","cancelled"],
            format_func=lambda x:{"Всички":"Всички статуси","new":"Нови заявки","reviewed":"Прегледани","available":"Има наличност","ordered":"Поръчано от доставчик","ready":"Готови за получаване","delivered":"Получени","cancelled":"Отказани"}.get(x,x),
            key="shop_orders_export_status",
        )
        export_orders=all_orders if export_status=="Всички" else [row for row in all_orders if row["fulfilment_status"]==export_status]
        st.download_button(
            f"📥 Excel: заявки с избрания статус ({len(export_orders)})",
            shop_orders_excel(export_orders),
            "academy_shop_orders_current_status.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key="download_shop_orders_current_status",
        )
        for o in all_orders:
            if flt=="Чакащи плащане" and o['payment_status']!='pending':continue
            if flt=="Платени" and o['payment_status']!='paid':continue
            if flt=="За поръчване" and o['fulfilment_status'] not in ('new','ordered'):continue
            if flt=="Готови" and o['fulfilment_status']!='ready':continue
            if flt=="Приключени" and o['fulfilment_status'] not in ('delivered','cancelled'):continue
            with st.expander(f"{o['product_name']} · {o.get('selected_size') or ''} · {o['player_first']} {o['player_last']} · {o['unit_price']*o['quantity']:.2f} €"):
                pay=st.selectbox("Плащане",["pending","paid","rejected"],index=["pending","paid","rejected"].index(o['payment_status']),format_func=lambda x:{"pending":"Чака потвърждение","paid":"Платено","rejected":"Отказано"}[x],key=f"order_pay_{o['id']}")
                fulfil=st.selectbox("Изпълнение",["new","reviewed","available","ordered","ready","delivered","cancelled"],index=["new","reviewed","available","ordered","ready","delivered","cancelled"].index(o['fulfilment_status']),format_func=lambda x:{"new":"Нова заявка","reviewed":"Заявката е прегледана","available":"Има наличност","ordered":"Поръчано от доставчик","ready":"Готово за получаване","delivered":"Получено","cancelled":"Отказано"}[x],key=f"order_fulfil_{o['id']}")
                comment=st.text_area("Съобщение към родителя",value=o.get('admin_comment') or '',key=f"order_comment_{o['id']}")
                if st.button("Потвърди и изпрати статуса",key=f"save_order_{o['id']}",use_container_width=True):
                    was_paid=o['payment_status']=='paid'
                    with db() as conn,conn.cursor() as cur:
                        cur.execute("UPDATE shop_orders SET payment_status=%s,fulfilment_status=%s,admin_comment=%s,confirmed_by=%s,paid_at=CASE WHEN %s='paid' THEN COALESCE(paid_at,NOW()) ELSE NULL END,updated_at=NOW() WHERE id=%s AND academy_id=%s",(pay,fulfil,comment.strip() or None,user['id'],pay,o['id'],ACADEMY_ID))
                        if pay=='paid' and not was_paid and o.get('variant_id'):cur.execute("UPDATE shop_product_variants SET quantity=GREATEST(0,quantity-%s),updated_at=NOW() WHERE id=%s AND academy_id=%s",(o['quantity'],o['variant_id'],ACADEMY_ID))
                    create_notification(o.get('requester_user_id') or o.get('parent_user_id'),"shop_order_status",f"Обновена поръчка: {o['product_name']}",comment.strip() or "Провери новия статус на поръчката.");set_flash("✅ Статусът е записан и родителят е уведомен.");st.rerun()
    with club:
        st.caption("Вътрешна наличност на клуба. Тези артикули не се показват и не се продават на родителите.")
        with st.form("club_inventory_add",clear_on_submit=True):
            a,b,c=st.columns(3);item=a.text_input("Артикул");cat=b.selectbox("Тип",["Топки","Шапки","Чували","Конуси","Екипи за тренировка","Тренировъчни пособия","Друго"]);qty=c.number_input("Количество",0,100000,0)
            condition=st.selectbox("Състояние",["new","good","worn","damaged","retired"],format_func=lambda x:{"new":"Ново","good":"Добро","worn":"Амортизирано","damaged":"Повредено","retired":"Бракувано"}[x]);note=st.text_input("Бележка");add=st.form_submit_button("Добави към клубния асортимент",use_container_width=True)
        if add and item.strip():execute("INSERT INTO club_inventory(academy_id,item_name,category,quantity,condition_status,note,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",(ACADEMY_ID,item.strip(),cat,qty,condition,note.strip() or None,user['id']));st.rerun()
        c1,c2=st.columns(2);status=c1.selectbox("Филтър по състояние",["Всички","new","good","worn","damaged","retired"]);stock_filter=c2.selectbox("Филтър по наличност",["Всички","Има наличност","Изчерпани"])
        inv=fetchall("SELECT * FROM club_inventory WHERE academy_id=%s AND active ORDER BY category,item_name",(ACADEMY_ID,))
        filtered_inv=[]
        for row in inv:
            if status!="Всички" and row['condition_status']!=status:continue
            if stock_filter=="Има наличност" and row['quantity']<=0:continue
            if stock_filter=="Изчерпани" and row['quantity']!=0:continue
            filtered_inv.append(row)
        st.download_button(
            f"📥 Excel: актуален клубен асортимент ({len(filtered_inv)})",
            club_inventory_excel(filtered_inv),
            "academy_club_assortment_current_status.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key="download_club_inventory_current_status",
        )
        for x in filtered_inv:
            with st.container(border=True):
                a,b=st.columns([4,1]);a.write(f"**{x['item_name']}** · {x['category']} · {x['condition_status']}");q=b.number_input("Бройки",0,100000,int(x['quantity']),key=f"club_qty_{x['id']}")
                if st.button("Запази клубната наличност",key=f"save_club_{x['id']}"):execute("UPDATE club_inventory SET quantity=%s,updated_at=NOW() WHERE id=%s AND academy_id=%s",(q,x['id'],ACADEMY_ID));st.rerun()


def match_poll_responses_excel(rows,match_title):
    answer_names={"yes":"Да","no":"Не","maybe":"Не съм сигурен/а"}
    data=[]
    for row in rows:
        data.append({
            "Мач":match_title,
            "Дете":f"{row.get('player_first') or ''} {row.get('player_last') or ''}".strip(),
            "Родител":f"{row.get('parent_first') or ''} {row.get('parent_last') or ''}".strip(),
            "Отговор":answer_names.get(row.get('answer'),row.get('answer') or "Без отговор"),
            "Дата на отговора":row.get('created_at').replace(tzinfo=None) if row.get('created_at') and getattr(row.get('created_at'),'tzinfo',None) else row.get('created_at'),
        })
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine="openpyxl") as writer:
        pd.DataFrame(data).to_excel(writer,index=False,sheet_name="Анкета за мача")
        ws=writer.book["Анкета за мача"];ws.freeze_panes="A2";ws.auto_filter.ref=ws.dimensions
        for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=min(max(len(str(c.value or "")) for c in col)+2,38)
    return out.getvalue()

def render_match_poll_responses(match):
    poll=fetchone("SELECT * FROM polls WHERE academy_id=%s AND event_id=%s AND poll_type='match_attendance' ORDER BY created_at DESC LIMIT 1",(ACADEMY_ID,match["id"]))
    if not poll:
        st.caption("За този мач няма изпратена анкета.")
        return
    rows=fetchall("""SELECT pr.answer,pr.created_at,p.first_name player_first,p.last_name player_last,
        u.first_name parent_first,u.last_name parent_last
        FROM poll_responses pr
        JOIN users u ON u.id=pr.user_id
        LEFT JOIN players p ON p.id=pr.player_id
        WHERE pr.academy_id=%s AND pr.poll_id=%s
        ORDER BY p.first_name,p.last_name,pr.created_at DESC""",(ACADEMY_ID,poll["id"]))
    st.markdown("#### 📊 Отговори от родителите")
    if not rows:
        st.info("Все още няма отговори от родители.")
        return
    answer_names={"yes":"🟢 Да","no":"🔴 Не","maybe":"🟠 Не съм сигурен/а"}
    for row in rows:
        st.write(f"**{row.get('player_first') or 'Без дете'} {row.get('player_last') or ''}** · {answer_names.get(row.get('answer'),row.get('answer'))} · {row.get('parent_first') or ''} {row.get('parent_last') or ''}")
    st.download_button(
        f"📥 Excel: анкета за мача ({len(rows)} отговора)",
        match_poll_responses_excel(rows,match["title"]),
        f"match_poll_{match['id']}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        key=f"download_match_poll_{match['id']}",
    )

def coach_inventory_status_panel(user):
    st.subheader("📦 Клубен асортимент")
    st.caption("Треньорски достъп: виждаш текущия асортимент, състоянието и бройките. Създаване на нови артикули остава само за Admin.")
    search_name=st.text_input("🔎 Търсене по име на артикул",placeholder="Напр. Адидас топки 2017",key="coach_inventory_name_search").strip().lower()
    c1,c2=st.columns(2)
    status=c1.selectbox("Филтър по състояние",["Всички","new","good","worn","damaged","retired"],format_func=lambda x:{"Всички":"Всички","new":"Ново","good":"Добро","worn":"Амортизирано","damaged":"Повредено","retired":"Бракувано"}.get(x,x),key="coach_inventory_condition")
    stock_filter=c2.selectbox("Филтър по наличност",["Всички","Има наличност","Изчерпани"],key="coach_inventory_stock")
    rows=fetchall("SELECT * FROM club_inventory WHERE academy_id=%s AND active ORDER BY category,item_name",(ACADEMY_ID,))
    shown=0
    labels={"new":"Ново","good":"Добро","worn":"Амортизирано","damaged":"Повредено","retired":"Бракувано"}
    for row in rows:
        if search_name and search_name not in (row.get('item_name') or '').lower():continue
        if status!="Всички" and row['condition_status']!=status:continue
        if stock_filter=="Има наличност" and row['quantity']<=0:continue
        if stock_filter=="Изчерпани" and row['quantity']!=0:continue
        shown+=1
        with st.container(border=True):
            info_col,qty_col=st.columns([4,1])
            info_col.write(f"**{row['item_name']}** · {row['category']}")
            info_col.caption(f"Състояние: {labels.get(row['condition_status'],row['condition_status'])}")
            new_qty=qty_col.number_input("Бройки",0,100000,int(row['quantity']),key=f"coach_club_qty_{row['id']}")
            if row.get('note'):st.caption(f"Бележка: {row['note']}")
            if st.button("💾 Запази бройките",key=f"coach_save_club_{row['id']}",use_container_width=True):
                execute("UPDATE club_inventory SET quantity=%s,updated_at=NOW() WHERE id=%s AND academy_id=%s",(new_qty,row['id'],ACADEMY_ID))
                set_flash(f"✅ Наличността за {row['item_name']} е обновена.")
                st.rerun()
    if not shown:st.info("Няма артикули за избраните филтри.")

def admin_training_resources(user):
    st.subheader("🎓 Управление тренировки")
    with st.form("training_resource_form",clear_on_submit=True):
        title=st.text_input("Заглавие");typ=st.radio("Тип",["video","link","image"],format_func=lambda x:{"video":"Видео","link":"Линк към сайт","image":"Снимка и информация"}[x],horizontal=True);url=st.text_input("Линк");description=st.text_area("Описание/упражнение");image=st.file_uploader("Снимка",type=["jpg","jpeg","png","webp"]);save=st.form_submit_button("Добави упражнение",use_container_width=True)
    if save and title.strip(): execute("INSERT INTO training_resources(academy_id,title,resource_type,url,description,image_data,image_mime,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",(ACADEMY_ID,title.strip(),typ,url.strip() or None,description.strip() or None,image.getvalue() if image else None,image.type if image else None,user['id']));set_flash("✅ Упражнението е публикувано към треньорите.");st.rerun()
    for r in fetchall("SELECT * FROM training_resources WHERE academy_id=%s AND active ORDER BY created_at DESC",(ACADEMY_ID,)):
        with st.expander(r['title']):
            if r.get('description'):st.write(r['description'])
            if st.button("🗑️ Премахни",key=f"del_resource_{r['id']}"):execute("UPDATE training_resources SET active=FALSE WHERE id=%s AND academy_id=%s",(r['id'],ACADEMY_ID));st.rerun()

def coach_exercises_panel():
    st.subheader("🎓 Упражнения")
    rows=fetchall("SELECT * FROM training_resources WHERE academy_id=%s AND active ORDER BY created_at DESC",(ACADEMY_ID,))
    if not rows:st.info("Няма добавени упражнения.");return
    for r in rows:
        with st.container(border=True):
            st.subheader(r['title'])
            if r.get('description'):st.write(r['description'])
            if r.get('image_data'):st.image(bytes(r['image_data']),use_container_width=True)
            if r.get('url'):
                if r['resource_type']=='video':st.video(r['url'])
                else:st.link_button("🔗 Отвори материала",r['url'])

def prepare_profile_photo(uploaded_file, focus_x=50, focus_y=50, zoom=1.0):
    """Create a square, face-focused JPEG using parent-selected focus and zoom."""
    if not uploaded_file:return None,None
    raw=uploaded_file.getvalue()
    if len(raw)>8*1024*1024:
        st.error("Снимката е прекалено голяма. Максимум 8 MB.");return None,None
    try:
        image=Image.open(io.BytesIO(raw));image.load();image=ImageOps.exif_transpose(image).convert("RGB")
        w,h=image.size;zoom=max(1.0,min(float(zoom),3.0));side=max(1,int(min(w,h)/zoom))
        cx=int((max(0,w-side))*max(0,min(100,int(focus_x)))/100)
        cy=int((max(0,h-side))*max(0,min(100,int(focus_y)))/100)
        image=image.crop((cx,cy,cx+side,cy+side)).resize((600,600),Image.Resampling.LANCZOS)
        out=io.BytesIO();image.save(out,format="JPEG",quality=78,optimize=True,progressive=True)
        return out.getvalue(),"image/jpeg"
    except Exception:
        st.error("Файлът не може да бъде обработен като валидна снимка.");return None,None
def circular_preview(image_bytes,size=190):
    if not image_bytes:return
    image=Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((size,size),Image.Resampling.LANCZOS)
    mask=Image.new("L",(size,size),0);ImageDraw.Draw(mask).ellipse((0,0,size-1,size-1),fill=255)
    preview=Image.new("RGBA",(size,size),(0,0,0,0));preview.paste(image,(0,0),mask)
    st.image(preview,width=size)
def staff_people(current_user_id):
    return fetchall("""SELECT id,first_name,last_name,role FROM users WHERE academy_id=%s AND active AND account_status='approved' AND role IN ('admin','coach','specialist') AND id<>%s ORDER BY role,first_name,last_name""",(ACADEMY_ID,current_user_id))
def staff_direct_messages_panel(user):
    st.subheader("✉️ Лични съобщения")
    people=staff_people(user["id"]);labels={f"{x['first_name']} {x['last_name']} · {specialist_role_label('conditioning') if x['role']=='specialist' else {'admin':'Admin','coach':'Треньор'}.get(x['role'],x['role'])}":x for x in people}
    with st.form(f"staff_direct_form_{user['id']}",clear_on_submit=True):
        selected=st.multiselect("Получатели",list(labels),help="Може да изберете един или повече души.")
        subject=st.text_input("Тема");body=st.text_area("Съобщение")
        attachment=st.file_uploader("📎 Файл или снимка",key=f"staff_direct_file_{user['id']}")
        send=st.form_submit_button("Изпрати съобщението",use_container_width=True,type="primary")
    if send:
        if not selected or not subject.strip() or not body.strip():st.error("Изберете поне един получател и попълнете тема и съобщение.")
        elif attachment and len(attachment.getvalue())>8*1024*1024:st.error("Файлът е прекалено голям. Максимум 8 MB.")
        else:
            msg=execute("""INSERT INTO staff_direct_messages(academy_id,sender_user_id,subject,body,file_name,file_mime,file_data) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",(ACADEMY_ID,user['id'],subject.strip(),body.strip(),attachment.name if attachment else None,attachment.type if attachment else None,attachment.getvalue() if attachment else None),True)
            for label in selected:
                recipient=labels[label];execute("INSERT INTO staff_direct_message_recipients(message_id,recipient_user_id) VALUES(%s,%s)",(msg['id'],recipient['id']));create_notification(recipient['id'],'staff_message',f"Ново съобщение: {subject.strip()}",f"От {user['first_name']} {user['last_name']}")
            set_flash(f"✅ Съобщението е изпратено до {len(selected)} получатели.");st.rerun()
    rows=fetchall("""SELECT m.*,s.first_name,s.last_name,r.is_read FROM staff_direct_message_recipients r JOIN staff_direct_messages m ON m.id=r.message_id LEFT JOIN users s ON s.id=m.sender_user_id WHERE m.academy_id=%s AND r.recipient_user_id=%s ORDER BY m.created_at DESC LIMIT 100""",(ACADEMY_ID,user['id']))
    execute("UPDATE staff_direct_message_recipients SET is_read=TRUE WHERE recipient_user_id=%s",(user['id'],))
    st.markdown("### Получени")
    if not rows:st.info("Няма получени лични съобщения.")
    for row in rows:
        with st.container(border=True):
            st.write(f"**{row['subject']}**");st.caption(f"От: {row.get('first_name') or 'Премахнат профил'} {row.get('last_name') or ''} · {row['created_at']:%d.%m.%Y %H:%M}");st.write(row['body'])
            if row.get('file_data'):st.download_button(f"📎 {row.get('file_name') or 'Прикачен файл'}",bytes(row['file_data']),row.get('file_name') or 'attachment',row.get('file_mime') or 'application/octet-stream',key=f"direct_download_{user['id']}_{row['id']}")
def staff_team_chat(user):
    execute("DELETE FROM staff_chat_messages WHERE academy_id=%s AND created_at<NOW()-INTERVAL '10 days'",(ACADEMY_ID,))
    st.subheader("💬 Екипен чат")
    st.caption("Общ чат за Admin, треньори и кондиционен/поддържащ екип. Историята се пази 10 дни.")
    with st.form(f"staff_chat_form_{user['id']}",clear_on_submit=True):
        body=st.text_area("Ново съобщение",max_chars=2000);attachment=st.file_uploader("📎 Файл или снимка",key=f"staff_chat_file_{user['id']}")
        send=st.form_submit_button("Изпрати в екипния чат",use_container_width=True,type="primary")
    if send:
        data=attachment.getvalue() if attachment else None
        if not body.strip() and not data:st.error("Напишете съобщение или прикачете файл.")
        elif data and len(data)>8*1024*1024:st.error("Файлът е прекалено голям. Максимум 8 MB.")
        else:
            execute(
                """INSERT INTO staff_chat_messages(
                       academy_id,sender_user_id,body,file_name,file_mime,file_data)
                   VALUES(%s,%s,%s,%s,%s,%s)""",
                (
                    ACADEMY_ID,
                    user['id'],
                    body.strip() or None,
                    attachment.name if attachment else None,
                    attachment.type if attachment else None,
                    data,
                ),
            )
            sender_name=f"{user['first_name']} {user['last_name']}".strip()
            staff_recipients=fetchall(
                """SELECT id FROM users
                   WHERE academy_id=%s
                     AND role IN ('admin','coach','specialist')
                     AND active
                     AND account_status='approved'
                     AND id<>%s""",
                (ACADEMY_ID,user['id']),
            )
            for recipient in staff_recipients:
                create_notification(
                    recipient['id'],
                    'staff_chat',
                    'Ново съобщение в Екипен чат',
                    f"От {sender_name}",
                )
            st.rerun()
    rows=fetchall("""SELECT m.*,u.first_name,u.last_name,u.role FROM staff_chat_messages m LEFT JOIN users u ON u.id=m.sender_user_id WHERE m.academy_id=%s AND m.created_at>=NOW()-INTERVAL '10 days' ORDER BY m.created_at DESC LIMIT 200""",(ACADEMY_ID,))
    if not rows:st.info("Няма съобщения от последните 10 дни.")
    for row in rows:
        with st.container(border=True):
            st.write(f"**{row.get('first_name') or 'Премахнат профил'} {row.get('last_name') or ''}** · {row.get('role') or ''}")
            if row.get('body'):st.write(row['body'])
            if row.get('file_data'):
                mime=(row.get('file_mime') or '').lower()
                if mime.startswith('image/'):st.image(bytes(row['file_data']),caption=row.get('file_name'),use_container_width=True)
                else:st.download_button(f"📎 {row.get('file_name') or 'Прикачен файл'}",bytes(row['file_data']),row.get('file_name') or 'attachment',row.get('file_mime') or 'application/octet-stream',key=f"chat_download_{user['id']}_{row['id']}")
            st.caption(row['created_at'].strftime('%d.%m.%Y %H:%M'))
PROFILE_CROP_COMPONENT = st.components.v2.component(
    "dyulgerov_profile_crop_v1",
    html="""
    <div class="crop-app">
      <div class="crop-stage" id="stage"><canvas id="canvas" width="600" height="600"></canvas><div class="crop-ring"></div></div>
      <div class="crop-help">Плъзнете снимката в кръга. Увеличавайте с два пръста или с колелцето на мишката.</div>
      <div class="crop-actions"><button type="button" id="reset">↺ Центрирай</button><button type="button" id="save">✓ Използвай този кадър</button></div>
      <div id="status" aria-live="polite"></div>
    </div>
    """,
    css="""
    .crop-app{max-width:430px;margin:8px auto 18px;text-align:center;color:var(--st-text-color)}
    .crop-stage{position:relative;width:min(82vw,360px);aspect-ratio:1;margin:auto;border-radius:50%;overflow:hidden;background:#111;touch-action:none;box-shadow:0 0 0 3px #c9a84c,0 12px 34px rgba(0,0,0,.42)}
    .crop-stage canvas{width:100%;height:100%;display:block;touch-action:none;cursor:grab}.crop-stage canvas:active{cursor:grabbing}
    .crop-ring{position:absolute;inset:0;border-radius:50%;pointer-events:none;box-shadow:inset 0 0 0 4px rgba(255,224,126,.9),inset 0 0 33px rgba(0,0,0,.3)}
    .crop-help{font-size:.82rem;line-height:1.35;margin:12px auto 6px;max-width:360px;opacity:.85}
    .crop-actions{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}.crop-actions button{border:1px solid #d2b45f;border-radius:10px;padding:9px 12px;font-weight:750;cursor:pointer;background:#17130b;color:#f6e7b6}.crop-actions #save{background:linear-gradient(135deg,#956b20,#d1ad57);color:#fff}
    #status{min-height:1.2em;margin-top:8px;font-size:.84rem}
    """,
    js="""
    export default function(component){
      const {parentElement,data,setStateValue,setTriggerValue}=component;
      const canvas=parentElement.querySelector('#canvas'),ctx=canvas.getContext('2d');
      const stage=parentElement.querySelector('#stage'),status=parentElement.querySelector('#status');
      const image=new Image();let x=0,y=0,scale=1,minScale=1,drag=false,lastX=0,lastY=0,pinchDistance=0,pinchScale=1;
      const clamp=()=>{const w=image.naturalWidth*scale,h=image.naturalHeight*scale;x=Math.min(0,Math.max(canvas.width-w,x));y=Math.min(0,Math.max(canvas.height-h,y));};
      const draw=()=>{if(!image.complete||!image.naturalWidth)return;clamp();ctx.clearRect(0,0,600,600);ctx.drawImage(image,x,y,image.naturalWidth*scale,image.naturalHeight*scale);};
      const center=()=>{minScale=Math.max(600/image.naturalWidth,600/image.naturalHeight);scale=Math.max(minScale,Math.min(minScale*4,scale||minScale));x=(600-image.naturalWidth*scale)/2;y=(600-image.naturalHeight*scale)/2;draw();};
      const coords=(e)=>{const r=canvas.getBoundingClientRect();return {x:(e.clientX-r.left)*600/r.width,y:(e.clientY-r.top)*600/r.height};};
      image.onload=()=>{scale=0;center();};image.src=data.image;
      canvas.onpointerdown=e=>{drag=true;canvas.setPointerCapture(e.pointerId);const p=coords(e);lastX=p.x;lastY=p.y;};
      canvas.onpointermove=e=>{if(!drag)return;const p=coords(e);x+=p.x-lastX;y+=p.y-lastY;lastX=p.x;lastY=p.y;draw();};
      canvas.onpointerup=canvas.onpointercancel=()=>{drag=false;};
      canvas.onwheel=e=>{e.preventDefault();const r=canvas.getBoundingClientRect(),px=(e.clientX-r.left)*600/r.width,py=(e.clientY-r.top)*600/r.height;const old=scale;scale=Math.max(minScale,Math.min(minScale*4,scale*(e.deltaY<0?1.1:.9)));x=px-(px-x)*(scale/old);y=py-(py-y)*(scale/old);draw();};
      stage.addEventListener('touchstart',e=>{if(e.touches.length===2){pinchDistance=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);pinchScale=scale;}},{passive:true});
      stage.addEventListener('touchmove',e=>{if(e.touches.length===2&&pinchDistance){e.preventDefault();const d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);const old=scale;scale=Math.max(minScale,Math.min(minScale*4,pinchScale*d/pinchDistance));const rect=canvas.getBoundingClientRect(),px=((e.touches[0].clientX+e.touches[1].clientX)/2-rect.left)*600/rect.width,py=((e.touches[0].clientY+e.touches[1].clientY)/2-rect.top)*600/rect.height;x=px-(px-x)*(scale/old);y=py-(py-y)*(scale/old);draw();}},{passive:false});
      parentElement.querySelector('#reset').onclick=()=>{scale=0;center();status.textContent='Снимката е центрирана.';};
      parentElement.querySelector('#save').onclick=()=>{draw();const jpeg=canvas.toDataURL('image/jpeg',.82);setStateValue('cropped',jpeg);setTriggerValue('savedAt',Date.now());status.textContent='✅ Кадърът е запазен.';};
    }
    """,
)

def direct_profile_crop(photo,index):
    if not photo:return None,None
    raw=photo.getvalue()
    if len(raw)>8*1024*1024:
        st.error("Снимката е прекалено голяма. Максимум 8 MB.");return None,None
    source="data:"+(photo.type or "image/jpeg")+";base64,"+base64.b64encode(raw).decode("ascii")
    key=f"direct_photo_crop_{index}_{hashlib.sha256(raw).hexdigest()[:12]}"
    result=PROFILE_CROP_COMPONENT(data={"image":source},default={"cropped":"","savedAt":None},key=key,on_cropped_change=lambda:None,on_savedAt_change=lambda:None,height=500)
    state=st.session_state.get(key,{}) or {}
    cropped=(getattr(result,"cropped","") or state.get("cropped") or "")
    if not cropped:return None,None
    try:
        payload=cropped.split(",",1)[1] if "," in cropped else cropped
        image_bytes=base64.b64decode(payload)
        image=Image.open(io.BytesIO(image_bytes));image.load()
        if image.size!=(600,600):image=image.resize((600,600),Image.Resampling.LANCZOS)
        out=io.BytesIO();image.convert("RGB").save(out,format="JPEG",quality=82,optimize=True,progressive=True)
        return out.getvalue(),"image/jpeg"
    except Exception:
        st.error("Избраният кадър не може да бъде обработен.");return None,None

def register_parent():
    st.subheader("Регистрация на родител")
    child_count=st.number_input("Брой деца за добавяне сега",1,5,1,key="registration_child_count")
    c1,c2=st.columns(2);first=c1.text_input("Име",key="reg_first");last=c2.text_input("Фамилия",key="reg_last")
    email=c1.text_input("Имейл",key="reg_email").strip().lower();phone=c2.text_input("Телефон",key="reg_phone")
    pw=c1.text_input("Парола",type="password",help="Минимум 8 символа.",key="reg_pw");pw2=c2.text_input("Повтори паролата",type="password",key="reg_pw2")
    st.caption("🔐 Паролата трябва да съдържа минимум 8 символа.")
    children=[]
    for i in range(int(child_count)):
        st.markdown(f"### Дете {i+1}");a,b,c=st.columns(3)
        cf=a.text_input("Име",key=f"cf{i}");cl=b.text_input("Фамилия",key=f"cl{i}");bd=c.date_input("Дата на раждане",date(2018,1,1),key=f"cb{i}")
        photo=st.file_uploader(
            "📷 Задължителна профилна снимка",
            type=["jpg","jpeg","png","webp"],
            key=f"child_upload_{i}",
            help="На телефон бутонът позволява да снимате с камерата или да изберете готова снимка.",
        )
        image_bytes=image_mime=None
        if photo:
            image_bytes,image_mime=direct_profile_crop(photo,i)
            if image_bytes:
                st.success("✅ Избраният кадър е готов за профилна снимка.")
            else:
                st.info("Преместете и увеличете снимката директно в кръга, после натиснете „Използвай този кадър“.")
        else:st.warning("Снимката на детето е задължителна за завършване на регистрацията.")
        children.append((cf,cl,bd,image_bytes,image_mime))
    submit=st.button("Изпрати регистрация",use_container_width=True,type="primary",key="submit_parent_registration")
    if submit:
        if not all([first.strip(),last.strip(),email,pw]) or pw!=pw2 or len(pw)<8:st.error("Провери данните и паролата.");return
        if any(not x[0].strip() or not x[1].strip() for x in children):st.error("Попълни имената на всяко дете.");return
        if any(not x[3] for x in children):st.error("Добави и фокусирай задължителна снимка за всяко дете.");return
        try:
            with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""INSERT INTO users(academy_id,first_name,last_name,email,phone,password_hash,role,account_status) VALUES(%s,%s,%s,%s,%s,%s,'parent','pending') RETURNING id""",(ACADEMY_ID,first.strip(),last.strip(),email,phone.strip(),hash_password(pw)));parent=cur.fetchone()
                for cf,cl,bd,image_bytes,image_mime in children:
                    cur.execute("INSERT INTO players(academy_id,first_name,last_name,birth_date,profile_image,profile_image_mime) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",(ACADEMY_ID,cf.strip(),cl.strip(),bd,image_bytes,image_mime));player=cur.fetchone()
                    cur.execute("INSERT INTO parent_players(academy_id,parent_user_id,player_id) VALUES(%s,%s,%s)",(ACADEMY_ID,parent['id'],player['id']))
            st.success("Регистрацията е изпратена. Администраторът трябва да одобри профила и да свърже децата с група.")
        except Exception:st.error("Имейлът вече е използван или данните са невалидни.")
def login():
    # Native Streamlit login is kept for reliable submission on iPhone, Android and desktop.
    # The custom iframe form was removed because iOS could keep it at "Влизане..."
    # while the component state was still being transferred to Python.
    with st.form("login_form", clear_on_submit=False):
        email=st.text_input(
            "Имейл",
            key="login_email",
            placeholder="name@example.com",
        ).strip().lower()
        pw=st.text_input(
            "Парола",
            type="password",
            key="login_password",
        )
        go=st.form_submit_button("Вход",use_container_width=True,type="primary")
    if go:
        if not email or not pw:
            st.error("Попълнете имейл и парола.")
            return
        user=fetchone("SELECT * FROM users WHERE academy_id=%s AND lower(email)=lower(%s)",(ACADEMY_ID,email))
        if not user or user["password_hash"]!=hash_password(pw): st.error("Невалиден имейл или парола.")
        elif not user["active"]: st.error("Профилът е деактивиран.")
        elif user["account_status"]!="approved": st.warning("Профилът очаква одобрение от академията.")
        else:
            create_login_session(user["id"])
            st.rerun()

def parent_lions_banner():
    lion_path = APP_DIR / "Designer (7).jpg"
    if not lion_path.exists():
        st.warning("Липсва файлът Designer (7).jpg в основната папка на проекта.")
        return
    # PNG се зарежда при runtime. Така app.py остава малък и GitHub го показва нормално.
    lion_src = "data:image/png;base64," + base64.b64encode(lion_path.read_bytes()).decode("ascii")
    html="""
    <div class="lions-parent-banner">
      <div class="lions-pitch-left"></div><div class="lions-pitch-right"></div>
      <div class="lions-mascot-zone">
        <img class="lions-real-cub" src="__LION_SRC__" alt="The Lions mascot">
        <div class="lions-moving-ball">⚽</div>
      </div>
      <div class="lions-brand-zone">
        <div class="lions-brand-title">THE LIONS</div>
        <div class="lions-brand-subtitle">DYULGEROV ACADEMY</div>
        <div class="lions-brand-divider"><span></span><b>⚽</b><span></span></div>
        <div class="lions-brand-year">2024</div>
      </div>
    </div>
    <style>
    .lions-parent-banner{position:relative;width:100%;min-height:255px;overflow:hidden;box-sizing:border-box;display:grid;grid-template-columns:43% 57%;align-items:center;padding:8px 5%;margin:2px 0 18px;background:radial-gradient(circle at 25% 50%,rgba(202,151,35,.08),transparent 33%),linear-gradient(135deg,#fffdf8 0%,#fff 58%,#fffaf0 100%);border:2px solid #c99523;border-radius:26px;box-shadow:0 10px 30px rgba(0,0,0,.16)}
    .lions-mascot-zone{position:relative;height:235px;display:flex;justify-content:center;align-items:flex-end}
    .lions-real-cub{position:relative;z-index:3;height:225px;width:auto;max-width:92%;object-fit:contain;filter:drop-shadow(0 8px 6px rgba(0,0,0,.16))}
    .lions-moving-ball{position:absolute;z-index:5;left:47%;bottom:10px;font-size:62px;line-height:1;filter:drop-shadow(0 6px 4px rgba(0,0,0,.24));transform-origin:center;animation:lionsBallTravel 7s ease-in-out infinite,lionsBallSpin 7s linear infinite}
    @keyframes lionsBallTravel{0%,8%,38%,45%,78%,100%{left:47%}23%{left:13%}62%{left:84%}}
    @keyframes lionsBallSpin{0%{rotate:0deg}23%{rotate:-540deg}38%{rotate:0deg}62%{rotate:540deg}78%,100%{rotate:0deg}}
    .lions-brand-zone{position:relative;z-index:4;text-align:center;padding:12px 18px}
    .lions-brand-title{font-family:Georgia,'Times New Roman',serif;font-size:clamp(42px,5vw,76px);font-weight:800;line-height:.95;letter-spacing:-2px;color:#080808!important}
    .lions-brand-subtitle{margin-top:15px;font-family:Arial,sans-serif;font-weight:700;font-size:clamp(12px,1.7vw,22px);letter-spacing:.30em;color:#111!important}
    .lions-brand-divider{margin-top:16px;display:flex;align-items:center;justify-content:center;gap:13px;color:#c99523!important}
    .lions-brand-divider span{width:86px;height:2px;background:#c99523}.lions-brand-divider b{font-size:18px}
    .lions-brand-year{margin-top:3px;font-family:Georgia,serif;font-size:20px;font-weight:700;letter-spacing:.35em;color:#080808!important}
    .lions-pitch-left,.lions-pitch-right{position:absolute;top:15%;height:70%;width:13%;opacity:.12;border-top:2px solid #c99523;border-bottom:2px solid #c99523}
    .lions-pitch-left{left:0;border-right:2px solid #c99523}.lions-pitch-right{right:0;border-left:2px solid #c99523}
    @media(max-width:650px){.lions-parent-banner{min-height:185px;grid-template-columns:43% 57%;padding:5px 2%;border-radius:18px}.lions-mascot-zone{height:165px}.lions-real-cub{height:157px}.lions-moving-ball{font-size:43px;left:47%;bottom:7px}.lions-brand-zone{padding:7px}.lions-brand-title{font-size:clamp(26px,8vw,40px);letter-spacing:-1px}.lions-brand-subtitle{font-size:9px;letter-spacing:.17em;margin-top:8px}.lions-brand-divider{margin-top:8px}.lions-brand-divider span{width:36px}.lions-brand-year{font-size:13px}}
    @media(prefers-reduced-motion:reduce){.lions-moving-ball{animation:none}}
    </style>
    """
    st.markdown(html.replace("__LION_SRC__",lion_src),unsafe_allow_html=True)


def staff_birthday_people():
    return fetchall("""SELECT c.display_name full_name,c.birth_date,c.profile_image,c.profile_image_mime FROM coaches c
        WHERE c.academy_id=%s AND c.active AND c.birth_date IS NOT NULL AND EXTRACT(MONTH FROM c.birth_date)=EXTRACT(MONTH FROM CURRENT_DATE) AND EXTRACT(DAY FROM c.birth_date)=EXTRACT(DAY FROM CURRENT_DATE)
        UNION ALL SELECT sp.display_name,sp.birth_date,sp.profile_image,sp.profile_image_mime FROM specialist_profiles sp JOIN users u ON u.id=sp.user_id
        WHERE sp.academy_id=%s AND sp.active AND u.active AND sp.birth_date IS NOT NULL AND EXTRACT(MONTH FROM sp.birth_date)=EXTRACT(MONTH FROM CURRENT_DATE) AND EXTRACT(DAY FROM sp.birth_date)=EXTRACT(DAY FROM CURRENT_DATE)
        ORDER BY full_name""",(ACADEMY_ID,ACADEMY_ID))

def render_staff_birthday_banners():
    people=staff_birthday_people()
    for person in people:
        parts=(person.get("full_name") or "").strip().split(maxsplit=1)
        banner=birthday_banner_bytes({"first_name":parts[0] if parts else "","last_name":parts[1] if len(parts)>1 else "","profile_image":person.get("profile_image"),"profile_image_mime":person.get("profile_image_mime")})
        if banner:st.image(banner,use_container_width=True)
    return len(people)

def birthday_players(team_id=None,parent_user_id=None):
    where=["p.academy_id=%s","p.active","p.approval_status='approved'","p.birth_date IS NOT NULL","EXTRACT(MONTH FROM p.birth_date)=EXTRACT(MONTH FROM CURRENT_DATE)","EXTRACT(DAY FROM p.birth_date)=EXTRACT(DAY FROM CURRENT_DATE)"]
    params=[ACADEMY_ID]
    joins=""
    if team_id is not None:
        where.append("p.team_id=%s");params.append(team_id)
    if parent_user_id is not None:
        joins=" JOIN parent_players pp ON pp.player_id=p.id AND pp.academy_id=p.academy_id "
        where.append("pp.parent_user_id=%s");params.append(parent_user_id)
    return fetchall(f"""SELECT DISTINCT p.id,p.first_name,p.last_name,p.birth_date,p.profile_image,p.profile_image_mime,t.name team_name
        FROM players p {joins} LEFT JOIN teams t ON t.id=p.team_id
        WHERE {' AND '.join(where)} ORDER BY p.first_name,p.last_name""",tuple(params))

def birthday_banner_bytes(player):
    template_path=ASSETS["birthday_template"]
    if not template_path.exists():
        return None
    image=Image.open(template_path).convert("RGB")
    draw=ImageDraw.Draw(image)
    width,height=image.size
    # Празното поле в шаблона е приблизително x=8%-92%, y=60.2%-70.7%.
    left,right=int(width*.08),int(width*.92)
    top,bottom=int(height*.602),int(height*.707)
    draw.rounded_rectangle((left,top,right,bottom),radius=max(18,int(width*.025)),fill=(8,8,10),outline=(205,153,42),width=max(4,int(width*.006)))
    photo_size=int((bottom-top)*.68)
    text_left=left+int(width*.035)
    if player.get("profile_image"):
        try:
            photo=Image.open(io.BytesIO(bytes(player["profile_image"]))).convert("RGB")
            photo=ImageOps.fit(photo,(photo_size,photo_size),method=Image.Resampling.LANCZOS)
            mask=Image.new("L",(photo_size,photo_size),0);ImageDraw.Draw(mask).ellipse((0,0,photo_size-1,photo_size-1),fill=255)
            px=left+int(width*.035);py=top+(bottom-top-photo_size)//2
            border=Image.new("RGB",(photo_size+10,photo_size+10),(205,153,42))
            border_mask=Image.new("L",border.size,0);ImageDraw.Draw(border_mask).ellipse((0,0,border.size[0]-1,border.size[1]-1),fill=255)
            image.paste(border,(px-5,py-5),border_mask);image.paste(photo,(px,py),mask)
            text_left=px+photo_size+int(width*.04)
        except Exception:
            pass
    name=f"{player['first_name']} {player['last_name']}".strip().upper()
    font_paths=["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
    font_path=next((fp for fp in font_paths if Path(fp).exists()),None)
    if not font_path:return None
    max_width=right-text_left-int(width*.035)
    font_size=max(28,int(height*.045))
    while font_size>24:
        font=ImageFont.truetype(font_path,font_size)
        box=draw.textbbox((0,0),name,font=font,stroke_width=2)
        if box[2]-box[0]<=max_width:break
        font_size-=2
    box=draw.textbbox((0,0),name,font=font,stroke_width=2)
    x=text_left+(max_width-(box[2]-box[0]))//2
    y=top+(bottom-top-(box[3]-box[1]))//2-box[1]
    draw.text((x,y),name,font=font,fill=(247,207,96),stroke_width=2,stroke_fill=(45,25,0))
    out=io.BytesIO();image.save(out,format="JPEG",quality=88,optimize=True)
    return out.getvalue()

def render_birthday_banners(team_id=None,parent_user_id=None,key_prefix="birthday"):
    players=birthday_players(team_id=team_id,parent_user_id=parent_user_id)
    for player in players:
        banner=birthday_banner_bytes(player)
        if banner:
            st.image(banner,use_container_width=True)
        else:
            st.warning("Липсва файлът чрд.jpg в основната папка на проекта.")
            break
    return len(players)

def render_all_academy_birthday_banners():
    """Показва всички днешни рожденици: деца, треньори и специалисти."""
    people=[]
    for player in birthday_players():
        people.append({
            "person_key":f"player_{player['id']}",
            "first_name":player.get("first_name") or "",
            "last_name":player.get("last_name") or "",
            "profile_image":player.get("profile_image"),
            "profile_image_mime":player.get("profile_image_mime"),
        })
    for index,person in enumerate(staff_birthday_people()):
        parts=(person.get("full_name") or "").strip().split(maxsplit=1)
        people.append({
            "person_key":f"staff_{index}_{person.get('full_name') or ''}",
            "first_name":parts[0] if parts else "",
            "last_name":parts[1] if len(parts)>1 else "",
            "profile_image":person.get("profile_image"),
            "profile_image_mime":person.get("profile_image_mime"),
        })
    seen=set()
    for person in people:
        if person["person_key"] in seen:continue
        seen.add(person["person_key"])
        banner=birthday_banner_bytes(person)
        if banner:st.image(banner,use_container_width=True)
    return len(seen)

def team_child_contact_rows(team_id):
    return fetchall("""SELECT p.id,p.first_name,p.last_name,p.birth_date,p.position,p.profile_image,p.profile_image_mime,
        u.first_name parent_first,u.last_name parent_last,u.email parent_email,u.phone parent_phone
        FROM players p LEFT JOIN parent_players pp ON pp.player_id=p.id AND pp.academy_id=p.academy_id
        LEFT JOIN users u ON u.id=pp.parent_user_id AND u.academy_id=pp.academy_id AND u.role='parent'
        WHERE p.academy_id=%s AND p.team_id=%s AND p.active AND p.approval_status='approved'
        ORDER BY p.first_name,p.last_name,u.first_name,u.last_name""",(ACADEMY_ID,team_id))

def render_child_contact_cards(team_id):
    rows=team_child_contact_rows(team_id)
    if not rows: st.info("Няма потвърдени деца в този набор."); return
    grouped={}
    for row in rows:
        grouped.setdefault(row["id"],{"player":row,"parents":[]})
        if row.get("parent_email") or row.get("parent_phone"): grouped[row["id"]]["parents"].append(row)
    for item in grouped.values():
        player=item["player"]
        with st.container(border=True):
            photo_col,info_col=st.columns([1,5])
            with photo_col: st.markdown(avatar_html(player.get("profile_image"),player.get("profile_image_mime"),72),unsafe_allow_html=True)
            with info_col:
                st.markdown(f"### ⚽ {player['first_name']} {player['last_name']}")
                if player.get("birth_date"): st.caption(f"Роден/а: {player['birth_date']:%d.%m.%Y}")
                if player.get("position"): st.caption(f"Позиция: {player['position']}")
                if item["parents"]:
                    for parent in item["parents"]:
                        parent_name=f"{parent.get('parent_first') or ''} {parent.get('parent_last') or ''}".strip() or "Регистриран родител"
                        st.write(f"**👤 Родител: {parent_name}**")
                        st.write(f"✉️ {parent.get('parent_email') or 'Няма имейл'} · ☎️ {parent.get('parent_phone') or 'Няма телефон'}")
                else: st.warning("Няма свързан родителски контакт.")

def registration_children(parent_user_id):
    return fetchall("""SELECT p.first_name,p.last_name,p.birth_date,t.name team_name,p.approval_status
        FROM parent_players pp JOIN players p ON p.id=pp.player_id
        LEFT JOIN teams t ON t.id=p.team_id
        WHERE pp.academy_id=%s AND pp.parent_user_id=%s
        ORDER BY p.first_name,p.last_name""",(ACADEMY_ID,parent_user_id))

def render_registration_children(parent_user_id):
    children=registration_children(parent_user_id)
    if not children:
        st.caption("Няма добавени деца към тази регистрация.")
        return
    st.markdown("**Деца в регистрацията:**")
    for child in children:
        birth=child.get("birth_date")
        birth_text=birth.strftime("%d.%m.%Y") if birth else "Липсва дата на раждане"
        st.write(f"• {child['first_name']} {child['last_name']} · 🎂 {birth_text} · {child.get('team_name') or 'Без набор'}")

def parent_nav():
    items=[("🏠 Начало","Начало"),("📅 Тренировки","Тренировки"),("⚽ Мачове","Мачове"),("💬 Съобщение от треньор","Съобщения"),("💰 Такси и Поръчки","Такси"),("ℹ️ Информация","Информация"),("📝 Обратна връзка","Обратна връзка"),("🗣️ ФЕН ЧАТ","ФЕН ЧАТ"),("👤 Настройки","Настройки")]
    st.session_state.setdefault("parent_page","Начало")
    with st.container(key="parent_nav"):
        for start in range(0,len(items),2):
            cols=st.columns(2,gap="small")
            for col,(label,page) in zip(cols,items[start:start+2]):
                if col.button(label,key=f"pnav_{page}",use_container_width=True,type="primary" if st.session_state.parent_page==page else "secondary"):
                    st.session_state.parent_page=page; st.rerun()

def parent_players(user_id):
    return fetchall("""SELECT p.*,t.name team_name,COALESCE(tc.coach_names,'Очаква свързване') coach_name,tc.coach_cards,
        CASE WHEN p.position='Вратар' THEN gk.goalkeeper_cards ELSE NULL END goalkeeper_cards
        FROM parent_players pp JOIN players p ON p.id=pp.player_id LEFT JOIN teams t ON t.id=p.team_id
        LEFT JOIN LATERAL (SELECT STRING_AGG(c.display_name, ', ' ORDER BY c.display_name) coach_names,
          JSONB_AGG(JSONB_BUILD_OBJECT('name',c.display_name,'image',ENCODE(c.profile_image,'base64'),'mime',COALESCE(c.profile_image_mime,'image/jpeg')) ORDER BY c.display_name) FILTER(WHERE c.id IS NOT NULL) coach_cards
          FROM coach_teams ct JOIN coaches c ON c.id=ct.coach_id AND c.active WHERE ct.academy_id=p.academy_id AND ct.team_id=p.team_id) tc ON TRUE
        LEFT JOIN LATERAL (SELECT JSONB_AGG(JSONB_BUILD_OBJECT('name',sp.display_name,'image',ENCODE(sp.profile_image,'base64'),'mime',COALESCE(sp.profile_image_mime,'image/jpeg')) ORDER BY sp.display_name) goalkeeper_cards
          FROM specialist_teams st JOIN specialist_profiles sp ON sp.user_id=st.specialist_user_id AND sp.active AND sp.specialist_type='goalkeeper'
          JOIN users su ON su.id=sp.user_id AND su.active WHERE st.academy_id=p.academy_id AND st.team_id=p.team_id) gk ON TRUE
        WHERE pp.academy_id=%s AND pp.parent_user_id=%s AND p.active ORDER BY p.first_name""",(ACADEMY_ID,user_id))
def profile_cards_html(cards,title):
    if not cards:return ''
    blocks=[]
    for card in cards:
        image=card.get('image') or '';mime=card.get('mime') or 'image/jpeg';name=card.get('name') or 'Профил'
        av=f'<img src="data:{mime};base64,{image}" style="width:64px;height:64px;border-radius:50%;object-fit:cover;border:2px solid #d6b765">' if image else '<div style="width:64px;height:64px;border-radius:50%;border:2px solid #d6b765;display:flex;align-items:center;justify-content:center">👤</div>'
        blocks.append(f'<div style="text-align:center;min-width:85px">{av}<div style="font-size:.78rem;font-weight:800">{name}</div></div>')
    return f'<div style="color:#c8c1b5;font-size:.78rem;margin:6px 0">{title}</div><div style="display:flex;flex-wrap:wrap;gap:12px">'+''.join(blocks)+'</div>'
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
    if not team_id:return {"trainings":0,"present":0,"absent":0,"percent":None}
    start,end=month_bounds(year,month)
    r=fetchone("""SELECT COUNT(DISTINCT e.id) trainings,
        COUNT(DISTINCT e.id) FILTER(WHERE a.status='present') present,
        COUNT(DISTINCT e.id) FILTER(WHERE a.status='absent') absent
        FROM events e LEFT JOIN attendance a ON a.event_id=e.id AND a.player_id=%s AND a.academy_id=%s
        WHERE e.academy_id=%s AND e.team_id=%s AND e.event_type='training'
        AND e.starts_at >= %s AND e.starts_at < %s AND e.status <> 'cancelled'""",
        (player_id,ACADEMY_ID,ACADEMY_ID,team_id,start,end)) or {}
    total=int(r.get("trainings") or 0);present=int(r.get("present") or 0)
    return {"trainings":total,"present":present,"absent":int(r.get("absent") or 0),"percent":present/total*100 if total else None}

def render_attendance_stats(player_id,team_id,year,month):
    a=player_attendance_stats(player_id,team_id,year,month);st.markdown("#### 📅 Автоматична посещаемост")
    if not a["trainings"]:st.info("За избрания месец все още няма достатъчно данни за проведени тренировки.");return
    c=st.columns(4);c[0].metric("Тренировки",a["trainings"]);c[1].metric("Присъствия",a["present"]);c[2].metric("Отсъствия",a["absent"]);c[3].metric("Посещаемост",f"{a['percent']:.1f}%")

def render_parent_player_development(user,child):
    rows=fetchall("""SELECT pe.* FROM player_evaluations pe JOIN parent_players pp ON pp.player_id=pe.player_id
        WHERE pe.academy_id=%s AND pp.academy_id=%s AND pp.parent_user_id=%s AND pe.player_id=%s
        ORDER BY pe.evaluation_year DESC,pe.evaluation_month DESC""",(ACADEMY_ID,ACADEMY_ID,user["id"],child["id"]))
    if not rows:
        st.info("Все още няма публикувана месечна оценка от треньора.")
        return
    labels=[("⚽ Техника","technique"),("📋 Тактика","tactics"),("🏃 Физическа подготовка","physical"),("🤝 Работа в екип","teamwork"),("💪 Дисциплина","discipline"),("⭐ Старание","effort")]
    available_years=sorted({int(row["evaluation_year"]) for row in rows},reverse=True)
    period_options=["Всички оценки"]+[f"{year} година" for year in available_years]
    selected_period=st.selectbox("Период за обобщената статистика",period_options,key=f"development_period_{child['id']}")
    period_rows=rows if selected_period=="Всички оценки" else [row for row in rows if int(row["evaluation_year"])==int(selected_period.split()[0])]
    period_label="целия наличен период" if selected_period=="Всички оценки" else selected_period
    period_averages={field:sum(float(row[field]) for row in period_rows)/len(period_rows) for _,field in labels}
    period_total=sum(period_averages.values())/len(period_averages)
    if selected_period == "Всички оценки":
        match_rating_row=fetchone("SELECT AVG(mps.match_rating)::numeric avg_rating FROM match_player_stats mps JOIN events e ON e.id=mps.event_id WHERE mps.academy_id=%s AND mps.player_id=%s AND mps.match_rating IS NOT NULL",(ACADEMY_ID,child["id"]))
    else:
        selected_year=int(selected_period.split()[0])
        match_rating_row=fetchone("SELECT AVG(mps.match_rating)::numeric avg_rating FROM match_player_stats mps JOIN events e ON e.id=mps.event_id WHERE mps.academy_id=%s AND mps.player_id=%s AND mps.match_rating IS NOT NULL AND EXTRACT(YEAR FROM e.starts_at)::int = %s",(ACADEMY_ID,child["id"],selected_year))
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
    latest_match_row=fetchone(
        """SELECT AVG(mps.match_rating)::numeric avg_rating FROM match_player_stats mps
           JOIN events e ON e.id=mps.event_id
           WHERE mps.academy_id=%s AND mps.player_id=%s AND mps.match_rating IS NOT NULL
             AND EXTRACT(YEAR FROM e.starts_at)::int = %s AND EXTRACT(MONTH FROM e.starts_at)::int = %s""",
        (ACADEMY_ID,child["id"],latest["evaluation_year"],latest["evaluation_month"]),
    )
    latest_match_avg=float(latest_match_row["avg_rating"]) if latest_match_row and latest_match_row.get("avg_rating") is not None else None
    if latest_match_avg is not None:
        score=score*0.80+latest_match_avg*0.20
    with st.container(border=True):
        st.markdown(f"### Последна оценка: {latest['evaluation_month']:02d}.{latest['evaluation_year']}")
        for label,field in labels:st.write(f"**{label}:** {star_rating(latest[field])}")
        st.markdown(f"### Общо развитие: {score:.1f} / 5");st.write(f"**{evaluation_status(score)}**")
        if latest_match_avg is not None:
            st.caption(f"Автоматично включени мачови оценки за месеца: {latest_match_avg:.1f}/5 (20% от общия резултат).")
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
    players=fetchall("""SELECT p.* FROM players p JOIN coach_teams ct ON ct.team_id=p.team_id AND ct.academy_id=p.academy_id
        JOIN coaches c ON c.id=ct.coach_id
        WHERE p.academy_id=%s AND p.team_id=%s AND p.approval_status='approved' AND p.active
        AND c.academy_id=%s AND c.user_id=%s AND c.active ORDER BY p.first_name,p.last_name""",(ACADEMY_ID,team["id"],ACADEMY_ID,user["id"]))
    if not players:st.info("Няма потвърдени футболисти в този набор.");return
    pmap={f"{x['first_name']} {x['last_name']}":x for x in players};label=st.selectbox("Футболист",list(pmap),key=f"eval_player_{user['id']}_{team['id']}");player=pmap[label];now=date.today()
    c1,c2=st.columns(2);month=c1.selectbox("Месец",range(1,13),index=now.month-1,key=f"eval_month_{team['id']}_{player['id']}");year=int(c2.number_input("Година",2020,2100,now.year,key=f"eval_year_{team['id']}_{player['id']}"))
    old=fetchone("""SELECT pe.* FROM player_evaluations pe JOIN coach_teams ct ON ct.team_id=pe.team_id AND ct.academy_id=pe.academy_id
        JOIN coaches c ON c.id=ct.coach_id
        WHERE pe.academy_id=%s AND pe.player_id=%s AND pe.team_id=%s AND pe.evaluation_year=%s AND pe.evaluation_month=%s AND c.user_id=%s AND c.active""",(ACADEMY_ID,player["id"],team["id"],year,month,user["id"]))
    if old:st.info("За този месец има запазена оценка. Промените ще обновят съществуващия запис.")
    criteria=[("⚽ Техника","technique"),("📋 Тактика","tactics"),("🏃 Физическа подготовка","physical"),("🤝 Работа в екип","teamwork"),("💪 Дисциплина","discipline"),("⭐ Старание / отношение към тренировките","effort")];vals={}
    for label,field in criteria:vals[field]=st.radio(label,options=[1,2,3,4,5],index=(int(old[field])-1) if old else 2,format_func=lambda x:f"{x} ★",horizontal=True,key=f"eval_{field}_{team['id']}_{player['id']}_{year}_{month}")
    note=st.text_area("📝 Бележка от треньора",value=(old.get("coach_note") or "") if old else "",key=f"eval_note_{team['id']}_{player['id']}_{year}_{month}")
    score=sum(vals.values())/6;st.markdown(f"### Общо развитие: {score:.1f} / 5");st.write(f"**{evaluation_status(score)}**");st.caption("Статусът показва развитието за периода, а не определя детето като добър или слаб футболист.");render_attendance_stats(player["id"],team["id"],year,month)
    if st.button("💾 Запази месечната оценка",key=f"save_eval_{team['id']}_{player['id']}_{year}_{month}",use_container_width=True,type="primary"):
        saved=execute("""INSERT INTO player_evaluations(academy_id,player_id,team_id,coach_user_id,evaluation_year,evaluation_month,technique,tactics,physical,teamwork,discipline,effort,coach_note)
            SELECT %s,p.id,p.team_id,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s FROM players p JOIN coach_teams ct ON ct.team_id=p.team_id AND ct.academy_id=p.academy_id
            JOIN coaches c ON c.id=ct.coach_id
            WHERE p.id=%s AND p.academy_id=%s AND p.team_id=%s AND p.approval_status='approved' AND p.active AND c.academy_id=%s AND c.user_id=%s AND c.active
            ON CONFLICT(player_id,evaluation_year,evaluation_month) DO UPDATE SET academy_id=EXCLUDED.academy_id,team_id=EXCLUDED.team_id,coach_user_id=EXCLUDED.coach_user_id,technique=EXCLUDED.technique,tactics=EXCLUDED.tactics,physical=EXCLUDED.physical,teamwork=EXCLUDED.teamwork,discipline=EXCLUDED.discipline,effort=EXCLUDED.effort,coach_note=EXCLUDED.coach_note,updated_at=NOW() RETURNING id""",
            (ACADEMY_ID,user["id"],year,month,vals["technique"],vals["tactics"],vals["physical"],vals["teamwork"],vals["discipline"],vals["effort"],note.strip() or None,player["id"],ACADEMY_ID,team["id"],ACADEMY_ID,user["id"]),True)
        if saved:set_flash(f"✅ Месечната оценка е запазена. Общо развитие: {score:.1f} / 5");st.rerun()
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
        """SELECT e.* FROM events e
           WHERE e.academy_id=%s AND e.team_id=%s AND e.event_type='match'
             AND NOT EXISTS(
                 SELECT 1 FROM match_reports mr
                 WHERE mr.academy_id=e.academy_id AND mr.event_id=e.id AND mr.confirmed
             )
           ORDER BY e.starts_at ASC""",
        (ACADEMY_ID,team["id"]),
    )
    if not matches:
        st.info("Няма активни мачове за този набор. Приключените срещи са в „Резултати“.")
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
    st.caption("За приключване на мача е задължителен само резултатът. Отзивите и оценките за футболистите са по желание.")
    old_stats={r["player_id"]:r for r in fetchall(
        "SELECT * FROM match_player_stats WHERE academy_id=%s AND event_id=%s",
        (ACADEMY_ID,match["id"]),
    )}
    with st.expander("⭐ Добави отзиви и оценки по желание",expanded=False):
        selected_player_names=st.multiselect(
            "Избери само футболистите, които искаш да оцениш",
            [f"{p['first_name']} {p['last_name']}" for p in squad],
            key=f"optional_feedback_players_{match['id']}",
        )
        player_by_name={f"{p['first_name']} {p['last_name']}":p for p in squad}
        if selected_player_names:
            with st.form(f"matchday_optional_feedback_{match['id']}"):
                feedback={}
                for player_name in selected_player_names:
                    player=player_by_name[player_name];old=old_stats.get(player["id"],{})
                    with st.container(border=True):
                        photo_col,info_col,rating_col=st.columns([1,5,2])
                        with photo_col:
                            st.markdown(avatar_html(player.get("profile_image"),player.get("profile_image_mime"),54),unsafe_allow_html=True)
                        with info_col:
                            st.write(f"**{player_name}**")
                            note=st.text_input("Кратък отзив",value=old.get("note") or "",key=f"match_note_{match['id']}_{player['id']}",placeholder="По желание",label_visibility="collapsed")
                        with rating_col:
                            rating=st.select_slider("Оценка",options=[1,2,3,4,5],value=int(old.get("match_rating") or 3),format_func=star_rating,key=f"match_rating_{match['id']}_{player['id']}")
                        feedback[player["id"]]=(rating,note)
                save_feedback=st.form_submit_button("💾 Запази избраните отзиви",use_container_width=True)
            if save_feedback:
                for player_id,(rating,note) in feedback.items():
                    execute("""INSERT INTO match_player_stats(academy_id,event_id,player_id,goals,match_rating,note,updated_by)
                               VALUES(%s,%s,%s,0,%s,%s,%s)
                               ON CONFLICT(event_id,player_id) DO UPDATE SET
                                 match_rating=EXCLUDED.match_rating,note=EXCLUDED.note,
                                 updated_by=EXCLUDED.updated_by,updated_at=NOW()""",
                            (ACADEMY_ID,match["id"],player_id,rating,note.strip() or None,user["id"]))
                set_flash("✅ Избраните отзиви са запазени и участват в статистиката на децата.");st.rerun()
        else:
            st.info("Няма избрани футболисти за оценяване. Можеш директно да въведеш резултата и да приключиш мача.")
    report=fetchone(
        "SELECT * FROM match_reports WHERE academy_id=%s AND event_id=%s",
        (ACADEMY_ID,match["id"]),
    ) or {}
    st.markdown("### 🏆 Как завърши мачът?")
    saved_outcome=report.get("outcome")
    if not saved_outcome and report:
        saved_outcome="win" if report.get("home_score",0)>report.get("away_score",0) else "draw" if report.get("home_score",0)==report.get("away_score",0) else "loss"
    outcome_options=["win","draw","loss"]
    with st.form(f"matchday_result_{match['id']}"):
        outcome=st.radio(
            "Резултат",outcome_options,
            index=outcome_options.index(saved_outcome) if saved_outcome in outcome_options else 0,
            format_func=lambda x:{"win":"🟢 ПОБЕДА","draw":"🔵 РАВЕН","loss":"🔴 ЗАГУБА"}[x],
            horizontal=True,
        )
        opponent=st.text_input(
            "Име на противника",
            value="" if (report.get("opponent_name") or "") == "Друг отбор" else (report.get("opponent_name") or match.get("opponent") or ""),
            placeholder="Друг отбор",
        )
        detailed=st.checkbox(
            "⚽ Добави точен резултат и голмайстори",
            value=bool(report and (report.get("home_score",0) or report.get("away_score",0))),
            help="По желание. Ако отметката не е включена, се записва само Победа, Равен или Загуба.",
        )
        home_score=away_score=None
        scorer_goals={}
        if detailed:
            home_col,away_col=st.columns(2)
            home_score=home_col.number_input("Голове на The Lions",0,30,int(report.get("home_score") or 0))
            away_score=away_col.number_input("Голове на противника",0,30,int(report.get("away_score") or 0))
            st.markdown("#### ⚽ Голмайстори")
            scorer_map={f"{p['first_name']} {p['last_name']}":p for p in squad}
            selected_scorers=st.multiselect("Избери голмайстори от групата",list(scorer_map))
            for scorer_name in selected_scorers:
                scorer_goals[scorer_map[scorer_name]["id"]]=st.number_input(f"Голове · {scorer_name}",1,20,1,key=f"scorer_goals_{match['id']}_{scorer_map[scorer_name]['id']}")
        confirm=st.form_submit_button("✅ Приключи мача и изпрати в архива",use_container_width=True,type="primary")
    if confirm:
        if not opponent.strip():
            st.error("Моля, въведи името на другия отбор.")
        elif detailed and ((home_score>away_score and outcome!='win') or (home_score==away_score and outcome!='draw') or (home_score<away_score and outcome!='loss')):
            st.error("Точният резултат не съответства на избраната опция Победа / Равен / Загуба.")
        else:
            # При прост режим пазим съвместими технически стойности, без да показваме точен резултат.
            stored_home,stored_away=(home_score,away_score) if detailed else ({"win":(1,0),"draw":(0,0),"loss":(0,1)}[outcome])
            execute("""INSERT INTO match_reports(
                       academy_id,event_id,team_id,opponent_name,home_score,away_score,outcome,has_exact_score,result_mode,
                       standings_url,confirmed,confirmed_by,confirmed_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,NOW())
                   ON CONFLICT(event_id) DO UPDATE SET
                       opponent_name=EXCLUDED.opponent_name,home_score=EXCLUDED.home_score,
                       away_score=EXCLUDED.away_score,outcome=EXCLUDED.outcome,has_exact_score=EXCLUDED.has_exact_score,result_mode=EXCLUDED.result_mode,
                       standings_url=EXCLUDED.standings_url,confirmed=TRUE,
                       confirmed_by=EXCLUDED.confirmed_by,confirmed_at=NOW(),updated_at=NOW()""",
                    (ACADEMY_ID,match["id"],team["id"],opponent.strip() or "Друг отбор",stored_home,stored_away,outcome,detailed,"detailed" if detailed else "simple",team.get("standings_url"),user["id"]))
            execute("UPDATE match_player_stats SET goals=0,updated_at=NOW() WHERE academy_id=%s AND event_id=%s",(ACADEMY_ID,match["id"]))
            if detailed:
                for scorer_id,goals in scorer_goals.items():
                    execute("""INSERT INTO match_player_stats(academy_id,event_id,player_id,goals,updated_by)
                               VALUES(%s,%s,%s,%s,%s)
                               ON CONFLICT(event_id,player_id) DO UPDATE SET goals=EXCLUDED.goals,updated_by=EXCLUDED.updated_by,updated_at=NOW()""",
                            (ACADEMY_ID,match["id"],scorer_id,goals,user["id"]))
            outcome_label={"win":"Победа","draw":"Равен","loss":"Загуба"}[outcome]
            detail_text=f" · {home_score}:{away_score}" if detailed else ""
            notify_team_parents(team["id"],"match_result","Публикуван е резултат",f"{outcome_label}{detail_text} срещу {opponent.strip() or 'Друг отбор'}")
            execute("DELETE FROM event_attachments WHERE academy_id=%s AND event_id=%s",(ACADEMY_ID,match["id"]))
            set_flash("✅ Мачът е приключен и е изпратен в архива.")
            st.rerun()


def render_results_dashboard(team, editable=False, key_prefix="results"):
    st.markdown(f"## 🏆 {team['name']} · Резултати и класиране")
    standings_url=team.get("standings_url") or ""
    top_link_col, _ = st.columns([1.2,2.8])
    with top_link_col:
        st.button(
            "🔒 Официално класиране · предстои",
            key=f"{key_prefix}_locked_standings_{team['id']}",
            use_container_width=True,
            disabled=True,
            help="Официалното класиране ще бъде активирано поетапно.",
        )
    if editable:
        with st.expander("⚙️ Класиране",expanded=False):
            new_url=st.text_input("Линк към актуалното класиране",value=standings_url,placeholder="Напр. страница на БФС",key=f"{key_prefix}_standings_{team['id']}")
            a,b=st.columns(2)
            if a.button("💾 Запази",key=f"{key_prefix}_save_standings_{team['id']}",use_container_width=True):
                execute("UPDATE teams SET standings_url=%s WHERE academy_id=%s AND id=%s",(new_url.strip() or None,ACADEMY_ID,team["id"]))
                set_flash("✅ Линкът към класирането е запазен.");st.rerun()
            if new_url.strip(): b.link_button("🔗 Отвори",new_url.strip(),use_container_width=True)
    years=fetchall("""SELECT DISTINCT EXTRACT(YEAR FROM e.starts_at)::int AS match_year FROM match_reports mr JOIN events e ON e.id=mr.event_id WHERE mr.academy_id=%s AND mr.team_id=%s AND mr.confirmed ORDER BY match_year DESC""",(ACADEMY_ID,team["id"]))
    if not years:
        st.info("Няма потвърдени резултати.")
        return
    selected_year=st.selectbox("Година",[r["match_year"] for r in years],key=f"{key_prefix}_year_{team['id']}")
    rows=fetchall("""SELECT mr.*,e.title,e.starts_at FROM match_reports mr JOIN events e ON e.id=mr.event_id WHERE mr.academy_id=%s AND mr.team_id=%s AND mr.confirmed AND EXTRACT(YEAR FROM e.starts_at)::int = %s ORDER BY e.starts_at DESC""",(ACADEMY_ID,team["id"],selected_year))
    def row_outcome(r):
        return r.get("outcome") or ("win" if r["home_score"]>r["away_score"] else "draw" if r["home_score"]==r["away_score"] else "loss")
    wins=sum(row_outcome(r)=="win" for r in rows);draws=sum(row_outcome(r)=="draw" for r in rows);losses=sum(row_outcome(r)=="loss" for r in rows)
    for col,label,val in zip(st.columns(4),["Мачове","Победи","Равни","Загуби"],[len(rows),wins,draws,losses]):col.metric(label,val)
    matches_tab,scorers_tab=st.tabs(["📅 Резултати","⚽ Голмайстори"])
    with matches_tab:
        for r in rows:
            outcome=row_outcome(r)
            status={"win":"Победа","draw":"Равен","loss":"Загуба"}[outcome]
            icon={"win":"🟢","draw":"🔵","loss":"🔴"}[outcome]
            with st.container(border=True):
                detailed_result=(r.get("result_mode") == "detailed") if r.get("result_mode") else bool(r.get("has_exact_score"))
                if detailed_result:
                    st.markdown(f"### {icon} The Lions {r['home_score']} : {r['away_score']} {r['opponent_name']}")
                    st.caption(f"{r['starts_at']:%d.%m.%Y %H:%M} · {r['title']} · {status}")
                else:
                    st.markdown(f"### {icon} {status} · The Lions срещу {r['opponent_name']}")
                    st.caption(f"{r['starts_at']:%d.%m.%Y %H:%M} · {r['title']}")
                scorers=fetchall("SELECT p.first_name,p.last_name,mps.goals FROM match_player_stats mps JOIN players p ON p.id=mps.player_id WHERE mps.academy_id=%s AND mps.event_id=%s AND mps.goals>0 ORDER BY mps.goals DESC",(ACADEMY_ID,r["event_id"]))
                if scorers:st.write("⚽ Голмайстори: "+", ".join(f"{x['first_name']} {x['last_name']} ({x['goals']})" for x in scorers))
    with scorers_tab:
        scorers=fetchall("""SELECT p.id,p.first_name,p.last_name,COALESCE(SUM(mps.goals),0)::int goals FROM match_player_stats mps JOIN players p ON p.id=mps.player_id JOIN events e ON e.id=mps.event_id WHERE mps.academy_id=%s AND p.team_id=%s AND EXTRACT(YEAR FROM e.starts_at)::int = %s GROUP BY p.id,p.first_name,p.last_name HAVING SUM(mps.goals)>0 ORDER BY goals DESC,p.first_name""",(ACADEMY_ID,team["id"],selected_year))
        if not scorers:st.info("Няма записани голмайстори за избраната година.")
        medals=["🥇","🥈","🥉"]
        for i,player in enumerate(scorers,1):
            prefix=medals[i-1] if i<=3 else f"{i}."
            st.markdown(f"### {prefix} {player['first_name']} {player['last_name']} · ⚽ {player['goals']}")

def results_statistics_panel(team):
    render_results_dashboard(team,editable=True,key_prefix="coach_results")

def attendance_history_rows(team_id,player_id=None):
    extra=" AND a.player_id=%s" if player_id is not None else "";params=(ACADEMY_ID,team_id,player_id) if player_id is not None else (ACADEMY_ID,team_id)
    return fetchall(f"""SELECT a.status,a.marked_at,e.starts_at,e.title,p.first_name,p.last_name FROM attendance a JOIN events e ON e.id=a.event_id JOIN players p ON p.id=a.player_id WHERE a.academy_id=%s AND e.team_id=%s {extra} ORDER BY e.starts_at DESC,p.first_name,p.last_name""",params)
def render_parent_attendance_history(child):
    st.subheader("✅ Присъствия и отсъствия");rows=attendance_history_rows(child.get("team_id"),child["id"]) if child.get("team_id") else []
    flt=st.selectbox("Покажи",["Всички","Присъствия","Отсъствия"],key=f"parent_att_{child['id']}")
    if flt!="Всички":rows=[r for r in rows if r["status"]==("present" if flt=="Присъствия" else "absent")]
    if not rows:st.info("Все още няма отразени присъствия или отсъствия.");return
    months={}
    for r in rows:months.setdefault(r["starts_at"].strftime("%m.%Y"),[]).append(r)
    for period,items in months.items():
        with st.expander(f"📅 {period} · {sum(r['status']=='present' for r in items)} присъствия · {sum(r['status']=='absent' for r in items)} отсъствия"):
            for r in items:st.write(f"{'🟢' if r['status']=='present' else '🔴'} **{r['starts_at']:%d.%m.%Y %H:%M}** · {'Присъства' if r['status']=='present' else 'Отсъства'}")
def render_coach_attendance_history(team):
    st.markdown("### 📚 Подадени присъствени форми");rows=attendance_history_rows(team["id"])
    if not rows:st.info("Все още няма подадени присъствени форми.");return
    c1,c2,c3=st.columns(3);nf=c1.selectbox("Име",["Всички"]+sorted({f"{r['first_name']} {r['last_name']}" for r in rows}),key=f"ahn{team['id']}");df=c2.selectbox("Дата",["Всички"]+sorted({r['starts_at'].strftime('%d.%m.%Y') for r in rows},reverse=True),key=f"ahd{team['id']}");sf=c3.selectbox("Статус",["Всички","Присъства","Отсъства"],key=f"ahs{team['id']}")
    rows=[r for r in rows if (nf=="Всички" or f"{r['first_name']} {r['last_name']}"==nf) and (df=="Всички" or r['starts_at'].strftime('%d.%m.%Y')==df) and (sf=="Всички" or r['status']==('present' if sf=="Присъства" else 'absent'))]
    groups={}
    for r in rows:groups.setdefault((r['starts_at'],r['title']),[]).append(r)
    for (dt,title),items in groups.items():
        with st.expander(f"📅 {dt:%d.%m.%Y %H:%M} · {title} · {len(items)} записа"):
            for r in items:st.write(f"{'🟢' if r['status']=='present' else '🔴'} **{r['first_name']} {r['last_name']}** · {'Присъства' if r['status']=='present' else 'Отсъства'}")

def parent_panel(user):
    parent_lions_banner(); parent_nav(); show_flash()
    page=st.session_state.parent_page
    if page=="Начало":
        render_all_academy_birthday_banners()
    players=parent_players(user["id"])
    if not players: st.info("Няма добавени деца към профила."); return
    labels={f"{p['first_name']} {p['last_name']}":p for p in players}; chosen=st.selectbox("Избрано дете",list(labels)); child=labels[chosen]
    st.caption(f"Група: {child.get('team_name') or 'Очаква свързване'} · Треньор: {child.get('coach_name') or 'Очаква свързване'}")
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
        st.markdown("### Треньорски екип")
        with st.container(border=True):
            st.markdown(profile_cards_html(child.get('coach_cards'),'Треньори на набора'),unsafe_allow_html=True)
            if child.get('position')=='Вратар' and child.get('goalkeeper_cards'):
                st.markdown(profile_cards_html(child.get('goalkeeper_cards'),'Треньор на вратарите'),unsafe_allow_html=True)
        with st.container(border=True):
            fee_col,player_col=st.columns([4,1])
            with fee_col:
                st.markdown("**Такса**")
                st.markdown(f"# {academy()['default_fee']:.2f} €")
                st.caption(f"За: {child['first_name']} {child['last_name']}")
            with player_col:
                st.markdown(avatar_html(child.get('profile_image'),child.get('profile_image_mime'),82),unsafe_allow_html=True)
        if latest_info:
            with st.container(border=True):
                st.subheader(latest_info["title"])
                st.write(latest_info.get("body") or "")
                st.caption(latest_info["created_at"].strftime("%d.%m.%Y %H:%M"))
        else:
            st.info("Няма нова информация от академията.")
    elif page == "Тренировки":
        rows=fetchall("""SELECT e.* FROM events e WHERE e.academy_id=%s AND e.team_id=%s AND e.event_type='training' AND e.status<>'cancelled' AND (e.target_all_players OR EXISTS(SELECT 1 FROM event_players ep WHERE ep.event_id=e.id AND ep.player_id=%s)) ORDER BY e.starts_at ASC LIMIT 80""",(ACADEMY_ID,child.get("team_id"),child["id"])) if child.get("team_id") else []
        if rows:
            future=[r for r in rows if r["starts_at"]>=datetime.now(r["starts_at"].tzinfo)]
            nxt=future[0] if future else rows[0]
            st.markdown("### 🟢 Следваща тренировка")
            with st.container(border=True):
                st.markdown(training_datetime_html(nxt['starts_at'],large=True),unsafe_allow_html=True)
                st.write(f"📍 {nxt.get('location') or 'Без посочено място'}")
            notes=[]
            for r in rows:
                n=(r.get("description") or "").strip()
                if n and n not in notes:notes.append(n)
            if notes:st.info("ℹ️ "+notes[0])
            weeks={}
            for r in rows:
                ws=(r["starts_at"]-timedelta(days=r["starts_at"].weekday())).date();weeks.setdefault(ws,[]).append(r)
            cw=date.today()-timedelta(days=date.today().weekday())
            for ws,items in weeks.items():
                with st.expander(f"📅 {ws:%d.%m} – {(ws+timedelta(days=6)):%d.%m} · {len(items)} тренировки",expanded=ws==cw):
                    for r in items:
                        a,b,c=st.columns([2,1,4]);a.markdown(training_date_html(r['starts_at']),unsafe_allow_html=True);b.write(f"**{r['starts_at']:%H:%M}**");c.write(f"📍 {r.get('location') or 'Без място'}")
        else:st.info("Няма добавени тренировки за избраното дете.")
        st.divider()
        render_parent_attendance_history(child)
        st.divider();show_development=st.checkbox("📊 Развитие на футболиста",False,key=f"show_development_{child['id']}")
        st.info("ℹ️ Тази статистика не е актуална. Ще бъдете уведомени от треньора, когато оценяването влезе в сила.")
        if show_development:render_parent_player_development(user,child)
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
                   AND status <> 'cancelled'
                   AND NOT EXISTS(
                       SELECT 1 FROM match_reports mr
                       WHERE mr.academy_id=events.academy_id AND mr.event_id=events.id AND mr.confirmed
                   )
                   ORDER BY starts_at ASC LIMIT 40""",
                (ACADEMY_ID,child.get("team_id")),
            ) if child.get("team_id") else []
            for r in rows:
                with st.container(border=True):
                    st.markdown(
                        f"<div class='schedule-line' style='color:#ff2020!important;font-weight:900!important;font-size:1.12rem!important;text-shadow:0 0 2px #000,0 0 8px rgba(255,0,0,.50);'>📅 {r['starts_at']:%d.%m.%Y} · 🕒 {r['starts_at']:%H:%M}</div>",
                        unsafe_allow_html=True,
                    )
                    if r.get("description"): st.info(f"ℹ️ {r['description']}")
                    attachments=fetchall(
                        "SELECT * FROM event_attachments WHERE academy_id=%s AND event_id=%s ORDER BY created_at DESC",
                        (ACADEMY_ID,r["id"]),
                    )
                    for attachment in attachments:
                        mime=(attachment.get("file_mime") or "").lower()
                        if mime.startswith("image/"):
                            st.image(
                                bytes(attachment["file_data"]),
                                caption=attachment["file_name"],
                                use_container_width=True,
                            )
                        else:
                            st.download_button(
                                f"📎 {attachment['file_name']}",
                                data=bytes(attachment["file_data"]),
                                file_name=attachment["file_name"],
                                mime=attachment.get("file_mime") or "application/octet-stream",
                                key=f"parent_match_attachment_{r['id']}_{attachment['id']}",
                                use_container_width=False,
                            )
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
            if child.get("team_id"):
                parent_team=fetchone("SELECT * FROM teams WHERE academy_id=%s AND id=%s",(ACADEMY_ID,child["team_id"]))
                if parent_team: render_results_dashboard(parent_team,editable=False,key_prefix=f"parent_results_{child['id']}")
                else: st.info("Няма данни за набора.")
            else:
                st.info("Детето още не е свързано с набор.")
    elif page=="Съобщения":
        rows=fetchall("SELECT * FROM team_messages WHERE academy_id=%s AND team_id=%s ORDER BY created_at DESC",(ACADEMY_ID,child.get("team_id"))) if child.get("team_id") else []
        for r in rows:
            with st.container(border=True): st.subheader(r["title"]); st.write(r["body"]); st.caption(r["created_at"].strftime("%d.%m.%Y %H:%M"))
        if not rows: st.info("Няма съобщения.")
    elif page=="Такси":
        finance_view=st.radio("Такси и Поръчки",["💰 Месечни такси","🛍️ Екипировка / Аксесоари"],horizontal=True,key=f"parent_finance_view_{child['id']}",label_visibility="collapsed")
        if finance_view=="🛍️ Екипировка / Аксесоари":
            product_catalog_parent(user,child)
            return
        rows=fetchall("SELECT * FROM player_fees WHERE academy_id=%s AND player_id=%s ORDER BY fee_year DESC,fee_month DESC",(ACADEMY_ID,child["id"]))
        for r in rows:
            paid=r["status"]=="paid"
            pending=r.get("confirmation_status")=="pending"
            icon="🟢" if paid else "🟠" if pending else "🔴"
            label="ПЛАТЕНА" if paid else "ЧАКА ПОТВЪРЖДЕНИЕ" if pending else "ДЪЛЖИМА"
            with st.expander(f"{icon} {r['fee_month']:02d}.{r['fee_year']} · {r['final_amount']:.2f} € · {label}"):
                st.write(f"**{icon} {label}**")
                if paid:
                    st.success(f"Платено · {payment_method_label(r.get('payment_method'))}")
                elif pending:
                    st.warning(f"Заявен метод: {payment_method_label(r.get('payment_method'))}. Очаква потвърждение.")
                    if r.get("payment_reported_date"): st.caption(f"Посочена дата: {r['payment_reported_date']:%d.%m.%Y}")
                else:
                    method=st.radio(
                        "Начин на плащане",["cash","bank","card"],
                        format_func=lambda x:{"cash":"💶 В брой","bank":"🏦 По банка","card":"💳 С карта"}[x],
                        horizontal=True,key=f"choice_{r['id']}",
                    )
                    if method=="cash":
                        st.info("💶 Изборът за плащане в брой ще бъде отразен от треньора или Admin след получаване на сумата.")
                    elif method=="bank":
                        st.info("""🏦 **Банкова сметка**

**IBAN:** BG26UBBS80021058607350  
**Получател:** ФК ДЮЛГЕРОВ АКАДЕМИ  
**Основание:** Име на детето и период на плащане""")
                        payment_date=st.date_input("Дата на банковия превод",date.today(),key=f"reported_date_{r['id']}_bank")
                        if st.button("📤 Изпрати банковия превод за потвърждение",key=f"report_payment_{r['id']}_bank",use_container_width=True):
                            execute("""UPDATE player_fees SET payment_method='bank',payment_reported_date=%s,
                                       bank_transfer_date=%s,payment_note=%s,confirmation_status='pending',
                                       parent_reported_at=NOW() WHERE id=%s AND academy_id=%s AND status<>'paid'""",
                                    (payment_date,payment_date,"Заявено от родител",r["id"],ACADEMY_ID))
                            notify_admins("bank_payment","Заявен банков превод",f"{child['first_name']} {child['last_name']} · {child.get('team_name') or 'Без набор'} · {payment_date:%d.%m.%Y}")
                            set_flash("🟠 Банковият превод е изпратен и чака потвърждение от Admin.","warning");st.rerun()
                    else:
                        st.info("🔒 Плащането с карта предстои да бъде активирано.")
                        st.button("💳 Предстои",key=f"card_coming_{r['id']}",use_container_width=True,disabled=True)
        if not rows: st.info("Няма начислени такси.")
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
        coach_users = fetchall(
            """SELECT c.user_id,c.display_name FROM coach_teams ct JOIN coaches c ON c.id=ct.coach_id
               JOIN users u ON u.id=c.user_id AND u.active
               WHERE ct.academy_id=%s AND ct.team_id=%s AND c.active ORDER BY c.display_name""",
            (ACADEMY_ID,child.get("team_id")),
        ) if child.get("team_id") else []
        admin_users=fetchall(
            "SELECT id user_id FROM users WHERE academy_id=%s AND role='admin' AND active AND account_status='approved' ORDER BY id LIMIT 1",
            (ACADEMY_ID,)
        )
        
        recipients=list(coach_users)
        
        if admin_users:
            recipients.append({
                "user_id": admin_users[0]["user_id"],
                "display_name": "Към Администрация"
            })
        coach_user = None
        if recipients:
            coach_map={c["display_name"]:c for c in recipients}
            coach_user=coach_map[st.selectbox("Избери получател",list(coach_map),key=f"feedback_coach_{child['id']}")]
        if not coach_user or not coach_user.get("user_id"):
            st.warning("Няма активен получател за обратната връзка.")
        else:
            with st.form(f"feedback_{child['id']}"):
                feedback_type = st.selectbox("Тип", ["signal", "rating", "suggestion", "absence"], format_func=lambda x:{"signal":"Сигнал","rating":"Оценка","suggestion":"Предложение","absence":"Отсъствие"}[x])
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
                    set_flash("✅ Обратната връзка е изпратена само към треньора."); st.rerun()
    else:
        st.subheader("👤 Настройки")
        st.markdown("### 🔑 Смяна на паролата")
        st.caption("Ако Admin Ви е дал временна парола, влезте с нея и задайте тук нова лична парола.")
        with st.form("parent_change_password", clear_on_submit=True):
            current_password = st.text_input("Текуща / временна парола", type="password")
            new_password = st.text_input("Нова парола", type="password", help="Минимум 8 символа")
            repeat_new_password = st.text_input("Повтори новата парола", type="password")
            change_password = st.form_submit_button("🔑 Смени паролата", use_container_width=True, type="primary")
        if change_password:
            fresh_user = fetchone(
                "SELECT id,password_hash FROM users WHERE id=%s AND academy_id=%s AND role='parent' AND active",
                (user["id"], ACADEMY_ID),
            )
            if not fresh_user or fresh_user["password_hash"] != hash_password(current_password):
                st.error("Текущата / временната парола е неправилна.")
            elif len(new_password) < 8:
                st.error("Новата парола трябва да е минимум 8 символа.")
            elif new_password != repeat_new_password:
                st.error("Двете нови пароли не съвпадат.")
            elif current_password == new_password:
                st.error("Новата парола трябва да е различна от текущата.")
            else:
                current_token = st.session_state.get("login_token") or st.query_params.get("session")
                with db() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET password_hash=%s WHERE id=%s AND academy_id=%s AND role='parent'",
                        (hash_password(new_password), user["id"], ACADEMY_ID),
                    )
                    cur.execute(
                        "DELETE FROM login_sessions WHERE academy_id=%s AND user_id=%s",
                        (ACADEMY_ID, user["id"]),
                    )
                st.session_state.clear()
                st.query_params.clear()
                st.success("Паролата е сменена успешно. Влезте отново с новата парола.")
                st.rerun()
        st.divider()
        st.subheader("Добавяне на дете")
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

def multi_date_event_form(form_key, teams, user_id, fixed_team_id=None, event_mode=None):
    tmap = {t["name"]: t["id"] for t in teams}
    typ = event_mode
    if typ is None:
        typ = st.radio(
            "Какво добавяш?",
            ["training", "match"],
            format_func=lambda x: "🏃 Тренировъчен график" if x == "training" else "⚽ Създаване МАЧ",
            horizontal=True,
            key=f"{form_key}_type",
        )

    if typ == "training":
        st.success("🏃 Създаване на повтарящ се тренировъчен график")
        if fixed_team_id is None:
            team_name = st.selectbox("Набор", list(tmap), key=f"{form_key}_training_team")
            team_id = tmap[team_name]
        else:
            team_id = fixed_team_id

        team_players=fetchall("SELECT id,first_name,last_name FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' AND active ORDER BY first_name,last_name",(ACADEMY_ID,team_id))
        all_player_ids=[p["id"] for p in team_players]
        player_name_by_id={p["id"]:f"{p['first_name']} {p['last_name']}" for p in team_players}
        all_children=st.checkbox("✅ Графикът е за всички деца от набора",True,key=f"{form_key}_training_all_children")
        if all_children:
            st.success(f"Стандартен график: един час за избран ден и всички {len(team_players)} деца от набора.")
        else:
            st.info("Разделен график: за всеки ден и всеки отделен час избери децата. Всички са маркирани по подразбиране и отмаркираш само тези, които няма да идват.")
        title = st.text_input(
            "Заглавие",
            value="Тренировка",
            key=f"{form_key}_training_title",
        )
        training_location_options = [
            'Футболно игрище "Дюлгеров Академи" - База на Андонов',
            '5. СУ „Георги Измирлиев“ - ул. „Христо Чернопеев“ № 16',
            'Спортна зала на „Американски университет“',
            '✍️ Друго място - въведи ръчно',
        ]
        selected_training_location = st.selectbox(
            "Място",
            training_location_options,
            key=f"{form_key}_training_location_choice",
        )
        if selected_training_location == '✍️ Друго място - въведи ръчно':
            location = st.text_input(
                "Въведи място",
                key=f"{form_key}_training_location_custom",
                placeholder="Напр. друго игрище, зала или адрес",
            )
        else:
            location = selected_training_location
        description = st.text_area("Допълнителна информация", key=f"{form_key}_training_description")

        st.markdown("### 1. Избери тренировъчните дни")
        weekday_options = {
            "Понеделник": 0,
            "Вторник": 1,
            "Сряда": 2,
            "Четвъртък": 3,
            "Петък": 4,
            "Събота": 5,
            "Неделя": 6,
        }
        selected_days = st.multiselect(
            "Дни от седмицата",
            list(weekday_options),
            placeholder="Избери един или повече дни",
            key=f"{form_key}_weekdays",
        )

        st.markdown("### 2. Задай стандартен час за всеки избран ден" if all_children else "### 2. Задай часове и деца за всеки избран ден")
        day_slots = {}
        if selected_days:
            for day_name in selected_days:
                day_number=weekday_options[day_name]
                st.markdown(f"#### {day_name}")
                if all_children:
                    add_second_time=False
                    st.caption("Стандартен график: един час за всички деца от набора.")
                else:
                    add_second_time=st.checkbox(
                        f"➕ Добави втори час за {day_name}",
                        key=f"{form_key}_second_time_{day_number}",
                        help="Включи само ако в същия ден има втора група в различен час.",
                    )
                slot_count=2 if add_second_time else 1
                day_slots[day_name]=[]
                slot_columns=st.columns(slot_count)
                for slot_index in range(slot_count):
                    with slot_columns[slot_index]:
                        slot_label=("Час" if all_children else ("Първи час" if slot_index==0 else "Втори час"))
                        default_time="18:30" if slot_index==0 else "19:30"
                        event_time=st.time_input(
                            slot_label,
                            value=datetime.strptime(default_time,"%H:%M").time(),
                            key=f"{form_key}_time_{day_number}_{slot_index}",
                        )
                        if all_children:
                            slot_player_ids=list(all_player_ids)
                        else:
                            slot_player_ids=st.multiselect(
                                f"Деца за {day_name}, {event_time:%H:%M}",
                                all_player_ids,
                                default=all_player_ids,
                                format_func=lambda pid:player_name_by_id[pid],
                                key=f"{form_key}_players_{day_number}_{slot_index}",
                            )
                            if not slot_player_ids:
                                st.warning("За този час няма избрано дете и тренировката няма да бъде създадена.")
                        day_slots[day_name].append({"time":event_time,"player_ids":slot_player_ids})
                st.divider()
        else:
            st.info("Първо избери тренировъчните дни.")

        st.markdown("### 3. Избери период")
        period_col_1, period_col_2 = st.columns(2)
        start_date = period_col_1.date_input(
            "От дата",
            value=date.today(),
            key=f"{form_key}_period_start",
        )
        end_date = period_col_2.date_input(
            "До дата",
            value=date.today() + timedelta(days=30),
            key=f"{form_key}_period_end",
        )

        schedule = []
        if selected_days and end_date >= start_date:
            current_date = start_date
            while current_date <= end_date:
                for day_name in selected_days:
                    if current_date.weekday() == weekday_options[day_name]:
                        for slot in day_slots.get(day_name,[]):
                            if slot["player_ids"]:
                                schedule.append((current_date,slot["time"],day_name,slot["player_ids"]))
                current_date += timedelta(days=1)
            schedule.sort(key=lambda item:(item[0],item[1]))

        st.markdown("### 4. Преглед на графика")
        if end_date < start_date:
            st.error("Крайната дата не може да бъде преди началната дата.")
        elif selected_days and not schedule:
            st.warning("В избрания период няма дати за посочените дни.")
        elif schedule:
            st.info(f"Ще бъдат създадени {len(schedule)} тренировки.")
            with st.expander("Покажи всички дати", expanded=len(schedule) <= 10):
                for event_date, event_time, day_name, slot_player_ids in schedule:
                    preview_dt=datetime.combine(event_date,event_time)
                    st.markdown("✅ "+training_datetime_html(preview_dt),unsafe_allow_html=True)
                    if not all_children:
                        st.caption(f"Избрани деца: {len(slot_player_ids)}")

        if st.button(
            f"✅ Потвърди и създай {len(schedule)} тренировки" if schedule else "✅ Създай тренировъчния график",
            key=f"{form_key}_save_training_schedule",
            use_container_width=True,
            type="primary",
            disabled=not schedule,
        ):
            if not title.strip():
                st.error("Въведи заглавие.")
            elif not selected_days:
                st.error("Избери поне един тренировъчен ден.")
            elif end_date < start_date:
                st.error("Провери периода.")
            else:
                with db() as conn,conn.cursor() as cur:
                    if all_children:cur.execute("DELETE FROM events WHERE academy_id=%s AND team_id=%s AND event_type='training'",(ACADEMY_ID,team_id))
                    else:
                        cur.execute("DELETE FROM events WHERE academy_id=%s AND team_id=%s AND event_type='training' AND starts_at::date BETWEEN %s AND %s",(ACADEMY_ID,team_id,start_date,end_date))
                    notified_player_ids=set()
                    for event_date,event_time,_,slot_player_ids in schedule:
                        target_all=all_children and set(slot_player_ids)==set(all_player_ids)
                        cur.execute("""INSERT INTO events(academy_id,team_id,event_type,title,starts_at,location,description,created_by,target_all_players) VALUES(%s,%s,'training',%s,%s,%s,%s,%s,%s) RETURNING id""",(ACADEMY_ID,team_id,title.strip(),datetime.combine(event_date,event_time),location.strip() or None,description.strip() or None,user_id,target_all));event_id=cur.fetchone()[0]
                        if not target_all:
                            for pid in slot_player_ids:
                                cur.execute("INSERT INTO event_players(academy_id,event_id,player_id) VALUES(%s,%s,%s)",(ACADEMY_ID,event_id,pid))
                                notified_player_ids.add(pid)
                notice=f"{len(schedule)} тренировки за {start_date:%d.%m.%Y} - {end_date:%d.%m.%Y}."
                notified=notify_team_parents(team_id,"training_schedule","Нов тренировъчен график",notice) if all_children else notify_selected_players_parents(list(notified_player_ids),"training_schedule","График по групи и часове",notice)
                set_flash(f"✅ Графикът е публикуван. Уведомени родители: {notified}.");st.rerun()

    else:
        st.info("⚽ Създаване МАЧ")
        with st.form(f"{form_key}_single_match"):
            if fixed_team_id is None:
                team_name = st.selectbox("Набор", list(tmap), key=f"{form_key}_match_team")
                team_id = tmap[team_name]
            else:
                team_id = fixed_team_id

            opponent = st.text_input(
                "Противник",
                placeholder="Въведи име на отбора",
                key=f"{form_key}_match_opponent",
            )
            event_date = st.date_input("Дата", date.today(), key=f"{form_key}_match_date")
            event_time = st.time_input("Час", key=f"{form_key}_match_time")
            location = st.text_input("Място", key=f"{form_key}_match_location")
            description = st.text_area("Допълнителна информация", key=f"{form_key}_match_description")
            st.caption("🖼️ Плакатът се генерира автоматично от шаблона Designer.jpg с името на противника.")
            save_match = st.form_submit_button("⚽ Създай мача", use_container_width=True, type="primary")

        if save_match:
            if not opponent.strip():
                st.error("Въведи името на противника.")
            else:
                created_match = execute(
                    """INSERT INTO events(
                           academy_id,team_id,event_type,title,opponent,starts_at,
                           location,description,created_by)
                       VALUES(%s,%s,'match',%s,%s,%s,%s,%s,%s)
                       RETURNING id""",
                    (
                        ACADEMY_ID,
                        team_id,
                        f"Мач срещу {opponent.strip()}",
                        opponent.strip(),
                        datetime.combine(event_date, event_time),
                        location.strip() or None,
                        description.strip() or None,
                        user_id,
                    ),
                    True,
                )
                try:
                    poster_bytes=generate_match_poster(opponent)
                    execute(
                        """INSERT INTO event_attachments(
                               academy_id,event_id,file_name,file_mime,file_data,uploaded_by)
                           VALUES(%s,%s,%s,%s,%s,%s)""",
                        (ACADEMY_ID,created_match["id"],f"Мач срещу {opponent.strip()}.jpg","image/jpeg",poster_bytes,user_id),
                    )
                except Exception as exc:
                    execute("DELETE FROM events WHERE academy_id=%s AND id=%s",(ACADEMY_ID,created_match["id"]))
                    st.error(f"Мачът не е създаден, защото плакатът не може да бъде генериран: {exc}")
                    st.stop()
                notified_parents = notify_team_parents(
                    team_id,
                    "match",
                    "⚽ Публикуван е нов мач",
                    f"Мач срещу {opponent.strip()} · {event_date:%d.%m.%Y} {event_time:%H:%M} · {location.strip() or 'Без посочено място'}",
                )
                if notified_parents:
                    set_flash(f"✅ Мачът е създаден. Известие е изпратено до {notified_parents} родителски профила.")
                else:
                    set_flash("⚠️ Мачът е създаден, но няма активни одобрени родители, свързани с този набор.", "warning")
                st.rerun()

def coach_account_manager():
    coaches=fetchall("""SELECT c.*,u.email,u.account_status,u.active user_active FROM coaches c LEFT JOIN users u ON u.id=c.user_id WHERE c.academy_id=%s AND c.active ORDER BY c.id""",(ACADEMY_ID,))
    st.subheader("👤 Управление на треньор")
    if not coaches: st.info("Няма активни треньори."); return
    labels={f"{c['display_name']} · {c.get('email') or 'без достъп'}":c for c in coaches}
    coach=labels[st.selectbox("Избери треньор",list(labels),key="admin_selected_coach")]
    with st.container(border=True):
        a,b=st.columns([1,5])
        with a: st.markdown(avatar_html(coach.get("profile_image"),coach.get("profile_image_mime"),82),unsafe_allow_html=True)
        with b:
            image=st.file_uploader("Снимка на треньора",type=["jpg","jpeg","png","webp"],key=f"coach_image_{coach['id']}")
            if st.button("Запази снимката",key=f"save_coach_image_{coach['id']}"):
                ib,im=valid_profile_image(image)
                if ib: execute("UPDATE coaches SET profile_image=%s,profile_image_mime=%s WHERE id=%s AND academy_id=%s",(ib,im,coach["id"],ACADEMY_ID));set_flash("✅ Снимката е запазена.");st.rerun()
        name=st.text_input("Име на треньора",coach["display_name"],key=f"coach_name_{coach['id']}")
        coach_birth_date=st.date_input("🎂 Дата на раждане",value=coach.get("birth_date") or date(1990,1,1),min_value=date(1940,1,1),max_value=date.today(),key=f"coach_birth_date_{coach['id']}")
        email=st.text_input("Имейл за вход",coach.get("email") or "",key=f"coach_email_{coach['id']}").strip().lower()
        password=st.text_input("Нова / начална парола",type="password",key=f"coach_pw_{coach['id']}")
        c1,c2=st.columns(2)
        if c1.button("Запази име",key=f"coach_save_name_{coach['id']}",use_container_width=True): execute("UPDATE coaches SET display_name=%s,birth_date=%s WHERE id=%s AND academy_id=%s",(name.strip(),coach_birth_date,coach["id"],ACADEMY_ID));set_flash("✅ Името е запазено.");st.rerun()
        if c2.button("Създай / обнови достъп",key=f"coach_access_{coach['id']}",use_container_width=True):
            if not email or len(password)<8: st.error("Въведи валиден имейл и парола с минимум 8 символа.")
            else:
                existing=fetchone("SELECT id FROM users WHERE academy_id=%s AND lower(email)=lower(%s)",(ACADEMY_ID,email))
                if existing:
                    uid=existing["id"];execute("UPDATE users SET first_name=%s,last_name='Треньор',password_hash=%s,role='coach',account_status='approved',active=TRUE WHERE id=%s AND academy_id=%s",(name.strip(),hash_password(password),uid,ACADEMY_ID))
                else:
                    uid=execute("INSERT INTO users(academy_id,first_name,last_name,email,password_hash,role,account_status) VALUES(%s,%s,'Треньор',%s,%s,'coach','approved') RETURNING id",(ACADEMY_ID,name.strip(),email,hash_password(password)),True)["id"]
                execute("UPDATE coaches SET user_id=%s,display_name=%s,birth_date=%s WHERE id=%s AND academy_id=%s",(uid,name.strip(),coach_birth_date,coach["id"],ACADEMY_ID));set_flash("✅ Треньорският достъп е готов.");st.rerun()

SPECIALIST_TYPE_LABELS={
    "conditioning":"Кондиционен треньор",
    "goalkeeper":"Треньор на вратарите",
    "medical":"Медицинско лице",
    "psychologist":"Психолог",
    "other":"Друг специалист",
}
def specialist_role_label(value):
    return SPECIALIST_TYPE_LABELS.get(value,value or "Специалист")
def specialist_teams(user_id):
    return fetchall("""SELECT t.* FROM specialist_teams st
        JOIN teams t ON t.id=st.team_id
        WHERE st.academy_id=%s AND st.specialist_user_id=%s AND t.active
        ORDER BY t.birth_year,t.name""",(ACADEMY_ID,user_id))
def specialist_statistics_excel(user_id,team_ids):
    if not team_ids:return b""
    rows=fetchall("""SELECT spn.observation_date,t.name team_name,p.first_name,p.last_name,
        spn.technique,spn.teamwork,spn.effort,spn.discipline,spn.note,
        sp.display_name,specialist_type
        FROM specialist_player_notes spn
        JOIN players p ON p.id=spn.player_id
        JOIN teams t ON t.id=spn.team_id
        JOIN specialist_profiles sp ON sp.user_id=spn.specialist_user_id
        WHERE spn.academy_id=%s AND spn.specialist_user_id=%s AND spn.team_id=ANY(%s)
        ORDER BY p.last_name,p.first_name,spn.observation_date DESC""",(ACADEMY_ID,user_id,team_ids))
    data=[]
    for r in rows:
        data.append({"Дата":r["observation_date"],"Набор":r["team_name"],
            "Дете":f"{r['first_name']} {r['last_name']}","Техника":r["technique"],
            "Работа в екип":r["teamwork"],"Старание":r["effort"],"Дисциплина":r["discipline"],
            "Средна стойност":round((r["technique"]+r["teamwork"]+r["effort"]+r["discipline"])/4,2),
            "Коментар":r.get("note") or "","Специалист":r["display_name"],
            "Профил":specialist_role_label(r["specialist_type"])})
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine="openpyxl") as writer:
        pd.DataFrame(data).to_excel(writer,index=False,sheet_name="Вътрешна статистика")
        ws=writer.book["Вътрешна статистика"];ws.freeze_panes="A2";ws.auto_filter.ref=ws.dimensions
        for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=min(max(len(str(c.value or "")) for c in col)+2,45)
    return out.getvalue()
def specialist_statistics_panel(user,team):
    st.subheader("📊 Вътрешна статистика и наблюдения")
    st.info("Тези записи са вътрешни за кондиционния/поддържащия екип. Не се показват в родителския профил и не участват в треньорската месечна оценка.")
    players=fetchall("""SELECT * FROM players WHERE academy_id=%s AND team_id=%s
        AND approval_status='approved' AND active ORDER BY first_name,last_name""",(ACADEMY_ID,team["id"]))
    if not players:st.info("Няма потвърдени деца в този набор.");return
    pmap={f"{p['first_name']} {p['last_name']}":p for p in players}
    player=pmap[st.selectbox("Дете",list(pmap),key=f"specialist_player_{team['id']}")]
    observations=fetchall("""SELECT * FROM specialist_player_notes WHERE academy_id=%s
        AND specialist_user_id=%s AND player_id=%s ORDER BY observation_date DESC,created_at DESC""",
        (ACADEMY_ID,user["id"],player["id"]))
    with st.form(f"specialist_note_{team['id']}_{player['id']}",clear_on_submit=True):
        obs_date=st.date_input("Дата на наблюдението",date.today())
        st.caption("Оценяването е от 1 до 5 и служи само за вътрешно проследяване.")
        c1,c2=st.columns(2)
        technique=c1.select_slider("Техника",options=[1,2,3,4,5],value=3,format_func=lambda x:f"{x} ★")
        teamwork=c2.select_slider("Работа в екип",options=[1,2,3,4,5],value=3,format_func=lambda x:f"{x} ★")
        effort=c1.select_slider("Старание",options=[1,2,3,4,5],value=3,format_func=lambda x:f"{x} ★")
        discipline=c2.select_slider("Дисциплина",options=[1,2,3,4,5],value=3,format_func=lambda x:f"{x} ★")
        note=st.text_area("Бележка / професионално наблюдение",placeholder="Кратък обективен коментар за конкретното наблюдение.")
        save=st.form_submit_button("💾 Запази във вътрешната история",use_container_width=True,type="primary")
    if save:
        execute("""INSERT INTO specialist_player_notes(academy_id,specialist_user_id,player_id,team_id,observation_date,technique,teamwork,effort,discipline,note)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(specialist_user_id,player_id,observation_date) DO UPDATE SET
            team_id=EXCLUDED.team_id,technique=EXCLUDED.technique,teamwork=EXCLUDED.teamwork,
            effort=EXCLUDED.effort,discipline=EXCLUDED.discipline,note=EXCLUDED.note,updated_at=NOW()""",
            (ACADEMY_ID,user["id"],player["id"],team["id"],obs_date,technique,teamwork,effort,discipline,note.strip() or None))
        set_flash("✅ Наблюдението е запазено само във вътрешната история на специалиста.");st.rerun()
    st.markdown(f"### История · {player['first_name']} {player['last_name']}")
    if not observations:st.info("Все още няма записани наблюдения за това дете.")
    for row in observations:
        avg=(row["technique"]+row["teamwork"]+row["effort"]+row["discipline"])/4
        with st.expander(f"{row['observation_date']:%d.%m.%Y} · Средно {avg:.1f}/5"):
            a,b,c,d=st.columns(4);a.metric("Техника",row["technique"]);b.metric("Екип",row["teamwork"]);c.metric("Старание",row["effort"]);d.metric("Дисциплина",row["discipline"])
            if row.get("note"):st.write(f"📝 {row['note']}")
            if st.button("🗑️ Изтрий този запис",key=f"del_specialist_note_{row['id']}"):
                execute("DELETE FROM specialist_player_notes WHERE id=%s AND academy_id=%s AND specialist_user_id=%s",(row["id"],ACADEMY_ID,user["id"]));st.rerun()
    all_team_ids=[t["id"] for t in specialist_teams(user["id"])]
    export_data=specialist_statistics_excel(user["id"],all_team_ids)
    if export_data:st.download_button("📥 Excel: статистика, дати и коментари",export_data,"specialist_player_statistics.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",use_container_width=True)
BG_DAY_INITIALS = {0:"П",1:"В",2:"С",3:"Ч",4:"П",5:"С",6:"Н"}
def training_datetime_html(dt,large=False):
    size = "2rem" if large else "1rem"
    return (f'<div style="font-size:{size};font-weight:900;line-height:1.25">'
            f'<span style="color:#39d98a!important">{BG_DAY_INITIALS[dt.weekday()]} · {dt:%d.%m.%Y}</span>'
            f'<span style="color:#ffffff!important"> · {dt:%H:%M}</span></div>')
def training_date_html(dt):
    return f'<span style="color:#39d98a!important;font-weight:900">{BG_DAY_INITIALS[dt.weekday()]} · {dt:%d.%m}</span>'

def compact_team_training_view(team,key_prefix="specialist_training"):
    rows=fetchall("""SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND event_type='training'
        AND status<>'cancelled' ORDER BY starts_at ASC LIMIT 100""",(ACADEMY_ID,team["id"]))
    future=[r for r in rows if r["starts_at"]>=datetime.now(r["starts_at"].tzinfo)]
    if not future:
        st.info("Няма предстоящи тренировки за избрания набор.")
        return
    nxt=future[0]
    st.markdown("### 🟢 Следваща тренировка")
    with st.container(border=True):
        st.markdown(training_datetime_html(nxt['starts_at'],large=True),unsafe_allow_html=True)
        st.write(f"📍 {nxt.get('location') or 'Без посочено място'}")
    notes=[]
    for row in future:
        note=(row.get("description") or "").strip()
        if note and note not in notes:notes.append(note)
    if notes:st.info("ℹ️ "+notes[0])
    weeks={}
    for row in future:
        ws=(row["starts_at"]-timedelta(days=row["starts_at"].weekday())).date()
        weeks.setdefault(ws,[]).append(row)
    current_week=date.today()-timedelta(days=date.today().weekday())
    for ws,items in weeks.items():
        with st.expander(f"📅 {ws:%d.%m} – {(ws+timedelta(days=6)):%d.%m} · {len(items)} тренировки",expanded=ws==current_week):
            for row in items:
                a,b,c=st.columns([2,1,4])
                a.markdown(training_date_html(row['starts_at']),unsafe_allow_html=True)
                b.write(f"**{row['starts_at']:%H:%M}**")
                c.write(f"📍 {row.get('location') or 'Без място'}")

def specialist_panel(user):
    profile=fetchone("SELECT * FROM specialist_profiles WHERE academy_id=%s AND user_id=%s AND active",(ACADEMY_ID,user["id"]))
    st.title("Кондиционен и поддържащ екип")
    show_flash()
    if not profile:st.warning("Профилът не е настроен. Admin трябва да определи вид специалист и набори.");return
    teams=specialist_teams(user["id"])
    st.sidebar.markdown(avatar_html(profile.get("profile_image"),profile.get("profile_image_mime"),72),unsafe_allow_html=True)
    st.sidebar.caption(f"{profile['display_name']} · {specialist_role_label(profile['specialist_type'])}")
    sections=["Табло","Тренировки","Мачове","Съобщения","Информация","Статистика","🏆 Резултати","🛍️ Екипировка / Аксесоари","💬 Екипен чат","📇 Деца и контакти"]
    sec=st.sidebar.radio("Меню",sections,key="specialist_menu")
    if sec=="💬 Екипен чат":
        staff_team_chat(user)
        return
    if not teams:st.info("Все още няма зачислени набори към този профил.");return
    tmap={t["name"]:t for t in teams};team=tmap[st.selectbox("Набор",list(tmap),key="specialist_team")]
    if sec=="Табло":
        render_all_academy_birthday_banners()
        players=fetchone("SELECT COUNT(*) c FROM players WHERE academy_id=%s AND team_id=%s AND active AND approval_status='approved'",(ACADEMY_ID,team["id"]))["c"]
        st.metric("Деца в набора",players)
        st.caption("Профилът има достъп само до наборите, които са зададени от Admin.")
        unread=unread_notification_count(user["id"])
        if unread:notification_center(user)
    elif sec=="📇 Деца и контакти":
        st.subheader("📇 Информация и контакти за децата")
        render_child_contact_cards(team["id"])
    elif sec=="Тренировки":
        compact_team_training_view(team,key_prefix=f"specialist_training_{team['id']}")
    elif sec=="Мачове":
        rows=fetchall("SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND event_type='match' AND status<>'cancelled' ORDER BY starts_at DESC LIMIT 100",(ACADEMY_ID,team["id"]))
        for r in rows:
            with st.container(border=True):
                st.write(f"**{r['title']} · {r['starts_at']:%d.%m.%Y %H:%M}**")
                if r.get("opponent"):st.write(f"Противник: {r['opponent']}")
                st.caption(r.get("location") or "Без посочено място")
                if r.get("description"):st.info(r["description"])
        if not rows:st.info("Няма добавени мачове за избрания набор.")
    elif sec=="🏆 Резултати":
        st.caption("Избери набор от падащото меню по-горе, за да видиш неговите резултати.")
        render_results_dashboard(team,editable=False,key_prefix=f"specialist_results_{user['id']}_{team['id']}")
    elif sec=="🛍️ Екипировка / Аксесоари":
        product_catalog_parent(user,None)
    elif sec=="Съобщения":
        rows=fetchall("""SELECT tm.*,u.first_name,u.last_name FROM team_messages tm
            LEFT JOIN users u ON u.id=tm.sender_user_id
            WHERE tm.academy_id=%s AND tm.team_id=%s ORDER BY tm.created_at DESC""",(ACADEMY_ID,team["id"]))
        direct=fetchall("""SELECT * FROM specialist_messages WHERE academy_id=%s AND specialist_user_id=%s ORDER BY created_at DESC""",(ACADEMY_ID,user["id"]))
        st.subheader("Съобщения от Admin")
        for m in direct:
            with st.container(border=True):st.write(f"**{m['subject']}**");st.write(m['body']);st.caption(m['created_at'].strftime("%d.%m.%Y %H:%M"))
        if not direct:st.info("Няма директни съобщения от Admin.")
        execute("UPDATE specialist_messages SET is_read=TRUE WHERE academy_id=%s AND specialist_user_id=%s",(ACADEMY_ID,user["id"]))
        st.subheader("Съобщения за набора от Admin и треньори")
        for m in rows:
            with st.container(border=True):st.write(f"**{m['title']}**");st.write(m['body']);st.caption(f"{m.get('first_name') or 'Admin'} {m.get('last_name') or ''} · {m['created_at']:%d.%m.%Y %H:%M}")
        if not rows:st.info("Няма съобщения за този набор.")
    elif sec=="Информация":parent_information(None)
    else:specialist_statistics_panel(user,team)
def admin_specialist_manager(admin_user):
    st.subheader("🏃 Кондиционен и поддържащ екип")
    orphan_specialists=fetchall("""SELECT u.* FROM users u LEFT JOIN specialist_profiles sp ON sp.user_id=u.id
        WHERE u.academy_id=%s AND u.role='specialist' AND sp.id IS NULL ORDER BY u.first_name,u.last_name""",(ACADEMY_ID,))
    if orphan_specialists:
        st.warning("Има Specialist профил(и), създадени по старата логика без настройки. Поправи ги оттук.")
        for orphan in orphan_specialists:
            with st.container(border=True):
                st.write(f"**{orphan['first_name']} {orphan['last_name']}** · {orphan['email']}")
                repair_type=st.selectbox("Вид специалист",list(SPECIALIST_TYPE_LABELS),format_func=specialist_role_label,key=f"repair_specialist_type_{orphan['id']}")
                if st.button("🔧 Създай липсващия Specialist профил",key=f"repair_specialist_{orphan['id']}",use_container_width=True):
                    execute("""INSERT INTO specialist_profiles(academy_id,user_id,display_name,specialist_type,active)
                        VALUES(%s,%s,%s,%s,TRUE) ON CONFLICT(user_id) DO NOTHING""",
                        (ACADEMY_ID,orphan['id'],f"{orphan['first_name']} {orphan['last_name']}",repair_type))
                    set_flash("✅ Липсващият Specialist профил е създаден. Сега зачисли необходимите набори.");st.rerun()
    st.caption("Създай профил за кондиционен треньор, медицинско лице, психолог или друг специалист и му зачисли един или повече набори.")
    with st.form("new_specialist_account",clear_on_submit=True):
        c1,c2=st.columns(2);name=c1.text_input("Име и фамилия");email=c2.text_input("Имейл за вход").strip().lower()
        typ=c1.selectbox("Вид профил",list(SPECIALIST_TYPE_LABELS),format_func=specialist_role_label)
        qualification=c2.text_input("Квалификация / длъжност",placeholder="Напр. кондиционен треньор, физиотерапевт")
        specialist_birth_date=st.date_input("🎂 Дата на раждане",value=date(1990,1,1),min_value=date(1940,1,1),max_value=date.today(),key="new_specialist_birth_date")
        password=st.text_input("Начална парола",type="password")
        create=st.form_submit_button("Създай профила",use_container_width=True)
    if create:
        if not name.strip() or not email or len(password)<8:st.error("Въведи име, имейл и парола с минимум 8 символа.")
        else:
            parts=name.strip().split(maxsplit=1);first=parts[0];last=parts[1] if len(parts)>1 else "Специалист"
            existing=fetchone("SELECT id FROM users WHERE academy_id=%s AND lower(email)=lower(%s)",(ACADEMY_ID,email))
            if existing:uid=existing["id"];execute("UPDATE users SET first_name=%s,last_name=%s,password_hash=%s,role='specialist',account_status='approved',active=TRUE WHERE id=%s AND academy_id=%s",(first,last,hash_password(password),uid,ACADEMY_ID))
            else:uid=execute("INSERT INTO users(academy_id,first_name,last_name,email,password_hash,role,account_status) VALUES(%s,%s,%s,%s,%s,'specialist','approved') RETURNING id",(ACADEMY_ID,first,last,email,hash_password(password)),True)["id"]
            execute("""INSERT INTO specialist_profiles(academy_id,user_id,display_name,specialist_type,qualification,birth_date,active)
                VALUES(%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT(user_id) DO UPDATE SET display_name=EXCLUDED.display_name,
                specialist_type=EXCLUDED.specialist_type,qualification=EXCLUDED.qualification,birth_date=EXCLUDED.birth_date,active=TRUE""",(ACADEMY_ID,uid,name.strip(),typ,qualification.strip() or None,specialist_birth_date))
            set_flash("✅ Профилът на специалиста е създаден.");st.rerun()
    profiles=fetchall("""SELECT sp.*,u.email,u.active user_active FROM specialist_profiles sp JOIN users u ON u.id=sp.user_id
        WHERE sp.academy_id=%s ORDER BY sp.active DESC,sp.display_name""",(ACADEMY_ID,))
    teams=fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year",(ACADEMY_ID,))
    for sp in profiles:
        with st.expander(f"{'🟢' if sp['active'] else '⚪'} {sp['display_name']} · {specialist_role_label(sp['specialist_type'])}"):
            photo_col,upload_col=st.columns([1,5])
            with photo_col:st.markdown(avatar_html(sp.get("profile_image"),sp.get("profile_image_mime"),82),unsafe_allow_html=True)
            with upload_col:
                specialist_image=st.file_uploader("Снимка на специалиста",type=["jpg","jpeg","png","webp"],key=f"specialist_image_{sp['id']}")
                if st.button("Запази снимката",key=f"save_specialist_image_{sp['id']}"):
                    ib,im=valid_profile_image(specialist_image)
                    if ib:execute("UPDATE specialist_profiles SET profile_image=%s,profile_image_mime=%s WHERE id=%s AND academy_id=%s",(ib,im,sp["id"],ACADEMY_ID));set_flash("✅ Снимката на специалиста е запазена.");st.rerun()
            specialist_birth_edit=st.date_input("🎂 Дата на раждане",value=sp.get("birth_date") or date(1990,1,1),min_value=date(1940,1,1),max_value=date.today(),key=f"specialist_birth_date_{sp['id']}")
            assigned={r["team_id"] for r in fetchall("SELECT team_id FROM specialist_teams WHERE academy_id=%s AND specialist_user_id=%s",(ACADEMY_ID,sp["user_id"]))}
            selected=st.multiselect("Зачислени набори",options=[t["id"] for t in teams],default=list(assigned),format_func=lambda tid:next(t["name"] for t in teams if t["id"]==tid),key=f"specialist_teams_{sp['id']}")
            active=st.checkbox("Активен профил",sp["active"] and sp["user_active"],key=f"specialist_active_{sp['id']}")
            a,b=st.columns(2)
            if a.button("💾 Запази наборите",key=f"save_specialist_{sp['id']}",use_container_width=True):
                with db() as conn,conn.cursor() as cur:
                    cur.execute("DELETE FROM specialist_teams WHERE academy_id=%s AND specialist_user_id=%s",(ACADEMY_ID,sp["user_id"]))
                    for team_id in selected:cur.execute("INSERT INTO specialist_teams(academy_id,specialist_user_id,team_id,assigned_by) VALUES(%s,%s,%s,%s)",(ACADEMY_ID,sp["user_id"],team_id,admin_user["id"]))
                    cur.execute("UPDATE specialist_profiles SET active=%s,birth_date=%s WHERE id=%s AND academy_id=%s",(active,specialist_birth_edit,sp["id"],ACADEMY_ID));cur.execute("UPDATE users SET active=%s WHERE id=%s AND academy_id=%s",(active,sp["user_id"],ACADEMY_ID))
                set_flash("✅ Наборите и достъпът са обновени.");st.rerun()
            if b.button("📨 Съобщение",key=f"toggle_specialist_message_{sp['id']}",use_container_width=True):st.session_state[f"show_specialist_message_{sp['id']}"]=not st.session_state.get(f"show_specialist_message_{sp['id']}",False);st.rerun()
            if st.session_state.get(f"show_specialist_message_{sp['id']}"):
                with st.form(f"specialist_message_form_{sp['id']}",clear_on_submit=True):
                    subject=st.text_input("Тема");body=st.text_area("Съобщение");send=st.form_submit_button("Изпрати")
                if send and subject.strip() and body.strip():
                    execute("INSERT INTO specialist_messages(academy_id,specialist_user_id,sender_user_id,subject,body) VALUES(%s,%s,%s,%s,%s)",(ACADEMY_ID,sp["user_id"],admin_user["id"],subject.strip(),body.strip()));create_notification(sp["user_id"],"specialist_message",subject.strip(),body.strip());set_flash("✅ Съобщението е изпратено.");st.rerun()

def admin_dyulgerov_panel(user):
    owner_profile=fetchone("SELECT profile_image,profile_image_mime FROM users WHERE academy_id=%s AND id=%s",(ACADEMY_ID,user["id"])) or {}
    title_col,photo_col=st.columns([5,1])
    with title_col:
        st.subheader("Дюлгеров")
        st.caption("Собственически и спортно-технически достъп през Admin профила, без отделен Coach или Specialist вход.")
    with photo_col:
        st.markdown(avatar_html(owner_profile.get("profile_image"),owner_profile.get("profile_image_mime"),92),unsafe_allow_html=True)
    teams=fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year,name",(ACADEMY_ID,))
    if not teams: st.info("Няма активни набори."); return
    tmap={t["name"]:t for t in teams}; team=tmap[st.selectbox("Набор",list(tmap),key="dyulgerov_team")]
    view=st.radio("Меню",["Табло","🏃 Тренировъчен график","📅 Създаден график","⚽ Създаване МАЧ","⚽ Група за мача","Мачове","🏆 Резултати","🛍️ Екипировка / Аксесоари","Съобщение"],horizontal=True,key="dyulgerov_view",label_visibility="collapsed")
    if view=="Табло":
        render_all_academy_birthday_banners()
        players=fetchone("SELECT COUNT(*) c FROM players WHERE academy_id=%s AND team_id=%s AND active AND approval_status='approved'",(ACADEMY_ID,team["id"]))["c"]
        upcoming=fetchone("SELECT COUNT(*) c FROM events WHERE academy_id=%s AND team_id=%s AND starts_at>=NOW() AND status<>'cancelled'",(ACADEMY_ID,team["id"]))["c"]
        messages=fetchone("SELECT COUNT(*) c FROM team_messages WHERE academy_id=%s AND team_id=%s",(ACADEMY_ID,team["id"]))["c"]
        c1,c2,c3=st.columns(3);c1.metric("Деца",players);c2.metric("Предстоящи събития",upcoming);c3.metric("Съобщения",messages)
    elif view=="🏃 Тренировъчен график":
        multi_date_event_form("dyulgerov_training_form",teams,user["id"],fixed_team_id=team["id"],event_mode="training")
    elif view=="📅 Създаден график":
        st.caption("Тук се визуализира актуално създаденият тренировъчен график за избрания набор.")
        compact_team_training_view(team,key_prefix=f"dyulgerov_created_schedule_{team['id']}")
    elif view=="⚽ Създаване МАЧ":
        multi_date_event_form("dyulgerov_match_form",teams,user["id"],fixed_team_id=team["id"],event_mode="match")
    elif view=="⚽ Група за мача":
        matches=fetchall("SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND event_type='match' AND status<>'cancelled' ORDER BY starts_at",(ACADEMY_ID,team["id"]))
        if not matches:st.info("Първо създай мач.")
        else:
            mm={f"{m['starts_at']:%d.%m.%Y %H:%M} · {m['title']}":m for m in matches};match=mm[st.selectbox("Избери мач",list(mm),key=f"dy_match_{team['id']}")]
            players=fetchall("SELECT * FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' AND active ORDER BY first_name,last_name",(ACADEMY_ID,team["id"]));existing={r['player_id']:r['squad_status'] for r in fetchall("SELECT player_id,squad_status FROM match_squads WHERE academy_id=%s AND event_id=%s",(ACADEMY_ID,match['id']))}
            with st.form(f"dy_squad_{match['id']}"):
                choices={p['id']:st.checkbox(f"{p['first_name']} {p['last_name']}",existing.get(p['id'])=='in_group',key=f"dysq_{match['id']}_{p['id']}") for p in players};save=st.form_submit_button("Изпрати групата към родителите",use_container_width=True,type="primary")
            if save:
                with db() as conn,conn.cursor() as cur:
                    cur.execute("DELETE FROM match_squads WHERE academy_id=%s AND event_id=%s",(ACADEMY_ID,match['id']))
                    for pid,val in choices.items():cur.execute("INSERT INTO match_squads(academy_id,event_id,player_id,squad_status,selected_by) VALUES(%s,%s,%s,%s,%s)",(ACADEMY_ID,match['id'],pid,'in_group' if val else 'not_in_group',user['id']))
                notify_team_parents(team['id'],'match_squad','Публикувана е група за мача',match['title']);set_flash("✅ Групата е изпратена.");st.rerun()
            selected=fetchall("SELECT p.first_name,p.last_name FROM match_squads ms JOIN players p ON p.id=ms.player_id WHERE ms.academy_id=%s AND ms.event_id=%s AND ms.squad_status='in_group' ORDER BY p.first_name,p.last_name",(ACADEMY_ID,match['id']))
            with st.expander(f"👥 Визуализация на групата · {len(selected)} футболисти",expanded=True):
                for x in selected:st.write(f"✅ {x['first_name']} {x['last_name']}")
                if not selected:st.caption("Все още няма избрани футболисти.")
    elif view=="Мачове":
        rows=fetchall("SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND event_type='match' AND status<>'cancelled' ORDER BY starts_at DESC LIMIT 100",(ACADEMY_ID,team["id"]))
        for row in rows:
            with st.container(border=True):
                st.write(f"**{row['title']} · {row['starts_at']:%d.%m.%Y %H:%M}**")
                if row.get("opponent"): st.write(f"Противник: {row['opponent']}")
                st.caption(row.get("location") or "Без посочено място")
                if row.get("description"): st.info(row["description"])
        if not rows: st.info("Няма добавени мачове.")
    elif view=="🏆 Резултати":
        render_results_dashboard(team,editable=False,key_prefix=f"dyulgerov_results_{team['id']}")
    elif view=="🛍️ Екипировка / Аксесоари":
        product_catalog_parent(user,None)
    elif view=="Съобщение":
        with st.form(f"dyulgerov_message_{team['id']}",clear_on_submit=True):
            title=st.text_input("Заглавие"); body=st.text_area("Съобщение към родителите"); send=st.form_submit_button("Изпрати към групата",use_container_width=True,type="primary")
        if send and title.strip() and body.strip():
            execute("INSERT INTO team_messages(academy_id,team_id,sender_user_id,title,body) VALUES(%s,%s,%s,%s,%s)",(ACADEMY_ID,team["id"],user["id"],title.strip(),body.strip()))
            notify_team_parents(team["id"],"message",title.strip(),body.strip()); set_flash("✅ Съобщението е изпратено."); st.rerun()
        st.divider(); st.subheader("Архив")
        for row in fetchall("SELECT * FROM team_messages WHERE academy_id=%s AND team_id=%s ORDER BY created_at DESC",(ACADEMY_ID,team["id"])):
            with st.container(border=True):
                st.write(f"**{row['title']}**"); st.write(row['body']); st.caption(row['created_at'].strftime('%d.%m.%Y %H:%M'))
                if st.button("🗑️ Изтрий",key=f"dyulgerov_delete_{row['id']}"): execute("DELETE FROM team_messages WHERE academy_id=%s AND id=%s",(ACADEMY_ID,row["id"])); st.rerun()

def admin_panel(user):
    count = unread_notification_count(user["id"])
    st.title("Администратор")
    show_flash()
    if count > 0:
        with st.expander(f"🔔 {count} нови известия", expanded=False):
            notification_center(user)
    sections = ["ТАБЛО", "РЕГИСТРАЦИЯ", "ДЕЦА И ГРУПИ", "НАСТРОЙКИ ЗА ПРОФИЛИТЕ", "ФИНАНСИ", "ИНФОРМАЦИЯ", "ДЮЛГЕРОВ", "СЪОБЩЕНИЯ", "💬 Екипен чат"]
    sec = st.sidebar.radio("Админ меню", sections)
    profile_settings_page = None
    if sec == "НАСТРОЙКИ ЗА ПРОФИЛИТЕ":
        st.subheader("⚙️ Настройки за профилите")
        profile_settings_page = st.radio(
            "Избери настройка",
            ["Треньори и набори", "Роли и права", "Управление тренировки", "Кондиционен екип"],
            horizontal=True,
            key="admin_profile_settings_page",
        )

    if sec == "ТАБЛО":
        render_all_academy_birthday_banners()
        vals = [
            fetchone("SELECT COUNT(*) c FROM users WHERE academy_id=%s AND account_status='pending'", (ACADEMY_ID,))["c"],
            fetchone("SELECT COUNT(*) c FROM players WHERE academy_id=%s AND approval_status='pending'", (ACADEMY_ID,))["c"],
            fetchone("""SELECT COUNT(DISTINCT t.id) c FROM teams t JOIN coach_teams ct ON ct.team_id=t.id AND ct.academy_id=t.academy_id JOIN coaches c ON c.id=ct.coach_id AND c.academy_id=ct.academy_id WHERE t.academy_id=%s AND t.active AND c.active""", (ACADEMY_ID,))["c"],
        ]
        for col, label, value in zip(st.columns(3), ["Чакащи родители", "Чакащи деца", "Активни набори"], vals):
            col.metric(label, value)
        st.divider()
        st.session_state.setdefault("admin_show_match_results",False)
        if st.button("🏆 Резултати и статистика от мачовете",key="admin_toggle_match_results",use_container_width=False):
            st.session_state.admin_show_match_results=not st.session_state.admin_show_match_results
            st.rerun()
        if st.session_state.admin_show_match_results:
            admin_teams=fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year",(ACADEMY_ID,))
            if admin_teams:
                admin_team_map={t["name"]:t for t in admin_teams}
                admin_team_name=st.selectbox("Избери набор за резултати",list(admin_team_map),key="admin_results_team")
                render_results_dashboard(admin_team_map[admin_team_name],editable=False,key_prefix="admin_results")
            else:
                st.info("Няма активни набори.")

    elif sec == "РЕГИСТРАЦИЯ":
        registration_teams=fetchall("SELECT id,name FROM teams WHERE academy_id=%s AND active ORDER BY birth_year,name",(ACADEMY_ID,))
        registration_options={"Всички набори":None,**{t['name']:t['id'] for t in registration_teams}}
        registration_team_label=st.selectbox("Покажи регистрации за набор",list(registration_options),key="admin_registration_team_filter")
        registration_team_id=registration_options[registration_team_label]
        pending, archive = st.tabs(["🔴 Очакващи", "🟢 Архив потвърдени"])
        with pending:
            rows = fetchall("""SELECT DISTINCT u.* FROM users u LEFT JOIN parent_players pp ON pp.parent_user_id=u.id LEFT JOIN players p ON p.id=pp.player_id WHERE u.academy_id=%s AND u.role='parent' AND u.account_status='pending' AND (%s IS NULL OR p.team_id=%s) ORDER BY u.created_at""", (ACADEMY_ID,registration_team_id,registration_team_id))
            if not rows: st.info("Няма чакащи регистрации.")
            for r in rows:
                c1, c2, c3 = st.columns([5, 1, 1])
                c1.markdown(f"### 🔴 Регистриран родител: {r['first_name']} {r['last_name']}")
                c1.write(f"✉️ {r['email']} · ☎️ {r.get('phone') or 'Без телефон'}")
                with c1:
                    render_registration_children(r["id"])
                if c2.button("Одобри", key=f"au{r['id']}"):
                    execute("UPDATE users SET account_status='approved' WHERE id=%s AND academy_id=%s", (r["id"], ACADEMY_ID)); st.rerun()
                if c3.button("Откажи", key=f"ar{r['id']}"):
                    execute("UPDATE users SET account_status='rejected' WHERE id=%s AND academy_id=%s", (r["id"], ACADEMY_ID)); st.rerun()
        with archive:
            rows = fetchall("""SELECT DISTINCT u.* FROM users u LEFT JOIN parent_players pp ON pp.parent_user_id=u.id LEFT JOIN players p ON p.id=pp.player_id WHERE u.academy_id=%s AND u.role='parent' AND u.account_status='approved' AND (%s IS NULL OR p.team_id=%s) ORDER BY u.created_at DESC""", (ACADEMY_ID,registration_team_id,registration_team_id))
            if not rows: st.info("Архивът е празен.")
            for r in rows:
                with st.expander(f"🟢 Родител: {r['first_name']} {r['last_name']} · {r['email']} · {r.get('phone') or 'Без телефон'}"):
                    render_registration_children(r["id"])
                    st.divider()
                    st.caption("Ако родителят е забравил паролата, Admin може да зададе временна парола. След това родителят я сменя от Настройки.")
                    temporary_password = st.text_input(
                        "Временна парола",
                        type="password",
                        key=f"admin_temp_password_{r['id']}",
                        placeholder="Минимум 8 символа",
                    )
                    repeat_temporary_password = st.text_input(
                        "Повтори временната парола",
                        type="password",
                        key=f"admin_temp_password_repeat_{r['id']}",
                    )
                    if st.button(
                        "🔑 Задай временна парола",
                        key=f"admin_set_temp_password_{r['id']}",
                        use_container_width=True,
                        type="primary",
                    ):
                        if len(temporary_password) < 8:
                            st.error("Временната парола трябва да е минимум 8 символа.")
                        elif temporary_password != repeat_temporary_password:
                            st.error("Двете пароли не съвпадат.")
                        else:
                            with db() as conn, conn.cursor() as cur:
                                cur.execute(
                                    "UPDATE users SET password_hash=%s WHERE id=%s AND academy_id=%s AND role='parent'",
                                    (hash_password(temporary_password), r["id"], ACADEMY_ID),
                                )
                                cur.execute(
                                    "DELETE FROM login_sessions WHERE academy_id=%s AND user_id=%s",
                                    (ACADEMY_ID, r["id"]),
                                )
                            set_flash(f"✅ Временната парола за {r['email']} е зададена. Всички стари сесии са прекратени.")
                            st.rerun()

    elif sec == "ДЕЦА И ГРУПИ":
        teams = fetchall("""SELECT t.*,COALESCE(STRING_AGG(c.display_name, ', ' ORDER BY c.display_name),'Без треньор') display_name
            FROM teams t LEFT JOIN coach_teams ct ON ct.team_id=t.id AND ct.academy_id=t.academy_id
            LEFT JOIN coaches c ON c.id=ct.coach_id AND c.active
            WHERE t.academy_id=%s AND t.active GROUP BY t.id ORDER BY t.birth_year""",(ACADEMY_ID,))
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
                players=fetchall("""SELECT p.*,t.name team_name,COALESCE(tc.coach_names,'Без треньор') coach_name FROM players p LEFT JOIN teams t ON t.id=p.team_id
                    LEFT JOIN LATERAL (SELECT STRING_AGG(c.display_name, ', ' ORDER BY c.display_name) coach_names FROM coach_teams ct JOIN coaches c ON c.id=ct.coach_id AND c.active WHERE ct.academy_id=p.academy_id AND ct.team_id=p.team_id) tc ON TRUE
                    WHERE p.academy_id=%s AND p.active AND p.approval_status='approved' ORDER BY t.birth_year,p.first_name,p.last_name""",(ACADEMY_ID,))
            else:
                players=fetchall("""SELECT p.*,t.name team_name,COALESCE(tc.coach_names,'Без треньор') coach_name FROM players p LEFT JOIN teams t ON t.id=p.team_id
                    LEFT JOIN LATERAL (SELECT STRING_AGG(c.display_name, ', ' ORDER BY c.display_name) coach_names FROM coach_teams ct JOIN coaches c ON c.id=ct.coach_id AND c.active WHERE ct.academy_id=p.academy_id AND ct.team_id=p.team_id) tc ON TRUE
                    WHERE p.academy_id=%s AND p.team_id=%s AND p.active AND p.approval_status='approved' ORDER BY p.first_name,p.last_name""",(ACADEMY_ID,archive_team_id))
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

    elif sec == "НАСТРОЙКИ ЗА ПРОФИЛИТЕ" and profile_settings_page == "Треньори и набори":
        coach_account_manager();st.divider()
        coaches=fetchall("SELECT * FROM coaches WHERE academy_id=%s AND active ORDER BY id",(ACADEMY_ID,)); teams=fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year",(ACADEMY_ID,))
        cmap={c["display_name"]:c["id"] for c in coaches};st.subheader("⚽ Управление на набор")
        if teams:
            tmap={t["name"]:t for t in teams};team=tmap[st.selectbox("Избери набор",list(tmap),key="admin_selected_team_assignment")]
            assigned_rows=fetchall("SELECT coach_id FROM coach_teams WHERE academy_id=%s AND team_id=%s",(ACADEMY_ID,team["id"]))
            assigned_ids={r["coach_id"] for r in assigned_rows}
            selected_ids=st.multiselect("Треньори на набора",options=[c["id"] for c in coaches],default=list(assigned_ids),format_func=lambda cid:next(c["display_name"] for c in coaches if c["id"]==cid),key=f"tc_multi_{team['id']}",help="Може да избереш един или повече треньори.")
            if st.button("💾 Запази треньорите за набора",key=f"ts{team['id']}",use_container_width=True):
                with db() as conn,conn.cursor() as cur:
                    cur.execute("DELETE FROM coach_teams WHERE academy_id=%s AND team_id=%s",(ACADEMY_ID,team["id"]))
                    for coach_id in selected_ids:cur.execute("INSERT INTO coach_teams(academy_id,coach_id,team_id) VALUES(%s,%s,%s)",(ACADEMY_ID,coach_id,team["id"]))
                    cur.execute("UPDATE teams SET coach_id=%s WHERE id=%s AND academy_id=%s",(selected_ids[0] if selected_ids else None,team["id"],ACADEMY_ID))
                set_flash(f"✅ Треньорите за {team['name']} са обновени.");st.rerun()
    elif sec == "НАСТРОЙКИ ЗА ПРОФИЛИТЕ" and profile_settings_page == "Роли и права":
        st.caption("Тук се управляват Parent, Coach и Admin. Specialist профилите се създават и настройват само от „Кондиционен екип“.")
        users = fetchall("SELECT * FROM users WHERE academy_id=%s ORDER BY role,first_name,last_name", (ACADEMY_ID,))
        user_labels={f"{a['first_name']} {a['last_name']} · {a['email']} · {a['role']}":a for a in users}
        account=user_labels[st.selectbox("Избери потребител",list(user_labels),key="admin_selected_rights_user")]
        with st.container(border=True):
            roles = ["parent", "coach", "admin"]
            role = st.selectbox("Роля", roles, index=roles.index(account["role"]) if account["role"] in roles else 0, key=f"role_{account['id']}")
            active = st.checkbox("Активен профил", value=account["active"], key=f"active_{account['id']}")
            if account["role"] == "specialist":
                st.info("Този профил е Specialist. Видът специалист, снимката и зачислените набори се управляват само от „Кондиционен екип“.")
            if st.button("Запази права", key=f"rights_{account['id']}", use_container_width=True):
                if account["id"] == user["id"] and (role != "admin" or not active):
                    st.error("Не можеш да премахнеш собствените си Admin права или да деактивираш текущия профил.")
                elif account["role"] == "specialist":
                    st.error("Specialist профилът се управлява от „Кондиционен екип“. Така ролята и настройките му не могат да се разминават.")
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

    elif sec == "ДЮЛГЕРОВ":
        admin_dyulgerov_panel(user)
    elif sec == "СЪОБЩЕНИЯ":
        staff_direct_messages_panel(user)
    elif sec == "💬 Екипен чат":
        staff_team_chat(user)
    elif sec == "НАСТРОЙКИ ЗА ПРОФИЛИТЕ" and profile_settings_page == "Управление тренировки":
        admin_training_resources(user)
    elif sec == "НАСТРОЙКИ ЗА ПРОФИЛИТЕ" and profile_settings_page == "Кондиционен екип":
        admin_specialist_manager(user)
    elif sec == "ФИНАНСИ":
        st.session_state.setdefault("admin_fee_page", "Начисляване")
        b1,b2,b3,b4=st.columns(4)
        if b1.button("💰 Начисляване",use_container_width=True,type="primary" if st.session_state.admin_fee_page=="Начисляване" else "secondary"):
            st.session_state.admin_fee_page="Начисляване"; st.rerun()
        if b2.button("📊 Справка и плащания",use_container_width=True,type="primary" if st.session_state.admin_fee_page=="Справка" else "secondary"):
            st.session_state.admin_fee_page="Справка"; st.rerun()
        if b3.button(
            "🛍️ Екипировка / Аксесоари",
            use_container_width=True,
            type="primary" if st.session_state.admin_fee_page == "Екипировка" else "secondary"
        ):
            st.session_state.admin_fee_page = "Екипировка"
            st.rerun()
        if b4.button("🧾 Разход",use_container_width=True,type="primary" if st.session_state.admin_fee_page=="Разход" else "secondary"):
            st.session_state.admin_fee_page="Разход"; st.rerun()

        fee_page=st.session_state.admin_fee_page
        if fee_page == "Начисляване":
            teams = fetchall("SELECT * FROM teams WHERE academy_id=%s AND active ORDER BY birth_year", (ACADEMY_ID,)); tmap = {t["name"]: t for t in teams}; team_name = st.selectbox("Набор", list(tmap)); team = tmap[team_name]
            default = fetchone("SELECT amount FROM fee_templates WHERE academy_id=%s AND team_id=%s", (ACADEMY_ID,team["id"])); amount = st.number_input("Стандартна сума €",0.0,1000.0,float(default["amount"] if default else 35),.01); month = st.selectbox("Месец",range(1,13),index=date.today().month-1); year = st.number_input("Година",2024,2045,date.today().year)
            players = fetchall("SELECT * FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' ORDER BY first_name", (ACADEMY_ID,team["id"])); selected = {}
            for player in players:
                c1,c2,c3=st.columns([1,4,2]); use=c1.checkbox("",True,key=f"fu{player['id']}"); c2.write(f"{player['first_name']} {player['last_name']}"); custom=c3.number_input("Индивидуална",0.0,1000.0,float(amount),.01,key=f"fa{player['id']}"); selected[player["id"]]=(use,custom)
            reason=st.text_input("Основание за индивидуални корекции",placeholder="Отстъпка за второ/трето дете или форсмажор")
            if st.button("Начисли на маркираните деца",type="primary",use_container_width=True):
                execute("INSERT INTO fee_templates(academy_id,team_id,amount) VALUES(%s,%s,%s) ON CONFLICT(academy_id,team_id) DO UPDATE SET amount=EXCLUDED.amount",(ACADEMY_ID,team["id"],amount))
                for pid,(use,custom) in selected.items():
                    if use: execute("""INSERT INTO player_fees(academy_id,player_id,team_id,fee_year,fee_month,standard_amount,final_amount,adjustment_reason,custom_status,due_date)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(player_id,fee_year,fee_month) DO UPDATE SET standard_amount=EXCLUDED.standard_amount,final_amount=EXCLUDED.final_amount,adjustment_reason=EXCLUDED.adjustment_reason,custom_status=EXCLUDED.custom_status""",(ACADEMY_ID,pid,team["id"],int(year),month,amount,custom,reason.strip() or None,"force_majeure" if custom!=amount else None,date(int(year),month,28)))
                set_flash("✅ Таксите са начислени на маркираните деца."); st.rerun()
        elif fee_page == "Справка":
            payment_panel(user)
            st.divider()
            financial_dashboard()
        elif fee_page == "Екипировка":
            admin_shop_panel(user)
            st.divider()
            financial_dashboard()
        elif fee_page == "Разход":
            expense_panel(user)

    elif sec == "ИНФОРМАЦИЯ":
        st.subheader("📨 Съобщение до треньор")
        coach_users=fetchall("SELECT c.display_name,u.id user_id FROM coaches c JOIN users u ON u.id=c.user_id WHERE c.academy_id=%s AND c.active AND u.active ORDER BY c.display_name",(ACADEMY_ID,))
        if coach_users:
            cmap={c['display_name']:c['user_id'] for c in coach_users}
            with st.form("admin_to_coach_message",clear_on_submit=True):
                cnames=st.multiselect("Получатели",list(cmap),placeholder="Избери един или повече души");subject=st.text_input("Тема");body=st.text_area("Съобщение");send=st.form_submit_button("Изпрати в Обратна връзка",use_container_width=True)
            if send and cnames and subject.strip() and body.strip():
                for cname in cnames:
                    execute("INSERT INTO coach_inbox_messages(academy_id,coach_user_id,admin_user_id,subject,body) VALUES(%s,%s,%s,%s,%s)",(ACADEMY_ID,cmap[cname],user['id'],subject.strip(),body.strip()))
                    create_notification(cmap[cname],"admin_feedback","Ново съобщение от Admin",subject.strip())
                set_flash(f"✅ Съобщението е изпратено до {len(cnames)} получател(и).");st.rerun()
        else: st.info("Няма активни треньорски профили.")
        st.divider()
        st.subheader("📢 Информация към родители")
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
        st.divider()
        show_video_settings=st.checkbox("🎬 Покажи настройките за видео инструкцията",value=False,key="admin_show_parent_help_video")
        if show_video_settings:
            current_help_video=academy().get("parent_help_video_url") or ""
            with st.container(border=True):
                st.subheader("🎬 Видео инструкция за родители")
                help_video_url=st.text_input("Линк към видео инструкция",value=current_help_video,key="parent_help_video_url")
                vh1,vh2=st.columns(2)
                if vh1.button("Запази",use_container_width=True,key="save_parent_help_video"):
                    execute("UPDATE academies SET parent_help_video_url=%s WHERE id=%s",(help_video_url.strip() or None,ACADEMY_ID)); academy.clear(); st.rerun()
                if vh2.button("Премахни",use_container_width=True,key="remove_parent_help_video"):
                    execute("UPDATE academies SET parent_help_video_url=NULL WHERE id=%s",(ACADEMY_ID,)); academy.clear(); st.rerun()
                if current_help_video: st.video(current_help_video)
                else: st.info("Видео инструкцията все още не е добавена.")
            current_anthem_video=academy().get("lions_anthem_video_url") or ""
            with st.container(border=True):
                st.subheader("🦁 Химнът на лъвовете")
                st.caption("Показва се на началния екран под мотото „Където успехът е танц на таланта“.")
                anthem_video_url=st.text_input("Линк към видеото с химна",value=current_anthem_video,key="lions_anthem_video_url")
                va1,va2=st.columns(2)
                if va1.button("Запази",use_container_width=True,key="save_lions_anthem_video"):
                    execute("UPDATE academies SET lions_anthem_video_url=%s WHERE id=%s",(anthem_video_url.strip() or None,ACADEMY_ID)); academy.clear(); st.rerun()
                if va2.button("Премахни",use_container_width=True,key="remove_lions_anthem_video"):
                    execute("UPDATE academies SET lions_anthem_video_url=NULL WHERE id=%s",(ACADEMY_ID,)); academy.clear(); st.rerun()
                if current_anthem_video: st.video(normalize_video_url(current_anthem_video))
                else: st.info("Химнът все още не е добавен.")
    else:
        notification_center(user)


def coach_panel(user):
    coach = fetchone("SELECT * FROM coaches WHERE academy_id=%s AND user_id=%s AND active", (ACADEMY_ID, user["id"]))
    st.title("Треньорски панел")
    show_flash()
    bell_space,bell_col=st.columns([12,1])
    with bell_col:
        compact_notification_bell(user,"coach")
    if not coach:
        st.warning("Този Coach профил още не е свързан с треньор. Admin трябва да го свърже от „Треньори и набори“.")
        return
    teams = fetchall("""SELECT DISTINCT t.* FROM coach_teams ct JOIN teams t ON t.id=ct.team_id
        WHERE ct.academy_id=%s AND ct.coach_id=%s AND t.active ORDER BY t.birth_year""",(ACADEMY_ID,coach["id"]))
    if not teams:
        st.info("Няма възложени набори към този треньор.")
        return
    st.sidebar.markdown(avatar_html(coach.get("profile_image"),coach.get("profile_image_mime"),72),unsafe_allow_html=True)
    st.sidebar.caption(coach["display_name"])
    sections = ["Табло", "Тренировки и мачове", "Такси", "Съобщения", "Обратна връзка", "Присъствие", "Упражнения", "🏆 Резултати", "🛍️ Екипировка / Аксесоари", "📦 Инвентар", "💬 Екипен чат", "📇 Деца и контакти"]
    sec = st.sidebar.radio("Треньорско меню", sections, key="coach_menu")
    if sec == "💬 Екипен чат":
        staff_team_chat(user)
        return
    tmap = {t["name"]: t for t in teams}
    team_name = st.selectbox("Набор", list(tmap), key="coach_selected_team")
    team = tmap[team_name]

    if sec == "📇 Деца и контакти":
        st.subheader("📇 Информация и контакти за децата")
        render_child_contact_cards(team["id"])
    elif sec == "Табло":
        render_all_academy_birthday_banners()
        players=fetchall("SELECT * FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' AND active ORDER BY first_name,last_name",(ACADEMY_ID,team["id"]))
        st.metric("Деца в групата",len(players))
        st.divider(); st.subheader("⚽ Играч")
        for player in players:
            with st.container(border=True): player_card(player,position_edit=True,key_prefix=f"coach_{team['id']}")
        if not players: st.info("Няма потвърдени деца в този набор.")
    elif sec == "Тренировки и мачове":
        suite_section=st.radio(
            "Избор",
            ["🏃 Тренировъчен график","📅 Създаден график","⚽ Създаване МАЧ","⚽ Група за мача","🧩 Match Day","📊 Статистика","🏆 Резултати"],
            horizontal=True,
            key=f"coach_training_match_suite_{team['id']}",
            label_visibility="collapsed",
        )
        squad_section=None
        if suite_section == "🏃 Тренировъчен график":
            multi_date_event_form("coach_training_form",teams,user["id"],fixed_team_id=team["id"],event_mode="training")
        elif suite_section == "📅 Създаден график":
            st.caption("Всеки нов общ график заменя стария общ график за избрания набор. Тук виждаш актуално публикувания график.")
            compact_team_training_view(team,key_prefix=f"coach_created_schedule_{team['id']}")
        elif suite_section == "⚽ Създаване МАЧ":
            multi_date_event_form("coach_match_form",teams,user["id"],fixed_team_id=team["id"],event_mode="match")
        elif suite_section == "🧩 Match Day":
            match_day_panel(user,team)
        elif suite_section == "🏆 Резултати":
            results_statistics_panel(team)
        elif suite_section == "📊 Статистика":
            coach_player_statistics(user,team)
        elif suite_section == "⚽ Група за мача":
            squad_section="⚽ Група за мача"
        if squad_section == "⚽ Група за мача":
            matches = fetchall(
                """SELECT e.* FROM events e
                   WHERE e.academy_id=%s AND e.team_id=%s AND e.event_type='match'
                     AND NOT EXISTS(
                         SELECT 1 FROM match_reports mr
                         WHERE mr.academy_id=e.academy_id AND mr.event_id=e.id AND mr.confirmed
                     )
                   ORDER BY e.starts_at ASC""",
                (ACADEMY_ID, team["id"]),
            )
            if not matches:
                st.info("Няма активни мачове за този набор. Приключените срещи са в „Резултати“.")
            else:
                mmap = {f"{m['starts_at']:%d.%m.%Y %H:%M} · {m['title']}": m for m in matches}
                match_label = st.selectbox("Избери мач", list(mmap), key="squad_match")
                match = mmap[match_label]
                with st.expander("🗑️ Изтриване на погрешно създаден мач", expanded=False):
                    st.warning("Изтриването е окончателно. Ще бъдат премахнати мачът, групата, анкетата, присъствията и прикаченият плакат за този мач.")
                    delete_match_confirm = st.checkbox(
                        f"Потвърждавам изтриването на: {match_label}",
                        key=f"delete_match_confirm_{match['id']}",
                    )
                    if st.button(
                        "🗑️ Изтрий този мач",
                        key=f"delete_match_{match['id']}",
                        use_container_width=True,
                        disabled=not delete_match_confirm,
                    ):
                        with db() as conn, conn.cursor() as cur:
                            # Keep bell cleanup scoped to notifications connected with this match flow.
                            cur.execute(
                                "DELETE FROM notifications WHERE academy_id=%s AND notification_type IN ('match','match_squad','match_result') AND body LIKE %s",
                                (ACADEMY_ID, f"%{match['title']}%"),
                            )
                            # All direct match records (squad, poll, attendance, poster, stats etc.)
                            # are removed through the event foreign keys with ON DELETE CASCADE.
                            cur.execute(
                                "DELETE FROM events WHERE academy_id=%s AND team_id=%s AND id=%s AND event_type='match'",
                                (ACADEMY_ID, team["id"], match["id"]),
                            )
                        st.session_state.pop(f"squad_sent_{match['id']}", None)
                        set_flash("✅ Погрешно създаденият мач е изтрит окончателно.")
                        st.rerun()
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
                squad_sent_key=f"squad_sent_{match['id']}"
                squad_is_sent=bool(st.session_state.get(squad_sent_key))
                if squad_is_sent:
                    st.markdown(f"""<style>
                    .st-key-match_squad_{match['id']} button[kind="formSubmit"] {{
                        background:linear-gradient(135deg,#11833b,#27b85b)!important;
                        border-color:#69e698!important;color:#fff!important;
                    }}</style>""",unsafe_allow_html=True)
                with st.form(f"match_squad_{match['id']}"):
                    choices={}
                    st.markdown("#### Футболисти")
                    for player in players:
                        current=existing.get(player["id"],"not_in_group")=="in_group"
                        name_col,check_col=st.columns([8,1])
                        name_col.write(f"**{player['first_name']} {player['last_name']}**")
                        choices[player["id"]]=check_col.checkbox("В групата",value=current,key=f"squad_check_{match['id']}_{player['id']}",label_visibility="collapsed")
                    existing_poll=fetchone("SELECT id FROM polls WHERE academy_id=%s AND event_id=%s AND poll_type='match_attendance' AND active ORDER BY created_at DESC LIMIT 1",(ACADEMY_ID,match["id"]))
                    create_match_poll=st.checkbox("📊 Изпрати анкета до родителите на избраните деца",value=False,key=f"match_poll_{match['id']}")
                    publish_squad=st.form_submit_button(
                        "✅ Групата е изпратена" if squad_is_sent else "Изпрати групата към родителите",
                        use_container_width=True,
                    )
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
                    notified_parents=notify_team_parents(team["id"],"match_squad","Публикувана е група за мача",match_label)
                    st.session_state[squad_sent_key]=True
                    if notified_parents:
                        set_flash(f"✅ Групата за мача е изпратена до {notified_parents} родителски профила.")
                    else:
                        set_flash("⚠️ Групата е запазена, но няма активни одобрени родители за уведомяване.","warning")
                    st.rerun()
                render_match_poll_responses(match)
    elif sec == "Упражнения":
        coach_exercises_panel()
    elif sec == "Присъствие":
        events = fetchall("SELECT * FROM events WHERE academy_id=%s AND team_id=%s AND starts_at >= NOW() AND status <> 'cancelled' ORDER BY starts_at ASC LIMIT 40", (ACADEMY_ID, team["id"]))
        if not events: st.info("Няма предстоящи тренировки или мачове за отразяване.")
        else:
            emap = {f"{e['starts_at']:%d.%m.%Y %H:%M} · {e['title']}": e for e in events}
            event_labels = st.multiselect(
                "Събитие / събития",
                list(emap),
                placeholder="Избери едно или повече събития",
                help="Може да отразиш едно и също присъствие/отсъствие за няколко тренировки или мача наведнъж.",
                key=f"attendance_events_{team['id']}",
            )
            selected_events = [emap[label] for label in event_labels]
            players = fetchall("SELECT * FROM players WHERE academy_id=%s AND team_id=%s AND approval_status='approved' AND active ORDER BY first_name", (ACADEMY_ID, team["id"]))
            if not selected_events:
                st.info("Избери поне едно събитие, за да отразиш присъствие или отсъствие.")
            else:
                with st.form(f"attendance_form_{team['id']}"):
                    st.caption(f"Избрани събития: {len(selected_events)}")
                    marks = {}
                    for player in players:
                        marks[player["id"]] = st.selectbox(
                            f"{player['first_name']} {player['last_name']}",
                            ["present","absent"],
                            format_func=lambda x:{"present":"Присъства","absent":"Отсъства"}[x],
                            key=f"att_multi_{team['id']}_{player['id']}",
                        )
                    saved = st.form_submit_button("Запази за избраните събития", use_container_width=True)
                if saved:
                    with db() as conn, conn.cursor() as cur:
                        for event in selected_events:
                            for player_id,status in marks.items():
                                cur.execute("""INSERT INTO attendance(academy_id,event_id,player_id,status,marked_by) VALUES(%s,%s,%s,%s,%s)
                                               ON CONFLICT(event_id,player_id) DO UPDATE SET status=EXCLUDED.status,marked_by=EXCLUDED.marked_by,marked_at=NOW()""",
                                            (ACADEMY_ID,event["id"],player_id,status,user["id"]))
                    set_flash(f"✅ Присъствието е запазено за {len(selected_events)} събития."); st.rerun()
            st.divider();render_coach_attendance_history(team)
    elif sec == "Такси":
        payment_panel(user,[t["id"] for t in teams],fixed_team_id=team["id"])
    elif sec == "🏆 Резултати":
        results_statistics_panel(team)
    elif sec == "🛍️ Екипировка / Аксесоари":
        product_catalog_parent(user,None)
    elif sec == "📦 Инвентар":
        coach_inventory_status_panel(user)
    elif sec == "💬 Екипен чат":
        staff_team_chat(user)
    elif sec == "Съобщения":
        st.subheader("📢 Съобщение към родители")
        st.caption(f"Избран набор: {team['name']}")
        with st.form("coach_message_form"):
            title = st.text_input("Заглавие")
            body = st.text_area("Съобщение към родителите")
            send = st.form_submit_button("Изпрати към групата", use_container_width=True)
        if send and title.strip() and body.strip():
            execute("INSERT INTO team_messages(academy_id,team_id,sender_user_id,title,body) VALUES(%s,%s,%s,%s,%s)", (ACADEMY_ID,team["id"],user["id"],title.strip(),body.strip()))
            notify_team_parents(team["id"], "message", title.strip(), body.strip())
            st.success("Съобщението е добавено в архива.")
            st.rerun()
        st.divider()
        st.subheader("Архив на съобщенията към родители")
        rows = fetchall("SELECT * FROM team_messages WHERE academy_id=%s AND team_id=%s ORDER BY created_at DESC", (ACADEMY_ID,team["id"]))
        for row in rows:
            with st.container(border=True):
                st.write(f"**{row['title']}**"); st.write(row['body']); st.caption(row['created_at'].strftime('%d.%m.%Y %H:%M'))
                if row.get("sender_user_id") == user["id"] and st.button("🗑️ Изтрий", key=f"coach_delete_message_{row['id']}"):
                    execute("DELETE FROM team_messages WHERE academy_id=%s AND id=%s", (ACADEMY_ID,row['id']))
                    st.rerun()
        st.divider()
        with st.expander("✉️ Лични съобщения между треньори", expanded=False):
            staff_direct_messages_panel(user)

    elif sec == "Обратна връзка":
        st.subheader("📨 Съобщения от Admin")
        execute("DELETE FROM coach_inbox_messages WHERE academy_id=%s AND coach_user_id=%s AND is_read=TRUE AND read_at IS NOT NULL AND read_at < NOW()-INTERVAL '10 days'",(ACADEMY_ID,user['id']))
        admin_messages=fetchall("SELECT * FROM coach_inbox_messages WHERE academy_id=%s AND coach_user_id=%s ORDER BY is_read ASC,created_at DESC",(ACADEMY_ID,user['id']))
        if not admin_messages: st.info("Няма съобщения от Admin.")
        unread_admin_ids=[]
        for msg in admin_messages:
            status="🔴 Непрочетено" if not msg.get("is_read") else "🟢 Прочетено"
            if not msg.get("is_read"): unread_admin_ids.append(msg["id"])
            with st.container(border=True):
                st.caption(status);st.write(f"**{msg['subject']}**");st.write(msg['body']);st.caption(msg['created_at'].strftime('%d.%m.%Y %H:%M'))
        if unread_admin_ids:
            execute("UPDATE coach_inbox_messages SET is_read=TRUE,read_at=COALESCE(read_at,NOW()) WHERE academy_id=%s AND coach_user_id=%s AND id=ANY(%s)",(ACADEMY_ID,user['id'],unread_admin_ids))
        st.divider()
        st.subheader("📝 Обратна връзка от родители")
        execute("DELETE FROM feedback WHERE academy_id=%s AND coach_user_id=%s AND is_read=TRUE AND read_at IS NOT NULL AND read_at < NOW()-INTERVAL '10 days'",(ACADEMY_ID,user["id"]))
        rows = fetchall(
            """SELECT f.*,u.first_name,u.last_name,p.first_name player_first,p.last_name player_last,t.name team_name
               FROM feedback f JOIN users u ON u.id=f.parent_user_id
               LEFT JOIN players p ON p.id=f.player_id
               LEFT JOIN teams t ON t.id=p.team_id
               WHERE f.academy_id=%s AND f.coach_user_id=%s ORDER BY f.is_read ASC,f.created_at DESC""",
            (ACADEMY_ID, user["id"]),
        )
        if not rows: st.info("Няма изпратена обратна връзка.")
        unread_feedback_ids=[]
        for row in rows:
            status="🔴 Непрочетено" if not row.get("is_read") else "🟢 Прочетено"
            if not row.get("is_read"): unread_feedback_ids.append(row["id"])
            with st.container(border=True):
                st.caption(status)
                st.markdown(f"**{row.get('first_name') or 'Родител'} / {row.get('player_first') or ''} {row.get('player_last') or ''} / {row.get('team_name') or 'Без набор'}**")
                st.write(f"**{row['subject']}** · {row['feedback_type']}")
                if row.get("rating"): st.write(f"Оценка: {row['rating']}/5")
                st.write(row["body"])
                st.caption(f"За: {row.get('player_first') or ''} {row.get('player_last') or ''} · {row['created_at']:%d.%m.%Y %H:%M}")
        if unread_feedback_ids:
            execute("UPDATE feedback SET is_read=TRUE,read_at=COALESCE(read_at,NOW()) WHERE academy_id=%s AND coach_user_id=%s AND id=ANY(%s)",(ACADEMY_ID,user["id"],unread_feedback_ids))
    else:
        st.info("Избери секция от треньорското меню.")

def logout_button():
    if st.sidebar.button("Изход",use_container_width=True):
        token = st.session_state.get("login_token") or st.query_params.get("session")
        if token:
            execute("DELETE FROM login_sessions WHERE token_hash=%s", (session_token_hash(token),))
        st.session_state.clear()
        st.query_params.clear()
        st.rerun()

PUSH_COMPONENT = st.components.v2.component(
    "dyulgerov_push_auto_v1",
    html="""
    <div id="push-overlay" class="push-overlay" hidden>
      <div class="push-dialog">
        <div class="push-icon">⚽</div>
        <h3>Известия от Dyulgerov Academy</h3>
        <p>Разрешете известията, за да получавате веднага нови съобщения, графици, мачове и информация от Академията.</p>
        <button type="button" id="push-allow">🔔 Разреши известията</button>
        <button type="button" id="push-later" class="secondary">По-късно</button>
        <div id="push-status" aria-live="polite"></div>
      </div>
    </div>
    """,
    css="""
    .push-overlay{position:fixed;inset:0;z-index:999999;background:rgba(0,0,0,.76);display:flex;align-items:center;justify-content:center;padding:18px}
    .push-overlay[hidden]{display:none!important}
    .push-dialog{width:min(430px,94vw);background:linear-gradient(145deg,#090909,#18130a);border:1px solid #d6b765;border-radius:22px;padding:24px;text-align:center;box-shadow:0 24px 70px rgba(0,0,0,.65)}
    .push-dialog h3{color:#fff;margin:8px 0 12px}.push-dialog p{color:#ded5c2;line-height:1.5}.push-icon{font-size:52px}
    .push-dialog button{width:100%;min-height:46px;margin-top:10px;border-radius:12px;border:1px solid #e2c777;font-weight:800;cursor:pointer;background:linear-gradient(135deg,#956b20,#d1ad57);color:#fff}
    .push-dialog button.secondary{background:transparent;border-color:#756641;color:#d8cfbd}.push-dialog button:disabled{opacity:.55;cursor:wait}
    #push-status{margin-top:12px;color:#fff;font-size:.88rem}
    """,
    js="""
    export default function(component) {
      const {parentElement,data,setStateValue,setTriggerValue}=component;
      const overlay=parentElement.querySelector('#push-overlay');
      const allow=parentElement.querySelector('#push-allow');
      const later=parentElement.querySelector('#push-later');
      const status=parentElement.querySelector('#push-status');
      const standalone=window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone===true;
      const withTimeout=(promise,ms,label)=>Promise.race([promise,new Promise((_,reject)=>setTimeout(()=>reject(new Error(label+' не отговори.')),ms))]);
      const decodeKey=(value)=>{const padding='='.repeat((4-value.length%4)%4);const raw=atob((value+padding).replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from([...raw].map(c=>c.charCodeAt(0)));};
      const sendSubscription=(sub)=>{setStateValue('subscription',JSON.stringify(sub.toJSON()));setStateValue('userAgent',navigator.userAgent);setTriggerValue('saveRequest',Date.now());};
      const register=async(force=false)=>{
        const reg=await withTimeout(navigator.serviceWorker.register('/app/static/service-worker.js',{scope:'/app/static/',updateViaCache:'none'}),10000,'Service Worker');
        let sub=await withTimeout(reg.pushManager.getSubscription(),5000,'Push абонаментът');
        if(force && sub){await withTimeout(sub.unsubscribe(),10000,'Старата регистрация');sub=null;}
        if(!sub)sub=await withTimeout(reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:decodeKey(data.publicKey)}),15000,'Новият Push абонамент');
        sendSubscription(sub);return sub;
      };
      const boot=async()=>{
        if(!('serviceWorker' in navigator)||!('PushManager' in window))return;
        const reg=await navigator.serviceWorker.getRegistration('/app/static/');
        const sub=reg?await reg.pushManager.getSubscription():null;
        if(sub){sendSubscription(sub);return;}
        if(standalone && Notification.permission!=='denied')overlay.hidden=false;
      };
      allow.onclick=async()=>{
        allow.disabled=true;later.disabled=true;
        try{
          status.textContent='Активиране...';
          const permission=await Notification.requestPermission();
          if(permission!=='granted')throw new Error('Разрешението не е дадено. Може да го включите от настройките на браузъра.');
          await register(false);
          status.textContent='✅ Известията са активирани.';
          setTimeout(()=>{overlay.hidden=true;},900);
        }catch(err){status.textContent='Грешка: '+(err?.message||String(err));allow.disabled=false;later.disabled=false;}
      };
      later.onclick=()=>{overlay.hidden=true;};
      boot().catch(()=>{});
    }
    """,
)

def render_push_opt_in(user):
    if not browser_push_ready():
        return
    component_key=f"push_auto_{user['id']}"
    result=PUSH_COMPONENT(
        data={"publicKey":VAPID_PUBLIC_KEY},
        default={"subscription":"","userAgent":"","saveRequest":None},
        key=component_key,
        on_subscription_change=lambda: None,
        on_userAgent_change=lambda: None,
        on_saveRequest_change=lambda: None,
        height=0,
    )
    component_state=st.session_state.get(component_key,{}) or {}
    raw=(getattr(result,"subscription","") or component_state.get("subscription") or "")
    user_agent=(getattr(result,"userAgent","") or component_state.get("userAgent") or None)
    if not raw:
        return
    try:
        subscription=json.loads(raw)
        endpoint=subscription.get("endpoint","")
        if not endpoint:
            return
        fingerprint=hashlib.sha256(endpoint.encode()).hexdigest()
        state_key=f"saved_push_{user['id']}"
        existing=fetchone("SELECT id FROM push_subscriptions WHERE academy_id=%s AND user_id=%s AND endpoint=%s AND active",(ACADEMY_ID,user["id"],endpoint))
        if not existing or st.session_state.get(state_key)!=fingerprint:
            if save_push_subscription(user["id"],subscription,user_agent):
                execute("UPDATE push_subscriptions SET active=FALSE,updated_at=NOW() WHERE academy_id=%s AND user_id=%s AND endpoint<>%s AND active",(ACADEMY_ID,user["id"],endpoint))
                st.session_state[state_key]=fingerprint
    except Exception:
        pass

def render_app():
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
        st.markdown("### 📞 Контакти")
        with st.container(border=True):
            st.markdown("🌐 [Facebook страница](https://www.facebook.com/profile.php?id=61556982730263)")
            st.markdown("✉️ [dyulgerovacademy@gmail.com](mailto:dyulgerovacademy@gmail.com)")
            st.markdown("☎️ [089 929 7337](tel:+359899297337)")

    else:
        logout_button()
        render_push_opt_in(user)

        if user["role"] == "admin":
            admin_panel(user)
        elif user["role"] == "coach":
            coach_panel(user)
        elif user["role"] == "specialist":
            specialist_panel(user)
        else:
            parent_panel(user)


try:
    render_app()
except (psycopg2.OperationalError, psycopg2.InterfaceError):
    # Hide the technical PostgreSQL/SSL traceback from end users.
    st.markdown("## 🔄 Връзката беше прекъсната")
    st.info("Временна връзка с базата данни беше прекъсната. Приложението вече направи автоматични опити за възстановяване.")
    if st.button("🔄 Опитай отново", use_container_width=True, type="primary"):
        reset_db_pool()
        st.rerun()
