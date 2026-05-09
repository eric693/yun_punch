"""
core/background.py - Background thread tasks (keep-alive, annual leave sync,
monthly salary auto-generate).
"""
import os
import threading
import time
import urllib.request
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from core.database import get_db, DATABASE_URL

TW_TZ = _tz(_td(hours=8))

RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL', '')


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


# ─── Annual Leave Sync ────────────────────────────────────────────────────────

def _run_annual_leave_sync():
    """依勞基法第38條，依到職日計算特休天數，寫入 leave_balances。每日午夜自動執行。"""
    from datetime import date as _d_sync
    from routes.leave import _calc_annual_leave_days
    year = str(_d_sync.today().year)
    try:
        with get_db() as conn:
            # 多 worker 防重：取得 advisory lock，拿不到就跳過
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


# ─── Monthly Salary Auto-Generate ─────────────────────────────────────────────

def _run_monthly_salary_auto_generate():
    """每月1日 00:10（台北時間）自動產生所有在職員工上月薪資（draft）。"""
    from datetime import date as _d_sal
    import json as _json_sal

    # 計算上月月份字串，例如今天是 2026-05-01 → 上月為 2026-04
    today = _d_sal.today()
    if today.month == 1:
        last_month = f"{today.year - 1}-12"
    else:
        last_month = f"{today.year}-{today.month - 1:02d}"

    print(f"[salary_auto] 開始自動產生 {last_month} 薪資...")
    try:
        with get_db() as conn:
            # 多 worker 防重：取得 advisory lock，拿不到就跳過
            got_lock = conn.execute("SELECT pg_try_advisory_lock(1002)").fetchone()[0]
            if not got_lock:
                print(f"[salary_auto] 另一 worker 正在執行，略過。")
                return
            try:
                # Lazy import to avoid circular imports
                from routes.salary import _auto_generate_salary
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
                print(f"[salary_auto] {last_month} 薪資自動產生完成，共 {generated} 筆。")
            finally:
                conn.execute("SELECT pg_advisory_unlock(1002)")
    except Exception as e:
        print(f"[salary_auto] 產生失敗：{e}")


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
                # 執行完後等待 25 小時，避免同一天重複執行
                _time_sal.sleep(25 * 3600)
                continue
        # 計算下次執行時間：下個月1日 00:10
        if now.month == 12:
            next_run = _dt(now.year + 1, 1, 1, 0, 10, tzinfo=TW_TZ)
        else:
            next_run = _dt(now.year, now.month + 1, 1, 0, 10, tzinfo=TW_TZ)
        sleep_secs = (next_run - now).total_seconds()
        print(f"[salary_auto] 下次執行時間：{next_run.strftime('%Y-%m-%d %H:%M')}（台北時間），約 {sleep_secs/3600:.1f} 小時後")
        _time_sal.sleep(max(sleep_secs, 60))


def start_background_threads():
    """Call this once from the app factory to start all daemon threads."""
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=_annual_leave_sync_loop, daemon=True).start()
    threading.Thread(target=_monthly_salary_auto_loop, daemon=True).start()
