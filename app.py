import hashlib
import math
import os
import secrets
import threading
import time
import traceback
import urllib.request
from datetime import date
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import (
    Flask, request, jsonify, render_template,
    session, redirect, url_for, abort
)
from linebot import LineBotApi
from linebot.models import TextSendMessage

from config import (
    SECRET_KEY, ADMIN_PASSWORD, ANTHROPIC_API_KEY,
    DATABASE_URL, RENDER_EXTERNAL_URL, TW_TZ, WEEKDAY_ZH,
)
from utils import (
    _month_ts_range, _month_date_range, _hash_pw, _gps_distance,
    _parse_tw_datetime, _calc_service_years, _eval_formula,
    _roc_date, _roc_year, _month_last_day, _b64url_encode, _b64url_decode,
)
from db import get_db, _init_pool
from serializers import (
    _admin_row, punch_staff_row, punch_record_row, loc_row, punch_req_row,
    ot_req_row, shift_type_row, shift_assign_row, sched_req_row, leave_type_row,
    leave_req_row, leave_balance_row, salary_item_row, salary_record_row, ann_row,
    holiday_row, _finance_cat_row, _finance_rec_row, _recurring_row, _bank_row,
    _payable_row, _expense_row, _perf_template_row, _perf_review_row,
)
from auth import login_required, require_module, require_super
from services import (
    get_line_punch_config, _send_line_punch, _send_line_with_quick_reply,
    _call_line_api, _notify_staff_line, _notify_review_result,
    _broadcast_announcement_line, _calc_punch_hours, _auto_generate_salary,
    _trigger_salary_regen_for_leave, _calc_annual_leave_days,
    _calc_annual_leave_schedule, _calc_leave_days, _get_staff_scheduled_dates,
    _update_leave_balance, _calc_ot_pay, _is_holiday,
    _get_grade_config, _grade_labels, _score_to_grade,
    _get_finance_settings,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ─── gzip 壓縮（縮小首屏與 JSON 傳輸量）────────────────────────────────────────
try:
    from flask_compress import Compress
    app.config['COMPRESS_MIMETYPES'] = [
        'text/html', 'text/css', 'text/javascript', 'application/javascript',
        'application/json', 'image/svg+xml',
    ]
    app.config['COMPRESS_LEVEL'] = 6
    app.config['COMPRESS_MIN_SIZE'] = 1024
    Compress(app)
    print("[OK] gzip compression enabled")
except ImportError:
    print("[WARN] flask_compress not installed - responses will not be gzipped")


@app.after_request
def _set_cache_headers(resp):
    """靜態資源給長快取；其餘預設不快取，避免後台資料被瀏覽器留存。"""
    if request.path.startswith('/static/'):
        resp.headers.setdefault('Cache-Control', 'public, max-age=86400')
    return resp

print(f"[startup] DATABASE_URL prefix: {DATABASE_URL[:20] if DATABASE_URL else 'NOT SET'}")

# LINE 推播一律用各設定自帶的 channel_access_token 即時建立 LineBotApi(...),
# 故此處不再建立模組級 client(原 line_bot_api/handler 為未使用的死碼)。

# ─── Imports ──────────────────────────────────────────────────────────────────
import json as _json
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

import calendar as _calendar
from datetime import date as _date_cls

# 月份/時間區間、GPS、雜湊、公式等純工具函式已抽至 utils.py

# ─── PostgreSQL ───────────────────────────────────────────────────────────────
# 連線池與 get_db 已抽至 db.py

def init_db():
    if not DATABASE_URL:
        print("[WARNING] DATABASE_URL not set - skipping init_db()")
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
        # leave_requests / finance_documents 的跨表欄位移至 _apply_late_migrations()
        # (該些表於 init_db 之後才建立,放這裡冷啟動首次部署會被略過)
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
        # admin_accounts.store_ids 移至 _apply_late_migrations()(其 CREATE 在本清單後段)
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
        # ── 效能索引 ──────────────────────────────────────────────────────────
        # punch_records: 最常被查詢的資料表,依 staff_id + punched_at 過濾
        "CREATE INDEX IF NOT EXISTS idx_pr_staff_at   ON punch_records(staff_id, punched_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pr_at          ON punch_records(punched_at DESC)",
        # overtime_requests
        "CREATE INDEX IF NOT EXISTS idx_ot_staff_date  ON overtime_requests(staff_id, request_date)",
        "CREATE INDEX IF NOT EXISTS idx_ot_status      ON overtime_requests(status)",
        # punch_staff: LINE bot 依 line_user_id 查詢
        "CREATE INDEX IF NOT EXISTS idx_ps_line_uid    ON punch_staff(line_user_id) WHERE line_user_id IS NOT NULL",
        # punch_requests
        "CREATE INDEX IF NOT EXISTS idx_pq_staff_status ON punch_requests(staff_id, status)",
        # shift_assignments: 月份整表查詢用 shift_date;薪資/請假的每員工查詢用複合索引
        "CREATE INDEX IF NOT EXISTS idx_sa_date        ON shift_assignments(shift_date)",
        "CREATE INDEX IF NOT EXISTS idx_sa_staff_date  ON shift_assignments(staff_id, shift_date)",
        # schedule_requests: 月份查詢
        "CREATE INDEX IF NOT EXISTS idx_schedr_month   ON schedule_requests(month)",
        # 註:leave_requests / salary_records / finance_records / leave_balances 的索引
        #    移至各自的 init_*_db(),因該些表在 init_db 之後才建立(冷啟動順序)。
    ]
    for sql in migrations:
        try:
            with get_db() as mc:
                mc.execute(sql)
        except Exception as me:
            print(f"[MIGRATION SKIP] {sql[:70]}: {me}")

    # Seed default super admin; always sync password from ADMIN_PASSWORD env var
    try:
        all_modules = _json.dumps(['punch','sched','leave','salary','ann','holiday','finance'])
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

    # 確保預設店家存在,並補齊舊資料
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO stores (name, code) VALUES ('主店','main') ON CONFLICT (code) DO NOTHING")
            conn.execute("UPDATE punch_staff     SET store_id=(SELECT id FROM stores WHERE code='main') WHERE store_id IS NULL")
            conn.execute("UPDATE punch_locations SET store_id=(SELECT id FROM stores WHERE code='main') WHERE store_id IS NULL")
    except Exception as e:
        print(f"[WARN] store seed: {e}")

    print("[OK] Database initialised")


_init_pool()
init_db()

# ─── Keep-Alive ───────────────────────────────────────────────────────────────

def keep_alive():
    time.sleep(10)
    while True:
        try:
            base = RENDER_EXTERNAL_URL.rstrip('/') if RENDER_EXTERNAL_URL else 'http://localhost:5000'
            urllib.request.urlopen(
                urllib.request.Request(f'{base}/health', headers={'User-Agent': 'KeepAlive/1.0'}),
                timeout=10
            )
        except Exception as e:
            print(f"[keep-alive] ping failed: {e}")
        time.sleep(14 * 60)

threading.Thread(target=keep_alive, daemon=True).start()


# ─── 特休自動同步 ─────────────────────────────────────────────────────────────

def _run_annual_leave_sync():
    """依勞基法第38條,依到職日計算特休天數,寫入 leave_balances.每日午夜自動執行."""
    from datetime import date as _d_sync
    year = str(_d_sync.today().year)
    try:
        with get_db() as conn:
            # 多 worker 防重:取得 advisory lock,拿不到就跳過
            got_lock = conn.execute("SELECT pg_try_advisory_lock(1001)").fetchone()[0]
            if not got_lock:
                return
            try:
                staff_list = conn.execute(
                    "SELECT id, name, hire_date FROM punch_staff WHERE active=TRUE AND hire_date IS NOT NULL"
                ).fetchall()
                lt = conn.execute("SELECT id FROM leave_types WHERE code='annual'").fetchone()
                if not lt:
                    return
                lt_id = lt['id']
                for s in staff_list:
                    days = _calc_annual_leave_days(s['hire_date'])
                    conn.execute("""
                        INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
                        VALUES (%s,%s,%s,%s,0)
                        ON CONFLICT (staff_id, leave_type_id, year) DO UPDATE
                          SET total_days=EXCLUDED.total_days, updated_at=NOW()
                    """, (s['id'], lt_id, int(year), days))
            finally:
                conn.execute("SELECT pg_advisory_unlock(1001)")
    except Exception as e:
        print(f"[annual_leave_sync] {e}")


def _annual_leave_sync_loop():
    import time as _time_sync
    # 啟動時立即執行一次
    _run_annual_leave_sync()
    while True:
        # 計算距離明天 00:05 台北時間的秒數
        now = _dt.now(TW_TZ)
        tmr = (now + _td(days=1)).date()
        tomorrow_05 = _dt(tmr.year, tmr.month, tmr.day, 0, 5, tzinfo=TW_TZ)
        sleep_secs = (tomorrow_05 - now).total_seconds()
        if sleep_secs < 0:
            sleep_secs = 3600
        _time_sync.sleep(sleep_secs)
        _run_annual_leave_sync()


threading.Thread(target=_annual_leave_sync_loop, daemon=True).start()


# ─── 每月1日自動產生上月薪資 ──────────────────────────────────────────────────

def _run_monthly_salary_auto_generate():
    """每月1日 00:10(台北時間)自動產生所有在職員工上月薪資(draft)."""
    from datetime import date as _d_sal
    import json as _json_sal

    # 計算上月月份字串,例如今天是 2026-05-01 -> 上月為 2026-04
    today = _d_sal.today()
    if today.month == 1:
        last_month = f"{today.year - 1}-12"
    else:
        last_month = f"{today.year}-{today.month - 1:02d}"

    print(f"[salary_auto] 開始自動產生 {last_month} 薪資...")
    try:
        with get_db() as conn:
            # 多 worker 防重:取得 advisory lock,拿不到就跳過
            got_lock = conn.execute("SELECT pg_try_advisory_lock(1002)").fetchone()[0]
            if not got_lock:
                print(f"[salary_auto] 另一 worker 正在執行,略過.")
                return
            try:
              staff_list = conn.execute(
                "SELECT * FROM punch_staff WHERE active=TRUE"
              ).fetchall()
              generated = 0
              for staff in staff_list:
                data = _auto_generate_salary(conn, dict(staff), last_month)
                items_json = _json_sal.dumps(data['items'], ensure_ascii=False)
                conn.execute("""
                    INSERT INTO salary_records
                      (staff_id, month, base_salary, insured_salary, work_days, actual_days,
                       leave_days, unpaid_days, ot_pay, allowance_total, deduction_total,
                       net_pay, items, status, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'draft',NOW())
                    ON CONFLICT (staff_id, month) DO NOTHING
                """, (
                    data['staff_id'], last_month, data['base_salary'], data['insured_salary'],
                    data['work_days'], data['actual_days'], data['leave_days'], data['unpaid_days'],
                    data['ot_pay'], data['allowance_total'], data['deduction_total'],
                    data['net_pay'], items_json,
                ))
                generated += 1
              print(f"[salary_auto] {last_month} 薪資自動產生完成,共 {generated} 筆.")
            finally:
                conn.execute("SELECT pg_advisory_unlock(1002)")
    except Exception as e:
        print(f"[salary_auto] 產生失敗:{e}")


def _monthly_salary_auto_loop():
    import time as _time_sal
    # 等待 app 完全啟動後再進入迴圈
    _time_sal.sleep(30)
    while True:
        now = _dt.now(TW_TZ)
        # 只在每月1日執行
        if now.day == 1:
            target = _dt(now.year, now.month, 1, 0, 10, tzinfo=TW_TZ)
            if now >= target:
                _run_monthly_salary_auto_generate()
                # 執行完後等待 25 小時,避免同一天重複執行
                _time_sal.sleep(25 * 3600)
                continue
        # 計算下次執行時間:下個月1日 00:10
        if now.month == 12:
            next_run = _dt(now.year + 1, 1, 1, 0, 10, tzinfo=TW_TZ)
        else:
            next_run = _dt(now.year, now.month + 1, 1, 0, 10, tzinfo=TW_TZ)
        sleep_secs = (next_run - now).total_seconds()
        print(f"[salary_auto] 下次執行時間:{next_run.strftime('%Y-%m-%d %H:%M')}(台北時間),約 {sleep_secs/3600:.1f} 小時後")
        _time_sal.sleep(max(sleep_secs, 60))


threading.Thread(target=_monthly_salary_auto_loop, daemon=True).start()


# ─── Health ───────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    try:
        with get_db() as conn:
            conn.execute('SELECT 1')
        return jsonify({'status': 'ok', 'db': 'connected'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500

# ─── Admin Auth ───────────────────────────────────────────────────────────────

# login_required / require_module / require_super 已抽至 auth.py

@app.route('/')
def index():
    return redirect(url_for('admin_login'))

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not password:
            error = '請輸入帳號與密碼'
        else:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT * FROM admin_accounts WHERE username=%s AND active=TRUE",
                    (username,)
                ).fetchone()
            if row and row['password_hash'] == _hash_pw(password):
                perms = row['permissions']
                if isinstance(perms, str):
                    try: perms = _json.loads(perms)
                    except: perms = []
                session['logged_in']          = True
                session['admin_id']           = row['id']
                session['admin_username']     = row['username']
                session['admin_display_name'] = row['display_name'] or row['username']
                session['admin_permissions']  = perms
                session['admin_is_super']     = bool(row['is_super'])
                with get_db() as conn:
                    conn.execute("UPDATE admin_accounts SET last_login_at=NOW() WHERE id=%s", (row['id'],))
                return redirect(url_for('admin_dashboard'))
            error = '帳號或密碼錯誤'
    return render_template('login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

@app.route('/admin')
@app.route('/admin/')
@login_required
def admin_dashboard():
    perms    = session.get('admin_permissions') or []
    is_super = bool(session.get('admin_is_super'))
    return render_template('admin.html',
        admin_display_name=session.get('admin_display_name',''),
        admin_permissions=perms,
        admin_is_super=is_super,
    )

# ── Admin Accounts API ────────────────────────────────────────────────────────


@app.route('/api/admin/me', methods=['GET'])
@login_required
def api_admin_me():
    return jsonify({
        'id':           session.get('admin_id'),
        'username':     session.get('admin_username'),
        'display_name': session.get('admin_display_name'),
        'permissions':  session.get('admin_permissions') or [],
        'is_super':     bool(session.get('admin_is_super')),
    })

@app.route('/api/admin/accounts', methods=['GET'])
@require_super
def api_admin_accounts_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM admin_accounts ORDER BY id").fetchall()
    return jsonify([_admin_row(r) for r in rows])

@app.route('/api/admin/accounts', methods=['POST'])
@require_super
def api_admin_account_create():
    b = request.get_json(force=True)
    username = b.get('username','').strip()
    password = b.get('password','').strip()
    if not username: return jsonify({'error': '帳號為必填'}), 400
    if not password or len(password) < 4: return jsonify({'error': '密碼至少 4 個字元'}), 400
    perms = b.get('permissions', [])
    with get_db() as conn:
        try:
            row = conn.execute("""
                INSERT INTO admin_accounts (username, password_hash, display_name, permissions, is_super, active)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
            """, (username, _hash_pw(password), b.get('display_name','').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)), True)).fetchone()
        except Exception as e:
            if 'unique' in str(e).lower(): return jsonify({'error': '帳號已存在'}), 409
            return jsonify({'error': str(e)}), 500
    return jsonify(_admin_row(row)), 201

@app.route('/api/admin/accounts/<int:aid>', methods=['PUT'])
@require_super
def api_admin_account_update(aid):
    b = request.get_json(force=True)
    username = b.get('username','').strip()
    if not username: return jsonify({'error': '帳號為必填'}), 400
    password = b.get('password','').strip()
    perms = b.get('permissions', [])
    with get_db() as conn:
        if password:
            if len(password) < 4: return jsonify({'error': '密碼至少 4 個字元'}), 400
            row = conn.execute("""
                UPDATE admin_accounts SET username=%s, password_hash=%s, display_name=%s,
                  permissions=%s, is_super=%s, active=%s WHERE id=%s RETURNING *
            """, (username, _hash_pw(password), b.get('display_name','').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)),
                  bool(b.get('active', True)), aid)).fetchone()
        else:
            row = conn.execute("""
                UPDATE admin_accounts SET username=%s, display_name=%s,
                  permissions=%s, is_super=%s, active=%s WHERE id=%s RETURNING *
            """, (username, b.get('display_name','').strip(),
                  _json.dumps(perms), bool(b.get('is_super', False)),
                  bool(b.get('active', True)), aid)).fetchone()
    return jsonify(_admin_row(row)) if row else ('', 404)

@app.route('/api/admin/accounts/<int:aid>', methods=['DELETE'])
@require_super
def api_admin_account_delete(aid):
    if aid == session.get('admin_id'):
        return jsonify({'error': '不能刪除自己的帳號'}), 400
    with get_db() as conn:
        conn.execute("DELETE FROM admin_accounts WHERE id=%s", (aid,))
    return jsonify({'deleted': aid})

# ─── Shared Helpers ───────────────────────────────────────────────────────────











# ═══════════════════════════════════════════════════════════════════
# Employee Punch Page
# ═══════════════════════════════════════════════════════════════════


# ── Employee Session ──────────────────────────────────────────────




# ── GPS Settings ──────────────────────────────────────────────────







# ── Clock In/Out ──────────────────────────────────────────────────




# ── Admin: Staff CRUD ─────────────────────────────────────────────





# ── Admin: Punch Records ──────────────────────────────────────────







# ── Punch Requests (補打卡申請) ───────────────────────────────────





# ═══════════════════════════════════════════════════════════════════
# LINE Punch Clock
# ═══════════════════════════════════════════════════════════════════
















# ── Admin LINE Punch Config API ────────────────────────────────────





# ── Rich Menu ──────────────────────────────────────────────────────












# ═══════════════════════════════════════════════════════════════════
# Schedule / Shift API
# ═══════════════════════════════════════════════════════════════════

# ── Employee: schedule config + my request ────────────────────────






# ── Admin: schedule config ────────────────────────────────────────




# ── Admin: schedule requests ──────────────────────────────────────










# ── Shift Types CRUD ──────────────────────────────────────────────






# ── Shift Assignments ─────────────────────────────────────────────














# ── Overtime Requests ─────────────────────────────────────────────

















# ═══════════════════════════════════════════════════════════════════
# Leave Management (請假管理)
# 2026 勞基法:
#   特休:到職1年10天、2年15天、3~5年每年+1、滿5年20天(上限)
#   病假:每年30天(半薪),超過住院病假 365 天內 30 天(全薪)
#   事假:每年14天(無薪)
#   生理假:每月1天(含病假計算,前3天半薪)
#   婚假:8天全薪
#   喪假:父母/配偶/子女8天;祖父母/孫子女/兄弟姐妹6天;曾祖父母3天
#   產假:8週全薪;陪產假:7天全薪
#   公假:全薪
# ═══════════════════════════════════════════════════════════════════

# ── Leave Tables ─────────────────────────────────────────────────────────────

def init_leave_db():
    migrations = [
        """CREATE TABLE IF NOT EXISTS leave_types (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            code        TEXT NOT NULL UNIQUE,
            pay_rate    NUMERIC(4,2) DEFAULT 1.0,
            max_days    NUMERIC(5,1),
            description TEXT DEFAULT '',
            color       TEXT DEFAULT '#4a7bda',
            active      BOOLEAN DEFAULT TRUE,
            sort_order  INT DEFAULT 0,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS leave_requests (
            id              SERIAL PRIMARY KEY,
            staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            leave_type_id   INT REFERENCES leave_types(id),
            start_date      DATE NOT NULL,
            end_date        DATE NOT NULL,
            start_half      BOOLEAN DEFAULT FALSE,
            end_half        BOOLEAN DEFAULT FALSE,
            total_days      NUMERIC(5,1) NOT NULL DEFAULT 0,
            reason          TEXT DEFAULT '',
            status          TEXT DEFAULT 'pending',
            reviewed_by     TEXT DEFAULT '',
            review_note     TEXT DEFAULT '',
            reviewed_at     TIMESTAMPTZ,
            substitute_name TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS leave_balances (
            id          SERIAL PRIMARY KEY,
            staff_id    INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            leave_type_id INT REFERENCES leave_types(id),
            year        INT NOT NULL,
            total_days  NUMERIC(5,1) DEFAULT 0,
            used_days   NUMERIC(5,1) DEFAULT 0,
            note        TEXT DEFAULT '',
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(staff_id, leave_type_id, year)
        )""",
        # 索引(表建立後)
        "CREATE INDEX IF NOT EXISTS idx_lr_staff_date  ON leave_requests(staff_id, start_date)",
        "CREATE INDEX IF NOT EXISTS idx_lr_status      ON leave_requests(status)",
        "CREATE INDEX IF NOT EXISTS idx_lr_status_date ON leave_requests(status, start_date)",
        "CREATE INDEX IF NOT EXISTS idx_lb_staff_year  ON leave_balances(staff_id, year)",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[leave_init] {str(e)[:80]}")

    # Seed default leave types
    defaults = [
        ('特休假',   'annual',      1.0,  30,  '#2e9e6b', 1),
        ('病假',     'sick',        0.5,  30,  '#e07b2a', 2),
        ('住院病假', 'hospitalize', 1.0,  30,  '#d64242', 3),
        ('事假',     'personal',    0.0,  14,  '#8892a4', 4),
        ('生理假',   'menstrual',   0.5,  12,  '#c45cb8', 5),
        ('婚假',     'marriage',    1.0,   8,  '#c8a96e', 6),
        ('喪假',     'funeral',     1.0,   8,  '#4a7bda', 7),
        ('產假',     'maternity',   1.0,  56,  '#e05c8a', 8),
        ('陪產假',   'paternity',   1.0,   7,  '#5cb8c4', 9),
        ('公假',     'official',    1.0, None, '#243d6e', 10),
        ('補休',     'compensatory',1.0, None, '#8b5cf6', 11),
    ]
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM leave_types").fetchone()['c']
            if cnt == 0:
                for name, code, pay, maxd, color, sort in defaults:
                    conn.execute(
                        "INSERT INTO leave_types (name,code,pay_rate,max_days,color,sort_order) VALUES (%s,%s,%s,%s,%s,%s)",
                        (name, code, pay, maxd, color, sort)
                    )
    except Exception as e:
        print(f"[leave_seed] {e}")

init_leave_db()










# ── Leave Type CRUD ──────────────────────────────────────────────






# ── Leave Requests ────────────────────────────────────────────────







# ── Employee: submit leave request ────────────────────────────────



# ── Leave Balance ─────────────────────────────────────────────────









# ── Leave Summary (for salary integration) ───────────────────────


# ═══════════════════════════════════════════════════════════════════
# Salary Management (薪資管理)
# 2026 勞基法:
#   勞保費率 10.5%(員工負擔 20%=2.1%,含就業保險)
#   健保費率 5.17%(員工負擔 30%=1.551%)
#   勞退提撥 6%(雇主強制提撥,員工自願另計)
#   最低工資 2026年 NT$28,590(月薪)
# ═══════════════════════════════════════════════════════════════════

def init_salary_db():
    migrations = [
        """CREATE TABLE IF NOT EXISTS salary_items (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            item_type   TEXT NOT NULL DEFAULT 'allowance',
            formula     TEXT DEFAULT '',
            amount      NUMERIC(12,2) DEFAULT 0,
            description TEXT DEFAULT '',
            color       TEXT DEFAULT '#4a7bda',
            active      BOOLEAN DEFAULT TRUE,
            sort_order  INT DEFAULT 0,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_item_ids JSONB DEFAULT NULL",
        "ALTER TABLE punch_staff ADD COLUMN IF NOT EXISTS salary_item_overrides JSONB DEFAULT NULL",
        """CREATE TABLE IF NOT EXISTS salary_records (
            id              SERIAL PRIMARY KEY,
            staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            month           TEXT NOT NULL,
            base_salary     NUMERIC(12,2) DEFAULT 0,
            insured_salary  NUMERIC(12,2) DEFAULT 0,
            work_days       NUMERIC(5,1)  DEFAULT 0,
            actual_days     NUMERIC(5,1)  DEFAULT 0,
            leave_days      NUMERIC(5,1)  DEFAULT 0,
            unpaid_days     NUMERIC(5,1)  DEFAULT 0,
            ot_pay          NUMERIC(12,2) DEFAULT 0,
            allowance_total NUMERIC(12,2) DEFAULT 0,
            deduction_total NUMERIC(12,2) DEFAULT 0,
            net_pay         NUMERIC(12,2) DEFAULT 0,
            items           JSONB         DEFAULT '[]',
            status          TEXT          DEFAULT 'draft',
            note            TEXT          DEFAULT '',
            confirmed_by    TEXT          DEFAULT '',
            confirmed_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ   DEFAULT NOW(),
            updated_at      TIMESTAMPTZ   DEFAULT NOW(),
            UNIQUE(staff_id, month)
        )""",
        # salary_records 建立後才能加欄位/索引(否則冷啟動首次部署會失敗)
        "ALTER TABLE salary_records ADD COLUMN IF NOT EXISTS income_tax_withheld NUMERIC(12,2) DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_sr_month       ON salary_records(month)",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[salary_init] {str(e)[:80]}")

    # Seed default salary items
    defaults = [
        ('本薪',        'allowance', 'base_salary+service_years*1000', 0,    '#2e9e6b', 1),
        ('職務加給',    'allowance', '',                                0,    '#0ea5e9', 2),
        ('全勤獎金',    'allowance', '',                                0,    '#c8a96e', 3),
        ('獎金',        'allowance', '',                                0,    '#8b5cf6', 4),
        ('生日禮金',    'allowance', '',                                1000, '#e05c8a', 5),
        ('勞退6%',      'allowance', 'base_salary*0.06+service_years*1000*0.06', 0, '#4a7bda', 6),
        ('病/事/假',    'deduction', '',                                0,    '#8892a4', 7),
        ('勞保費',      'deduction', 'insured_salary*0.125*0.2',       0,    '#d64242', 8),
        ('健保費',      'deduction', 'insured_salary*0.0517*0.3',      0,    '#e07b2a', 9),
        ('勞退提撥6%',  'deduction', 'base_salary*0.06+service_years*1000*0.06', 0, '#4a7bda', 10),
    ]
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM salary_items").fetchone()['c']
            if cnt == 0:
                for name, itype, formula, amount, color, sort in defaults:
                    conn.execute("""
                        INSERT INTO salary_items (name,item_type,formula,amount,color,sort_order)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (name, itype, formula, amount, color, sort))
    except Exception as e:
        print(f"[salary_seed] {e}")

init_salary_db()






# ── Employee: view own payslip ────────────────────────────────────


# ── Salary Items CRUD ─────────────────────────────────────────────





# ── Salary Records ─────────────────────────────────────────────────








# ── Salary Staff Settings ─────────────────────────────────────────




# ═══════════════════════════════════════════════════════════════════
# Announcement Module (公告管理)
# ═══════════════════════════════════════════════════════════════════

def init_announcement_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS announcements (
                    id          SERIAL PRIMARY KEY,
                    title       TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    category    TEXT DEFAULT 'general',
                    priority    TEXT DEFAULT 'normal',
                    is_pinned   BOOLEAN DEFAULT FALSE,
                    visible_to  TEXT DEFAULT 'all',
                    published_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at  TIMESTAMPTZ,
                    author      TEXT DEFAULT '管理員',
                    active      BOOLEAN DEFAULT TRUE,
                    view_count  INT DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    except Exception as e:
        print(f"[announcement_init] {e}")

init_announcement_db()


# ── Admin: CRUD ───────────────────────────────────────────────────






# ── Public: employee reads ────────────────────────────────────────




# ═══════════════════════════════════════════════════════════════════
# Feature 1: Taiwan Public Holidays (國定假日)
# ═══════════════════════════════════════════════════════════════════

def init_holiday_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS public_holidays (
                    id          SERIAL PRIMARY KEY,
                    date        DATE NOT NULL UNIQUE,
                    name        TEXT NOT NULL,
                    holiday_type TEXT DEFAULT 'national',
                    note        TEXT DEFAULT '',
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        # Seed 2025 & 2026 Taiwan holidays
        _seed_holidays()
    except Exception as e:
        print(f"[holiday_init] {e}")

def _seed_holidays():
    """台灣2025-2026國定假日"""
    holidays_2025 = [
        ('2025-01-01', '元旦'),
        ('2025-01-27', '農曆除夕'),
        ('2025-01-28', '春節'),
        ('2025-01-29', '春節'),
        ('2025-01-30', '春節'),
        ('2025-01-31', '春節補假'),
        ('2025-02-28', '和平紀念日'),
        ('2025-04-03', '兒童節補假'),
        ('2025-04-04', '兒童節/清明節'),
        ('2025-05-01', '勞動節'),
        ('2025-05-30', '端午節補假'),
        ('2025-06-02', '端午節'),
        ('2025-10-06', '中秋節補假'),
        ('2025-10-07', '中秋節'),
        ('2025-10-10', '國慶日'),
    ]
    holidays_2026 = [
        ('2026-01-01', '元旦'),
        ('2026-01-28', '農曆除夕'),
        ('2026-01-29', '春節'),
        ('2026-01-30', '春節'),
        ('2026-01-31', '春節'),
        ('2026-02-02', '春節補假'),
        ('2026-02-28', '和平紀念日'),
        ('2026-03-02', '和平紀念日補假'),
        ('2026-04-03', '兒童節'),
        ('2026-04-04', '清明節'),
        ('2026-04-05', '清明節補假'),
        ('2026-05-01', '勞動節'),
        ('2026-06-19', '端午節'),
        ('2026-09-25', '中秋節'),
        ('2026-10-09', '國慶日補假'),
        ('2026-10-10', '國慶日'),
    ]
    all_holidays = holidays_2025 + holidays_2026
    try:
        with get_db() as conn:
            existing = conn.execute("SELECT COUNT(*) as c FROM public_holidays").fetchone()['c']
            if existing == 0:
                for date_str, name in all_holidays:
                    try:
                        conn.execute(
                            "INSERT INTO public_holidays (date, name) VALUES (%s,%s) ON CONFLICT (date) DO NOTHING",
                            (date_str, name)
                        )
                    except Exception:
                        pass
    except Exception as e:
        print(f"[holiday_seed] {e}")

init_holiday_db()



# ── Holiday CRUD API ─────────────────────────────────────────────







# ═══════════════════════════════════════════════════════════════════
# Feature 2: LINE Notification Helper
# ═══════════════════════════════════════════════════════════════════





# ═══════════════════════════════════════════════════════════════════
# Feature 3: Export Reports (出勤/薪資報表匯出)
# ═══════════════════════════════════════════════════════════════════

import csv
import io











# ── Patch existing review functions with LINE notifications ──────

def _patch_reviews_with_notifications():
    """
    This is called after all route functions are defined.
    We monkey-patch the review endpoints to send LINE notifications.
    The actual patching is done inline in the route handlers below
    via the _notify_review_result helper.
    """
    pass



# ═══════════════════════════════════════════════════════════════════
# Dashboard API
# ═══════════════════════════════════════════════════════════════════



# ── Dashboard 擴充 API ────────────────────────────────────────────────────────







# ── 年度扣繳憑單 ────────────────────────────────────────────────────────────



# ── 勞健保 EDI 申報 ─────────────────────────────────────────────────────────
















# ── 多店管理 ─────────────────────────────────────────────────────────────────








# ── 排班需求 & 自動排班 ──────────────────────────────────────────────────────







# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8010))
    app.run(host='0.0.0.0', port=port, debug=False)

# ═══════════════════════════════════════════════════════════════════
# Feature: Salary PDF (HTML print endpoint)
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# Feature: Batch Review (批次審核)
# ═══════════════════════════════════════════════════════════════════









# ═══════════════════════════════════════════════════════════════════
# Feature: Attendance Anomaly Detection (出勤異常)
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# Feature: Staff Termination (離職流程)
# ═══════════════════════════════════════════════════════════════════







# ═══════════════════════════════════════════════════════════════════
# Feature: Salary Formula Builder support (公式說明 API)
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# Finance Module (財務模組)
# ═══════════════════════════════════════════════════════════════════


def init_finance_db():
    migrations = [
        """CREATE TABLE IF NOT EXISTS finance_categories (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'expense',
            color       TEXT DEFAULT '#4a7bda',
            sort_order  INT DEFAULT 0,
            active      BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_records (
            id              SERIAL PRIMARY KEY,
            record_date     DATE NOT NULL,
            category_id     INT REFERENCES finance_categories(id) ON DELETE SET NULL,
            type            TEXT NOT NULL DEFAULT 'expense',
            title           TEXT NOT NULL,
            amount          NUMERIC(14,2) NOT NULL DEFAULT 0,
            tax_amount      NUMERIC(14,2) DEFAULT 0,
            vendor          TEXT DEFAULT '',
            invoice_no      TEXT DEFAULT '',
            note            TEXT DEFAULT '',
            document_id     INT,
            created_by      TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_documents (
            id              SERIAL PRIMARY KEY,
            filename        TEXT NOT NULL,
            doc_type        TEXT DEFAULT '',
            ocr_raw         JSONB DEFAULT '{}',
            upload_date     DATE DEFAULT CURRENT_DATE,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_recurring (
            id              SERIAL PRIMARY KEY,
            title           TEXT NOT NULL,
            type            TEXT NOT NULL DEFAULT 'expense',
            category_id     INT REFERENCES finance_categories(id) ON DELETE SET NULL,
            amount          NUMERIC(14,2) NOT NULL DEFAULT 0,
            tax_amount      NUMERIC(14,2) DEFAULT 0,
            vendor          TEXT DEFAULT '',
            note            TEXT DEFAULT '',
            frequency       TEXT NOT NULL DEFAULT 'monthly',
            day_of_month    INT DEFAULT 1,
            start_date      DATE NOT NULL,
            end_date        DATE,
            last_generated  TEXT DEFAULT '',
            active          BOOLEAN DEFAULT TRUE,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS bank_statements (
            id                  SERIAL PRIMARY KEY,
            account_name        TEXT DEFAULT '',
            txn_date            DATE NOT NULL,
            amount              NUMERIC(14,2) NOT NULL,
            txn_type            TEXT DEFAULT 'debit',
            description         TEXT DEFAULT '',
            reconciled          BOOLEAN DEFAULT FALSE,
            matched_record_id   INT REFERENCES finance_records(id) ON DELETE SET NULL,
            import_batch        TEXT DEFAULT '',
            created_at          TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_payables (
            id              SERIAL PRIMARY KEY,
            payable_type    TEXT NOT NULL DEFAULT 'payable',
            title           TEXT NOT NULL,
            party_name      TEXT DEFAULT '',
            invoice_no      TEXT DEFAULT '',
            amount          NUMERIC(14,2) NOT NULL DEFAULT 0,
            due_date        DATE,
            status          TEXT NOT NULL DEFAULT 'open',
            paid_date       DATE,
            linked_record_id INT REFERENCES finance_records(id) ON DELETE SET NULL,
            note            TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS finance_budgets (
            id              SERIAL PRIMARY KEY,
            year            INT NOT NULL,
            month           INT NOT NULL,
            category_id     INT REFERENCES finance_categories(id) ON DELETE CASCADE,
            budget_amount   NUMERIC(14,2) NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(year, month, category_id)
        )""",
        "ALTER TABLE salary_records ADD COLUMN IF NOT EXISTS finance_synced BOOLEAN DEFAULT FALSE",
        "CREATE INDEX IF NOT EXISTS idx_fr_date        ON finance_records(record_date)",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[finance_init] {str(e)[:80]}")

    # Seed default categories
    defaults_income = [
        ('餐飲內用收入', 'income', '#2e9e6b', 1),
        ('外帶收入',     'income', '#0ea5e9', 2),
        ('外送收入',     'income', '#8b5cf6', 3),
        ('其他收入',     'income', '#c8a96e', 4),
    ]
    defaults_expense = [
        ('食材成本',   'expense', '#d64242', 10),
        ('薪資支出',   'expense', '#e07b2a', 11),
        ('租金',       'expense', '#8892a4', 12),
        ('水電費',     'expense', '#4a7bda', 13),
        ('設備維修',   'expense', '#e05c8a', 14),
        ('消耗品',     'expense', '#6366f1', 15),
        ('廣告行銷',   'expense', '#f59e0b', 16),
        ('其他支出',   'expense', '#64748b', 17),
    ]
    try:
        with get_db() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM finance_categories").fetchone()['c']
            if cnt == 0:
                for name, ftype, color, sort in (defaults_income + defaults_expense):
                    conn.execute(
                        "INSERT INTO finance_categories (name,type,color,sort_order) VALUES (%s,%s,%s,%s)",
                        (name, ftype, color, sort)
                    )
    except Exception as e:
        print(f"[finance_seed] {e}")

init_finance_db()



# ── Finance Categories ─────────────────────────────────────────





# ── Finance Records ────────────────────────────────────────────







# ── Finance P&L Summary ────────────────────────────────────────


# ── Finance OCR ────────────────────────────────────────────────


# ── Finance Export ─────────────────────────────────────────────


# ── Finance Settings & Financial Statements ────────────────────

def init_finance_settings_db():
    migrations = [
        "ALTER TABLE finance_categories ADD COLUMN IF NOT EXISTS statement_section TEXT",
        """CREATE TABLE IF NOT EXISTS finance_settings (
            id            SERIAL PRIMARY KEY,
            setting_key   TEXT UNIQUE NOT NULL,
            setting_value TEXT DEFAULT ''
        )""",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[finance_settings_init] {str(e)[:80]}")

    # Set default statement_section based on type for existing rows with NULL
    section_defaults = {
        '餐飲內用收入': 'operating_revenue',
        '外帶收入':     'operating_revenue',
        '外送收入':     'operating_revenue',
        '其他收入':     'other_revenue',
        '食材成本':     'cogs',
        '薪資支出':     'operating_expense',
        '租金':         'operating_expense',
        '水電費':       'operating_expense',
        '設備維修':     'operating_expense',
        '消耗品':       'operating_expense',
        '廣告行銷':     'operating_expense',
        '其他支出':     'other_expense',
    }
    try:
        with get_db() as conn:
            # Fill named defaults
            for name, sec in section_defaults.items():
                conn.execute(
                    "UPDATE finance_categories SET statement_section=%s WHERE name=%s AND statement_section IS NULL",
                    (sec, name)
                )
            # Remaining NULLs: income -> operating_revenue, expense -> operating_expense
            conn.execute("""
                UPDATE finance_categories
                SET statement_section = CASE WHEN type='income' THEN 'operating_revenue' ELSE 'operating_expense' END
                WHERE statement_section IS NULL
            """)
    except Exception as e:
        print(f"[finance_settings_seed] {e}")

    # Seed settings defaults
    for k, v in [('company_name', ''), ('opening_cash', '0'), ('opening_equity', '0'),
                  ('company_tax_id', ''), ('company_address', '')]:
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO finance_settings (setting_key, setting_value) VALUES (%s,%s) ON CONFLICT (setting_key) DO NOTHING",
                    (k, v)
                )
        except Exception as e:
            print(f"[finance_settings_default] {e}")

init_finance_settings_db()


def init_insurance_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS insurance_settings (
                    setting_key   TEXT PRIMARY KEY,
                    setting_value TEXT DEFAULT ''
                )
            """)
        for k, v in [('labor_insurance_no', ''), ('health_insurance_no', ''),
                     ('employer_name', ''), ('employer_id', '')]:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO insurance_settings (setting_key, setting_value) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (k, v))
    except Exception as e:
        print(f"[insurance_init] {e}")

init_insurance_db()


# ═══════════════════════════════════════════════════════════════════════════════
# 教育訓練追蹤 (Training & Certificate Tracking)
# ═══════════════════════════════════════════════════════════════════════════════

def init_training_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS training_records (
                    id              SERIAL PRIMARY KEY,
                    staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
                    course_name     TEXT NOT NULL,
                    category        TEXT NOT NULL DEFAULT 'general',
                    completed_date  DATE,
                    expiry_date     DATE,
                    certificate_no  TEXT DEFAULT '',
                    note            TEXT DEFAULT '',
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    except Exception as e:
        print(f"[training_init] {e}")

init_training_db()

TRAINING_CATEGORIES = {
    'food_safety':  '食品安全',
    'fire_safety':  '消防安全',
    'first_aid':    '急救訓練',
    'hygiene':      '衛生管理',
    'service':      '服務禮儀',
    'equipment':    '設備操作',
    'general':      '一般訓練',
    'other':        '其他',
}






# ── 薪資計算預覽 (Salary Preview without saving) ───────────────────────────────

















# ═══════════════════════════════════════════════════════════════════
# Feature 1: 定期自動分錄 (Recurring Entries)
# ═══════════════════════════════════════════════════════════════════








# ═══════════════════════════════════════════════════════════════════
# Feature 2: 銀行對帳 (Bank Reconciliation)
# ═══════════════════════════════════════════════════════════════════









# ═══════════════════════════════════════════════════════════════════
# Feature 3: 稅務申報準備 (Tax Filing Prep - Taiwan VAT 401)
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# Feature 4: 應收/應付帳款 (AR/AP Tracking)
# ═══════════════════════════════════════════════════════════════════








# ═══════════════════════════════════════════════════════════════════
# Feature 5: 預算管理 (Budget vs Actual)
# ═══════════════════════════════════════════════════════════════════





# ═══════════════════════════════════════════════════════════════════
# Feature 6: 薪資費用連動 (Payroll -> Finance)
# ═══════════════════════════════════════════════════════════════════




# ── Tax -> Finance sync ──────────────────────────────────────────



# ═══════════════════════════════════════════════════════════════════
# LINE Broadcast Helper
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# Expense Claims 費用報帳申請
# ═══════════════════════════════════════════════════════════════════

def _init_expense_db():
    sqls = [
        """CREATE TABLE IF NOT EXISTS expense_claims (
            id                SERIAL PRIMARY KEY,
            staff_id          INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            title             TEXT NOT NULL,
            amount            NUMERIC(12,2) NOT NULL DEFAULT 0,
            expense_date      DATE NOT NULL,
            category          TEXT DEFAULT '',
            note              TEXT DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'pending',
            document_id       INT REFERENCES finance_documents(id) ON DELETE SET NULL,
            review_note       TEXT DEFAULT '',
            reviewed_by       TEXT DEFAULT '',
            reviewed_at       TIMESTAMPTZ,
            finance_record_id INT REFERENCES finance_records(id) ON DELETE SET NULL,
            created_at        TIMESTAMPTZ DEFAULT NOW()
        )""",
    ]
    for sql in sqls:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[expense_init] {e}")

_init_expense_db()




# ── Employee endpoints ──────────────────────────────────────────







# ── Leave: medical certificate upload ───────────────────────────





# ── Admin endpoints ─────────────────────────────────────────────





# ═══════════════════════════════════════════════════════════════════════════
# 績效考核模組
# ═══════════════════════════════════════════════════════════════════════════

def _init_performance_db():
    sqls = [
        """CREATE TABLE IF NOT EXISTS performance_templates (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            period      TEXT DEFAULT 'quarterly',
            items       JSONB DEFAULT '[]',
            active      BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS performance_reviews (
            id              SERIAL PRIMARY KEY,
            staff_id        INT REFERENCES punch_staff(id) ON DELETE CASCADE,
            template_id     INT REFERENCES performance_templates(id) ON DELETE SET NULL,
            period_label    TEXT NOT NULL,
            scores          JSONB DEFAULT '{}',
            total_score     NUMERIC(6,2) DEFAULT 0,
            max_score       NUMERIC(6,2) DEFAULT 100,
            grade           TEXT DEFAULT '',
            comments        TEXT DEFAULT '',
            reviewer        TEXT DEFAULT '',
            salary_adjusted BOOLEAN DEFAULT FALSE,
            salary_delta    NUMERIC(12,2) DEFAULT 0,
            reviewed_at     TIMESTAMPTZ DEFAULT NOW(),
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS performance_config (
            key        TEXT PRIMARY KEY,
            value      JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )""",
    ]
    for sql in sqls:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[perf_init] {e}")

_init_performance_db()


def _apply_late_migrations():
    """跨表欄位遷移:這些 ALTER 涉及在 init_db 之後才建立的表(或 FK 指向較晚
    建立的表),必須在所有 init_*_db() 完成、全部表都存在後才執行,
    否則全新資料庫首次部署時會被略過,導致欄位缺失。"""
    migrations = [
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS start_time TEXT DEFAULT ''",
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS end_time TEXT DEFAULT ''",
        "ALTER TABLE finance_documents ADD COLUMN IF NOT EXISTS image_data TEXT",
        # document_id 的 FK 指向 finance_documents,需兩表皆存在
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS document_id INT REFERENCES finance_documents(id) ON DELETE SET NULL",
        "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS store_ids JSONB DEFAULT '[]'",
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception as e:
            print(f"[late_migration] {str(e)[:80]}")

_apply_late_migrations()


# ── 考核範本 CRUD ───────────────────────────────────────────────





# ── 考核記錄 CRUD ───────────────────────────────────────────────





# ── 員工查自己的考核 ────────────────────────────────────────────



# ── 評級設定 CRUD ───────────────────────────────────────────────




# ═══════════════════════════════════════════════════════════════════════════
# LINE Bot 雙向查詢擴充
# ═══════════════════════════════════════════════════════════════════════════




















# ── Blueprints(模組化路由)──
from blueprints.punch import bp as punch_bp
app.register_blueprint(punch_bp)
from blueprints.line_bot import bp as line_bot_bp
app.register_blueprint(line_bot_bp)
from blueprints.finance import bp as finance_bp
app.register_blueprint(finance_bp)
from blueprints.exports import bp as exports_bp
app.register_blueprint(exports_bp)
from blueprints.insurance import bp as insurance_bp
app.register_blueprint(insurance_bp)
from blueprints.attendance import bp as attendance_bp
app.register_blueprint(attendance_bp)
from blueprints.dashboard import bp as dashboard_bp
app.register_blueprint(dashboard_bp)
from blueprints.shifts import bp as shifts_bp
app.register_blueprint(shifts_bp)
from blueprints.schedule import bp as schedule_bp
app.register_blueprint(schedule_bp)
from blueprints.salary import bp as salary_bp
app.register_blueprint(salary_bp)
from blueprints.leave import bp as leave_bp
app.register_blueprint(leave_bp)
from blueprints.overtime import bp as overtime_bp
app.register_blueprint(overtime_bp)
from blueprints.stores import bp as stores_bp
app.register_blueprint(stores_bp)
from blueprints.holidays import bp as holidays_bp
app.register_blueprint(holidays_bp)
from blueprints.expense import bp as expense_bp
app.register_blueprint(expense_bp)
from blueprints.performance import bp as performance_bp
app.register_blueprint(performance_bp)
from blueprints.training import bp as training_bp
app.register_blueprint(training_bp)
from blueprints.mobile import bp as mobile_bp
app.register_blueprint(mobile_bp)
from blueprints.webauthn import bp as webauthn_bp
app.register_blueprint(webauthn_bp)
from blueprints.announcements import bp as announcements_bp
app.register_blueprint(announcements_bp)
