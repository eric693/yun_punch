"""
routes/holiday.py - Taiwan public holidays management routes.
"""
from flask import Blueprint, request, jsonify

from core.database import get_db
from core.auth import login_required, require_module
from core.helpers import _month_date_range

bp = Blueprint('holiday', __name__)


def holiday_row(row):
    if not row: return None
    d = dict(row)
    # DB column is holiday_date; expose as 'date' for API compatibility
    if d.get('holiday_date'):
        d['date'] = d.pop('holiday_date').isoformat()
    elif d.get('date') and hasattr(d['date'], 'isoformat'):
        d['date'] = d['date'].isoformat()
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d

def _is_holiday(conn, date_str):
    """Check if a date is a public holiday"""
    row = conn.execute(
        "SELECT id FROM holidays WHERE holiday_date=%s", (date_str,)
    ).fetchone()
    return row is not None

# ── Holiday CRUD API ─────────────────────────────────────────────

@bp.route('/api/holidays', methods=['GET'])
@require_module('holiday')
def api_holidays_list():
    year = request.args.get('year', '')
    conds, params = ['TRUE'], []
    if year:
        conds.append("EXTRACT(YEAR FROM holiday_date)=%s")
        params.append(int(year))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM holidays WHERE {' AND '.join(conds)} ORDER BY holiday_date",
            params
        ).fetchall()
    return jsonify([holiday_row(r) for r in rows])

@bp.route('/api/holidays/public', methods=['GET'])
def api_holidays_public():
    """Public endpoint for staff page"""
    year = request.args.get('year', '')
    month = request.args.get('month', '')
    conds, params = ['TRUE'], []
    if year:
        conds.append("EXTRACT(YEAR FROM holiday_date)=%s"); params.append(int(year))
    if month:
        _d_s, _d_e = _month_date_range(month)
        conds.append('holiday_date >= %s AND holiday_date < %s')
        params.extend([_d_s, _d_e])
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT holiday_date, name FROM holidays WHERE {' AND '.join(conds)} ORDER BY holiday_date",
            params
        ).fetchall()
    return jsonify({r['holiday_date'].isoformat(): r['name'] for r in rows})

@bp.route('/api/holidays', methods=['POST'])
@require_module('holiday')
def api_holiday_create():
    b = request.get_json(force=True)
    if not b.get('date') or not b.get('name','').strip():
        return jsonify({'error': '請填寫日期和名稱'}), 400
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO holidays (holiday_date, name, holiday_type, note)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (holiday_date) DO UPDATE
              SET name=EXCLUDED.name, holiday_type=EXCLUDED.holiday_type, note=EXCLUDED.note
            RETURNING *
        """, (b['date'], b['name'].strip(),
              b.get('holiday_type','national'), b.get('note',''))).fetchone()
    return jsonify(holiday_row(row)), 201

@bp.route('/api/holidays/<int:hid>', methods=['DELETE'])
@require_module('holiday')
def api_holiday_delete(hid):
    with get_db() as conn:
        conn.execute("DELETE FROM holidays WHERE id=%s", (hid,))
    return jsonify({'deleted': hid})

@bp.route('/api/holidays/batch', methods=['POST'])
@require_module('holiday')
def api_holiday_batch():
    """Batch import holidays from JSON list"""
    b    = request.get_json(force=True)
    rows = b.get('holidays', [])
    count = 0
    with get_db() as conn:
        for item in rows:
            try:
                conn.execute("""
                    INSERT INTO holidays (holiday_date, name, holiday_type, note)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (holiday_date) DO UPDATE SET name=EXCLUDED.name
                """, (item['date'], item['name'],
                      item.get('holiday_type','national'), item.get('note','')))
                count += 1
            except Exception:
                pass
    return jsonify({'imported': count})
