"""純工具函式(無 Flask / DB 依賴),供各模組共用。

從 app.py 抽出。任何只做計算、轉換、雜湊的小函式都應放這裡。
"""
import hashlib
import math
import ast as _ast
import calendar as _calendar
import base64 as _b64
from datetime import datetime as _dt, timedelta as _td, date as _date_cls

from config import TW_TZ


def _month_ts_range(month: str):
    """'YYYY-MM' -> (start, end) TIMESTAMPTZ (UTC+8),用於 punched_at >= %s AND punched_at < %s"""
    y, m = int(month[:4]), int(month[5:7])
    start = _dt(y, m, 1, tzinfo=TW_TZ)
    end   = _dt(y + 1, 1, 1, tzinfo=TW_TZ) if m == 12 else _dt(y, m + 1, 1, tzinfo=TW_TZ)
    return start, end


def _month_date_range(month: str):
    """'YYYY-MM' -> (first_date, first_date_of_next_month),用於 date >= %s AND date < %s"""
    y, m = int(month[:4]), int(month[5:7])
    last_day = _calendar.monthrange(y, m)[1]
    return _date_cls(y, m, 1), _date_cls(y, m, last_day) + _td(days=1)


def _hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


def _gps_distance(lat1, lng1, lat2, lng2):
    R = 6371000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lng2 - lng1) * p / 2) ** 2)
    return int(2 * R * math.asin(math.sqrt(a)))


def _parse_tw_datetime(s):
    """Parse a datetime string (with or without tz) treating naive strings as Taiwan time (UTC+8)."""
    if not s:
        return None
    dt = _dt.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TW_TZ)
    return dt


def _calc_service_years(hire_date_str):
    if not hire_date_str: return 0.0
    from datetime import date as _d4
    try:
        hire = _d4.fromisoformat(str(hire_date_str))
        return round((_d4.today() - hire).days / 365.25, 2)
    except Exception:
        return 0.0


def _eval_formula(formula, base_salary, insured_salary, service_years, extra_vars=None):
    """安全計算薪資公式(使用 ast 解析,禁止任意程式碼執行)"""
    if not formula: return 0.0
    ctx = {
        'base_salary':    float(base_salary or 0),
        'insured_salary': float(insured_salary or 0),
        'service_years':  float(service_years or 0),
    }
    if extra_vars:
        ctx.update({k: float(v or 0) for k, v in extra_vars.items()})

    def _safe_eval(node):
        if isinstance(node, _ast.Expression):
            return _safe_eval(node.body)
        elif isinstance(node, _ast.Constant):
            return float(node.value)
        elif isinstance(node, _ast.Num):          # Python < 3.8 相容
            return float(node.n)
        elif isinstance(node, _ast.Name):
            if node.id not in ctx:
                raise ValueError(f'未知變數: {node.id}')
            return float(ctx[node.id])
        elif isinstance(node, _ast.BinOp):
            l, r = _safe_eval(node.left), _safe_eval(node.right)
            op = node.op
            if isinstance(op, _ast.Add):      return l + r
            if isinstance(op, _ast.Sub):      return l - r
            if isinstance(op, _ast.Mult):     return l * r
            if isinstance(op, _ast.Div):      return l / r if r != 0 else 0.0
            if isinstance(op, _ast.FloorDiv): return float(int(l // r)) if r != 0 else 0.0
            if isinstance(op, _ast.Mod):      return l % r if r != 0 else 0.0
            if isinstance(op, _ast.Pow):      return l ** r
            raise ValueError(f'不支援的運算子: {type(op).__name__}')
        elif isinstance(node, _ast.UnaryOp):
            v = _safe_eval(node.operand)
            if isinstance(node.op, _ast.USub): return -v
            if isinstance(node.op, _ast.UAdd): return v
            raise ValueError(f'不支援的單元運算子: {type(node.op).__name__}')
        elif isinstance(node, _ast.IfExp):
            return _safe_eval(node.body) if _safe_eval(node.test) else _safe_eval(node.orelse)
        elif isinstance(node, _ast.Compare):
            left = _safe_eval(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = _safe_eval(comp)
                if isinstance(op, _ast.Eq):    ok = left == right
                elif isinstance(op, _ast.NotEq): ok = left != right
                elif isinstance(op, _ast.Lt):  ok = left < right
                elif isinstance(op, _ast.LtE): ok = left <= right
                elif isinstance(op, _ast.Gt):  ok = left > right
                elif isinstance(op, _ast.GtE): ok = left >= right
                else: raise ValueError(f'不支援的比較運算子')
                if not ok: return 0.0
                left = right
            return 1.0
        elif isinstance(node, _ast.BoolOp):
            vals = [_safe_eval(v) for v in node.values]
            if isinstance(node.op, _ast.And): return float(all(vals))
            if isinstance(node.op, _ast.Or):  return float(any(vals))
        raise ValueError(f'不允許的公式語法: {type(node).__name__}')

    try:
        tree = _ast.parse(formula.strip(), mode='eval')
        result = _safe_eval(tree)
        return round(float(result), 2)
    except Exception as _fe:
        print(f"[formula_error] 公式計算失敗:{formula!r}  原因:{_fe}")
        return 0.0


def _roc_date(date_str):
    """Convert YYYY-MM-DD to YYYMMDD (ROC year)"""
    if not date_str: return '0000000'
    try:
        from datetime import date as _dedi
        d = _dedi.fromisoformat(str(date_str)[:10])
        return f'{d.year - 1911:03d}{d.month:02d}{d.day:02d}'
    except Exception:
        return '0000000'


def _roc_year(year):
    return int(year) - 1911


def _month_last_day(year, month):
    import calendar
    return calendar.monthrange(int(year), int(month))[1]


def _b64url_encode(data: bytes) -> str:
    return _b64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    s = s.replace(' ', '+').replace('-', '+').replace('_', '/')
    padding = 4 - len(s) % 4
    if padding != 4:
        s += '=' * padding
    return _b64.b64decode(s)
