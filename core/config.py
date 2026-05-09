"""
core/config.py - Centralised configuration, read from environment variables.
"""
import os

DATABASE_URL   = os.environ.get('DATABASE_URL', '')
SECRET_KEY     = os.environ.get('SECRET_KEY', 'dev-secret-key')
KEEP_ALIVE_URL = os.environ.get('KEEP_ALIVE_URL', '')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_NOTIFY_TOKEN         = os.environ.get('LINE_NOTIFY_TOKEN', '')

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

MOBILE_JWT_SECRET  = os.environ.get('MOBILE_JWT_SECRET', SECRET_KEY)
JWT_EXPIRE_HOURS   = 24 * 7   # 7 days

WEBAUTHN_RP_ID   = os.environ.get('WEBAUTHN_RP_ID', '')
WEBAUTHN_RP_NAME = '打卡系統'
WEBAUTHN_ORIGIN  = os.environ.get('WEBAUTHN_ORIGIN', '')
