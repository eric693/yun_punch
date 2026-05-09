"""
core/helpers.py - Shared helper functions used across multiple blueprints.
"""
import math
import json as _json
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

TW_TZ = _tz(_td(hours=8))   # Asia/Taipei (UTC+8)

WEEKDAY_ZH = ['一', '二', '三', '四', '五', '六', '日']


def _month_ts_range(month: str):
    """Convert 'YYYY-MM' to (start, end) TIMESTAMPTZ in Taiwan time (UTC+8).
    Use for range queries: punched_at >= start AND punched_at < end
    instead of to_char(...) = %s so indexes can be used.
    """
    y, m = int(month[:4]), int(month[5:7])
    start = _dt(y, m, 1, tzinfo=TW_TZ)
    if m == 12:
        end = _dt(y + 1, 1, 1, tzinfo=TW_TZ)
    else:
        end = _dt(y, m + 1, 1, tzinfo=TW_TZ)
    return start, end


def _month_date_range(month: str):
    """Convert 'YYYY-MM' to (first_date, last_date_exclusive) as date strings.
    Use for DATE columns: col >= %s AND col < %s
    """
    import calendar as _cal
    y, m = int(month[:4]), int(month[5:7])
    last_day = _cal.monthrange(y, m)[1]
    from datetime import date as _date
    return _date(y, m, 1), _date(y, m, last_day) + _td(days=1)


# ─── GPS ──────────────────────────────────────────────────────────────────────

def _gps_distance(lat1, lng1, lat2, lng2):
    R = 6371000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lng2 - lng1) * p / 2) ** 2)
    return int(2 * R * math.asin(math.sqrt(a)))


# ─── Row Serialisers ──────────────────────────────────────────────────────────

def punch_staff_row(row):
    if not row: return None
    d = dict(row)
    d.pop('password_hash', None)
    # password_plain is kept so admin can view it; keep as empty string if null
    if d.get('password_plain') is None: d['password_plain'] = ''
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('hire_date'):  d['hire_date']  = d['hire_date'].isoformat()
    if d.get('birth_date'): d['birth_date'] = d['birth_date'].isoformat()
    return d


def _parse_tw_datetime(s):
    """Parse a datetime string (with or without tz) treating naive strings as Taiwan time (UTC+8)."""
    if not s:
        return None
    dt = _dt.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TW_TZ)
    return dt


def punch_record_row(row):
    if not row: return None
    d = dict(row)
    for f in ['latitude', 'longitude']:
        if d.get(f) is not None: d[f] = float(d[f])
    for f in ['punched_at', 'created_at']:
        if d.get(f):
            dt = d[f]
            if dt.tzinfo is None:
                from datetime import timezone as _utz
                dt = dt.replace(tzinfo=_utz.utc)
            d[f] = dt.astimezone(TW_TZ).isoformat()
    return d


def loc_row(row):
    if not row: return None
    d = dict(row)
    for f in ['lat', 'lng']:
        if d.get(f) is not None: d[f] = float(d[f])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return d


def punch_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('requested_at'): d['requested_at'] = d['requested_at'].isoformat()
    if d.get('reviewed_at'):  d['reviewed_at']  = d['reviewed_at'].isoformat()
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    return d


def ot_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('request_date'): d['request_date'] = d['request_date'].isoformat()
    if d.get('start_time'):   d['start_time']   = str(d['start_time'])[:5]
    if d.get('end_time'):     d['end_time']      = str(d['end_time'])[:5]
    if d.get('ot_pay'):       d['ot_pay']        = float(d['ot_pay'])
    if d.get('ot_hours'):     d['ot_hours']      = float(d['ot_hours'])
    if d.get('reviewed_at'):  d['reviewed_at']   = d['reviewed_at'].isoformat()
    if d.get('created_at'):   d['created_at']    = d['created_at'].isoformat()
    return d


def shift_type_row(row):
    if not row: return None
    d = dict(row)
    if d.get('start_time'): d['start_time'] = str(d['start_time'])[:5]
    if d.get('end_time'):   d['end_time']   = str(d['end_time'])[:5]
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def shift_assign_row(row):
    if not row: return None
    d = dict(row)
    if d.get('shift_date'): d['shift_date'] = d['shift_date'].isoformat()
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def sched_req_row(row):
    if not row: return None
    d = dict(row)
    if isinstance(d.get('dates'), str):
        try:
            d['dates'] = _json.loads(d['dates'])
        except Exception:
            d['dates'] = []
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('updated_at'):  d['updated_at']  = d['updated_at'].isoformat()
    return d


def get_schedule_config(conn, month):
    row = conn.execute("SELECT * FROM schedule_config WHERE month=%s", (month,)).fetchone()
    if not row:
        return {'month': month, 'max_off_per_day': 2, 'vacation_quota': 8, 'notes': ''}
    return dict(row)


def get_off_counts(conn, month):
    rows = conn.execute("""
        SELECT elem as d, COUNT(*) as cnt
        FROM schedule_requests,
             jsonb_array_elements_text(dates) as elem
        WHERE month=%s AND status IN ('approved','pending')
        GROUP BY elem
    """, (month,)).fetchall()
    return {r['d']: int(r['cnt']) for r in rows}


# ─── Admin row ────────────────────────────────────────────────────────────────

def _admin_row(r):
    if not r: return None
    d = dict(r)
    d.pop('password_hash', None)
    perms = d.get('permissions')
    if isinstance(perms, str):
        try:
            d['permissions'] = _json.loads(perms)
        except Exception:
            d['permissions'] = []
    if d.get('created_at'):    d['created_at']    = d['created_at'].isoformat()
    if d.get('last_login_at'): d['last_login_at'] = d['last_login_at'].isoformat()
    return d


# ─── Salary/Leave trigger helpers ─────────────────────────────────────────────

def _trigger_salary_regen_for_leave(conn, staff_id, month):
    """Trigger salary regeneration for a given staff/month.
    Lazy-imports _auto_generate_salary from routes.salary to avoid circular imports.
    """
    try:
        from routes.salary import _auto_generate_salary
        import json as _jl
        staff = conn.execute("SELECT * FROM punch_staff WHERE id=%s", (staff_id,)).fetchone()
        if not staff:
            return
        data = _auto_generate_salary(conn, dict(staff), month)
        items_json = _jl.dumps(data['items'], ensure_ascii=False)
        conn.execute("""
            UPDATE salary_records
            SET base_salary=%s, insured_salary=%s, work_days=%s, actual_days=%s,
                leave_days=%s, unpaid_days=%s, ot_pay=%s, allowance_total=%s,
                deduction_total=%s, net_pay=%s, items=%s::jsonb, updated_at=NOW()
            WHERE staff_id=%s AND month=%s AND status='draft'
        """, (
            data['base_salary'], data['insured_salary'], data['work_days'],
            data['actual_days'], data['leave_days'], data['unpaid_days'],
            data['ot_pay'], data['allowance_total'], data['deduction_total'],
            data['net_pay'], items_json, staff_id, month,
        ))
    except Exception as e:
        print(f"[salary regen] {e}")
