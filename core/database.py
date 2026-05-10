"""
core/database.py - Database connection and schema initialisation.
Uses psycopg (v3) with optional ConnectionPool.
"""
import os
import json as _json
import hashlib

import psycopg
from psycopg.rows import dict_row

try:
    from psycopg_pool import ConnectionPool as _ConnectionPool
    _pool_available = True
except ImportError:
    _pool_available = False

_raw_db_url = os.environ.get('DATABASE_URL', '')
DATABASE_URL = (
    _raw_db_url.replace('postgres://', 'postgresql://', 1)
    if _raw_db_url.startswith('postgres://')
    else _raw_db_url
)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

print(f"[startup] DATABASE_URL prefix: {DATABASE_URL[:20] if DATABASE_URL else 'NOT SET'}")

# Optional connection pool (psycopg[pool])
_pool = None

def _init_pool():
    global _pool
    if _pool_available and DATABASE_URL:
        try:
            _pool = _ConnectionPool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                kwargs={'row_factory': dict_row},
            )
            print("[OK] Connection pool initialised")
        except Exception as e:
            print(f"[WARN] Pool init failed, falling back to direct connect: {e}")


def get_db():
    """Return a psycopg connection.  Uses pool when available."""
    if _pool is not None:
        return _pool.connection()
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def init_db():
    if not DATABASE_URL:
        print("[WARNING] DATABASE_URL not set — skipping init_db()")
        return
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_staff (
                    id              SERIAL PRIMARY KEY,
                    name            TEXT NOT NULL UNIQUE,
                    username        TEXT UNIQUE,
                    password_hash   TEXT DEFAULT '',
                    role            TEXT DEFAULT '',
                    active          BOOLEAN DEFAULT TRUE,
                    employee_code   TEXT DEFAULT '',
                    department      TEXT DEFAULT '',
                    position_title  TEXT DEFAULT '',
                    hire_date       DATE,
                    birth_date      DATE,
                    base_salary     NUMERIC(12,2) DEFAULT 0,
                    insured_salary  NUMERIC(12,2) DEFAULT 0,
                    daily_hours     NUMERIC(4,1) DEFAULT 8,
                    ot_rate1        NUMERIC(4,2) DEFAULT 1.33,
                    ot_rate2        NUMERIC(4,2) DEFAULT 1.67,
                    salary_type     TEXT DEFAULT 'monthly',
                    hourly_rate     NUMERIC(12,2) DEFAULT 0,
                    vacation_quota  INT DEFAULT NULL,
                    salary_notes    TEXT DEFAULT '',
                    line_user_id    TEXT,
                    bind_code       TEXT,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_records (
                    id            SERIAL PRIMARY KEY,
                    staff_id      INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    punch_type    TEXT NOT NULL,
                    punched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    note          TEXT DEFAULT '',
                    is_manual     BOOLEAN DEFAULT FALSE,
                    manual_by     TEXT DEFAULT '',
                    latitude      NUMERIC(10,6),
                    longitude     NUMERIC(10,6),
                    gps_distance  INT,
                    location_name TEXT DEFAULT '',
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_locations (
                    id            SERIAL PRIMARY KEY,
                    location_name TEXT NOT NULL DEFAULT '打卡地點',
                    lat           NUMERIC(10,6) NOT NULL,
                    lng           NUMERIC(10,6) NOT NULL,
                    radius_m      INT DEFAULT 100,
                    active        BOOLEAN DEFAULT TRUE,
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_config (
                    id           INT PRIMARY KEY DEFAULT 1,
                    gps_required BOOLEAN DEFAULT FALSE,
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                INSERT INTO punch_config (id, gps_required)
                VALUES (1, FALSE)
                ON CONFLICT (id) DO NOTHING
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS line_punch_config (
                    id                   INT PRIMARY KEY DEFAULT 1,
                    channel_access_token TEXT DEFAULT '',
                    channel_secret       TEXT DEFAULT '',
                    enabled              BOOLEAN DEFAULT FALSE,
                    updated_at           TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                INSERT INTO line_punch_config (id)
                VALUES (1)
                ON CONFLICT (id) DO NOTHING
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schedule_config (
                    month           TEXT PRIMARY KEY,
                    max_off_per_day INT DEFAULT 2,
                    vacation_quota  INT DEFAULT 8,
                    notes           TEXT DEFAULT '',
                    updated_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schedule_requests (
                    id           SERIAL PRIMARY KEY,
                    staff_id     INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    month        TEXT NOT NULL,
                    dates        JSONB NOT NULL DEFAULT '[]',
                    status       TEXT DEFAULT 'pending',
                    submit_note  TEXT DEFAULT '',
                    reviewed_by  TEXT DEFAULT '',
                    reviewed_at  TIMESTAMPTZ,
                    review_note  TEXT DEFAULT '',
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(staff_id, month)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS punch_requests (
                    id            SERIAL PRIMARY KEY,
                    staff_id      INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    punch_type    TEXT NOT NULL,
                    requested_at  TIMESTAMPTZ NOT NULL,
                    reason        TEXT DEFAULT '',
                    status        TEXT DEFAULT 'pending',
                    reviewed_by   TEXT DEFAULT '',
                    review_note   TEXT DEFAULT '',
                    reviewed_at   TIMESTAMPTZ,
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shift_types (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL,
                    start_time  TIME NOT NULL,
                    end_time    TIME NOT NULL,
                    color       TEXT DEFAULT '#4a7bda',
                    departments TEXT DEFAULT '',
                    active      BOOLEAN DEFAULT TRUE,
                    sort_order  INT DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shift_assignments (
                    id            SERIAL PRIMARY KEY,
                    staff_id      INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    shift_type_id INT REFERENCES shift_types(id) ON DELETE CASCADE,
                    shift_date    DATE NOT NULL,
                    note          TEXT DEFAULT '',
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(staff_id, shift_date)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS overtime_requests (
                    id              SERIAL PRIMARY KEY,
                    staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    request_date    DATE NOT NULL,
                    start_time      TIME NOT NULL,
                    end_time        TIME NOT NULL,
                    ot_hours        NUMERIC(5,2),
                    reason          TEXT DEFAULT '',
                    status          TEXT DEFAULT 'pending',
                    reviewed_by     TEXT DEFAULT '',
                    review_note     TEXT DEFAULT '',
                    ot_pay          NUMERIC(12,2) DEFAULT 0,
                    day_type        TEXT DEFAULT 'weekday',
                    reviewed_at     TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Seed default shifts if empty
            existing_shifts = conn.execute("SELECT COUNT(*) as cnt FROM shift_types").fetchone()
            if existing_shifts['cnt'] == 0:
                defaults = [
                    ('吧台班',  '08:00', '16:00', '#8b5cf6', '吧台', 1),
                    ('外場A班', '09:00', '17:00', '#2e9e6b', '外場', 2),
                    ('外場B班', '14:00', '22:00', '#0ea5e9', '外場', 3),
                    ('廚房A班', '08:00', '16:00', '#e07b2a', '廚房', 4),
                    ('廚房B班', '12:00', '20:00', '#d64242', '廚房', 5),
                ]
                for name, st, et, color, dept, sort in defaults:
                    conn.execute(
                        "INSERT INTO shift_types (name,start_time,end_time,color,departments,sort_order) VALUES (%s,%s,%s,%s,%s,%s)",
                        (name, st, et, color, dept, sort)
                    )

        print("[OK] Database tables created")
    except Exception as e:
        print(f"[ERROR] init_db failed: {e}")
        raise

    # Schema migrations (each in its own connection to avoid transaction abort)
    migrations = [
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS username TEXT",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS password_hash TEXT DEFAULT ''",
        "ALTER TABLE punch_records ADD COLUMN IF NOT EXISTS latitude NUMERIC(10,6)",
        "ALTER TABLE punch_records ADD COLUMN IF NOT EXISTS longitude NUMERIC(10,6)",
        "ALTER TABLE punch_records ADD COLUMN IF NOT EXISTS gps_distance INT",
        "ALTER TABLE punch_records ADD COLUMN IF NOT EXISTS location_name TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS line_user_id TEXT",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bind_code TEXT",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS employee_code TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS department TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS position_title TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS hire_date DATE",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS birth_date DATE",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS base_salary NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS insured_salary NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_notes TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS daily_hours NUMERIC(4,1) DEFAULT 8",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS ot_rate1 NUMERIC(4,2) DEFAULT 1.33",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS ot_rate2 NUMERIC(4,2) DEFAULT 1.67",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS ot_rate3 NUMERIC(4,2) DEFAULT 2.0",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS document_id INT REFERENCES finance_documents(id) ON DELETE SET NULL",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS start_time TEXT DEFAULT ''",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS end_time TEXT DEFAULT ''",
        "ALTER TABLE finance_documents ADD COLUMN IF NOT EXISTS image_data TEXT",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_type TEXT DEFAULT 'monthly'",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS hourly_rate NUMERIC(12,2) DEFAULT 0",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS vacation_quota INT DEFAULT NULL",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_code TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_name TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_branch TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS bank_account TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS account_holder TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS password_plain TEXT DEFAULT ''",
        "ALTER TABLE overtime_requests ADD COLUMN IF NOT EXISTS day_type TEXT DEFAULT 'weekday'",
        "ALTER TABLE overtime_requests ALTER COLUMN start_time DROP NOT NULL",
        "ALTER TABLE overtime_requests ALTER COLUMN end_time DROP NOT NULL",
        # 員工個人/保險欄位
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS national_id TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS gender TEXT DEFAULT ''",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS insurance_type TEXT DEFAULT 'regular'",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS address TEXT DEFAULT ''",
        # 多店
        """CREATE TABLE IF NOT EXISTS stores (
            id         SERIAL PRIMARY KEY,
            name       TEXT NOT NULL,
            code       TEXT UNIQUE,
            address    TEXT DEFAULT '',
            active     BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS store_id INT REFERENCES stores(id) ON DELETE SET NULL",
        "ALTER TABLE punch_locations ADD COLUMN IF NOT EXISTS store_id INT REFERENCES stores(id) ON DELETE SET NULL",
        "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS store_ids JSONB DEFAULT '[]'",
        "ALTER TABLE schedule_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
        """CREATE TABLE IF NOT EXISTS shift_staffing_requirements (
            id            SERIAL PRIMARY KEY,
            shift_type_id INT REFERENCES shift_types(id) ON DELETE CASCADE,
            day_of_week   SMALLINT NOT NULL,
            required_count INT NOT NULL DEFAULT 1,
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            updated_at    TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(shift_type_id, day_of_week)
        )""",
        """CREATE TABLE IF NOT EXISTS admin_accounts (
            id              SERIAL PRIMARY KEY,
            username        TEXT NOT NULL UNIQUE,
            password_hash   TEXT NOT NULL,
            display_name    TEXT DEFAULT '',
            permissions     JSONB DEFAULT '[]',
            is_super        BOOLEAN DEFAULT FALSE,
            active          BOOLEAN DEFAULT TRUE,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            last_login_at   TIMESTAMPTZ
        )""",
        # leave tables
        """CREATE TABLE IF NOT EXISTS leave_types (
            id          SERIAL PRIMARY KEY,
            code        TEXT UNIQUE,
            name        TEXT NOT NULL,
            pay_rate    NUMERIC(4,2) DEFAULT 1.0,
            max_days    NUMERIC(5,1),
            carry_over  BOOLEAN DEFAULT FALSE,
            active      BOOLEAN DEFAULT TRUE,
            color       TEXT DEFAULT '#4a7bda',
            description TEXT DEFAULT '',
            sort_order  INT DEFAULT 0,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        "ALTER TABLE leave_types ADD COLUMN IF NOT EXISTS pay_rate    NUMERIC(4,2) DEFAULT 1.0",
        "ALTER TABLE leave_types ADD COLUMN IF NOT EXISTS color       TEXT DEFAULT '#4a7bda'",
        "ALTER TABLE leave_types ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS leave_requests (
            id             SERIAL PRIMARY KEY,
            staff_id       INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            leave_type_id  INT REFERENCES leave_types(id),
            start_date     DATE NOT NULL,
            end_date       DATE NOT NULL,
            total_days     NUMERIC(5,1) DEFAULT 1,
            start_half     BOOLEAN DEFAULT FALSE,
            end_half       BOOLEAN DEFAULT FALSE,
            reason         TEXT DEFAULT '',
            status         TEXT DEFAULT 'pending',
            reviewed_by    TEXT DEFAULT '',
            review_note    TEXT DEFAULT '',
            reviewed_at    TIMESTAMPTZ,
            start_time     TEXT DEFAULT '',
            end_time       TEXT DEFAULT '',
            document_id    INT,
            created_at     TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS leave_balances (
            id             SERIAL PRIMARY KEY,
            staff_id       INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            leave_type_id  INT REFERENCES leave_types(id),
            year           INT NOT NULL,
            total_days     NUMERIC(5,1) DEFAULT 0,
            used_days      NUMERIC(5,1) DEFAULT 0,
            updated_at     TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(staff_id, leave_type_id, year)
        )""",
        # salary tables
        """CREATE TABLE IF NOT EXISTS salary_items (
            id          SERIAL PRIMARY KEY,
            code        TEXT UNIQUE,
            name        TEXT NOT NULL,
            item_type   TEXT DEFAULT 'allowance',
            formula     TEXT DEFAULT '',
            amount      NUMERIC(12,2) DEFAULT 0,
            taxable     BOOLEAN DEFAULT TRUE,
            sort_order  INT DEFAULT 0,
            active      BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS salary_records (
            id               SERIAL PRIMARY KEY,
            staff_id         INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            month            TEXT NOT NULL,
            base_salary      NUMERIC(12,2) DEFAULT 0,
            insured_salary   NUMERIC(12,2) DEFAULT 0,
            work_days        INT DEFAULT 0,
            actual_days      NUMERIC(5,1) DEFAULT 0,
            leave_days       NUMERIC(5,1) DEFAULT 0,
            unpaid_days      NUMERIC(5,1) DEFAULT 0,
            ot_pay           NUMERIC(12,2) DEFAULT 0,
            allowance_total  NUMERIC(12,2) DEFAULT 0,
            deduction_total  NUMERIC(12,2) DEFAULT 0,
            net_pay          NUMERIC(12,2) DEFAULT 0,
            items            JSONB DEFAULT '[]',
            status           TEXT DEFAULT 'draft',
            confirmed_at     TIMESTAMPTZ,
            updated_at       TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(staff_id, month)
        )""",
        """CREATE TABLE IF NOT EXISTS salary_staff_items (
            id           SERIAL PRIMARY KEY,
            staff_id     INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            item_id      INT REFERENCES salary_items(id) ON DELETE CASCADE,
            amount       NUMERIC(12,2),
            formula      TEXT,
            active       BOOLEAN DEFAULT TRUE,
            updated_at   TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(staff_id, item_id)
        )""",
        # announcement + holiday tables
        """CREATE TABLE IF NOT EXISTS announcements (
            id          SERIAL PRIMARY KEY,
            title       TEXT NOT NULL,
            content     TEXT DEFAULT '',
            author      TEXT DEFAULT '',
            pinned      BOOLEAN DEFAULT FALSE,
            active      BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS holidays (
            id          SERIAL PRIMARY KEY,
            holiday_date DATE NOT NULL UNIQUE,
            name         TEXT DEFAULT '',
            created_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
        # finance tables
        """CREATE TABLE IF NOT EXISTS finance_categories (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            cat_type    TEXT DEFAULT 'expense',
            active      BOOLEAN DEFAULT TRUE,
            sort_order  INT DEFAULT 0,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_records (
            id            SERIAL PRIMARY KEY,
            record_date   DATE NOT NULL,
            cat_id        INT REFERENCES finance_categories(id),
            amount        NUMERIC(15,2) NOT NULL,
            description   TEXT DEFAULT '',
            store_id      INT REFERENCES stores(id) ON DELETE SET NULL,
            document_id   INT,
            staff_id      INT REFERENCES punch_staff(id) ON DELETE SET NULL,
            created_by    TEXT DEFAULT '',
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            updated_at    TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_documents (
            id            SERIAL PRIMARY KEY,
            doc_date      DATE NOT NULL,
            doc_type      TEXT DEFAULT 'receipt',
            vendor        TEXT DEFAULT '',
            amount        NUMERIC(15,2) DEFAULT 0,
            description   TEXT DEFAULT '',
            file_path     TEXT DEFAULT '',
            image_data    TEXT,
            store_id      INT REFERENCES stores(id) ON DELETE SET NULL,
            created_by    TEXT DEFAULT '',
            created_at    TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_recurring (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            cat_id      INT REFERENCES finance_categories(id),
            amount      NUMERIC(15,2) NOT NULL,
            freq        TEXT DEFAULT 'monthly',
            day_of_month INT DEFAULT 1,
            active      BOOLEAN DEFAULT TRUE,
            last_run    DATE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS bank_statements (
            id            SERIAL PRIMARY KEY,
            statement_date DATE NOT NULL,
            bank_name     TEXT DEFAULT '',
            account_no    TEXT DEFAULT '',
            amount        NUMERIC(15,2) DEFAULT 0,
            balance       NUMERIC(15,2) DEFAULT 0,
            description   TEXT DEFAULT '',
            matched       BOOLEAN DEFAULT FALSE,
            created_at    TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_payables (
            id            SERIAL PRIMARY KEY,
            vendor        TEXT NOT NULL,
            due_date      DATE,
            amount        NUMERIC(15,2) DEFAULT 0,
            paid_amount   NUMERIC(15,2) DEFAULT 0,
            status        TEXT DEFAULT 'unpaid',
            description   TEXT DEFAULT '',
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            updated_at    TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_budgets (
            id            SERIAL PRIMARY KEY,
            month         TEXT NOT NULL,
            cat_id        INT REFERENCES finance_categories(id),
            budget_amount NUMERIC(15,2) DEFAULT 0,
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            updated_at    TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(month, cat_id)
        )""",
        # finance settings + insurance
        """CREATE TABLE IF NOT EXISTS finance_settings (
            id           INT PRIMARY KEY DEFAULT 1,
            tax_id       TEXT DEFAULT '',
            company_name TEXT DEFAULT '',
            settings     JSONB DEFAULT '{}',
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
        """INSERT INTO finance_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING""",
        """CREATE TABLE IF NOT EXISTS insurance_settings (
            id              INT PRIMARY KEY DEFAULT 1,
            labor_rate      NUMERIC(6,4) DEFAULT 0.105,
            health_rate     NUMERIC(6,4) DEFAULT 0.0517,
            employer_ratio  NUMERIC(6,4) DEFAULT 0.6,
            settings        JSONB DEFAULT '{}',
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """INSERT INTO insurance_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING""",
        # training
        """CREATE TABLE IF NOT EXISTS training_records (
            id          SERIAL PRIMARY KEY,
            staff_id    INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            train_date  DATE,
            hours       NUMERIC(5,1) DEFAULT 0,
            provider    TEXT DEFAULT '',
            score       NUMERIC(5,1),
            status      TEXT DEFAULT 'completed',
            notes       TEXT DEFAULT '',
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        # expense
        """CREATE TABLE IF NOT EXISTS expense_requests (
            id           SERIAL PRIMARY KEY,
            staff_id     INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            request_date DATE NOT NULL,
            cat_id       INT REFERENCES finance_categories(id),
            amount       NUMERIC(12,2) NOT NULL,
            description  TEXT DEFAULT '',
            receipt_data TEXT,
            status       TEXT DEFAULT 'pending',
            reviewed_by  TEXT DEFAULT '',
            review_note  TEXT DEFAULT '',
            reviewed_at  TIMESTAMPTZ,
            created_at   TIMESTAMPTZ DEFAULT NOW()
        )""",
        # performance
        """CREATE TABLE IF NOT EXISTS performance_templates (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            items       JSONB DEFAULT '[]',
            active      BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS performance_reviews (
            id            SERIAL PRIMARY KEY,
            staff_id      INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            template_id   INT REFERENCES performance_templates(id),
            period_label  TEXT DEFAULT '',
            scores        JSONB DEFAULT '{}',
            total_score   NUMERIC(8,2) DEFAULT 0,
            max_score     NUMERIC(8,2) DEFAULT 0,
            grade         TEXT DEFAULT '',
            comments      TEXT DEFAULT '',
            salary_adjusted BOOLEAN DEFAULT FALSE,
            salary_delta  NUMERIC(12,2) DEFAULT 0,
            reviewed_by   TEXT DEFAULT '',
            reviewed_at   TIMESTAMPTZ DEFAULT NOW(),
            created_at    TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS performance_grade_config (
            id          INT PRIMARY KEY DEFAULT 1,
            grades      JSONB DEFAULT '{}',
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """INSERT INTO performance_grade_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING""",
        # webauthn
        """CREATE TABLE IF NOT EXISTS webauthn_credentials (
            id            SERIAL PRIMARY KEY,
            user_key      TEXT NOT NULL,
            credential_id TEXT NOT NULL UNIQUE,
            public_key    BYTEA NOT NULL,
            sign_count    BIGINT DEFAULT 0,
            device_name   TEXT DEFAULT '',
            created_at    TIMESTAMPTZ DEFAULT NOW()
        )""",
        # ── 效能索引 ──────────────────────────────────────────────────────────
        "CREATE INDEX IF NOT EXISTS idx_pr_staff_at    ON punch_records(staff_id, punched_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pr_at           ON punch_records(punched_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_lr_staff_date   ON leave_requests(staff_id, start_date)",
        "CREATE INDEX IF NOT EXISTS idx_lr_status       ON leave_requests(status)",
        "CREATE INDEX IF NOT EXISTS idx_ot_staff_date   ON overtime_requests(staff_id, request_date)",
        "CREATE INDEX IF NOT EXISTS idx_ot_status       ON overtime_requests(status)",
        "CREATE INDEX IF NOT EXISTS idx_sr_month        ON salary_records(month)",
        "CREATE INDEX IF NOT EXISTS idx_ps_line_uid     ON punch_staff(line_user_id) WHERE line_user_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_pq_staff_status ON punch_requests(staff_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_sa_date         ON shift_assignments(shift_date)",
        "CREATE INDEX IF NOT EXISTS idx_fr_date         ON finance_records(record_date)",
        "CREATE INDEX IF NOT EXISTS idx_schedr_month    ON schedule_requests(month)",
        "CREATE INDEX IF NOT EXISTS idx_lb_staff_year   ON leave_balances(staff_id, year)",
        # ── 補欄位 migrations ─────────────────────────────────────────────────────
        "ALTER TABLE announcements   ADD COLUMN IF NOT EXISTS category   TEXT DEFAULT 'general'",
        "ALTER TABLE announcements   ADD COLUMN IF NOT EXISTS priority   TEXT DEFAULT 'normal'",
        "ALTER TABLE announcements   ADD COLUMN IF NOT EXISTS visible_to TEXT DEFAULT 'all'",
        "ALTER TABLE announcements   ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
        "ALTER TABLE announcements   ADD COLUMN IF NOT EXISTS view_count INT DEFAULT 0",
        "ALTER TABLE holidays        ADD COLUMN IF NOT EXISTS holiday_type TEXT DEFAULT 'national'",
        "ALTER TABLE holidays        ADD COLUMN IF NOT EXISTS note         TEXT DEFAULT ''",
        "ALTER TABLE leave_requests  ADD COLUMN IF NOT EXISTS substitute_name TEXT DEFAULT ''",
        "ALTER TABLE leave_requests  ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMPTZ DEFAULT NOW()",
        "ALTER TABLE leave_balances  ADD COLUMN IF NOT EXISTS note        TEXT DEFAULT ''",
        "ALTER TABLE salary_items    ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''",
        "ALTER TABLE salary_items    ADD COLUMN IF NOT EXISTS color       TEXT DEFAULT '#4a7bda'",
        "ALTER TABLE salary_records  ADD COLUMN IF NOT EXISTS note        TEXT DEFAULT ''",
        "ALTER TABLE salary_records  ADD COLUMN IF NOT EXISTS confirmed_by TEXT DEFAULT ''",
        "ALTER TABLE punch_staff     ADD COLUMN IF NOT EXISTS salary_item_ids       JSONB",
        "ALTER TABLE punch_staff     ADD COLUMN IF NOT EXISTS salary_item_overrides JSONB",
    ]
    for sql in migrations:
        try:
            with get_db() as mc:
                mc.execute(sql)
        except Exception as me:
            print(f"[MIGRATION SKIP] {sql[:70]}: {me}")

    # Seed default super admin; always sync password from ADMIN_PASSWORD env var
    try:
        all_modules = _json.dumps(['punch', 'sched', 'leave', 'salary', 'ann', 'holiday', 'finance'])
        pw_hash = _hash_pw(ADMIN_PASSWORD)
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM admin_accounts WHERE username='admin'"
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE admin_accounts SET password_hash=%s, is_super=TRUE WHERE username='admin'",
                    (pw_hash,)
                )
                print("[OK] admin password synced from ADMIN_PASSWORD env var")
            else:
                conn.execute("""
                    INSERT INTO admin_accounts (username, password_hash, display_name, permissions, is_super)
                    VALUES (%s,%s,'超級管理員',%s,TRUE)
                """, ('admin', pw_hash, all_modules))
                print("[OK] Default super admin seeded (username: admin)")
    except Exception as e:
        print(f"[WARN] admin seed: {e}")

    # Seed default leave types
    try:
        with get_db() as conn:
            existing = conn.execute("SELECT COUNT(*) AS cnt FROM leave_types").fetchone()
            if existing['cnt'] == 0:
                defaults = [
                    ('annual',     '特休假',   None,  True,  1),
                    ('sick',       '病假',     30.0,  False, 2),
                    ('personal',   '事假',     14.0,  False, 3),
                    ('menstrual',  '生理假',   1.0,   False, 4),
                    ('marriage',   '婚假',     8.0,   False, 5),
                    ('funeral',    '喪假',     None,  False, 6),
                    ('maternity',  '產假',     None,  False, 7),
                    ('paternity',  '陪產假',   None,  False, 8),
                    ('official',   '公假',     None,  False, 9),
                    ('compensatory','補休',    None,  False, 10),
                ]
                for code, name, max_d, carry, sort in defaults:
                    conn.execute(
                        "INSERT INTO leave_types (code, name, max_days, carry_over, sort_order) VALUES (%s,%s,%s,%s,%s)",
                        (code, name, max_d, carry, sort)
                    )
                print("[OK] Default leave types seeded")
    except Exception as e:
        print(f"[WARN] leave type seed: {e}")

    # 確保預設店家存在，並補齊舊資料
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO stores (name, code) VALUES ('主店','main') ON CONFLICT (code) DO NOTHING")
            conn.execute("UPDATE punch_staff     SET store_id=(SELECT id FROM stores WHERE code='main') WHERE store_id IS NULL")
            conn.execute("UPDATE punch_locations SET store_id=(SELECT id FROM stores WHERE code='main') WHERE store_id IS NULL")
    except Exception as e:
        print(f"[WARN] store seed: {e}")

    print("[OK] Database initialised")
