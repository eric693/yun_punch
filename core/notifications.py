"""
core/notifications.py - LINE notification helpers.
"""
import os

from linebot import LineBotApi
from linebot.models import TextSendMessage

from core.database import get_db, DATABASE_URL


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
    msg   = f"{icon} {category}{label}\n{extra_info}\n\n請至員工系統查看詳情。"
    _notify_staff_line(staff_id, msg.strip())
