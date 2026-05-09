"""
routes/line_bot.py - LINE Bot webhook, punch helpers, and config API.
"""
import json as _json
import math
import traceback
import urllib.request
import urllib.error
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
from functools import wraps

from linebot import LineBotApi
from linebot.models import (
    TextSendMessage, QuickReply, QuickReplyButton,
    MessageAction, LocationAction,
)
from flask import Blueprint, request, jsonify, session

from core.database import get_db, DATABASE_URL, _hash_pw
from core.auth import login_required
from core.helpers import TW_TZ, _gps_distance, _month_ts_range
from core.notifications import _notify_staff_line, _notify_review_result

bp = Blueprint('line_bot', __name__)

CUSTOM_RICHMENU_IMAGE_PATH = '/tmp/custom_richmenu.png'
_pending_line_punches = {}   # {line_user_id: punch_type}


# ─── Config helpers ───────────────────────────────────────────────────────────

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
    items: [{'label': str (<=20 chars), 'text': str (message to send on tap)}, ...]
    """
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


# ─── Webhook ──────────────────────────────────────────────────────────────────

@bp.route('/line-punch/webhook', methods=['POST'])
def line_punch_webhook():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_secret'):
        return 'disabled', 200

    signature = request.headers.get('X-Line-Signature', '')
    body      = request.get_data(as_text=True)

    import hmac, hashlib as _hl, base64 as _b64
    secret   = cfg['channel_secret'].encode('utf-8')
    computed = _b64.b64encode(
        hmac.new(secret, body.encode('utf-8'), _hl.sha256).digest()
    ).decode('utf-8')
    if not hmac.compare_digest(computed, signature):
        return 'Invalid signature', 400

    events = _json.loads(body).get('events', [])
    for event in events:
        try:
            _handle_line_punch_event(event, cfg)
        except Exception as e:
            print(f"[LINE PUNCH] event handler error: {e}\n{traceback.format_exc()}")
    return 'OK', 200


def _handle_line_punch_event(event, cfg):
    source   = event.get('source', {})
    user_id  = source.get('userId')
    evt_type = event.get('type')
    if not user_id: return

    msg      = event.get('message', {})
    msg_type = msg.get('type', '')

    if evt_type == 'follow':
        _send_line_punch(user_id,
            '歡迎使用員工打卡系統！👋\n\n'
            '請輸入您的登入帳號完成綁定。\n\n'
            '✏️ 輸入範例：\n  綁定 mary123\n'
            '（請將 mary123 換成您自己的帳號）\n\n'
            '不知道帳號？請詢問管理員。')
        return

    if evt_type != 'message': return

    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM punch_staff WHERE line_user_id=%s AND active=TRUE", (user_id,)
        ).fetchone()

    # ── Not bound yet ─────────────────────────────────────────
    if not staff:
        if msg_type == 'text':
            text = msg.get('text', '').strip()
            if text.startswith('綁定 ') or text.startswith('绑定 '):
                username = text.split(' ', 1)[1].strip()
                if username in ('帳號', '您的帳號', '[您的帳號]', 'username', '帳號名稱'):
                    _send_line_punch(user_id,
                        '請輸入您「實際的」登入帳號，而非說明文字。\n\n'
                        '範例：綁定 mary123')
                    return
                with get_db() as conn:
                    candidate = conn.execute(
                        "SELECT * FROM punch_staff WHERE username=%s AND active=TRUE",
                        (username,)
                    ).fetchone()
                if not candidate:
                    _send_line_punch(user_id,
                        f'找不到帳號「{username}」\n\n'
                        '請確認帳號是否正確，或詢問管理員您的登入帳號。')
                    return
                if candidate['line_user_id']:
                    _send_line_punch(user_id, '此帳號已綁定其他 LINE 帳號，請聯絡管理員。')
                    return
                with get_db() as conn:
                    conn.execute(
                        "UPDATE punch_staff SET line_user_id=%s WHERE id=%s",
                        (user_id, candidate['id'])
                    )
                _send_line_punch(user_id,
                    f'✅ 綁定成功！\n歡迎 {candidate["name"]}！\n\n'
                    '打卡方式：\n📍 傳送位置訊息 → 自動打卡\n'
                    '💬 或輸入：上班 / 下班 / 休息 / 回來\n\n'
                    '輸入「狀態」可查看今日打卡記錄。')
            else:
                _send_line_punch(user_id,
                    '您尚未綁定打卡帳號。\n\n'
                    '請輸入您的登入帳號：\n  綁定 [您的帳號]\n\n'
                    '範例：綁定 mary123')
        return

    # ── Bound staff ───────────────────────────────────────────
    PUNCH_CMDS = {
        '上班': 'in', '上班打卡': 'in',
        '下班': 'out', '下班打卡': 'out',
        '休息': 'break_out', '休息開始': 'break_out',
        '回來': 'break_in', '休息結束': 'break_in',
    }
    PUNCH_LABEL = {
        'in': '上班打卡', 'out': '下班打卡',
        'break_out': '休息開始', 'break_in': '休息結束',
    }

    if msg_type == 'location':
        lat = msg.get('latitude'); lng = msg.get('longitude')
        _do_line_punch(staff, user_id, lat, lng, None, PUNCH_LABEL)

    elif msg_type == 'text':
        text = msg.get('text', '').strip()

        if text in ('狀態', '打卡記錄'):
            _send_status(staff, user_id); return

        if text == '解除綁定':
            with get_db() as conn:
                conn.execute("UPDATE punch_staff SET line_user_id=NULL WHERE id=%s", (staff['id'],))
            _send_line_punch(user_id, '已解除 LINE 帳號綁定。'); return

        punch_type = PUNCH_CMDS.get(text)
        if punch_type:
            with get_db() as conn:
                pcfg = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
                locs = conn.execute("SELECT * FROM punch_locations WHERE active=TRUE").fetchall()
            gps_required = pcfg['gps_required'] if pcfg else False
            if gps_required and locs:
                cfg_lp = get_line_punch_config()
                if cfg_lp and cfg_lp.get('enabled') and cfg_lp.get('channel_access_token'):
                    qr = QuickReply(items=[QuickReplyButton(action=LocationAction(label='📍 傳送位置'))])
                    msg_obj = TextSendMessage(
                        text=f'請傳送您的位置來完成{PUNCH_LABEL[punch_type]}\n點下方「傳送位置」按鈕即可打卡',
                        quick_reply=qr)
                    try:
                        LineBotApi(cfg_lp['channel_access_token']).push_message(user_id, msg_obj)
                    except Exception as _e:
                        print(f"[LINE PUNCH] location qr error: {_e}")
                _pending_line_punches[user_id] = punch_type
            else:
                _do_line_punch(staff, user_id, None, None, punch_type, PUNCH_LABEL)
        elif text in ('查餘假', '餘假', '假期', '查假', '特休'):
            _line_query_leave_balance(staff, user_id)
        elif text in ('查薪資', '薪資', '薪水', '薪資單', '查薪水'):
            _line_query_salary(staff, user_id)
        elif text.startswith('請假'):
            _line_submit_leave(staff, user_id, text)
        elif text in ('績效', '考核', '我的考核', '查績效'):
            _line_query_performance(staff, user_id)
        elif text in ('假別', '假別清單', '假別列表'):
            _line_show_leave_types(staff, user_id)
        elif (text in ('出勤紀錄', '出勤記錄', '月出勤', '打卡紀錄', '打卡記錄', '出勤查詢')
              or text.startswith('出勤紀錄 ') or text.startswith('出勤記錄 ')
              or text.startswith('打卡紀錄 ') or text.startswith('打卡記錄 ')):
            _line_query_monthly_records(staff, user_id, text)
        elif text == '加班':
            _line_overtime_start(staff, user_id)
        elif text.startswith('申請加班'):
            _line_submit_overtime(staff, user_id, text)
        elif text in ('選單', '功能', '菜單', '?', '？', 'help', 'Help', 'HELP'):
            _line_show_help(staff, user_id)
        else:
            _line_show_help(staff, user_id)


def _do_line_punch(staff, user_id, lat, lng, forced_type, PUNCH_LABEL):
    from datetime import datetime as _dt3, timezone as _tz3, timedelta as _td3
    TW = _tz3(_td3(hours=8))

    # Determine punch type
    if forced_type:
        punch_type = forced_type
    elif user_id in _pending_line_punches:
        punch_type = _pending_line_punches.pop(user_id)
    else:
        with get_db() as conn:
            last = conn.execute("""
                SELECT punch_type, punched_at FROM punch_records
                WHERE staff_id=%s
                  AND punched_at >= NOW() - INTERVAL '30 hours'
                ORDER BY punched_at DESC LIMIT 1
            """, (staff['id'],)).fetchone()
        if not last:
            punch_type = 'in'
        elif last['punch_type'] == 'in':
            punch_type = 'out'
        elif last['punch_type'] == 'break_out':
            punch_type = 'break_in'
        else:
            if last['punch_type'] == 'out' and last.get('punched_at'):
                last_time = last['punched_at']
                now_tw = _dt3.now(TW)
                if not last_time.tzinfo:
                    last_time = last_time.replace(tzinfo=TW)
                diff_min = (now_tw - last_time).total_seconds() / 60
                if diff_min < 10:
                    last_str = last_time.strftime('%H:%M')
                    _send_line_with_quick_reply(user_id,
                        f'⚠️ 您剛於 {last_str} 打卡下班（{diff_min:.0f} 分鐘前）\n\n確定要打卡上班嗎？',
                        [{'label': '✅ 確認上班打卡', 'text': '上班打卡'},
                         {'label': '❌ 取消',        'text': '狀態'}])
                    return
            punch_type = 'in'

    label = PUNCH_LABEL.get(punch_type, punch_type)

    # GPS check
    gps_distance = None; matched_name = ''
    if lat is not None and lng is not None:
        with get_db() as conn:
            pcfg = conn.execute("SELECT * FROM punch_config WHERE id=1").fetchone()
            locs = conn.execute("SELECT * FROM punch_locations WHERE active=TRUE").fetchall()
        gps_required = pcfg['gps_required'] if pcfg else False
        if locs:
            min_dist = None; min_loc = None
            for loc in locs:
                d = _gps_distance(lat, lng, float(loc['lat']), float(loc['lng']))
                if min_dist is None or d < min_dist:
                    min_dist = d; min_loc = loc
            gps_distance = min_dist
            matched_name = min_loc['location_name'] if min_loc else ''
            if gps_required and min_dist > int(min_loc['radius_m']):
                _send_line_punch(user_id,
                    f'❌ {label}失敗\n'
                    f'您距離「{min_loc["location_name"]}」{min_dist} 公尺\n'
                    f'超出允許範圍 {min_loc["radius_m"]} 公尺\n\n'
                    '請確認您在正確地點後重試。')
                return

    # Duplicate guard
    with get_db() as conn:
        recent = conn.execute("""
            SELECT id FROM punch_records
            WHERE staff_id=%s AND punch_type=%s
              AND punched_at > NOW() - INTERVAL '1 minute'
        """, (staff['id'], punch_type)).fetchone()
        if recent:
            _send_line_punch(user_id, f'⚠️ 1 分鐘內已打過{label}，請勿重複打卡。'); return

        conn.execute("""
            INSERT INTO punch_records
              (staff_id, punch_type, latitude, longitude, gps_distance, location_name)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (staff['id'], punch_type, lat, lng, gps_distance, matched_name))

    now      = _dt3.now(TW)
    gps_info = f'\n📍 {matched_name} ({gps_distance}m)' if gps_distance is not None else ''
    _send_line_punch(user_id,
        f'✅ {label}成功\n'
        f'👤 {staff["name"]}\n'
        f'🕐 {now.strftime("%Y/%m/%d %H:%M")}'
        f'{gps_info}')


def _send_status(staff, user_id):
    from datetime import timezone as _tz4, timedelta as _td4
    TW = _tz4(_td4(hours=8))
    with get_db() as conn:
        rows = conn.execute("""
            SELECT punch_type, punched_at, gps_distance, location_name, is_manual
            FROM punch_records
            WHERE staff_id=%s
              AND (punched_at AT TIME ZONE 'Asia/Taipei')::date
                = (NOW() AT TIME ZONE 'Asia/Taipei')::date
            ORDER BY punched_at ASC
        """, (staff['id'],)).fetchall()
    LABEL = {'in': '上班', 'out': '下班', 'break_out': '休息開始', 'break_in': '休息結束'}
    if not rows:
        _send_line_punch(user_id, f'📋 {staff["name"]} 今日尚無打卡記錄。'); return
    lines = [f'📋 {staff["name"]} 今日打卡記錄']
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            from datetime import timezone as _utz2
            pa = pa.replace(tzinfo=_utz2.utc)
        t    = pa.astimezone(TW).strftime('%H:%M')
        dist = f' ({r["gps_distance"]}m)' if r['gps_distance'] is not None else ''
        man  = ' [補打]' if r['is_manual'] else ''
        lines.append(f'• {LABEL.get(r["punch_type"], r["punch_type"])} {t}{dist}{man}')
    _send_line_punch(user_id, '\n'.join(lines))


# ─── LINE Query Helpers ───────────────────────────────────────────────────────

def _line_query_leave_balance(staff, user_id):
    """查詢員工本年度假期餘額。"""
    from datetime import date as _dl
    year = _dl.today().year
    try:
        with get_db() as conn:
            balances = conn.execute("""
                SELECT lt.name, lb.total_days, lb.used_days
                FROM leave_balances lb
                JOIN leave_types lt ON lt.id = lb.leave_type_id
                WHERE lb.staff_id=%s AND lb.year=%s
                ORDER BY lt.sort_order
            """, (staff['id'], year)).fetchall()
            pending = conn.execute("""
                SELECT lt.name, COALESCE(SUM(lr.total_days),0) AS pending_days
                FROM leave_requests lr
                JOIN leave_types lt ON lt.id = lr.leave_type_id
                WHERE lr.staff_id=%s AND lr.status='pending'
                  AND EXTRACT(YEAR FROM lr.start_date)=%s
                GROUP BY lt.name
            """, (staff['id'], year)).fetchall()
    except Exception as e:
        _send_line_punch(user_id, f'查詢失敗：{e}')
        return

    pending_map = {r['name']: float(r['pending_days']) for r in pending}

    if not balances:
        _send_line_punch(user_id, f'{staff["name"]} {year} 年\n尚無假期餘額資料。\n請聯絡管理員設定特休配額。')
        return

    lines = [f'🌿 {staff["name"]} {year} 年假期餘額']
    for b in balances:
        total  = float(b['total_days'] or 0)
        used   = float(b['used_days']  or 0)
        remain = total - used
        pend   = pending_map.get(b['name'], 0)
        pend_str = f'（含待審 {pend} 天）' if pend > 0 else ''
        lines.append(f'• {b["name"]}：剩 {remain:.1f} 天（共 {total:.1f}，已用 {used:.1f}）{pend_str}')

    lines.append('\n申請請假：請假 [假別] [日期]')
    _send_line_punch(user_id, '\n'.join(lines))


def _line_query_salary(staff, user_id):
    """查詢員工最近薪資單。"""
    try:
        with get_db() as conn:
            row = conn.execute("""
                SELECT month, base_salary, allowance_total, deduction_total,
                       net_pay, status, updated_at
                FROM salary_records
                WHERE staff_id=%s
                ORDER BY month DESC LIMIT 1
            """, (staff['id'],)).fetchone()
    except Exception as e:
        _send_line_punch(user_id, f'查詢失敗：{e}')
        return
    if not row:
        _send_line_punch(user_id, f'{staff["name"]}\n尚無薪資記錄。')
        return
    STATUS = {'draft': '草稿', 'confirmed': '已確認', 'paid': '已發放'}
    status = STATUS.get(row['status'], row['status'])
    net = float(row['net_pay'] or 0)
    base = float(row['base_salary'] or 0)
    allow = float(row['allowance_total'] or 0)
    deduct = float(row['deduction_total'] or 0)
    _send_line_punch(user_id,
        f'💰 {staff["name"]} {row["month"]} 薪資\n\n'
        f'底薪：NT$ {base:,.0f}\n'
        f'加項：NT$ {allow:,.0f}\n'
        f'扣項：NT$ {deduct:,.0f}\n'
        f'實領：NT$ {net:,.0f}\n'
        f'狀態：{status}')


def _line_submit_leave(staff, user_id, text):
    """
    LINE 請假申請流程（漸進式 Quick Reply）：
    1. 請假                         → Quick Reply: 選假別
    2. 請假 假別                    → Quick Reply: 選日期
    3. 請假 假別 DATE               → Quick Reply: 選上午/下午/全天
    4. 請假 假別 DATE HH:MM         → Quick Reply: 選結束時間
    5. 請假 假別 DATE HH:MM HH:MM   → 送出
    6. 請假 假別 DATE 全天/上午/下午  → 送出（全天/半天）
    7. 請假 假別 DATE DATE           → 送出（多日）
    """
    import re as _re
    from datetime import date as _dlv
    DATE_PAT = _re.compile(r'^\d{4}-\d{2}-\d{2}$')
    TIME_PAT = _re.compile(r'^\d{2}:\d{2}$')

    parts = text.strip().split(None)

    # Step 1: "請假" only → show leave type Quick Reply
    if len(parts) == 1:
        try:
            with get_db() as conn:
                types = conn.execute(
                    "SELECT name FROM leave_types WHERE active=TRUE ORDER BY sort_order"
                ).fetchall()
        except Exception as e:
            _send_line_punch(user_id, f'查詢假別失敗：{e}')
            return
        if not types:
            _send_line_punch(user_id, '目前無可用假別，請聯絡管理員。')
            return
        items = [{'label': t['name'], 'text': f'請假 {t["name"]}'} for t in types[:13]]
        _send_line_with_quick_reply(user_id, '🌿 請假申請\n\n請選擇假別：', items)
        return

    # Step 2: "請假 假別" only → Quick Reply: pick date
    if len(parts) == 2:
        leave_type_name = parts[1]
        WDAY = ['一', '二', '三', '四', '五', '六', '日']
        today = _dlv.today()
        from datetime import timedelta as _tdlv
        items = []
        for i in range(0, 8):
            d = today + _tdlv(days=i)
            label = ('今天 ' if i == 0 else '明天 ' if i == 1 else '') + \
                    f'{d.strftime("%m/%d")}({WDAY[d.weekday()]})'
            items.append({'label': label, 'text': f'請假 {leave_type_name} {d.isoformat()}'})
        _send_line_with_quick_reply(user_id,
            f'🌿 請假 · {leave_type_name}\n\n請選擇請假日期：', items)
        return

    # Step 2.5: "請假 假別 DATE" → Quick Reply: 全天/上午/下午 + 多日
    if len(parts) == 3 and DATE_PAT.match(parts[2]):
        leave_type_name = parts[1]
        date_str = parts[2]
        from datetime import timedelta as _tdlv2
        WDAY2 = ['一', '二', '三', '四', '五', '六', '日']
        items = [
            {'label': '全天', 'text': f'請假 {leave_type_name} {date_str} 全天'},
            {'label': '上午（半天）', 'text': f'請假 {leave_type_name} {date_str} 上午'},
            {'label': '下午（半天）', 'text': f'請假 {leave_type_name} {date_str} 下午'},
        ]
        base = _dlv.fromisoformat(date_str)
        for i in range(1, 4):
            nd = base + _tdlv2(days=i)
            items.append({'label': f'至 {nd.strftime("%m/%d")}({WDAY2[nd.weekday()]})',
                          'text': f'請假 {leave_type_name} {date_str} {nd.isoformat()}'})
        _send_line_with_quick_reply(user_id,
            f'🌿 請假 · {leave_type_name}\n日期：{date_str}\n\n請選擇開始時間：', items)
        return

    # Step 2.7: "請假 假別 DATE HH:MM" → Quick Reply: 選結束時間
    if len(parts) == 4 and DATE_PAT.match(parts[2]) and TIME_PAT.match(parts[3]):
        leave_type_name = parts[1]
        date_str = parts[2]
        start_str = parts[3]
        sh, sm = map(int, start_str.split(':'))
        end_opts = []
        for delta_h in [0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8]:
            total_m = sh * 60 + sm + int(delta_h * 60)
            if total_m > 23 * 60 + 59:
                break
            eh, em = total_m // 60, total_m % 60
            end_opts.append((f'{eh:02d}:{em:02d}', delta_h))
        items = [{'label': f'至 {t}（{d:.1g}h）', 'text': f'請假 {leave_type_name} {date_str} {start_str} {t}'}
                 for t, d in end_opts[:13]]
        _send_line_with_quick_reply(user_id,
            f'🌿 請假 · {leave_type_name}\n日期：{date_str}　開始：{start_str}\n\n請選擇結束時間：', items)
        return

    leave_type_name = parts[1]
    date_str1 = parts[2]

    # --- Time-based leave: 請假 假別 DATE HH:MM HH:MM ---
    start_time_val = ''; end_time_val = ''
    if len(parts) >= 5 and DATE_PAT.match(date_str1) and TIME_PAT.match(parts[3]) and TIME_PAT.match(parts[4]):
        start_str = parts[3]; end_str = parts[4]
        try:
            sh, sm = map(int, start_str.split(':'))
            eh, em = map(int, end_str.split(':'))
            hours = ((eh * 60 + em) - (sh * 60 + sm)) / 60
            if hours <= 0:
                _send_line_punch(user_id, '結束時間必須晚於開始時間，請重新選擇。')
                return
        except ValueError:
            _send_line_punch(user_id, '時間格式錯誤，請使用 HH:MM。')
            return
        days = max(0.5, round(hours / 8 * 2) / 2)
        start_time_val = start_str; end_time_val = end_str
        date_str2 = date_str1
        start_half = False; end_half = False
        reason = '（LINE 請假）'

        with get_db() as conn:
            lt = conn.execute(
                "SELECT * FROM leave_types WHERE active=TRUE AND name=%s", (leave_type_name,)
            ).fetchone()
            if not lt:
                lt = conn.execute(
                    "SELECT * FROM leave_types WHERE active=TRUE AND name ILIKE %s LIMIT 1",
                    (f'%{leave_type_name}%',)
                ).fetchone()
            if not lt:
                avail = conn.execute(
                    "SELECT name FROM leave_types WHERE active=TRUE ORDER BY sort_order"
                ).fetchall()
                _send_line_punch(user_id, f'找不到假別「{leave_type_name}」\n可用：{"、".join(r["name"] for r in avail)}')
                return
            year = date_str1[:4]
            bal = conn.execute("""
                SELECT total_days, used_days FROM leave_balances
                WHERE staff_id=%s AND leave_type_id=%s AND year=%s
            """, (staff['id'], lt['id'], int(year))).fetchone()
            remain = None
            if bal:
                remain = float(bal['total_days'] or 0) - float(bal['used_days'] or 0)
                if remain < days:
                    _send_line_punch(user_id,
                        f'⚠️ {lt["name"]} 餘額不足\n剩餘 {remain:.1f} 天，申請 {days} 天')
                    return
            row = conn.execute("""
                INSERT INTO leave_requests
                  (staff_id, leave_type_id, start_date, end_date, total_days,
                   start_half, end_half, reason, status, start_time, end_time, created_at)
                VALUES (%s,%s,%s,%s,%s,FALSE,FALSE,%s,'pending',%s,%s,NOW()) RETURNING id
            """, (staff['id'], lt['id'], date_str1, date_str1, days,
                  reason, start_time_val, end_time_val)).fetchone()
        bal_str = f'（剩餘 {remain:.1f} 天）' if remain is not None else ''
        _send_line_punch(user_id,
            f'✅ 請假申請已送出\n\n'
            f'假別：{lt["name"]} {bal_str}\n'
            f'日期：{date_str1}\n'
            f'時段：{start_str} ～ {end_str}（{hours:.1f} 小時）\n'
            f'天數：{days} 天\n\n'
            f'申請號：#{row["id"]}，等待管理員審核。')
        return

    # --- Day-based leave (全天 / 上午 / 下午 / multi-day) ---
    start_half = False; end_half = False
    period_token = None
    if len(parts) > 3:
        tok = parts[3].strip()
        if tok in ('全天', '上午', '下午'):
            period_token = tok
            date_str2 = date_str1
        elif DATE_PAT.match(tok):
            date_str2 = tok
        else:
            date_str2 = date_str1
    else:
        date_str2 = date_str1

    if period_token == '上午':
        start_half = True; end_half = True
    elif period_token == '下午':
        start_half = False; end_half = True

    reason = '（LINE 請假）'

    try:
        _dlv.fromisoformat(date_str1)
        _dlv.fromisoformat(date_str2)
    except ValueError:
        _send_line_punch(user_id, f'日期格式錯誤，請使用 YYYY-MM-DD，例：{_dlv.today().isoformat()}')
        return

    with get_db() as conn:
        lt = conn.execute(
            "SELECT * FROM leave_types WHERE active=TRUE AND name=%s", (leave_type_name,)
        ).fetchone()
        if not lt:
            lt = conn.execute(
                "SELECT * FROM leave_types WHERE active=TRUE AND name ILIKE %s LIMIT 1",
                (f'%{leave_type_name}%',)
            ).fetchone()
        if not lt:
            avail = conn.execute(
                "SELECT name FROM leave_types WHERE active=TRUE ORDER BY sort_order"
            ).fetchall()
            names = '、'.join(r['name'] for r in avail)
            _send_line_punch(user_id, f'找不到假別「{leave_type_name}」\n\n可用假別：{names}')
            return

        year = date_str1[:4]
        bal = conn.execute("""
            SELECT total_days, used_days FROM leave_balances
            WHERE staff_id=%s AND leave_type_id=%s AND year=%s
        """, (staff['id'], lt['id'], int(year))).fetchone()

        from routes.leave import _get_staff_scheduled_dates, _calc_leave_days
        sched_lv = _get_staff_scheduled_dates(conn, staff['id'], date_str1, date_str2)
        days = _calc_leave_days(date_str1, date_str2, start_half, end_half,
                                scheduled_dates=sched_lv)

        remain = None
        if bal:
            remain = float(bal['total_days'] or 0) - float(bal['used_days'] or 0)
            if remain < days:
                _send_line_punch(user_id,
                    f'⚠️ {lt["name"]} 餘額不足\n剩餘 {remain:.1f} 天，申請 {days} 天\n\n'
                    f'請至員工系統調整後再申請。')
                return

        row = conn.execute("""
            INSERT INTO leave_requests
              (staff_id, leave_type_id, start_date, end_date, total_days,
               start_half, end_half, reason, status, start_time, end_time, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending','','',NOW()) RETURNING id
        """, (staff['id'], lt['id'], date_str1, date_str2, days,
              start_half, end_half, reason)).fetchone()

    period_label = '（上午半天）' if (start_half and end_half and date_str1 == date_str2) else \
                   '（下午半天）' if (end_half and not start_half and date_str1 == date_str2) else ''
    bal_str = f'（剩餘 {remain:.1f} 天）' if remain is not None else ''
    _send_line_punch(user_id,
        f'✅ 請假申請已送出\n\n'
        f'假別：{lt["name"]} {bal_str}\n'
        f'日期：{date_str1}' + (f' ～ {date_str2}' if date_str2 != date_str1 else '') +
        f'{period_label}\n'
        f'天數：{days} 天\n\n'
        f'申請號：#{row["id"]}，等待管理員審核。')


def _line_query_performance(staff, user_id):
    """查詢員工最近一次績效考核"""
    try:
        with get_db() as conn:
            row = conn.execute("""
                SELECT pr.period_label, pr.grade, pr.total_score, pr.max_score,
                       pr.comments, pr.salary_adjusted, pr.salary_delta,
                       pr.reviewed_at, pt.name AS tpl_name
                FROM performance_reviews pr
                LEFT JOIN performance_templates pt ON pt.id=pr.template_id
                WHERE pr.staff_id=%s
                ORDER BY pr.reviewed_at DESC LIMIT 1
            """, (staff['id'],)).fetchone()
    except Exception as e:
        _send_line_punch(user_id, f'查詢失敗：{e}')
        return
    if not row:
        _send_line_punch(user_id, f'{staff["name"]}\n尚無績效考核記錄。')
        return
    from routes.performance import _grade_labels
    grade_label = _grade_labels()
    pct = float(row['total_score']) / float(row['max_score']) * 100 if row['max_score'] else 0
    adj = f"\n薪資調整：NT$ {float(row['salary_delta']):+,.0f}" if row['salary_adjusted'] else ''
    reviewed = str(row['reviewed_at'])[:10] if row['reviewed_at'] else ''
    _send_line_punch(user_id,
        f'{staff["name"]} 最近考核\n\n'
        f'期間：{row["period_label"]}\n'
        f'範本：{row["tpl_name"] or "—"}\n'
        f'得分：{float(row["total_score"]):.1f} / {float(row["max_score"]):.0f}（{pct:.0f}%）\n'
        f'評級：{row["grade"]} {grade_label.get(row["grade"],"")}'
        f'{adj}\n'
        + (f'備注：{row["comments"][:60]}\n' if row['comments'] else '')
        + f'考核日：{reviewed}')


def _line_query_monthly_records(staff, user_id, text):
    """查詢員工月出勤記錄與打卡明細。
    格式：出勤紀錄 [YYYY-MM]（省略月份則查本月）
    """
    import re as _rem
    from datetime import date as _dm, timezone as _tzm, timedelta as _tdm, datetime as _dtm
    TW = _tzm(_tdm(hours=8))

    parts = text.strip().split()
    month = None
    if len(parts) >= 2:
        m = _rem.match(r'^(\d{4})-(\d{1,2})$', parts[1])
        if m:
            month = f"{m.group(1)}-{m.group(2).zfill(2)}"
    if not month:
        month = _dtm.now(TW).strftime('%Y-%m')

    try:
        _ts_start, _ts_end = _month_ts_range(month)
        with get_db() as conn:
            rows = conn.execute("""
                SELECT punch_type, punched_at, is_manual
                FROM punch_records
                WHERE staff_id=%s
                  AND punched_at >= %s AND punched_at < %s
                ORDER BY punched_at ASC
            """, (staff['id'], _ts_start, _ts_end)).fetchall()
    except Exception as e:
        _send_line_punch(user_id, f'查詢失敗：{e}')
        return

    if not rows:
        _send_line_punch(user_id, f'📋 {staff["name"]} {month}\n該月尚無打卡記錄。')
        return

    WDAY = ['一', '二', '三', '四', '五', '六', '日']

    days = {}
    for r in rows:
        pa = r['punched_at']
        if pa.tzinfo is None:
            from datetime import timezone as _utzm
            pa = pa.replace(tzinfo=_utzm.utc)
        pa_tw = pa.astimezone(TW)
        ds = pa_tw.strftime('%Y-%m-%d')
        if ds not in days:
            days[ds] = []
        days[ds].append({'type': r['punch_type'], 'time': pa_tw.strftime('%H:%M'), 'manual': bool(r['is_manual'])})

    total_mins = 0
    anomaly_days = 0
    lines = []

    for ds in sorted(days.keys()):
        recs = days[ds]
        d = _dm.fromisoformat(ds)
        wday = WDAY[d.weekday()]

        clock_in  = next((r['time'] for r in recs if r['type'] == 'in'),  None)
        clock_out = next((r['time'] for r in recs if r['type'] == 'out'), None)
        has_manual = any(r['manual'] for r in recs)

        if clock_in and clock_out:
            ci = _dtm.strptime(f'{ds} {clock_in}',  '%Y-%m-%d %H:%M')
            co = _dtm.strptime(f'{ds} {clock_out}', '%Y-%m-%d %H:%M')
            mins = max(0, int((co - ci).total_seconds() / 60))
            total_mins += mins
            h, m = divmod(mins, 60)
            dur = f'{h}h{m:02d}' if m else f'{h}h'
        elif clock_in:
            dur = '⚠️缺下班'
            anomaly_days += 1
        else:
            dur = '⚠️缺上班'
            anomaly_days += 1

        manual_mark = '【補】' if has_manual else ''
        ci_str = clock_in  or '--:--'
        co_str = clock_out or '--:--'
        lines.append(f'{ds[5:]}({wday}) {ci_str}↑{co_str}↓ {dur}{manual_mark}')

    th, tm = divmod(total_mins, 60)
    total_str = f'{th}h{tm:02d}' if tm else f'{th}h'
    anomaly_str = f'｜異常 {anomaly_days} 天' if anomaly_days else ''
    header = (f'📋 {staff["name"]} {month} 出勤\n'
              f'出勤 {len(days)} 天｜工時 {total_str}{anomaly_str}\n'
              + '─' * 20)

    full = header + '\n' + '\n'.join(lines)
    if len(full) <= 4500:
        _send_line_punch(user_id, full)
    else:
        _send_line_punch(user_id, header)
        chunk, chunk_len = [], 0
        for line in lines:
            if chunk_len + len(line) + 1 > 1800:
                _send_line_punch(user_id, '\n'.join(chunk))
                chunk, chunk_len = [], 0
            chunk.append(line)
            chunk_len += len(line) + 1
        if chunk:
            _send_line_punch(user_id, '\n'.join(chunk))


def _line_overtime_start(staff, user_id):
    """加班 button → Quick Reply with date options."""
    from datetime import date as _dot_s, timedelta as _tdot_s
    WDAY_OT = ['一', '二', '三', '四', '五', '六', '日']
    today = _dot_s.today()
    items = []
    for i in range(-1, 5):
        d = today + _tdot_s(days=i)
        label = ('昨天 ' if i == -1 else '今天 ' if i == 0 else '明天 ' if i == 1 else '') + \
                f'{d.strftime("%m/%d")}({WDAY_OT[d.weekday()]})'
        items.append({'label': label, 'text': f'申請加班 {d.isoformat()}'})
    _send_line_with_quick_reply(user_id, '⏰ 加班申請\n\n請選擇加班日期：', items)


def _line_submit_overtime(staff, user_id, text):
    """
    LINE 加班申請流程：
      申請加班 DATE           → Quick Reply 選開始時間
      申請加班 DATE HH:MM     → Quick Reply 選結束時間
      申請加班 DATE HH:MM HH:MM → 送出申請
    """
    import re as _re_ot
    from datetime import date as _dot, datetime as _dtt
    parts = text.strip().split(None, 3)

    if len(parts) < 2:
        _line_overtime_start(staff, user_id)
        return

    date_str = parts[1]
    try:
        _dot.fromisoformat(date_str)
    except ValueError:
        _send_line_punch(user_id, f'日期格式錯誤，請使用 YYYY-MM-DD，例：{_dot.today().isoformat()}')
        return

    if len(parts) == 2:
        start_options = ['08:00', '09:00', '17:00', '18:00', '19:00', '20:00', '21:00', '22:00']
        items = [{'label': t, 'text': f'申請加班 {date_str} {t}'} for t in start_options]
        _send_line_with_quick_reply(user_id,
            f'⏰ 加班申請 · {date_str}\n\n請選擇開始時間：', items)
        return

    start_str = parts[2]
    if not _re_ot.match(r'^\d{2}:\d{2}$', start_str):
        _send_line_punch(user_id, '時間格式錯誤，請使用 HH:MM，例：18:00')
        return

    if len(parts) == 3:
        sh, sm = map(int, start_str.split(':'))
        end_options = []
        for delta_h in [1, 1.5, 2, 2.5, 3, 4, 5, 6]:
            total_m = sh * 60 + sm + int(delta_h * 60)
            eh, em = (total_m // 60) % 24, total_m % 60
            end_options.append(f'{eh:02d}:{em:02d}')
        items = [{'label': f'至 {t}（+{d}h）', 'text': f'申請加班 {date_str} {start_str} {t}'}
                 for t, d in zip(end_options, [1, 1.5, 2, 2.5, 3, 4, 5, 6])]
        _send_line_with_quick_reply(user_id,
            f'⏰ 加班申請 · {date_str} {start_str} 開始\n\n請選擇結束時間：', items)
        return

    end_str = parts[3].strip().split()[0]
    if not _re_ot.match(r'^\d{2}:\d{2}$', end_str):
        _send_line_punch(user_id, '時間格式錯誤，請使用 HH:MM，例：20:00')
        return

    try:
        sh, sm = map(int, start_str.split(':'))
        eh, em = map(int, end_str.split(':'))
        hours = ((eh * 60 + em) - (sh * 60 + sm)) / 60
        if hours <= 0:
            hours += 24
        if hours <= 0 or hours > 24:
            raise ValueError
    except ValueError:
        _send_line_punch(user_id, '時間計算錯誤，請重新選擇。')
        return

    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO overtime_requests
              (staff_id, request_date, start_time, end_time, ot_hours, reason, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id
        """, (staff['id'], date_str, start_str, end_str, round(hours, 2), '（LINE 加班申請）')).fetchone()

    _send_line_punch(user_id,
        f'✅ 加班申請已送出\n\n'
        f'日期：{date_str}\n'
        f'時段：{start_str} ～ {end_str}（{hours:.1f} 小時）\n'
        f'申請編號：#{row["id"]}\n\n'
        '請等候管理員審核，審核結果將通知您。')


def _line_show_help(staff, user_id):
    _send_line_punch(user_id,
        f'哈囉 {staff["name"]}！以下是可用的指令：\n\n'
        '─── 打卡 ───\n'
        '📍 傳送位置 → 自動打卡\n'
        '💬 上班 / 下班 / 休息 / 回來\n'
        '📋 狀態 → 今日打卡記錄\n\n'
        '─── 查詢 ───\n'
        '🌿 查餘假 → 本年假期餘額\n'
        '💰 查薪資 → 最近薪資單\n'
        '📊 出勤紀錄 → 本月出勤明細\n'
        '   出勤紀錄 2026-03 → 指定月份\n'
        '考核 → 最近績效考核\n\n'
        '─── 申請 ───\n'
        '📝 請假 [假別] [日期] → 送出請假\n'
        '   範例：請假 特休 2026-04-01\n'
        '⏰ 加班 → 選擇日期與時段申請加班\n'
        '🗂️ 假別 → 查看可用假別清單\n\n'
        '─── 其他 ───\n'
        '🔓 解除綁定')


def _line_show_leave_types(staff, user_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name, max_days FROM leave_types WHERE active=TRUE ORDER BY sort_order"
        ).fetchall()
    if not rows:
        _send_line_punch(user_id, '目前無可用假別。'); return
    lines = ['🗂️ 可用假別清單\n']
    for r in rows:
        limit = f'（年限 {r["max_days"]} 天）' if r['max_days'] else ''
        lines.append(f'• {r["name"]} {limit}')
    lines.append('\n申請方式：請假 [假別] [日期]')
    _send_line_punch(user_id, '\n'.join(lines))


# ─── Admin LINE Punch Config API ──────────────────────────────────────────────

@bp.route('/api/line-punch/config', methods=['GET'])
@login_required
def api_line_punch_config_get():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
    if not row:
        return jsonify({'enabled': False, 'channel_access_token': '', 'channel_secret': ''})
    d = dict(row)
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return jsonify(d)


@bp.route('/api/line-punch/config', methods=['PUT'])
@login_required
def api_line_punch_config_put():
    b       = request.get_json(force=True)
    token   = b.get('channel_access_token', '').strip()
    secret  = b.get('channel_secret', '').strip()
    enabled = bool(b.get('enabled', False))
    with get_db() as conn:
        conn.execute("""
            UPDATE line_punch_config
            SET channel_access_token=%s, channel_secret=%s, enabled=%s, updated_at=NOW()
            WHERE id=1
        """, (token, secret, enabled))
    return jsonify({'ok': True, 'enabled': enabled})


@bp.route('/api/line-punch/staff', methods=['GET'])
@login_required
def api_line_punch_staff():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,name,username,role,active,line_user_id FROM punch_staff ORDER BY name"
        ).fetchall()
    return jsonify([{
        'id': r['id'], 'name': r['name'], 'username': r['username'],
        'role': r['role'], 'active': r['active'],
        'line_bound': bool(r['line_user_id']),
        'line_user_id': r['line_user_id'] or ''
    } for r in rows])


@bp.route('/api/line-punch/staff/<int:sid>/unbind', methods=['POST'])
@login_required
def api_line_punch_unbind(sid):
    with get_db() as conn:
        conn.execute("UPDATE punch_staff SET line_user_id=NULL WHERE id=%s", (sid,))
    return jsonify({'ok': True})


# ─── Rich Menu helpers ────────────────────────────────────────────────────────

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


def _make_richmenu_png():
    """Generate a simple 2500x1686 PNG with 4 colored quadrants."""
    import struct, zlib
    W, H = 2500, 1686
    colors = [(0x2e, 0x9e, 0x6b), (0xd6, 0x42, 0x42), (0xe0, 0x7b, 0x2a), (0x4a, 0x7b, 0xda)]
    rows = []
    for y in range(H):
        row = bytearray()
        for x in range(W):
            p = (0 if y < 843 else 1) * 2 + (0 if x < 1250 else 1)
            r, g, b = colors[p]
            if x in (1249, 1250) or y in (842, 843):
                r, g, b = 0x0f, 0x1c, 0x3a
            row += bytes([r, g, b])
        rows.append(bytes([0]) + bytes(row))
    compressed = zlib.compress(b''.join(rows), 1)

    def chunk(name, data):
        c = struct.pack('>I', len(data)) + name + data
        return c + struct.pack('>I', zlib.crc32(c[4:]) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', compressed)
            + chunk(b'IEND', b''))


@bp.route('/api/line-punch/richmenu/create', methods=['POST'])
@login_required
def api_richmenu_create():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'error': '請先設定 Channel Access Token'}), 400

    b = request.get_json(force=True) or {}
    gdrive_url = b.get('gdrive_url', '').strip()
    btn_texts  = b.get('button_texts') or []
    defaults   = ['上班', '下班', '請假', '加班']
    btn_texts  = [(btn_texts[i].strip() if i < len(btn_texts) and btn_texts[i].strip() else defaults[i]) for i in range(4)]

    body = {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "打卡選單",
        "chatBarText": "打卡",
        "areas": [
            {"bounds": {"x": 0,    "y": 0,   "width": 1250, "height": 843}, "action": {"type": "message", "text": btn_texts[0]}},
            {"bounds": {"x": 1250, "y": 0,   "width": 1250, "height": 843}, "action": {"type": "message", "text": btn_texts[1]}},
            {"bounds": {"x": 0,    "y": 843, "width": 1250, "height": 843}, "action": {"type": "message", "text": btn_texts[2]}},
            {"bounds": {"x": 1250, "y": 843, "width": 1250, "height": 843}, "action": {"type": "message", "text": btn_texts[3]}},
        ]
    }

    status, data = _call_line_api(cfg, 'POST', '/richmenu', body)
    if status != 200:
        return jsonify({'error': f'建立失敗 ({status}): {data.get("error","")}'}), 500

    rich_menu_id = data.get('richMenuId', '')

    png_bytes = None
    if gdrive_url:
        try:
            import re as _re
            file_id = None
            m = _re.search(r'/file/d/([^/?]+)', gdrive_url)
            if m:
                file_id = m.group(1)
            else:
                m = _re.search(r'[?&]id=([^&]+)', gdrive_url)
                if m:
                    file_id = m.group(1)
            if file_id:
                dl_url = f'https://drive.google.com/uc?export=download&id={file_id}'
                req = urllib.request.Request(dl_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    png_bytes = resp.read()
                if png_bytes and png_bytes[:1] not in (b'\x89', b'\xff', b'\x47', b'BM'):
                    png_bytes = None
        except Exception as e:
            print(f"[LINE PUNCH] gdrive download error: {e}")

    if not png_bytes:
        try:
            import os
            for _cp in [CUSTOM_RICHMENU_IMAGE_PATH,
                        CUSTOM_RICHMENU_IMAGE_PATH.replace('.png', '.jpg')]:
                if os.path.exists(_cp):
                    with open(_cp, 'rb') as f:
                        png_bytes = f.read()
                    break
        except Exception:
            pass

    if not png_bytes:
        try:
            png_bytes = _make_richmenu_png()
        except Exception:
            pass

    img_ok = False
    if png_bytes:
        content_type = 'image/jpeg' if png_bytes[:2] == b'\xff\xd8' else 'image/png'
        upload_url = f'https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content'
        req = urllib.request.Request(
            upload_url, data=png_bytes, method='POST',
            headers={'Content-Type': content_type, 'Authorization': f'Bearer {cfg["channel_access_token"]}'}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                img_ok = resp.status in (200, 204)
        except Exception:
            pass

    _call_line_api(cfg, 'POST', f'/user/all/richmenu/{rich_menu_id}')
    return jsonify({'ok': True, 'rich_menu_id': rich_menu_id, 'image_uploaded': img_ok})


@bp.route('/api/line-punch/richmenu/list', methods=['GET'])
@login_required
def api_richmenu_list():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'menus': []})
    status, data = _call_line_api(cfg, 'GET', '/richmenu/list')
    if status != 200:
        return jsonify({'menus': [], 'error': data.get('error', '')})
    return jsonify({'menus': data.get('richmenus', [])})


@bp.route('/api/line-punch/richmenu/<rich_menu_id>', methods=['DELETE'])
@login_required
def api_richmenu_delete(rich_menu_id):
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'error': '未設定 Token'}), 400
    _call_line_api(cfg, 'DELETE', '/user/all/richmenu')
    status, _ = _call_line_api(cfg, 'DELETE', f'/richmenu/{rich_menu_id}')
    return jsonify({'ok': status in (200, 204), 'status': status})


@bp.route('/api/line-punch/richmenu/default', methods=['DELETE'])
@login_required
def api_richmenu_unset_default():
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('channel_access_token'):
        return jsonify({'error': '未設定 Token'}), 400
    status, _ = _call_line_api(cfg, 'DELETE', '/user/all/richmenu')
    return jsonify({'ok': status in (200, 204)})
