"""跨模組商業邏輯服務層。

從 app.py 抽出,集中:LINE 推播/通知、薪資自動計算與重算、特休/請假天數
計算、加班費試算、國定假日判斷。各 blueprint 一律 `from services import ...`,
不再彼此(或對 app.py)循環相依。
"""
import json as _json
import urllib.request

from linebot import LineBotApi
from linebot.models import TextSendMessage

from config import DATABASE_URL, TW_TZ
from db import get_db
from utils import (
    _month_ts_range, _month_date_range, _calc_service_years, _eval_formula,
)


def get_line_punch_config():
    if not DATABASE_URL: return None
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
        return dict(row) if row else None
    except Exception:
        return None

def _send_line_punch(user_id, text):
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
        return
    try:
        LineBotApi(cfg['channel_access_token']).push_message(
            user_id, TextSendMessage(text=text)
        )
    except Exception as e:
        print(f"[LINE PUNCH] push_message error: {e}")

def _send_line_with_quick_reply(user_id, text, items):
    """Send a message with Quick Reply buttons.
    items: [{'label': str (≤20 chars), 'text': str (message to send on tap)}, ...]
    """
    from linebot.models import QuickReply, QuickReplyButton, MessageAction
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
        return
    qr_items = [
        QuickReplyButton(action=MessageAction(label=it['label'][:20], text=it['text']))
        for it in items[:13]
    ]
    msg = TextSendMessage(text=text, quick_reply=QuickReply(items=qr_items))
    try:
        LineBotApi(cfg['channel_access_token']).push_message(user_id, msg)
    except Exception as e:
        print(f"[LINE PUNCH] push_message (qr) error: {e}")

def _call_line_api(cfg, method, path, body=None):
    token = cfg.get('channel_access_token', '')
    url   = 'https://api.line.me/v2/bot' + path
    data  = _json.dumps(body).encode('utf-8') if body else None
    req   = urllib.request.Request(
        url, data=data, method=method,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {'error': e.read().decode('utf-8', errors='replace')}
    except Exception as e:
        return 0, {'error': str(e)}

def _calc_ot_pay(staff_row, ot_hours, day_type='weekday'):
    salary_type = staff_row.get('salary_type', 'monthly') or 'monthly'
    base_salary = float(staff_row.get('base_salary')  or 0)
    hourly_rate = float(staff_row.get('hourly_rate')  or 0)
    daily_hours = float(staff_row.get('daily_hours')  or 8)
    ot_rate1    = float(staff_row.get('ot_rate1')     or 1.33)
    ot_rate2    = float(staff_row.get('ot_rate2')     or 1.67)
    ot_rate3    = float(staff_row.get('ot_rate3')     or 2.0)

    if salary_type == 'hourly':
        base_hourly = hourly_rate
    else:
        base_hourly = base_salary / 30 / daily_hours if (base_salary and daily_hours) else 0

    if base_hourly <= 0:
        return 0.0, base_hourly

    h = float(ot_hours)
    if day_type in ('holiday', 'special'):
        pay = round(base_hourly * h * 2.0, 0)
    elif day_type == 'rest_day':
        billed = max(h, 4.0)
        h1 = min(billed, 2.0); h2 = min(max(0.0, billed - 2.0), 2.0); h3 = max(0.0, billed - 4.0)
        pay = round(base_hourly * (h1 * ot_rate1 + h2 * ot_rate2 + h3 * ot_rate3), 0)
    else:
        h1 = min(h, 2.0); h2 = min(max(0.0, h - 2.0), 2.0); h3 = max(0.0, h - 4.0)
        pay = round(base_hourly * (h1 * ot_rate1 + h2 * ot_rate2 + h3 * ot_rate3), 0)

    return pay, base_hourly

def _calc_annual_leave_days(hire_date_str, ref_date_str=None):
    """
    勞基法第38條特休天數計算(2017年修正版,現行有效)

    到職滿6個月:3天
    到職滿1年:7天
    到職滿2年:10天
    到職滿3年:14天
    到職滿4年:14天(同第3年)
    到職滿5年:15天
    到職滿6~9年:15天(同第5年)
    到職滿10年起:每年+1天,上限30天

    回傳當期應給特休天數(整數)
    """
    if not hire_date_str:
        return 0
    from datetime import date as _date
    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return 0

    ref = _date.today()
    if ref_date_str:
        try:
            ref = _date.fromisoformat(str(ref_date_str))
        except Exception:
            pass

    # 計算到職滿幾個月(以完整月份計)
    months = (ref.year - hire.year) * 12 + (ref.month - hire.month)
    # 若當月日期未到到職日,扣一個月
    if ref.day < hire.day:
        months -= 1
    if months < 0:
        months = 0

    # 正確換算年數(以整月為準)
    years_complete = months // 12
    months_extra   = months % 12

    # 勞基法第38條逐段對應
    if months < 6:
        return 0
    elif months < 12:
        # 滿6個月未滿1年:3天
        return 3
    elif years_complete < 2:
        # 滿1年未滿2年:7天
        return 7
    elif years_complete < 3:
        # 滿2年未滿3年:10天
        return 10
    elif years_complete < 5:
        # 滿3年未滿5年:14天
        return 14
    elif years_complete < 10:
        # 滿5年未滿10年:15天
        return 15
    else:
        # 滿10年:16天,之後每年+1,上限30天
        # years_complete=10 -> extra=1 -> 15+1=16 ✓
        extra = years_complete - 9
        return min(15 + extra, 30)

def _calc_annual_leave_schedule(hire_date_str):
    """
    回傳員工特休天數完整排程表,供前端顯示用.
    每一列:{ label, days, date_reached, is_past, is_current }
    """
    if not hire_date_str:
        return []
    from datetime import date as _date
    import calendar as _cal

    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return []

    today = _date.today()

    milestones = [
        (6,   3,  '滿6個月'),
        (12,  7,  '滿1年'),
        (24, 10,  '滿2年'),
        (36, 14,  '滿3年'),
        (60, 15,  '滿5年'),
        (120,16,  '滿10年'),
        (132,17,  '滿11年'),
        (144,18,  '滿12年'),
        (156,19,  '滿13年'),
        (168,20,  '滿14年'),
        (180,21,  '滿15年'),
        (192,22,  '滿16年'),
        (204,23,  '滿17年'),
        (216,24,  '滿18年'),
        (228,25,  '滿19年'),
        (240,30,  '滿20年(上限30天)'),
    ]

    result      = []
    current_days = _calc_annual_leave_days(hire_date_str)

    for months_needed, days, label in milestones:
        total_m = hire.month + months_needed
        y = hire.year + (total_m - 1) // 12
        m = (total_m - 1) % 12 + 1
        max_day = _cal.monthrange(y, m)[1]
        try:
            reached = _date(y, m, min(hire.day, max_day))
        except Exception:
            continue

        result.append({
            'label':        label,
            'days':         days,
            'date_reached': reached.isoformat(),
            'is_past':      reached <= today,
            'is_current':   (days == current_days and reached <= today),
        })

    return result

def _get_staff_scheduled_dates(conn, staff_id, start_date_str, end_date_str):
    """取得員工在日期範圍內的排班日集合;無排班記錄則回傳 None(由呼叫方決定備援邏輯)"""
    rows = conn.execute("""
        SELECT DISTINCT shift_date FROM shift_assignments
        WHERE staff_id=%s AND shift_date BETWEEN %s AND %s
    """, (staff_id, start_date_str, end_date_str)).fetchall()
    if not rows:
        return None
    return {r['shift_date'].isoformat() if hasattr(r['shift_date'], 'isoformat') else str(r['shift_date']) for r in rows}

def _calc_leave_days(start_date_str, end_date_str, start_half=False, end_half=False,
                     scheduled_dates=None):
    """計算請假天數(含半天選項).
    有排班時以 scheduled_dates 為準;無排班備援排除週六日."""
    from datetime import date as _date, timedelta as _tdd
    try:
        s = _date.fromisoformat(start_date_str)
        e = _date.fromisoformat(end_date_str)
    except Exception:
        return 0.0
    if e < s: return 0.0
    days = 0.0
    cur  = s
    while cur <= e:
        is_workday = (cur.isoformat() in scheduled_dates) if scheduled_dates is not None \
                     else (cur.weekday() < 5)
        if is_workday:
            if cur == s and cur == e:
                # 單日:兩個 half 旗標都打表示上午半天(0.5天);只有 end_half 表示下午半天(0.5天)
                if start_half or end_half:
                    days += 0.5
                else:
                    days += 1.0
            elif cur == s and start_half:
                days += 0.5
            elif cur == e and end_half:
                days += 0.5
            else:
                days += 1.0
        cur += _tdd(days=1)
    return days

def _trigger_salary_regen_for_leave(conn, staff_id, month):
    """請假/加班狀態異動後,若該月已有薪資草稿則自動重算"""
    try:
        existing = conn.execute(
            "SELECT status FROM salary_records WHERE staff_id=%s AND month=%s",
            (staff_id, month)
        ).fetchone()
        if not existing or existing['status'] == 'confirmed':
            return
        staff = conn.execute("SELECT * FROM punch_staff WHERE id=%s", (staff_id,)).fetchone()
        if not staff:
            return
        data = _auto_generate_salary(conn, dict(staff), month)
        items_json = _json.dumps(data['items'], ensure_ascii=False)
        conn.execute("""
            UPDATE salary_records
            SET base_salary=%s, insured_salary=%s, work_days=%s, actual_days=%s,
                leave_days=%s, unpaid_days=%s, ot_pay=%s, allowance_total=%s,
                deduction_total=%s, net_pay=%s, items=%s::jsonb, updated_at=NOW()
            WHERE staff_id=%s AND month=%s AND status='draft'
        """, (
            data['base_salary'], data['insured_salary'], data['work_days'], data['actual_days'],
            data['leave_days'], data['unpaid_days'], data['ot_pay'], data['allowance_total'],
            data['deduction_total'], data['net_pay'], items_json,
            staff_id, month,
        ))
    except Exception as _e:
        print(f"[salary_regen] 自動重算失敗 staff={staff_id} month={month}: {_e}")

def _update_leave_balance(conn, staff_id, leave_type_id, year_str, delta_days):
    year = int(year_str)
    conn.execute("""
        INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
        VALUES (%s, %s, %s, 0, %s)
        ON CONFLICT (staff_id, leave_type_id, year) DO UPDATE
          SET used_days = leave_balances.used_days + EXCLUDED.used_days,
              updated_at = NOW()
    """, (staff_id, leave_type_id, year, delta_days))

def _calc_punch_hours(conn, staff_id, month):
    """
    從打卡記錄計算實際工時(時薪制用)
    邏輯:每天找最早 in + 最晚 out,扣除休息時間
    回傳 (total_hours, work_days, details)
    """
    from datetime import datetime as _dth, timezone as _tzh, timedelta as _tdh
    TW = _tzh(_tdh(hours=8))

    _ts_s, _ts_e = _month_ts_range(month)
    rows = conn.execute("""
        SELECT punch_type, punched_at
        FROM punch_records
        WHERE staff_id=%s
          AND punched_at >= %s AND punched_at < %s
        ORDER BY punched_at ASC
    """, (staff_id, _ts_s, _ts_e)).fetchall()

    # Group by date
    day_map = {}
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_tzh.utc)
        pa_tw  = pa.astimezone(TW)
        ds     = pa_tw.strftime('%Y-%m-%d')
        if ds not in day_map:
            day_map[ds] = []
        day_map[ds].append({'type': r['punch_type'], 'dt': pa_tw})

    # 跨日班次合併:day N 有上班無下班 + day N+1 有下班無上班 -> 歸入 day N
    from datetime import date as _dch, timedelta as _tdch
    for _d1 in sorted(day_map.keys()):
        _d2 = (_dch.fromisoformat(_d1) + _tdch(days=1)).isoformat()
        if _d2 not in day_map:
            continue
        _p1, _p2 = day_map[_d1], day_map[_d2]
        _has_in1  = any(p['type'] == 'in'  for p in _p1)
        _has_out1 = any(p['type'] == 'out' for p in _p1)
        _has_in2  = any(p['type'] == 'in'  for p in _p2)
        _has_out2 = any(p['type'] == 'out' for p in _p2)
        if _has_in1 and not _has_out1 and _has_out2 and not _has_in2:
            # 將 day N+1 的下班及休息結束打卡移入 day N
            day_map[_d1] = _p1 + [p for p in _p2 if p['type'] in ('out', 'break_in')]
            day_map[_d2] = [p for p in _p2 if p['type'] not in ('out', 'break_in')]
            if not day_map[_d2]:
                del day_map[_d2]

    total_hours = 0.0
    details     = []
    for ds, punches in sorted(day_map.items()):
        ins   = [p['dt'] for p in punches if p['type'] == 'in']
        outs  = [p['dt'] for p in punches if p['type'] == 'out']
        b_out = [p['dt'] for p in punches if p['type'] == 'break_out']
        b_in  = [p['dt'] for p in punches if p['type'] == 'break_in']

        if not ins or not outs:
            continue

        work_start = min(ins)
        work_end   = max(outs)
        gross_mins = (work_end - work_start).total_seconds() / 60

        # 扣除休息時間
        break_mins = 0.0
        for bo in b_out:
            # 找最近的 break_in
            matched = [bi for bi in b_in if bi > bo]
            if matched:
                break_mins += (min(matched) - bo).total_seconds() / 60

        net_mins = max(0.0, gross_mins - break_mins)
        net_hrs  = round(net_mins / 60, 2)
        total_hours += net_hrs
        details.append({
            'date':        ds,
            'clock_in':    work_start.strftime('%H:%M'),
            'clock_out':   work_end.strftime('%H:%M'),
            'break_mins':  round(break_mins),
            'net_hours':   net_hrs,
        })

    return round(total_hours, 2), len(day_map), details

def _auto_generate_salary(conn, staff, month, work_days=None):
    """
    自動產生員工月薪資料
    ─ 月薪制:底薪 + 薪資項目公式 + 加班費 - 請假扣款
    ─ 時薪制:打卡實際工時 × 時薪 + 加班費 - 請假扣款
    """
    import calendar as _cal2
    from datetime import date as _d5, timedelta as _td5, datetime as _dts5, timezone as _tz5
    _TW5 = _tz5(_td5(hours=8))
    _today5 = _dts5.now(_TW5).date()
    y, m = int(month[:4]), int(month[5:])
    total_work_days = work_days
    scheduled_dates = set()

    _sal_d_s, _sal_d_e = _month_date_range(month)
    _sal_ts_s, _sal_ts_e = _month_ts_range(month)

    if total_work_days is None:
        # 1. 優先從排班取工作日
        shift_date_rows = conn.execute("""
            SELECT DISTINCT shift_date FROM shift_assignments
            WHERE staff_id=%s AND shift_date >= %s AND shift_date < %s
            ORDER BY shift_date
        """, (staff['id'], _sal_d_s, _sal_d_e)).fetchall()
        if shift_date_rows:
            scheduled_dates = {r['shift_date'].isoformat() if hasattr(r['shift_date'], 'isoformat') else str(r['shift_date']) for r in shift_date_rows}
            total_work_days = len(scheduled_dates)
        else:
            # 2. 備援:日曆扣除週日 + 國定假日
            holiday_rows = conn.execute("""
                SELECT date FROM public_holidays
                WHERE date >= %s AND date < %s
            """, (_sal_d_s, _sal_d_e)).fetchall()
            holiday_dates = {r['date'].isoformat() if hasattr(r['date'], 'isoformat') else str(r['date']) for r in holiday_rows}
            days_in_month = _cal2.monthrange(y, m)[1]
            for _d in range(1, days_in_month + 1):
                _dt = _d5(y, m, _d)
                _ds = _dt.isoformat()
                if _dt.weekday() < 5 and _ds not in holiday_dates:
                    scheduled_dates.add(_ds)
            total_work_days = len(scheduled_dates)

    salary_type    = staff.get('salary_type', 'monthly') or 'monthly'
    base_salary    = float(staff.get('base_salary')    or 0)
    hourly_rate    = float(staff.get('hourly_rate')    or 0)
    insured_salary = float(staff.get('insured_salary') or base_salary)
    daily_hours    = float(staff.get('daily_hours')    or 8)
    service_years  = _calc_service_years(staff.get('hire_date'))

    # ── 時薪制:從打卡記錄計算工時 ──────────────────────────
    actual_work_hours = 0.0
    punch_details     = []
    if salary_type == 'hourly':
        actual_work_hours, punch_work_days, punch_details = _calc_punch_hours(
            conn, staff['id'], month
        )
        # 時薪制的 base_salary 等於 實際工時 × 時薪
        hourly_base_pay = round(actual_work_hours * hourly_rate, 2)
    else:
        # 月薪制:daily_wage 用於請假扣款
        hourly_base_pay = 0.0

    # ── 已核准加班費 ────────────────────────────────────────
    ot_rows = conn.execute("""
        SELECT COALESCE(SUM(ot_pay), 0) as total
        FROM overtime_requests
        WHERE staff_id=%s AND status='approved'
          AND request_date >= %s AND request_date < %s
    """, (staff['id'], _sal_d_s, _sal_d_e)).fetchone()
    ot_pay = float(ot_rows['total']) if ot_rows else 0.0

    # ── 請假資訊 ────────────────────────────────────────────
    leave_rows = conn.execute("""
        SELECT lr.total_days, lr.start_time, lt.pay_rate, lt.code, lt.name as leave_name
        FROM leave_requests lr
        JOIN leave_types lt ON lt.id = lr.leave_type_id
        WHERE lr.staff_id=%s AND lr.status='approved'
          AND lr.start_date >= %s AND lr.start_date < %s
    """, (staff['id'], _sal_d_s, _sal_d_e)).fetchall()
    leave_days    = sum(float(r['total_days']) for r in leave_rows)
    unpaid_days   = sum(float(r['total_days']) for r in leave_rows if float(r['pay_rate']) == 0)
    half_pay_days = sum(float(r['total_days']) for r in leave_rows if 0 < float(r['pay_rate']) < 1)
    actual_days   = total_work_days - leave_days

    # 全天請假(start_time 為空)才計入全勤判斷,小時請假不影響全勤
    full_day_rows  = [r for r in leave_rows if not (r['start_time'] or '').strip()]
    fd_leave_days  = sum(float(r['total_days']) for r in full_day_rows)
    personal_days  = sum(float(r['total_days']) for r in full_day_rows
                         if '事假' in (r['leave_name'] or '') or (r['code'] or '').startswith('personal'))
    sick_days      = sum(float(r['total_days']) for r in full_day_rows
                         if '病假' in (r['leave_name'] or '') or (r['code'] or '').startswith('sick'))

    # ── 日薪 / 時薪(用於請假扣款) ───────────────────────
    if salary_type == 'hourly':
        daily_wage  = hourly_rate * daily_hours   # 時薪制日薪 = 時薪 × 每日工時
        hourly_wage = hourly_rate
    else:
        daily_wage  = base_salary / 30 if base_salary > 0 else 0
        hourly_wage = daily_wage / daily_hours if daily_hours > 0 else 0

    # ── 月薪制:提前計算缺勤天數,讓 _attendance_vars 中 actual_days 正確 ──
    absent_days      = 0
    absent_date_list = []
    if salary_type == 'monthly' and scheduled_dates and daily_wage > 0:
        _punch_rows_pre = conn.execute("""
            SELECT DISTINCT (punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date
            FROM punch_records WHERE staff_id=%s
              AND punched_at >= %s AND punched_at < %s
        """, (staff['id'], _sal_ts_s, _sal_ts_e)).fetchall()
        _punched_dates_pre = {
            r['work_date'].isoformat() if hasattr(r['work_date'], 'isoformat') else str(r['work_date'])
            for r in _punch_rows_pre
        }
        _leave_date_rows_pre = conn.execute("""
            SELECT start_date, end_date FROM leave_requests
            WHERE staff_id=%s AND status='approved'
              AND start_date >= %s AND start_date < %s
        """, (staff['id'], _sal_d_s, _sal_d_e)).fetchall()
        _leave_date_set_pre = set()
        for _lr in _leave_date_rows_pre:
            _ld = _lr['start_date']
            _le = _lr['end_date']
            while _ld <= _le:
                _leave_date_set_pre.add(_ld.isoformat() if hasattr(_ld, 'isoformat') else str(_ld))
                _ld += _td5(days=1)
        absent_date_list = sorted(
            ds for ds in scheduled_dates
            if ds not in _punched_dates_pre and ds not in _leave_date_set_pre
               and _d5.fromisoformat(ds) < _today5
        )
        absent_days = len(absent_date_list)
    actual_days = max(0, actual_days - absent_days)

    # 公式可用的出勤變數(leave_days/personal_days/sick_days 只計全天請假,小時請假不影響全勤)
    _attendance_vars = {
        'actual_days':   float(actual_days),
        'work_days':     float(total_work_days),
        'personal_days': personal_days,
        'sick_days':     sick_days,
        'leave_days':    fd_leave_days,
        'unpaid_days':   unpaid_days,
        'daily_wage':    daily_wage,
    }

    # ── 組裝薪資項目 ────────────────────────────────────────
    items           = []
    allowance_total = 0.0
    deduction_total = 0.0
    # 員工個人金額覆寫 {str(item_id): amount}
    _overrides = staff.get('salary_item_overrides') or {}
    if isinstance(_overrides, str):
        try: _overrides = _json.loads(_overrides)
        except Exception: _overrides = {}

    def _apply_override(item_id, calculated_amt):
        """若員工設有個人金額,使用個人金額;否則使用計算值"""
        key = str(item_id)
        if key in _overrides and _overrides[key] is not None and _overrides[key] != '':
            return float(_overrides[key]), True   # (amount, is_overridden)
        return calculated_amt, False

    if salary_type == 'hourly':
        # 時薪制:第一筆項目是「本薪(工時計算)」
        items.append({
            'id': 'hourly_base', 'name': '本薪(工時)', 'type': 'allowance',
            'amount': hourly_base_pay, 'formula': '',
            'calc_note': (
                f'{actual_work_hours}h × 時薪${hourly_rate}'
                + (f'({len(punch_details)}天出勤)' if punch_details else '')
            ),
        })
        allowance_total += hourly_base_pay

        # 時薪制加班費(從打卡計算,若無申請記錄則估算)
        # 先用「加班申請」核准金額;若為 0 則嘗試從工時估算
        if ot_pay == 0 and actual_work_hours > 0:
            # 每天超過 daily_hours 的部分算加班
            for pd in punch_details:
                overtime_h = max(0.0, pd['net_hours'] - daily_hours)
                if overtime_h > 0:
                    h1 = min(overtime_h, 2.0)
                    h2 = max(0.0, overtime_h - 2.0)
                    rate1 = float(staff.get('ot_rate1') or 1.33)
                    rate2 = float(staff.get('ot_rate2') or 1.67)
                    ot_pay += round(hourly_rate * (h1 * rate1 + h2 * rate2), 2)

        # 時薪制的保險費以 insured_salary 為準(若未設定則用月薪換算)
        if insured_salary == 0:
            insured_salary = round(hourly_rate * daily_hours * 30, 0)

        # 時薪制只加入保險類扣除項(若員工有指定則只取指定中的保險項)
        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            salary_items_rows = conn.execute(f"""
                SELECT * FROM salary_items
                WHERE active=TRUE AND id IN ({placeholders})
                  AND item_type='deduction'
                  AND (formula LIKE '%%insured_salary%%' OR formula LIKE '%%base_salary%%')
                ORDER BY sort_order, id
            """, staff_item_ids).fetchall()
        else:
            salary_items_rows = conn.execute("""
                SELECT * FROM salary_items
                WHERE active=TRUE
                  AND item_type='deduction'
                  AND (formula LIKE '%insured_salary%' OR formula LIKE '%base_salary%')
                ORDER BY sort_order, id
            """).fetchall()
        for it in salary_items_rows:
            calc_amt = _eval_formula(it['formula'] or '', base_salary,
                                     insured_salary, service_years, _attendance_vars)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else (it['formula'] or '')
            items.append({
                'id': it['id'], 'name': it['name'], 'type': 'deduction',
                'amount': round(amt, 2), 'formula': it['formula'] or '',
                'calc_note': note,
            })
            deduction_total += amt

    else:
        # 月薪制:跑啟用的薪資項目(若員工有指定則只跑指定項目)
        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            items_rows = conn.execute(
                f"SELECT * FROM salary_items WHERE active=TRUE AND id IN ({placeholders}) ORDER BY sort_order, id",
                staff_item_ids
            ).fetchall()
        else:
            items_rows = conn.execute(
                "SELECT * FROM salary_items WHERE active=TRUE ORDER BY sort_order, id"
            ).fetchall()
        for it in items_rows:
            formula  = it['formula'] or ''
            calc_amt = float(it['amount'] or 0)
            if formula:
                calc_amt = _eval_formula(formula, base_salary, insured_salary, service_years, _attendance_vars)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else formula
            items.append({
                'id':        it['id'],
                'name':      it['name'],
                'type':      it['item_type'],
                'amount':    round(amt, 2),
                'formula':   formula,
                'calc_note': note,
            })
            if it['item_type'] == 'allowance':
                allowance_total += amt
            else:
                deduction_total += amt

    # ── 加班費(申請核准) ──────────────────────────────────
    if ot_pay > 0:
        items.append({
            'id': 'ot', 'name': '加班費(申請)', 'type': 'allowance',
            'amount': round(ot_pay, 2), 'formula': '',
            'calc_note': '核准加班費合計',
        })
        allowance_total += ot_pay

    # ── 請假扣款 ────────────────────────────────────────────
    if unpaid_days > 0 and daily_wage > 0:
        leave_names = '、'.join(set(
            r['leave_name'] for r in leave_rows if float(r['pay_rate']) == 0
        ))
        deduct = round(daily_wage * unpaid_days, 2)
        items.append({
            'id': 'unpaid', 'name': f'無薪假扣款({leave_names})', 'type': 'deduction',
            'amount': deduct, 'formula': '',
            'calc_note': f'{unpaid_days}天 × 日薪${round(daily_wage, 0)}',
        })
        deduction_total += deduct

    # 半薪假:依各假別的 pay_rate 分組計算,扣款 = daily_wage × 天數 × (1 - pay_rate)
    if half_pay_days > 0 and daily_wage > 0:
        from collections import defaultdict as _dd
        _hp_groups = _dd(lambda: {'days': 0.0, 'names': set()})
        for r in leave_rows:
            pr = float(r['pay_rate'])
            if 0 < pr < 1:
                _hp_groups[pr]['days']  += float(r['total_days'])
                _hp_groups[pr]['names'].add(r['leave_name'])
        for pr, grp in sorted(_hp_groups.items()):
            if grp['days'] > 0:
                deduct_rate = round(1 - pr, 6)
                deduct = round(daily_wage * grp['days'] * deduct_rate, 2)
                leave_names = '、'.join(grp['names'])
                items.append({
                    'id': f'halfpay_{int(pr*100)}',
                    'name': f'半薪假扣款({leave_names})', 'type': 'deduction',
                    'amount': deduct, 'formula': '',
                    'calc_note': f"{grp['days']}天 × 日薪${round(daily_wage, 0)} × {deduct_rate:.0%}",
                })
                deduction_total += deduct

    # ── 月薪制:缺勤扣款項目(absent_days 已在 _attendance_vars 前計算完畢) ──
    if absent_days > 0 and daily_wage > 0:
        deduct = round(daily_wage * absent_days, 2)
        sample = '、'.join(absent_date_list[:3]) + ('等' if absent_days > 3 else '')
        items.append({
            'id': 'absent', 'name': f'缺勤扣款({absent_days} 天)', 'type': 'deduction',
            'amount': deduct, 'formula': '',
            'calc_note': f'{absent_days} 天 × 日薪 ${round(daily_wage, 0)}({sample})',
        })
        deduction_total += deduct

    net_pay = round(allowance_total - deduction_total, 2)

    return {
        'staff_id':           staff['id'],
        'month':              month,
        'salary_type':        salary_type,
        'base_salary':        base_salary if salary_type == 'monthly' else 0,
        'hourly_rate':        hourly_rate if salary_type == 'hourly' else 0,
        'hourly_base_pay':    hourly_base_pay if salary_type == 'hourly' else 0,
        'actual_work_hours':  actual_work_hours if salary_type == 'hourly' else 0,
        'insured_salary':     insured_salary,
        'work_days':          total_work_days,
        'actual_days':        actual_days,
        'leave_days':         leave_days,
        'unpaid_days':        unpaid_days,
        'absent_days':        absent_days,
        'ot_pay':             ot_pay,
        'allowance_total':    round(allowance_total, 2),
        'deduction_total':    round(deduction_total, 2),
        'net_pay':            net_pay,
        'items':              items,
        'punch_details':      punch_details,   # 時薪制:每日打卡明細
        'status':             'draft',
    }

def _is_holiday(conn, date_str):
    """Check if a date is a public holiday"""
    row = conn.execute(
        "SELECT id FROM public_holidays WHERE date=%s", (date_str,)
    ).fetchone()
    return row is not None

def _notify_staff_line(staff_id, message):
    """
    Send LINE notification to a staff member if they have LINE bound.
    Uses the line_punch_config token (same LINE OA).
    """
    if not DATABASE_URL:
        return
    try:
        with get_db() as conn:
            staff = conn.execute(
                "SELECT line_user_id FROM punch_staff WHERE id=%s", (staff_id,)
            ).fetchone()
            if not staff or not staff['line_user_id']:
                return
            cfg = conn.execute(
                "SELECT * FROM line_punch_config WHERE id=1"
            ).fetchone()
        if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
            return
        LineBotApi(cfg['channel_access_token']).push_message(
            staff['line_user_id'],
            TextSendMessage(text=message)
        )
    except Exception as e:
        print(f"[LINE notify] staff_id={staff_id}: {e}")

def _notify_review_result(staff_id, category, action, extra_info=''):
    """
    Send a formatted LINE notification for review results.
    category: '補打卡申請', '排休申請', '加班申請', '請假申請', '薪資確認'
    action:   'approved', 'rejected', 'confirmed'
    """
    ACTION_LABEL = {'approved': '核准', 'rejected': '退回', 'confirmed': '確認'}
    ACTION_ICON  = {'approved': '[核准]', 'rejected': '[退回]', 'confirmed': '[確認]'}
    label = ACTION_LABEL.get(action, action)
    icon  = ACTION_ICON.get(action, '')
    msg   = f"{icon} {category}{label}\n{extra_info}\n\n請至員工系統查看詳情."
    _notify_staff_line(staff_id, msg.strip())

def _broadcast_announcement_line(title, content):
    """廣播公告給所有已綁定 LINE 的在職員工"""
    try:
        with get_db() as conn:
            cfg = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
            if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
                return
            staff_rows = conn.execute(
                "SELECT line_user_id FROM punch_staff WHERE active=TRUE AND line_user_id IS NOT NULL"
            ).fetchall()
        if not staff_rows:
            return
        api = LineBotApi(cfg['channel_access_token'])
        snippet = content[:60] + ('…' if len(content) > 60 else '')
        msg = f"[公告] {title}\n{snippet}\n\n請至員工系統查看完整公告."
        for s in staff_rows:
            try:
                api.push_message(s['line_user_id'], TextSendMessage(text=msg))
            except Exception as e:
                print(f"[LINE broadcast] {s['line_user_id']}: {e}")
    except Exception as e:
        print(f"[LINE broadcast] error: {e}")


# ── 績效評級 ──
_DEFAULT_GRADE_CONFIG = [
    {'grade': 'A', 'label': '優秀', 'min_pct': 90},
    {'grade': 'B', 'label': '良好', 'min_pct': 75},
    {'grade': 'C', 'label': '待加強', 'min_pct': 60},
    {'grade': 'D', 'label': '需改善', 'min_pct':  0},
]

def _get_grade_config():
    """從 DB 讀取評級設定,若未設定則回傳預設值(按門檻由高到低排序)."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM performance_config WHERE key='grade_config'"
            ).fetchone()
        if row:
            cfg = row['value']
            if isinstance(cfg, str):
                cfg = _json.loads(cfg)
            if isinstance(cfg, list) and cfg:
                return sorted(cfg, key=lambda x: -float(x.get('min_pct', 0)))
    except Exception:
        pass
    return _DEFAULT_GRADE_CONFIG

def _grade_labels():
    return {c['grade']: c['label'] for c in _get_grade_config()}




def _score_to_grade(pct):
    for cfg in _get_grade_config():
        if pct >= cfg['min_pct']:
            return cfg['grade']
    return _get_grade_config()[-1]['grade']


# ── 財務設定(公司抬頭等,供 finance 與報表共用)──
def _get_finance_settings():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT setting_key, setting_value FROM finance_settings").fetchall()
            return {r['setting_key']: r['setting_value'] for r in rows}
    except:
        return {}
