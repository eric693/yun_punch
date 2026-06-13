"""後台權限 decorator。

從 app.py 抽出。皆依賴 Flask 的 session/request,不依賴特定路由是否在同檔
(url_for 於請求時才解析)。其他模組 `from auth import login_required, ...`。
"""
from functools import wraps

from flask import session, request, jsonify, redirect, url_for


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': '請先登入'}), 401
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


def require_module(module):
    """確認已登入且擁有指定模組權限(超級管理員跳過模組檢查)."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get('logged_in'):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'error': '請先登入'}), 401
                return redirect(url_for('admin_login'))
            if not session.get('admin_is_super'):
                perms = session.get('admin_permissions') or []
                if module not in perms:
                    return jsonify({'error': f'無「{module}」模組的存取權限'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_super(f):
    """只允許超級管理員存取."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({'error': '請先登入'}), 401
        if not session.get('admin_is_super'):
            return jsonify({'error': '需要超級管理員權限'}), 403
        return f(*args, **kwargs)
    return decorated
