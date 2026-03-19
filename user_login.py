import psycopg2
import psycopg2.extras
import bcrypt
import streamlit as st
import re
import os
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import dns.resolver

# ── Single source of truth for IST timestamps ─────────────────────────────────
# FIX: removed `import pytz` and the unused `timedelta` import.
#      pytz-based get_ist_time() is replaced by now_ist() / today_ist() from
#      timezone_helper so this file is 100% consistent with llm_manager.py
#      and db_manager.py (all use the same stdlib-based IST timezone object).
from timezone_helper import IST, now_ist, today_ist


# ── Cached PostgreSQL connection ──────────────────────────────────────────────
@st.cache_resource
def _get_user_pg_connection():
    """
    Dedicated cached connection for user_login operations.
    @st.cache_resource means it is created once per Streamlit worker process
    and reused on every rerun — no reconnection on every click.
    """
    conn = psycopg2.connect(
        host=st.secrets["SUPABASE_HOST"],
        dbname=st.secrets["SUPABASE_DB"],
        user=st.secrets["SUPABASE_USER"],
        password=st.secrets["SUPABASE_PASSWORD"],
        port=st.secrets["SUPABASE_PORT"],
        connect_timeout=30,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    conn.autocommit = False
    return conn


def _conn():
    """Return the cached connection, reconnecting if the socket was dropped."""
    conn = _get_user_pg_connection()
    try:
        conn.isolation_level  # lightweight liveness check
    except Exception:
        st.cache_resource.clear()
        conn = _get_user_pg_connection()
    return conn


def _execute(sql: str, params=None, fetch: str = "none"):
    """
    Run a SQL statement inside an implicit transaction.
    fetch: 'one' | 'all' | 'none'
    Commits on success, rolls back on error.
    """
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            result = None
            if fetch == "one":
                result = cur.fetchone()
            elif fetch == "all":
                result = cur.fetchall()
            conn.commit()
            return result
    except Exception:
        conn.rollback()
        raise


# ── Utility ───────────────────────────────────────────────────────────────────

# FIX: get_ist_time() removed entirely.
#      Original: used pytz.timezone("Asia/Kolkata") — inconsistent with the
#      stdlib timezone(timedelta(hours=5, minutes=30)) used in llm_manager
#      and db_manager, causing subtle type mismatches in datetime arithmetic.
#      Replacement: now_ist() from timezone_helper (stdlib, IST-aware).


def is_strong_password(password):
    return (
        len(password) >= 8 and
        re.search(r'[A-Z]', password) and
        re.search(r'[a-z]', password) and
        re.search(r'[0-9]', password) and
        re.search(r'[!@#$%^&*(),.?":{}|<>]', password)
    )


def is_valid_email(email):
    return re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email) is not None


def domain_has_mx_record(email):
    try:
        domain = email.split('@')[1]
        dns.resolver.resolve(domain, 'MX')
        return True
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, IndexError):
        return False
    except Exception:
        return True


# ── Existence checks ──────────────────────────────────────────────────────────

def username_exists(username):
    row = _execute(
        "SELECT 1 FROM users WHERE username = %s", (username,), fetch="one"
    )
    return row is not None


def email_exists(email):
    row = _execute(
        "SELECT 1 FROM users WHERE email = %s", (email,), fetch="one"
    )
    return row is not None


# ── Table creation ────────────────────────────────────────────────────────────

def create_user_table():
    """
    Create users and user_logs tables if they don't already exist,
    and safely migrate existing tables to add missing columns.

    ── FIX 1 (is_verified — ROOT CAUSE) ──────────────────────────────────────
    The original DDL had NO is_verified column in the users table at all:

        CREATE TABLE IF NOT EXISTS users (
            id           SERIAL PRIMARY KEY,
            username     TEXT UNIQUE NOT NULL,
            password     TEXT NOT NULL,
            email        TEXT UNIQUE,
            groq_api_key TEXT
            -- is_verified did not exist!
        );

    Because the column was missing, complete_registration() could never set
    it to TRUE — the INSERT simply didn't include it and there was no
    subsequent UPDATE. Every user was permanently stuck at the implicit FALSE
    state (or the column was missing entirely, causing insert errors on any
    code that tried to write it).

    Fixed by:
      a) Adding `is_verified BOOLEAN NOT NULL DEFAULT FALSE` to the CREATE.
      b) Adding an ALTER TABLE … ADD COLUMN IF NOT EXISTS guard so existing
         live deployments gain the column without a manual migration.

    ── FIX 2 (created_at) ────────────────────────────────────────────────────
    Added created_at TIMESTAMPTZ so registration time is preserved with the
    full IST offset, consistent with the candidates table in db_manager.

    ── FIX 3 (user_logs.timestamp type) ─────────────────────────────────────
    Original: timestamp TEXT NOT NULL
    Problem:  Storing timestamps as plain strings loses timezone information
              and makes date arithmetic unreliable. DATE(text_column) works
              only by luck when the string happens to be ISO-8601; it fails
              silently for any other format.
    Fixed:    timestamp TIMESTAMPTZ NOT NULL
              A DO block migrates the column type on existing tables.
    """
    ddl = """
    -- ── users table ──────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS users (
        id           SERIAL PRIMARY KEY,
        username     TEXT UNIQUE NOT NULL,
        password     TEXT NOT NULL,
        email        TEXT UNIQUE,
        groq_api_key TEXT,
        -- FIX: is_verified added — root cause of the verification bug
        is_verified  BOOLEAN NOT NULL DEFAULT FALSE,
        -- FIX: created_at stores registration timestamp with IST offset
        created_at   TIMESTAMPTZ
    );

    -- FIX: Add missing columns to any existing users table (idempotent).
    ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified  BOOLEAN NOT NULL DEFAULT FALSE;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at   TIMESTAMPTZ;

    -- ── user_logs table ───────────────────────────────────────────────────────
    -- FIX: timestamp is TIMESTAMPTZ (was TEXT in original DDL).
    CREATE TABLE IF NOT EXISTS user_logs (
        id        SERIAL PRIMARY KEY,
        username  TEXT NOT NULL,
        action    TEXT NOT NULL,
        timestamp TIMESTAMPTZ NOT NULL
    );

    -- FIX: Migrate existing user_logs.timestamp from TEXT to TIMESTAMPTZ.
    --      The DO block is a no-op if the column is already TIMESTAMPTZ.
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM   information_schema.columns
            WHERE  table_name  = 'user_logs'
              AND  column_name = 'timestamp'
              AND  data_type   = 'text'
        ) THEN
            ALTER TABLE user_logs
                ALTER COLUMN timestamp TYPE TIMESTAMPTZ
                USING timestamp::TIMESTAMPTZ;
        END IF;
    END $$;
    """
    # FIX: capture conn once so the except block rolls back the same object.
    #      Original called _conn().rollback() — a fresh _conn() call in except
    #      may return a different connection reference and miss the rollback.
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    except Exception as e:
        conn.rollback()
        st.error(f"Error creating tables: {e}")


# ── OTP helpers ───────────────────────────────────────────────────────────────

def generate_otp():
    return str(random.randint(100000, 999999))


def _send_email(to_email: str, subject: str, body: str) -> bool:
    """Internal SMTP helper used by both registration and password reset."""
    try:
        sender_email    = st.secrets["email_address"]
        sender_password = st.secrets["email_password"]

        msg            = MIMEMultipart()
        msg['From']    = sender_email
        msg['To']      = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, to_email, msg.as_string())
        server.quit()
        return True
    except smtplib.SMTPException as e:
        st.error(f"SMTP Error: {e}")
        return False
    except Exception as e:
        st.error(f"Error sending email: {e}")
        return False


def send_registration_otp(to_email: str, otp: str) -> bool:
    body = f"""Hello,

Welcome! Your verification OTP for registration is: {otp}

This OTP will expire in 3 minutes.

If you did not request this registration, please ignore this email.

Best regards,
Resume App Team
"""
    return _send_email(to_email, "Email Verification OTP", body)


def send_email_otp(to_email: str, otp: str) -> bool:
    body = f"""Hello,

Your OTP for password reset is: {otp}

This OTP will expire in 3 minutes.

If you did not request this password reset, please ignore this email.

Best regards,
Resume App Team
"""
    return _send_email(to_email, "Password Reset OTP", body)


# ── Registration ──────────────────────────────────────────────────────────────

def add_user(username, password, email=None):
    """
    Validate details and send OTP. Does NOT write to the DB yet.
    Returns (success: bool, message: str).

    FIX: 'timestamp' stored in pending_registration now uses now_ist()
         instead of the old get_ist_time() (pytz-based). Both sides of the
         OTP-expiry subtraction in complete_registration() are now the same
         stdlib IST-aware type, so datetime arithmetic is always consistent.
    """
    if not is_strong_password(password):
        return False, "⚠ Password must be at least 8 characters long and include uppercase, lowercase, number, and special character."
    if not email:
        return False, "⚠ Email is required for registration."
    if not is_valid_email(email):
        return False, "⚠ Invalid email format. Please provide a valid email address."
    if not domain_has_mx_record(email):
        return False, "⚠ Email domain does not exist or has no valid mail server."
    if email_exists(email):
        return False, "🚫 Email already exists. Please use a different email."
    if username_exists(username):
        return False, "🚫 Username already exists."

    otp = generate_otp()
    if not send_registration_otp(email, otp):
        return False, "❌ Failed to send OTP email. Please check your email address and try again."

    st.session_state.pending_registration = {
        'username':  username,
        'password':  password,
        'email':     email,
        'otp':       otp,
        # FIX: now_ist() — stdlib IST-aware datetime.
        #      Was: get_ist_time() (pytz). Both sides of the expiry check
        #      in complete_registration() must use the same timezone library.
        'timestamp': now_ist(),
    }
    return True, "📧 Verification email sent! Please check your inbox for OTP."


def complete_registration(entered_otp):
    """
    Verify OTP and insert the new user into Supabase.
    Returns (success: bool, message: str).

    ── FIX: is_verified — THE PRIMARY BUG ───────────────────────────────────
    Original INSERT statement:
        INSERT INTO users (username, password, email) VALUES (%s, %s, %s)

    Problems:
      1. is_verified column was not in the DDL at all — it didn't exist.
      2. Even if it had existed, the INSERT omitted it, so it would have
         defaulted to FALSE with no subsequent UPDATE to set it TRUE.
      3. There was no ON CONFLICT clause, so a resend-OTP flow that sent
         a second OTP for the same username would crash with UniqueViolation
         on the second complete_registration() call.

    Fixed:
      - INSERT explicitly sets is_verified = TRUE in the same atomic statement.
      - ON CONFLICT (username) DO UPDATE heals any earlier stuck-at-FALSE row
        (e.g., if the table was partially populated before this fix was deployed).
      - created_at is also written with now_ist() for a full audit trail.

    ── FIX: OTP expiry comparison ────────────────────────────────────────────
    now_ist() used on both sides of the subtraction. Both are stdlib
    timezone-aware IST datetimes, so the timedelta arithmetic is exact.
    """
    if 'pending_registration' not in st.session_state:
        return False, "⚠ No pending registration found. Please start registration again."

    pending    = st.session_state.pending_registration
    stored_otp = pending['otp']
    timestamp  = pending['timestamp']

    # FIX: now_ist() on both sides — consistent stdlib IST-aware subtraction.
    time_elapsed = (now_ist() - timestamp).total_seconds()
    if time_elapsed > 180:
        del st.session_state.pending_registration
        return False, "⏱ OTP has expired. Please register again."
    if entered_otp != stored_otp:
        return False, "❌ Invalid OTP. Please try again."

    username          = pending['username']
    password          = pending['password']
    email             = pending['email']
    hashed_password   = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    registration_time = now_ist()   # IST-aware — stored in created_at column

    try:
        # FIX: is_verified=TRUE set atomically in the INSERT — never FALSE after this.
        #      ON CONFLICT heals any pre-existing stuck-at-FALSE row.
        _execute(
            """
            INSERT INTO users (username, password, email, is_verified, created_at)
            VALUES (%s, %s, %s, TRUE, %s)
            ON CONFLICT (username) DO UPDATE
                SET is_verified = TRUE,
                    email       = EXCLUDED.email,
                    created_at  = EXCLUDED.created_at
            """,
            (username, hashed_password, email, registration_time),
        )
        del st.session_state.pending_registration
        # Store username in session so main1.py can call log_user_action immediately
        st.session_state["username"] = username
        return True, "✅ Registration completed! You can now login."
    except psycopg2.errors.UniqueViolation as e:
        err = str(e)
        if 'username' in err:
            return False, "🚫 Username already exists."
        elif 'email' in err:
            return False, "🚫 Email already exists."
        return False, "🚫 Registration failed. Username or email already exists."
    except Exception as e:
        return False, f"❌ Database error: {e}"


# ── Authentication ────────────────────────────────────────────────────────────

def verify_user(username_or_email, password):
    """
    Verify credentials and return (True, api_key) or (False, None).

    FIX: SELECT now includes is_verified so we can reject users who completed
         the form but never verified their OTP (e.g., closed the browser).
         Original SELECT omitted this column because it didn't exist in the DDL.

    FIX: Login is rejected (returns False, None) when is_verified is FALSE,
         preventing unverified accounts from accessing the application.
    """
    if '@' in username_or_email:
        sql = "SELECT username, password, groq_api_key, is_verified FROM users WHERE email = %s"
    else:
        sql = "SELECT username, password, groq_api_key, is_verified FROM users WHERE username = %s"

    row = _execute(sql, (username_or_email,), fetch="one")
    if row:
        actual_username = row["username"]
        stored_hashed   = row["password"]
        stored_key      = row["groq_api_key"]
        # FIX: is_verified now exists in the DB and is fetched here.
        #      .get() with default False is a safe fallback during rollout.
        is_verified     = row.get("is_verified", False)

        if bcrypt.checkpw(password.encode('utf-8'), stored_hashed.encode('utf-8')):
            # FIX: block login for unverified accounts
            if not is_verified:
                return False, None
            st.session_state.username      = actual_username
            st.session_state.user_groq_key = stored_key or ""
            return True, stored_key
    return False, None


# ── API key management ────────────────────────────────────────────────────────

def save_user_api_key(username, api_key):
    _execute(
        "UPDATE users SET groq_api_key = %s WHERE username = %s",
        (api_key, username),
    )
    st.session_state.user_groq_key = api_key


def get_user_api_key(username):
    row = _execute(
        "SELECT groq_api_key FROM users WHERE username = %s", (username,), fetch="one"
    )
    return row["groq_api_key"] if row and row["groq_api_key"] else None


# ── Logging ───────────────────────────────────────────────────────────────────

def log_user_action(username, action):
    """
    Insert a row into user_logs with a timezone-aware IST timestamp.

    FIX: Original code:
             timestamp = get_ist_time().strftime("%Y-%m-%d %H:%M:%S")
             INSERT INTO user_logs ... VALUES (%s, %s, %s)  -- stored as TEXT

         Problems:
           1. strftime() produces a naive string — timezone offset is lost.
           2. The column was typed TEXT, so no timezone arithmetic is possible.
           3. get_logins_today() then called DATE(text_column) which works
              only because Postgres silently casts ISO-8601 strings; any other
              format would produce wrong results or an error.

    Fixed:
      - Pass now_ist() directly (a timezone-aware datetime object).
      - psycopg2 serialises it as a TIMESTAMPTZ value with +05:30 offset.
      - user_logs.timestamp column is now TIMESTAMPTZ (fixed in DDL above).
    """
    _execute(
        "INSERT INTO user_logs (username, action, timestamp) VALUES (%s, %s, %s)",
        # FIX: now_ist() — TIMESTAMPTZ-compatible, IST offset preserved in DB.
        (username, action, now_ist()),
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_total_registered_users():
    row = _execute("SELECT COUNT(*) AS cnt FROM users", fetch="one")
    return row["cnt"] if row else 0


def get_logins_today():
    """
    Count login actions that occurred today in IST.

    FIX: Original used DATE(timestamp) on a TEXT column with a pytz-formatted
         string date for comparison — fragile and timezone-incorrect.

         Now that user_logs.timestamp is TIMESTAMPTZ:
           - AT TIME ZONE 'Asia/Kolkata' converts the stored UTC offset to IST
             before extracting the date, so the day boundary is always IST midnight.
           - today_ist() generates today's IST date in Python, consistent with
             how timestamps are written (now_ist()).
    """
    row = _execute(
        """
        SELECT COUNT(*) AS cnt FROM user_logs
        WHERE action = 'login'
          AND DATE(timestamp AT TIME ZONE 'Asia/Kolkata') = %s
        """,
        # FIX: was get_ist_time().strftime('%Y-%m-%d') — now today_ist()
        (today_ist(),),
        fetch="one",
    )
    return row["cnt"] if row else 0


def get_all_user_logs():
    rows = _execute(
        "SELECT username, action, timestamp FROM user_logs ORDER BY timestamp DESC",
        fetch="all",
    )
    return [(r["username"], r["action"], r["timestamp"]) for r in (rows or [])]


# ── Forgot password ───────────────────────────────────────────────────────────

def get_user_by_email(email):
    row = _execute(
        "SELECT username FROM users WHERE email = %s", (email,), fetch="one"
    )
    return row["username"] if row else None


def update_password_by_email(email, new_password):
    """
    FIX: Original except block called _conn().rollback().
         _conn() is a function — invoking it inside except may return a
         different connection object than the `conn` already in use, which
         means the rollback targets the wrong connection and the failed
         transaction stays open.

         Fixed by capturing conn = _conn() once before the try block and
         reusing that same reference in the except clause.
    """
    if not is_strong_password(new_password):
        st.error("Password must be at least 8 characters long and include uppercase, lowercase, number, and special character.")
        return False

    hashed_password = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    # FIX: capture conn once — reused in except for rollback
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password = %s WHERE email = %s",
                (hashed_password, email),
            )
            updated = cur.rowcount
        conn.commit()
        return updated > 0
    except Exception as e:
        # FIX: was _conn().rollback() — now correctly rolls back the same conn
        conn.rollback()
        st.error(f"Database error: {e}")
        return False
