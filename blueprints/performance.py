"""performance API blueprint(自 app.py 拆出)。"""
from flask import Blueprint, request, jsonify, session
from db import (
    get_db,
)
from auth import (
    login_required,
)
from services import (
    _notify_staff_line, _get_grade_config, _grade_labels, _score_to_grade,
)
from serializers import (
    _perf_template_row, _perf_review_row,
)
import json as _json

bp = Blueprint('performance', __name__)


@bp.route('/api/performance/templates', methods=['GET'])
@login_required
def api_perf_templates_list():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM performance_templates ORDER BY created_at DESC"
        ).fetchall()
    return jsonify([_perf_template_row(r) for r in rows])

@bp.route('/api/performance/templates', methods=['POST'])
@login_required
def api_perf_template_create():
    b = request.get_json(force=True)
    name = (b.get('name') or '').strip()
    if not name: return jsonify({'error': '請填寫範本名稱'}), 400
    items = b.get('items', [])
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO performance_templates (name, description, period, items)
            VALUES (%s,%s,%s,%s) RETURNING *
        """, (name, b.get('description',''), b.get('period','quarterly'),
              _json.dumps(items))).fetchone()
    return jsonify(_perf_template_row(row)), 201

@bp.route('/api/performance/templates/<int:tid>', methods=['PUT'])
@login_required
def api_perf_template_update(tid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE performance_templates
            SET name=%s, description=%s, period=%s, items=%s, active=%s
            WHERE id=%s RETURNING *
        """, (b.get('name','').strip(), b.get('description',''),
              b.get('period','quarterly'), _json.dumps(b.get('items',[])),
              bool(b.get('active', True)), tid)).fetchone()
    return jsonify(_perf_template_row(row)) if row else ('', 404)

@bp.route('/api/performance/templates/<int:tid>', methods=['DELETE'])
@login_required
def api_perf_template_delete(tid):
    with get_db() as conn:
        conn.execute("DELETE FROM performance_templates WHERE id=%s", (tid,))
    return jsonify({'deleted': tid})

@bp.route('/api/performance/reviews', methods=['GET'])
@login_required
def api_perf_reviews_list():
    staff_id = request.args.get('staff_id')
    period   = request.args.get('period')
    conds, params = ['TRUE'], []
    if staff_id: conds.append("pr.staff_id=%s"); params.append(int(staff_id))
    if period:   conds.append("pr.period_label ILIKE %s"); params.append(f'%{period}%')
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT pr.*,
                   ps.name  AS staff_name,  ps.role   AS staff_role,
                   pt.name  AS tpl_name
            FROM performance_reviews pr
            JOIN punch_staff         ps ON ps.id = pr.staff_id
            LEFT JOIN performance_templates pt ON pt.id = pr.template_id
            WHERE {' AND '.join(conds)}
            ORDER BY pr.reviewed_at DESC
        """, params).fetchall()
    result = []
    for r in rows:
        d = _perf_review_row(r)
        d['staff_name']   = r['staff_name']
        d['staff_role']   = r['staff_role']
        d['template_name'] = r['tpl_name'] or ''
        result.append(d)
    return jsonify(result)

@bp.route('/api/performance/reviews', methods=['POST'])
@login_required
def api_perf_review_create():
    b           = request.get_json(force=True)
    staff_id    = b.get('staff_id')
    template_id = b.get('template_id')
    period_label= (b.get('period_label') or '').strip()
    scores      = b.get('scores', {})
    comments    = (b.get('comments') or '').strip()
    reviewer    = (b.get('reviewer') or '').strip() or session.get('admin_display_name', '管理員')

    if not staff_id or not period_label:
        return jsonify({'error': '請選擇員工及考核期間'}), 400

    # Calculate total & grade from template items
    total = 0.0; max_s = 100.0
    if template_id:
        with get_db() as conn:
            tpl = conn.execute(
                "SELECT items FROM performance_templates WHERE id=%s", (template_id,)
            ).fetchone()
        if tpl:
            items = tpl.get('items') or []
            if isinstance(items, str):
                try: items = _json.loads(items)
                except: items = []
            if items:
                max_s = sum(float(it.get('max_score', 10)) for it in items)
                total = sum(
                    float(scores.get(str(it.get('id', it.get('name',''))), 0))
                    for it in items
                )
    else:
        total = float(b.get('total_score', 0))
        max_s = float(b.get('max_score', 100))

    pct   = (total / max_s * 100) if max_s > 0 else 0
    grade = _score_to_grade(pct)

    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO performance_reviews
              (staff_id, template_id, period_label, scores, total_score,
               max_score, grade, comments, reviewer, reviewed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) RETURNING *
        """, (staff_id, template_id or None, period_label,
              _json.dumps(scores), round(total, 2), round(max_s, 2),
              grade, comments, reviewer)).fetchone()
        staff = conn.execute(
            "SELECT name FROM punch_staff WHERE id=%s", (staff_id,)
        ).fetchone()

    # LINE 通知
    grade_labels = _grade_labels()
    msg = (f"[績效考核] {period_label} 考核結果\n"
           f"總分:{total:.1f} / {max_s:.0f}({pct:.0f}%)\n"
           f"評級:{grade} {grade_labels.get(grade,'')}\n"
           f"考核人:{reviewer}\n"
           + (f"備注:{comments[:60]}\n" if comments else '')
           + "請至員工系統查看詳情.")
    _notify_staff_line(staff_id, msg)

    d = _perf_review_row(row)
    d['staff_name'] = staff['name'] if staff else ''
    return jsonify(d), 201

@bp.route('/api/performance/reviews/<int:rid>', methods=['PUT'])
@login_required
def api_perf_review_update(rid):
    b        = request.get_json(force=True)
    scores   = b.get('scores', {})
    comments = (b.get('comments') or '').strip()
    with get_db() as conn:
        rev = conn.execute(
            "SELECT * FROM performance_reviews WHERE id=%s", (rid,)
        ).fetchone()
        if not rev: return ('', 404)
        # Recalculate score
        total = float(b.get('total_score', rev['total_score']))
        max_s = float(b.get('max_score',   rev['max_score']))
        pct   = (total / max_s * 100) if max_s > 0 else 0
        grade = _score_to_grade(pct)
        row = conn.execute("""
            UPDATE performance_reviews
            SET scores=%s, total_score=%s, max_score=%s, grade=%s,
                comments=%s, reviewed_at=NOW()
            WHERE id=%s RETURNING *
        """, (_json.dumps(scores), round(total,2), round(max_s,2),
              grade, comments, rid)).fetchone()
    return jsonify(_perf_review_row(row)) if row else ('', 404)

@bp.route('/api/performance/reviews/<int:rid>/adjust-salary', methods=['POST'])
@login_required
def api_perf_adjust_salary(rid):
    """依考核結果調薪 - 直接更新員工底薪並記錄"""
    b     = request.get_json(force=True)
    delta = float(b.get('salary_delta', b.get('delta', 0)))
    note  = (b.get('note') or '').strip()
    if delta == 0: return jsonify({'error': '調薪金額不可為 0'}), 400
    with get_db() as conn:
        rev = conn.execute(
            "SELECT * FROM performance_reviews WHERE id=%s", (rid,)
        ).fetchone()
        if not rev: return ('', 404)
        staff = conn.execute(
            "SELECT id, name, base_salary FROM punch_staff WHERE id=%s", (rev['staff_id'],)
        ).fetchone()
        if not staff: return ('', 404)
        new_salary = float(staff['base_salary'] or 0) + delta
        conn.execute(
            "UPDATE punch_staff SET base_salary=%s, insured_salary=%s WHERE id=%s",
            (new_salary, new_salary, staff['id'])
        )
        conn.execute("""
            UPDATE performance_reviews
            SET salary_adjusted=TRUE, salary_delta=%s
            WHERE id=%s
        """, (delta, rid))

    direction = '調升' if delta > 0 else '調降'
    msg = (f"[薪資調整] 績效考核連動\n"
           f"考核期:{rev['period_label']}　評級:{rev['grade']}\n"
           f"{direction} NT$ {abs(delta):,.0f}\n"
           f"新底薪:NT$ {new_salary:,.0f}\n"
           + (f"說明:{note}" if note else ''))
    _notify_staff_line(staff['id'], msg)

    return jsonify({'ok': True, 'new_salary': new_salary, 'delta': delta})

@bp.route('/api/performance/my-reviews', methods=['GET'])
def api_perf_my_reviews():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        rows = conn.execute("""
            SELECT pr.*, pt.name AS tpl_name
            FROM performance_reviews pr
            LEFT JOIN performance_templates pt ON pt.id=pr.template_id
            WHERE pr.staff_id=%s
            ORDER BY pr.reviewed_at DESC LIMIT 10
        """, (sid,)).fetchall()
    result = []
    for r in rows:
        d = _perf_review_row(r)
        d['template_name'] = r['tpl_name'] or ''
        result.append(d)
    return jsonify(result)

@bp.route('/api/performance/config', methods=['GET'])
@login_required
def api_perf_config_get():
    return jsonify({'grades': _get_grade_config()})

@bp.route('/api/performance/config', methods=['PUT'])
@login_required
def api_perf_config_update():
    b      = request.get_json(force=True)
    grades = b.get('grades', [])
    if not grades:
        return jsonify({'error': '請至少設定一個評級'}), 400
    for g in grades:
        if not str(g.get('grade', '')).strip() or not str(g.get('label', '')).strip():
            return jsonify({'error': '評級代碼與標籤不可為空'}), 400
        pct = g.get('min_pct')
        if pct is None or not (0 <= float(pct) <= 100):
            return jsonify({'error': '門檻百分比需介於 0~100'}), 400
    # 確保至少有一個門檻為 0,避免無法分級
    if not any(float(g.get('min_pct', -1)) == 0 for g in grades):
        return jsonify({'error': '必須有一個評級的門檻設為 0%(作為最低等級)'}), 400
    grades_sorted = sorted(
        [{'grade': str(g['grade']).strip(), 'label': str(g['label']).strip(),
          'min_pct': float(g['min_pct'])} for g in grades],
        key=lambda x: -x['min_pct']
    )
    with get_db() as conn:
        conn.execute("""
            INSERT INTO performance_config (key, value, updated_at)
            VALUES ('grade_config', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (_json.dumps(grades_sorted),))
    return jsonify({'ok': True, 'grades': grades_sorted})
