"""集中管理環境變數與全域常數。

從 app.py 抽出,避免散落與重複讀取 os.environ。其他模組一律從此 import。
"""
import os
import secrets
from datetime import timezone as _tz, timedelta as _td

# Flask session 簽章金鑰(未設定時隨機產生,重啟會使既有 session 失效)
SECRET_KEY = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# LINE Messaging API
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')

# 後台預設管理密碼
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

# 資料庫(相容 Render 早期的 postgres:// 前綴)
_raw_db_url  = os.environ.get('DATABASE_URL', '')
DATABASE_URL = (
    _raw_db_url.replace('postgres://', 'postgresql://', 1)
    if _raw_db_url.startswith('postgres://') else _raw_db_url
)

# 用於 keep-alive ping 自身的對外網址
RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL', '')

# 時區與常數
TW_TZ = _tz(_td(hours=8))   # Asia/Taipei (UTC+8)
WEEKDAY_ZH = ['一', '二', '三', '四', '五', '六', '日']
