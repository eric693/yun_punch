"""
routes/punch.py - Employee punch clock, staff CRUD, attendance summary routes.
"""
import math
import psycopg
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from flask import Blueprint, request, jsonify, render_template, session

from core.database import get_db, _hash_pw
from core.auth import login_required
from core.helpers import (
    TW_TZ, _gps_distance, punch_staff_row, punch_record_row,
    loc_row, punch_req_row, _parse_tw_datetime, _trigger_salary_regen_for_leave,
    _month_ts_range,
)

bp = Blueprint('punch', __name__)


@bp.route('/punch')
@bp.route('/staff')
def punch_page():
    return render_template('staff.html')


# ── Employee Session ──────────────────────────────────────────────

@bp.route('/api/punch/login', methods=['POST'])
def api_punch_login():
    b = request.get_json(force=True)
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': '請輸入帳號及密碼'}), 400
    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM punch_staff WHERE username=%s AND active=TRUE", (username,)
        ).fetchone()
    if not staff or staff['password_hash'] != _hash_pw(password):
        return jsonify({'error': '帳號或密碼錯誤'}), 401
    session['punch_staff_id']   = staff['id']
    session['punch_staff_name'] = staff['name']
    return jsonify({'id': staff['id'], 'name': staff['name'], 'role': staff['role']})


@bp.route('/api/punch/logout', methods=['POST'])
def api_punch_logout():
    session.pop('punch_staff_id', None)
    session.pop('punch_staff_name', None)
    return jsonify({'ok': True})


@bp.route('/api/punch/me', methods=['GET'])
def api_punch_me():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        staff = conn.execute(
            "SELECT id,name,role FROM punch_staff WHERE id=%s AND active=TRUE", (sid,)
        ).fetchone()
    if not staff:
        session.pop('punch_staff_id', None)
        return jsonify({'error': 'not logged in'}), 401
    return jsonify(dict(staff))


# ── GPS Settings ──────────────────────────────────────────────────

@bp.route('/api/punch/settings', methods=['GET'])
def api_punch_settings_get():
    """Public: GPS config + active locations for the punch page."""
    with get_db() as conn:
        cfg  = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
        locs = conn.execute(
            "SELECT * FROM punch_locations WHERE active=TRUE ORDER BY id"
        ).fetchall()
    return jsonify({
        'gps_required': cfg['gps_required'] if cfg else False,
        'locations': [loc_row(r) for r in locs]
    })


@bp.route('/api/punch/config', methods=['PUT'])
@login_required
def api_punch_config_update():
    b = request.get_json(force=True)
    gps_required = bool(b.get('gps_required', False))
    with get_db() as conn:
        conn.execute(
            "UPDATE punch_config SET gps_required=%s, updated_at=NOW() WHERE id=1",
            (gps_required,)
        )
    return jsonify({'gps_required': gps_required})


@bp.route('/api/punch/locations', methods=['GET'])
@login_required
def api_punch_locations_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM punch_locations ORDER BY id").fetchall()
    return jsonify([loc_row(r) for r in rows])


@bp.route('/api/punch/locations', methods=['POST'])
@login_required
def api_punch_locations_create():
    b = request.get_json(force=True)
    name = b.get('location_name', '').strip() or '打卡地點'
    try:
        lat = float(b['lat']); lng = float(b['lng'])
    except Exception:
        return jsonify({'error': '請填入有效的緯度和經度'}), 400
    radius_m = int(b.get('radius_m') or 100)
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO punch_locations (location_name, lat, lng, radius_m) VALUES (%s,%s,%s,%s) RETURNING *",
            (name, lat, lng, radius_m)
        ).fetchone()
    return jsonify(loc_row(row)), 201


@bp.route('/api/punch/locations/<int:lid>', methods=['PUT'])
@login_required
def api_punch_locations_update(lid):
    b = request.get_json(force=True)
    name = b.get('location_name', '').strip() or '打卡地點'
    try:
        lat = float(b['lat']); lng = float(b['lng'])
    except Exception:
        return jsonify({'error': '請填入有效的緯度和經度'}), 400
    radius_m = int(b.get('radius_m') or 100)
    active   = bool(b.get('active', True))
    with get_db() as conn:
        row = conn.execute(
            "UPDATE punch_locations SET location_name=%s,lat=%s,lng=%s,radius_m=%s,active=%s,updated_at=NOW() WHERE id=%s RETURNING *",
            (name, lat, lng, radius_m, active, lid)
        ).fetchone()
    return jsonify(loc_row(row)) if row else ('', 404)


@bp.route('/api/punch/locations/<int:lid>', methods=['DELETE'])
@login_required
def api_punch_locations_delete(lid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_locations WHERE id=%s", (lid,))
    return jsonify({'deleted': lid})


# ── Clock In/Out ──────────────────────────────────────────────────

@bp.route('/api/punch/clock', methods=['POST'])
def api_punch_clock():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401

    b          = request.get_json(force=True)
    punch_type = b.get('punch_type')
    lat        = b.get('lat')
    lng        = b.get('lng')

    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400

    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM punch_staff WHERE id=%s AND active=TRUE", (sid,)
        ).fetchone()
        if not staff:
            return jsonify({'error': '員工不存在'}), 404
        cfg  = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
        locs = conn.execute("SELECT * FROM punch_locations WHERE active=TRUE").fetchall()

    gps_required = cfg['gps_required'] if cfg else False
    gps_distance = None
    matched_loc  = None

    if lat is not None and lng is not None and locs:
        for loc in locs:
            d = _gps_distance(lat, lng, float(loc['lat']), float(loc['lng']))
            if gps_distance is None or d < gps_distance:
                gps_distance = d
                matched_loc  = loc

    if gps_required:
        if lat is None or lng is None:
            return jsonify({'error': '無法取得 GPS，請允許定位權限後重試'}), 403
        if not locs:
            return jsonify({'error': '管理員尚未設定任何打卡地點'}), 403
        if gps_distance is None or gps_distance > int(matched_loc['radius_m']):
            return jsonify({
                'error': f'距離最近地點「{matched_loc["location_name"]}」{gps_distance} 公尺，超出允許範圍（{matched_loc["radius_m"]} 公尺）',
                'distance': gps_distance,
                'radius': int(matched_loc['radius_m'])
            }), 403

    with get_db() as conn:
        recent = conn.execute("""
            SELECT id FROM punch_records
            WHERE staff_id=%s AND punch_type=%s
              AND punched_at > NOW() - INTERVAL '1 minute'
        """, (sid, punch_type)).fetchone()
        if recent:
            return jsonify({'error': '1 分鐘內已打過卡'}), 429

        matched_name = matched_loc['location_name'] if matched_loc else ''
        row = conn.execute("""
            INSERT INTO punch_records
              (staff_id, punch_type, latitude, longitude, gps_distance, location_name)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
        """, (sid, punch_type, lat, lng, gps_distance, matched_name)).fetchone()

    d = punch_record_row(row)
    d['staff_name']   = staff['name']
    d['gps_distance'] = gps_distance
    return jsonify(d), 201


@bp.route('/api/punch/today', methods=['GET'])
def api_punch_today():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify([])
    with get_db() as conn:
        rows = conn.execute("""
            SELECT pr.*, ps.name as staff_name
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE pr.staff_id=%s
              AND (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
                = (NOW() AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY pr.punched_at ASC
        """, (sid,)).fetchall()
    return jsonify([punch_record_row(r) for r in rows])


@bp.route('/api/punch/my-records', methods=['GET'])
def api_punch_my_records():
    """Employee self-service: own punch records for a month."""
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    month = request.args.get('month', '')
    if not month:
        from datetime import timezone as _tz2, timedelta as _tda
        month = _dt.now(_tz2(_tda(hours=8))).strftime('%Y-%m')
    _ts_start, _ts_end = _month_ts_range(month)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT punch_type, punched_at, gps_distance, location_name, is_manual
            FROM punch_records
            WHERE staff_id=%s
              AND punched_at >= %s AND punched_at < %s
            ORDER BY punched_at ASC
        """, (sid, _ts_start, _ts_end)).fetchall()
    from datetime import timezone as _tz2, timedelta as _tdb
    TW = _tz2(_tdb(hours=8))
    LABEL = {'in': '上班', 'out': '下班', 'break_out': '休息開始', 'break_in': '休息結束'}
    result = {}
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            from datetime import timezone as _utz
            pa = pa.replace(tzinfo=_utz.utc)
        pa_tw    = pa.astimezone(TW)
        date_str = pa_tw.strftime('%Y-%m-%d')
        time_str = pa_tw.strftime('%H:%M')
        if date_str not in result:
            result[date_str] = []
        result[date_str].append({
            'type':          r['punch_type'],
            'label':         LABEL.get(r['punch_type'], r['punch_type']),
            'time':          time_str,
            'gps_distance':  r['gps_distance'],
            'location_name': r['location_name'] or '',
            'is_manual':     bool(r['is_manual']),
        })
    return jsonify({'month': month, 'records': result})


# ── Admin: Staff CRUD ─────────────────────────────────────────────

@bp.route('/api/punch/staff', methods=['GET'])
@login_required
def api_punch_staff_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM punch_staff ORDER BY name").fetchall()
    return jsonify([punch_staff_row(r) for r in rows])


@bp.route('/api/punch/staff', methods=['POST'])
@login_required
def api_punch_staff_create():
    b        = request.get_json(force=True)
    name     = b.get('name', '').strip()
    username = b.get('username', '').strip()
    password = b.get('password', '').strip()
    if not name:     return jsonify({'error': '姓名為必填'}), 400
    if not username: return jsonify({'error': '帳號為必填'}), 400
    if not password or len(password) < 4:
        return jsonify({'error': '密碼至少 4 個字元'}), 400
    employee_code = b.get('employee_code', '') or None
    if employee_code: employee_code = employee_code.strip() or None
    department     = (b.get('department') or '').strip()
    hire_date      = b.get('hire_date') or None
    birth_date     = b.get('birth_date') or None
    bank_code      = (b.get('bank_code') or '').strip()
    bank_name      = (b.get('bank_name') or '').strip()
    bank_branch    = (b.get('bank_branch') or '').strip()
    bank_account   = (b.get('bank_account') or '').strip()
    account_holder = (b.get('account_holder') or '').strip()
    try:
        with get_db() as conn:
            row = conn.execute("""
                INSERT INTO punch_staff
                  (name, username, password_hash, password_plain, role, employee_code,
                   department, hire_date, birth_date,
                   bank_code, bank_name, bank_branch, bank_account, account_holder)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """, (name, username, _hash_pw(password), password, b.get('role', '').strip(), employee_code,
                  department, hire_date, birth_date,
                  bank_code, bank_name, bank_branch, bank_account, account_holder)).fetchone()
        return jsonify(punch_staff_row(row)), 201
    except psycopg.errors.UniqueViolation:
        return jsonify({'error': '姓名或帳號已存在，請換一個'}), 409
    except Exception as e:
        print(f"[punch_staff_create] error: {e}")
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': '姓名或帳號已存在，請換一個'}), 409
        return jsonify({'error': f'新增失敗：{str(e)}'}), 500


@bp.route('/api/punch/staff/<int:sid>', methods=['PUT'])
@login_required
def api_punch_staff_update(sid):
    b             = request.get_json(force=True)
    name          = b.get('name', '').strip()
    username      = b.get('username', '').strip()
    password      = b.get('password', '').strip()
    role          = b.get('role', '').strip()
    active        = bool(b.get('active', True))
    employee_code = b.get('employee_code', '') or None
    if employee_code: employee_code = employee_code.strip() or None
    bank_code      = (b.get('bank_code') or '').strip()
    bank_name      = (b.get('bank_name') or '').strip()
    bank_branch    = (b.get('bank_branch') or '').strip()
    bank_account   = (b.get('bank_account') or '').strip()
    account_holder = (b.get('account_holder') or '').strip()
    department     = (b.get('department') or '').strip()
    hire_date      = b.get('hire_date') or None
    birth_date     = b.get('birth_date') or None
    if not name or not username:
        return jsonify({'error': '姓名和帳號為必填'}), 400
    with get_db() as conn:
        if password:
            if len(password) < 4:
                return jsonify({'error': '密碼至少 4 個字元'}), 400
            row = conn.execute("""
                UPDATE punch_staff
                SET name=%s,username=%s,password_hash=%s,password_plain=%s,role=%s,active=%s,employee_code=%s,
                    department=%s,hire_date=%s,birth_date=%s,
                    bank_code=%s,bank_name=%s,bank_branch=%s,bank_account=%s,account_holder=%s
                WHERE id=%s RETURNING *
            """, (name, username, _hash_pw(password), password, role, active, employee_code,
                  department, hire_date, birth_date,
                  bank_code, bank_name, bank_branch, bank_account, account_holder, sid)).fetchone()
        else:
            row = conn.execute("""
                UPDATE punch_staff
                SET name=%s,username=%s,role=%s,active=%s,employee_code=%s,
                    department=%s,hire_date=%s,birth_date=%s,
                    bank_code=%s,bank_name=%s,bank_branch=%s,bank_account=%s,account_holder=%s
                WHERE id=%s RETURNING *
            """, (name, username, role, active, employee_code,
                  department, hire_date, birth_date,
                  bank_code, bank_name, bank_branch, bank_account, account_holder, sid)).fetchone()
    return jsonify(punch_staff_row(row)) if row else ('', 404)


@bp.route('/api/punch/staff/<int:sid>', methods=['DELETE'])
@login_required
def api_punch_staff_delete(sid):
    with get_db() as conn:
        punch_cnt = conn.execute(
            "SELECT COUNT(*) AS n FROM punch_records WHERE staff_id=%s", (sid,)
        ).fetchone()['n']
        leave_cnt = conn.execute(
            "SELECT COUNT(*) AS n FROM leave_requests WHERE staff_id=%s", (sid,)
        ).fetchone()['n']
        salary_cnt = conn.execute(
            "SELECT COUNT(*) AS n FROM salary_records WHERE staff_id=%s", (sid,)
        ).fetchone()['n']
        if punch_cnt + leave_cnt + salary_cnt > 0:
            return jsonify({
                'error': f'此員工有歷史資料（打卡 {punch_cnt} 筆、假單 {leave_cnt} 筆、薪資 {salary_cnt} 筆），'
                         '請改用「離職」功能停用帳號以保留記錄。'
            }), 409
        conn.execute("DELETE FROM punch_staff WHERE id=%s", (sid,))
    return jsonify({'deleted': sid})


# ── Admin: Punch Records ──────────────────────────────────────────

@bp.route('/api/punch/records', methods=['GET'])
@login_required
def api_punch_records():
    staff_id  = request.args.get('staff_id')
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to')
    month     = request.args.get('month')

    conds, params = ["TRUE"], []
    if staff_id: conds.append("pr.staff_id=%s"); params.append(int(staff_id))
    if month:
        conds.append("TO_CHAR(pr.punched_at,'YYYY-MM')=%s"); params.append(month)
    elif date_from:
        conds.append("(pr.punched_at AT TIME ZONE 'Asia/Taipei')::date>=%s"); params.append(date_from)
        if date_to:
            conds.append("(pr.punched_at AT TIME ZONE 'Asia/Taipei')::date<=%s"); params.append(date_to)

    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*, ps.name as staff_name, ps.role as staff_role
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.punched_at DESC LIMIT 500
        """, params).fetchall()
    return jsonify([punch_record_row(r) for r in rows])


@bp.route('/api/punch/records', methods=['POST'])
@login_required
def api_punch_record_manual():
    b          = request.get_json(force=True)
    staff_id   = b.get('staff_id')
    punch_type = b.get('punch_type')
    punched_at = b.get('punched_at')
    note       = b.get('note', '').strip()
    manual_by  = b.get('manual_by', '').strip()
    if not all([staff_id, punch_type, punched_at]):
        return jsonify({'error': '缺少必要欄位'}), 400
    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400
    punched_at_parsed = _parse_tw_datetime(punched_at)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO punch_records
              (staff_id, punch_type, punched_at, note, is_manual, manual_by)
            VALUES (%s,%s,%s,%s,TRUE,%s) RETURNING *
        """, (staff_id, punch_type, punched_at_parsed, note, manual_by)).fetchone()
        staff = conn.execute("SELECT name FROM punch_staff WHERE id=%s", (staff_id,)).fetchone()
        punch_month = str(punched_at_parsed)[:7]
        _trigger_salary_regen_for_leave(conn, int(staff_id), punch_month)
    d = punch_record_row(row)
    if staff: d['staff_name'] = staff['name']
    return jsonify(d), 201


@bp.route('/api/punch/records/<int:rid>', methods=['PUT'])
@login_required
def api_punch_record_update(rid):
    b = request.get_json(force=True)
    punch_type = b.get('punch_type')
    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400
    punched_at_parsed = _parse_tw_datetime(b.get('punched_at'))
    if not punched_at_parsed:
        return jsonify({'error': '打卡時間格式錯誤'}), 400
    with get_db() as conn:
        row = conn.execute("""
            UPDATE punch_records
            SET punch_type=%s, punched_at=%s, note=%s, is_manual=TRUE, manual_by=%s
            WHERE id=%s RETURNING *
        """, (punch_type, punched_at_parsed,
              b.get('note', ''), b.get('manual_by', ''), rid)).fetchone()
        if row:
            _trigger_salary_regen_for_leave(conn, row['staff_id'],
                                            str(punched_at_parsed)[:7])
    return jsonify(punch_record_row(row)) if row else ('', 404)


@bp.route('/api/punch/records/<int:rid>', methods=['DELETE'])
@login_required
def api_punch_record_delete(rid):
    with get_db() as conn:
        old = conn.execute("SELECT staff_id, punched_at FROM punch_records WHERE id=%s",
                           (rid,)).fetchone()
        conn.execute("DELETE FROM punch_records WHERE id=%s", (rid,))
        if old:
            _trigger_salary_regen_for_leave(conn, old['staff_id'],
                                            str(old['punched_at'])[:7])
    return jsonify({'deleted': rid})


@bp.route('/api/punch/summary', methods=['GET'])
@login_required
def api_punch_summary():
    month = request.args.get('month') or _dt.now(TW_TZ).strftime('%Y-%m')
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT ps.id as staff_id, ps.name as staff_name,
                   (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                   MIN(CASE WHEN pr.punch_type='in'  THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_in,
                   MAX(CASE WHEN pr.punch_type='out' THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_out,
                   COUNT(*) as punch_count,
                   BOOL_OR(pr.is_manual) as has_manual
            FROM punch_records pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE TO_CHAR(pr.punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
            GROUP BY ps.id, ps.name, (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date ASC, ps.name
        """, (month,)).fetchall()]

    # 跨日班次合併：上班在 day N、下班在 day N+1
    from datetime import date as _dcm, timedelta as _tdcm
    _row_map = {(r['staff_id'], str(r['work_date'])): r for r in rows}
    _skip_keys = set()
    for r in rows:
        sid = r['staff_id']
        ds  = str(r['work_date'])
        if r['clock_in'] and not r['clock_out']:
            next_ds = (_dcm.fromisoformat(ds) + _tdcm(days=1)).isoformat()
            nr = _row_map.get((sid, next_ds))
            if nr and nr['clock_out'] and not nr['clock_in']:
                r['clock_out']   = nr['clock_out']
                r['punch_count'] = r['punch_count'] + nr['punch_count']
                _skip_keys.add((sid, next_ds))
    rows = sorted(
        [r for r in rows if (r['staff_id'], str(r['work_date'])) not in _skip_keys],
        key=lambda x: (str(x['work_date']), x['staff_name']),
        reverse=True,
    )

    result = []
    for r in rows:
        d = r
        d['work_date']  = d['work_date'].isoformat()  if d['work_date']  else None
        d['clock_in']   = d['clock_in'].isoformat()   if d['clock_in']   else None
        d['clock_out']  = d['clock_out'].isoformat()  if d['clock_out']  else None
        if d['clock_in'] and d['clock_out']:
            from datetime import datetime as _dt2
            ci = _dt2.fromisoformat(d['clock_in'].replace('Z', ''))
            co = _dt2.fromisoformat(d['clock_out'].replace('Z', ''))
            d['duration_min'] = max(0, int((co - ci).total_seconds() / 60))
        else:
            d['duration_min'] = None
        result.append(d)
    return jsonify(result)


@bp.route('/api/attendance/monthly-stats', methods=['GET'])
@login_required
def api_attendance_monthly_stats():
    """
    月出勤統計報表（每位員工匯總）
    回傳：出勤天數、總工時、遲到次數、缺打卡次數、平均工時
    """
    month = request.args.get('month') or _dt.now(TW_TZ).strftime('%Y-%m')
    with get_db() as conn:
        # 每人每日打卡彙整
        rows = [dict(r) for r in conn.execute("""
            SELECT ps.id as staff_id, ps.name as staff_name,
                   ps.department, ps.role,
                   (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date,
                   MIN(CASE WHEN pr.punch_type='in'  THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_in,
                   MAX(CASE WHEN pr.punch_type='out' THEN pr.punched_at AT TIME ZONE 'Asia/Taipei' END) as clock_out,
                   BOOL_OR(pr.punch_type='in')  as has_in,
                   BOOL_OR(pr.punch_type='out') as has_out
            FROM punch_records pr
            JOIN punch_staff ps ON ps.id = pr.staff_id AND ps.active = TRUE
            WHERE TO_CHAR(pr.punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM') = %s
            GROUP BY ps.id, ps.name, ps.department, ps.role,
                     (pr.punched_at AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY ps.name, work_date
        """, (month,)).fetchall()]

        # 跨日班次合併
        from datetime import date as _dcs, timedelta as _tdcs
        _stat_map = {(r['staff_id'], str(r['work_date'])): r for r in rows}
        _stat_skip = set()
        for r in rows:
            sid = r['staff_id']
            ds  = str(r['work_date'])
            if r['has_in'] and not r['has_out']:
                next_ds = (_dcs.fromisoformat(ds) + _tdcs(days=1)).isoformat()
                nr = _stat_map.get((sid, next_ds))
                if nr and nr['has_out'] and not nr['has_in']:
                    r['clock_out'] = nr['clock_out']
                    r['has_out']   = True
                    _stat_skip.add((sid, next_ds))
        rows = [r for r in rows if (r['staff_id'], str(r['work_date'])) not in _stat_skip]

        # 班別指派（用於遲到判斷）
        shift_rows = conn.execute("""
            SELECT sa.staff_id, sa.shift_date, st.start_time, st.end_time
            FROM shift_assignments sa
            JOIN shift_types st ON st.id = sa.shift_type_id
            WHERE TO_CHAR(sa.shift_date,'YYYY-MM') = %s
        """, (month,)).fetchall()
        shift_map = {(r['staff_id'], str(r['shift_date'])): r for r in shift_rows}

    from collections import defaultdict
    stats = defaultdict(lambda: {
        'staff_id': None, 'staff_name': '', 'department': '', 'role': '',
        'days_worked': 0, 'total_minutes': 0,
        'late_count': 0, 'early_count': 0, 'missing_in_count': 0, 'missing_out_count': 0,
        'anomaly_dates': [],
    })

    for r in rows:
        sid  = r['staff_id']
        ds   = str(r['work_date'])
        s    = stats[sid]
        s['staff_id']   = sid
        s['staff_name'] = r['staff_name']
        s['department'] = r['department'] or ''
        s['role']       = r['role']       or ''

        has_in  = bool(r['has_in'])
        has_out = bool(r['has_out'])

        if has_in or has_out:
            s['days_worked'] += 1

        if r['clock_in'] and r['clock_out']:
            diff = (r['clock_out'] - r['clock_in']).total_seconds() / 60
            if diff > 0:
                s['total_minutes'] += int(diff)

        if has_in and not has_out:
            s['missing_out_count'] += 1
            s['anomaly_dates'].append({'date': ds, 'type': 'missing_out', 'label': '缺下班卡'})
        if not has_in and has_out:
            s['missing_in_count'] += 1
            s['anomaly_dates'].append({'date': ds, 'type': 'missing_in', 'label': '缺上班卡'})

        if has_in and r['clock_in']:
            shift = shift_map.get((sid, ds))
            if shift and shift['start_time']:
                try:
                    sh, sm = map(int, str(shift['start_time'])[:5].split(':'))
                    ci_local = r['clock_in']
                    ih, im   = ci_local.hour, ci_local.minute
                    late_mins = (ih * 60 + im) - (sh * 60 + sm)
                    if late_mins > 10:
                        s['late_count'] += 1
                        s['anomaly_dates'].append({'date': ds, 'type': 'late',
                                                   'label': f'遲到 {late_mins} 分鐘'})
                except Exception:
                    pass

        if has_out and r['clock_out']:
            shift = shift_map.get((sid, ds))
            if shift and shift['end_time']:
                try:
                    eh, em = map(int, str(shift['end_time'])[:5].split(':'))
                    co_local = r['clock_out']
                    oh, om   = co_local.hour, co_local.minute
                    early_mins = (eh * 60 + em) - (oh * 60 + om)
                    if early_mins > 15:
                        s['early_count'] += 1
                        s['anomaly_dates'].append({'date': ds, 'type': 'early',
                                                   'label': f'早退 {early_mins} 分鐘'})
                except Exception:
                    pass

    result = []
    for s in sorted(stats.values(), key=lambda x: (x['department'], x['staff_name'])):
        h   = s['total_minutes'] // 60
        m   = s['total_minutes'] % 60
        avg = round(s['total_minutes'] / s['days_worked'] / 60, 1) if s['days_worked'] else 0
        s['total_hours']   = round(s['total_minutes'] / 60, 1)
        s['avg_hours_day'] = avg
        s['total_hm']      = f"{h}h {m:02d}m"
        result.append(s)
    return jsonify({'month': month, 'stats': result})


# ── Punch Requests (補打卡申請) ───────────────────────────────────

@bp.route('/api/punch/request', methods=['POST'])
def api_punch_req_submit():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    b            = request.get_json(force=True)
    punch_type   = b.get('punch_type')
    requested_at = b.get('requested_at')
    reason       = b.get('reason', '').strip()
    if punch_type not in ('in', 'out', 'break_out', 'break_in'):
        return jsonify({'error': '無效的打卡類型'}), 400
    if not requested_at:
        return jsonify({'error': '請選擇補打時間'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO punch_requests (staff_id, punch_type, requested_at, reason)
            VALUES (%s,%s,%s,%s) RETURNING *
        """, (sid, punch_type, requested_at, reason)).fetchone()
    return jsonify(punch_req_row(row)), 201


@bp.route('/api/punch/request/my', methods=['GET'])
def api_punch_req_my():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM punch_requests WHERE staff_id=%s ORDER BY requested_at DESC LIMIT 20",
            (sid,)
        ).fetchall()
    return jsonify([punch_req_row(r) for r in rows])


@bp.route('/api/punch/requests', methods=['GET'])
@login_required
def api_punch_reqs_list():
    status = request.args.get('status', '')
    conds, params = ['TRUE'], []
    if status: conds.append('pr.status=%s'); params.append(status)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*, ps.name as staff_name, ps.role as staff_role
            FROM punch_requests pr JOIN punch_staff ps ON ps.id=pr.staff_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.created_at DESC LIMIT 200
        """, params).fetchall()
    return jsonify([punch_req_row(r) for r in rows])


@bp.route('/api/punch/requests/<int:rid>', methods=['DELETE'])
@login_required
def api_punch_req_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM punch_requests WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})
