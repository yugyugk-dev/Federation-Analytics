from flask import Flask, request, jsonify
import sqlite3, subprocess, json, sys
from pathlib import Path

PIPELINE = str(Path(__file__).parent / "pipeline.py")
DRAFTS_DIR = Path(__file__).parent / "drafts"

app = Flask(__name__)
DB_PATH = "/Users/vladimirkopylov/Library/Application Support/federation-analytics/federation.db"


def query(sql, params=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def execute(sql, params=()):
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(sql, params)
    con.commit()
    last_id = cur.lastrowid
    con.close()
    return last_id


@app.route("/athletes", methods=["GET"])
def athletes():
    name = request.args.get("name", "")
    if name:
        rows = query(
            "SELECT * FROM athletes WHERE full_name LIKE ? ORDER BY full_name",
            (f"%{name}%",),
        )
    else:
        rows = query("SELECT * FROM athletes ORDER BY full_name")
    return jsonify(rows)


@app.route("/athletes/<int:aid>/results", methods=["GET"])
def athlete_results(aid):
    rows = query(
        "SELECT * FROM results WHERE athlete_id=? ORDER BY competition_date DESC",
        (aid,),
    )
    return jsonify(rows)


@app.route("/athletes/<int:aid>/rating", methods=["GET"])
def athlete_rating(aid):
    rows = query("SELECT * FROM athlete_ratings WHERE athlete_id=?", (aid,))
    return jsonify(rows[0] if rows else {})


@app.route("/results/search", methods=["GET"])
def results_search():
    name = request.args.get("name", "")
    competition = request.args.get("competition", "")
    rows = query(
        """SELECT r.*, a.full_name, a.coach, a.city
           FROM results r JOIN athletes a ON r.athlete_id=a.id
           WHERE (?='' OR a.full_name LIKE ?)
             AND (?='' OR r.competition_name LIKE ?)
           ORDER BY r.competition_date DESC""",
        (name, f"%{name}%", competition, f"%{competition}%"),
    )
    return jsonify(rows)


@app.route("/results", methods=["POST"])
def add_result():
    d = request.json
    athlete_name = d.get("athlete_name", "")
    athletes_found = query(
        "SELECT id FROM athletes WHERE full_name=?", (athlete_name,)
    )
    if not athletes_found:
        return jsonify({"error": f"Спортсмен '{athlete_name}' не найден"}), 404
    aid = athletes_found[0]["id"]
    levels = query("SELECT coefficient, participation_points FROM competition_levels WHERE code=?", (d.get("level_code",""),))
    level_coef = levels[0]["coefficient"] if levels else 1.0
    part_pts = levels[0]["participation_points"] if levels else 0
    place = d.get("place")
    place_rows = query("SELECT coefficient FROM place_coefficients WHERE place=?", (place,))
    place_coef = place_rows[0]["coefficient"] if place_rows else 0
    total = round(level_coef * place_coef, 2)
    last_id = execute(
        """INSERT INTO results (athlete_id, competition_date, competition_name, discipline, place,
           level_code, level_coefficient, participation_points, total_points, coach)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (aid, d.get("competition_date",""), d.get("competition_name",""),
         d.get("discipline",""), place, d.get("level_code",""),
         level_coef, part_pts, total, d.get("coach","")),
    )
    return jsonify({"id": last_id, "total_points": total})


@app.route("/competitions", methods=["GET"])
def competitions():
    rows = query("SELECT DISTINCT competition_name, competition_date, level_code FROM results ORDER BY competition_date DESC")
    return jsonify(rows)


@app.route("/top", methods=["GET"])
def top():
    limit = int(request.args.get("limit", 20))
    rows = query(
        """SELECT a.full_name, a.coach, a.city, ar.rating
           FROM athlete_ratings ar JOIN athletes a ON ar.athlete_id=a.id
           ORDER BY ar.rating DESC LIMIT ?""",
        (limit,),
    )
    return jsonify(rows)


@app.route("/pipeline/fetch", methods=["POST"])
def pipeline_fetch():
    d = request.json or {}
    yadisk_url = d.get("yadisk_url", "")
    competition_name = d.get("competition_name", "")
    if not yadisk_url:
        return jsonify({"error": "yadisk_url обязателен"}), 400
    args = [sys.executable, PIPELINE, "fetch", yadisk_url]
    if competition_name:
        args.append(competition_name)
    result = subprocess.run(args, capture_output=True, text=True, cwd=str(Path(PIPELINE).parent))
    return jsonify({"stdout": result.stdout[-3000:], "ok": result.returncode == 0})


@app.route("/pipeline/scan", methods=["POST"])
def pipeline_scan():
    d = request.json or {}
    competition_name = d.get("competition_name", "")
    if not competition_name:
        return jsonify({"error": "competition_name обязателен"}), 400
    pdfs_path = str(Path(PIPELINE).parent / "pdfs" / competition_name)
    result = subprocess.run(
        [sys.executable, PIPELINE, "scan", pdfs_path],
        capture_output=True, text=True, cwd=str(Path(PIPELINE).parent)
    )
    return jsonify({"stdout": result.stdout[-3000:], "ok": result.returncode == 0})


@app.route("/pipeline/draft", methods=["GET"])
def pipeline_draft():
    competition_name = request.args.get("competition_name", "")
    if not competition_name:
        return jsonify({"error": "competition_name обязателен"}), 400
    draft_file = DRAFTS_DIR / f"{competition_name}.json"
    if not draft_file.exists():
        return jsonify({"error": "Черновик не найден"}), 404
    with open(draft_file) as f:
        data = json.load(f)
    athletes = data.get("athletes", [])
    return jsonify({"competition": competition_name, "count": len(athletes), "athletes": athletes})


@app.route("/pipeline/import", methods=["POST"])
def pipeline_import():
    d = request.json or {}
    competition_name = d.get("competition_name", "")
    if not competition_name:
        return jsonify({"error": "competition_name обязателен"}), 400
    result = subprocess.run(
        [sys.executable, PIPELINE, "import", competition_name],
        capture_output=True, text=True, cwd=str(Path(PIPELINE).parent)
    )
    return jsonify({"stdout": result.stdout[-3000:], "ok": result.returncode == 0})


@app.route("/pipeline/run", methods=["GET"])
def pipeline_run():
    action = request.args.get("action", "")
    competition_name = request.args.get("competition_name", "")
    yadisk_url = request.args.get("yadisk_url", "")

    if action == "fetch":
        if not yadisk_url or not competition_name:
            return jsonify({"error": "Нужны yadisk_url и competition_name"}), 400
        args = [sys.executable, PIPELINE, "fetch", yadisk_url, competition_name]
        result = subprocess.run(args, capture_output=True, text=True, cwd=str(Path(PIPELINE).parent))
        return jsonify({"stdout": result.stdout[-3000:], "ok": result.returncode == 0})

    elif action == "scan":
        if not competition_name:
            return jsonify({"error": "Нужен competition_name"}), 400
        pdfs_path = str(Path(PIPELINE).parent / "pdfs" / competition_name)
        result = subprocess.run(
            [sys.executable, PIPELINE, "scan", pdfs_path],
            capture_output=True, text=True, cwd=str(Path(PIPELINE).parent)
        )
        return jsonify({"stdout": result.stdout[-3000:], "ok": result.returncode == 0})

    elif action == "draft":
        if not competition_name:
            return jsonify({"error": "Нужен competition_name"}), 400
        draft_file = DRAFTS_DIR / f"{competition_name}.json"
        if not draft_file.exists():
            return jsonify({"error": "Черновик не найден"}), 404
        with open(draft_file) as f:
            data = json.load(f)
        athletes = data.get("athletes", [])
        return jsonify({"competition": competition_name, "count": len(athletes), "athletes": athletes[:30]})

    elif action == "import":
        if not competition_name:
            return jsonify({"error": "Нужен competition_name"}), 400
        result = subprocess.run(
            [sys.executable, PIPELINE, "import", competition_name],
            capture_output=True, text=True, cwd=str(Path(PIPELINE).parent)
        )
        return jsonify({"stdout": result.stdout[-3000:], "ok": result.returncode == 0})

    elif action == "competitions":
        config_file = Path(PIPELINE).parent / "competitions_2026.json"
        if not config_file.exists():
            return jsonify([])
        with open(config_file) as f:
            return jsonify(json.load(f))

    else:
        return jsonify({"error": f"Неизвестное действие: {action}. Доступны: fetch, scan, draft, import, competitions"}), 400


@app.route("/pipeline/action", methods=["POST"])
def pipeline_action():
    d = request.json or {}
    action = d.get("action", "")
    competition_name = d.get("competition_name", "")
    yadisk_url = d.get("yadisk_url", "")

    if action == "fetch":
        if not yadisk_url or not competition_name:
            return jsonify({"error": "Нужны yadisk_url и competition_name"}), 400
        args = [sys.executable, PIPELINE, "fetch", yadisk_url, competition_name]
        result = subprocess.run(args, capture_output=True, text=True, cwd=str(Path(PIPELINE).parent))
        return jsonify({"stdout": result.stdout[-3000:], "ok": result.returncode == 0})

    elif action == "scan":
        if not competition_name:
            return jsonify({"error": "Нужен competition_name"}), 400
        pdfs_path = str(Path(PIPELINE).parent / "pdfs" / competition_name)
        result = subprocess.run(
            [sys.executable, PIPELINE, "scan", pdfs_path],
            capture_output=True, text=True, cwd=str(Path(PIPELINE).parent)
        )
        return jsonify({"stdout": result.stdout[-3000:], "ok": result.returncode == 0})

    elif action == "draft":
        if not competition_name:
            return jsonify({"error": "Нужен competition_name"}), 400
        draft_file = DRAFTS_DIR / f"{competition_name}.json"
        if not draft_file.exists():
            return jsonify({"error": "Черновик не найден"}), 404
        with open(draft_file) as f:
            data = json.load(f)
        athletes = data.get("athletes", [])
        return jsonify({"competition": competition_name, "count": len(athletes), "athletes": athletes[:30]})

    elif action == "import":
        if not competition_name:
            return jsonify({"error": "Нужен competition_name"}), 400
        result = subprocess.run(
            [sys.executable, PIPELINE, "import", competition_name],
            capture_output=True, text=True, cwd=str(Path(PIPELINE).parent)
        )
        return jsonify({"stdout": result.stdout[-3000:], "ok": result.returncode == 0})

    else:
        return jsonify({"error": f"Неизвестное действие: {action}. Доступны: fetch, scan, draft, import"}), 400


@app.route("/pipeline/competitions", methods=["GET"])
def pipeline_competitions():
    config_file = Path(PIPELINE).parent / "competitions_2026.json"
    if not config_file.exists():
        return jsonify([])
    with open(config_file) as f:
        comps = json.load(f)
    return jsonify(comps)


@app.route("/results/delete", methods=["POST"])
def delete_result():
    d = request.json or {}
    athlete_name = d.get("athlete_name", "")
    competition_name = d.get("competition_name", "")
    if not athlete_name:
        return jsonify({"error": "athlete_name обязателен"}), 400
    athletes_found = query("SELECT id FROM athletes WHERE full_name LIKE ?", (f"%{athlete_name}%",))
    if not athletes_found:
        return jsonify({"error": f"Спортсмен '{athlete_name}' не найден"}), 404
    aid = athletes_found[0]["id"]
    if competition_name:
        execute("DELETE FROM results WHERE athlete_id=? AND competition_name=?", (aid, competition_name))
        return jsonify({"ok": True, "message": f"Удалён результат {athlete_name} на {competition_name}"})
    else:
        count = query("SELECT COUNT(*) as cnt FROM results WHERE athlete_id=?", (aid,))[0]["cnt"]
        execute("DELETE FROM results WHERE athlete_id=?", (aid,))
        return jsonify({"ok": True, "message": f"Удалено {count} результатов для {athlete_name}"})


@app.route("/athletes/update", methods=["POST"])
def update_athlete():
    d = request.json or {}
    athlete_name = d.get("athlete_name", "")
    new_name = d.get("new_name", "")
    coach = d.get("coach", "")
    city = d.get("city", "")
    if not athlete_name:
        return jsonify({"error": "athlete_name обязателен"}), 400
    athletes_found = query("SELECT id FROM athletes WHERE full_name LIKE ?", (f"%{athlete_name}%",))
    if not athletes_found:
        return jsonify({"error": f"Спортсмен '{athlete_name}' не найден"}), 404
    aid = athletes_found[0]["id"]
    updates, params = [], []
    if new_name:
        updates.append("full_name=?"); params.append(new_name)
    if coach:
        updates.append("coach=?"); params.append(coach)
        execute("UPDATE results SET coach=? WHERE athlete_id=?", (coach, aid))
    if city:
        updates.append("city=?"); params.append(city)
    if not updates:
        return jsonify({"error": "Нечего обновлять"}), 400
    params.append(aid)
    execute(f"UPDATE athletes SET {', '.join(updates)} WHERE id=?", tuple(params))
    return jsonify({"ok": True, "message": f"Обновлено: {athlete_name}"})


@app.route("/results/update", methods=["POST"])
def update_result():
    d = request.json or {}
    athlete_name = d.get("athlete_name", "")
    competition_name = d.get("competition_name", "")
    place = d.get("place")
    coach = d.get("coach", "")

    athletes_found = query("SELECT id FROM athletes WHERE full_name LIKE ?", (f"%{athlete_name}%",))
    if not athletes_found:
        return jsonify({"error": f"Спортсмен '{athlete_name}' не найден"}), 404

    results_found = query(
        "SELECT r.id, r.place, r.level_coefficient, r.participation_points, r.competition_name FROM results r "
        "WHERE r.athlete_id=? AND r.competition_name LIKE ?",
        (athletes_found[0]["id"], f"%{competition_name}%")
    )
    if not results_found:
        return jsonify({"error": f"Результат не найден для '{athlete_name}' на '{competition_name}'"}), 404
    if len(results_found) > 1 and not competition_name:
        names = [r["competition_name"] for r in results_found]
        return jsonify({"error": f"Найдено несколько соревнований: {names}. Уточни название."}), 400

    r = results_found[0]
    updates = []
    params = []

    if place is not None:
        place_rows = query("SELECT coefficient FROM place_coefficients WHERE place=?", (int(place),))
        place_coef = place_rows[0]["coefficient"] if place_rows else 0
        total = round(r["level_coefficient"] * place_coef, 2)
        updates += ["place=?", "total_points=?"]
        params += [int(place), total]

    if coach:
        updates.append("coach=?")
        params.append(coach)

    if not updates:
        return jsonify({"error": "Нечего обновлять"}), 400

    params.append(r["id"])
    execute(f"UPDATE results SET {', '.join(updates)} WHERE id=?", tuple(params))
    return jsonify({"ok": True, "message": f"Обновлено: {athlete_name} на {competition_name}"})


@app.route("/audit/smart", methods=["POST"])
def audit_smart():
    """Умный аудит: исправляет всё возможное, возвращает только то что требует решения человека."""
    fixed = []
    needs_review = []

    # 1. Исправляем тренера в results из athletes
    rows = query(
        "SELECT r.id, r.athlete_id, a.full_name, a.coach as a_coach, a.city, r.competition_name, r.discipline "
        "FROM results r JOIN athletes a ON r.athlete_id=a.id "
        "WHERE (r.coach IS NULL OR r.coach='') AND a.coach IS NOT NULL AND a.coach != ''"
    )
    for r in rows:
        execute("UPDATE results SET coach=? WHERE id=?", (r["a_coach"], r["id"]))
        fixed.append(f"Тренер исправлен: {r['full_name']} ({r['competition_name']})")

    # 2. Исправляем place=None там где можно определить из диапазона в discipline
    # (например "9-13" → берём 9)
    rows2 = query(
        "SELECT r.id, a.full_name, r.competition_name, r.discipline, r.level_coefficient "
        "FROM results r JOIN athletes a ON r.athlete_id=a.id "
        "WHERE r.place IS NULL"
    )
    for r in rows2:
        # Ищем диапазон мест в названии дисциплины типа "9-13"
        import re
        m = re.search(r'\b([5-9]|[1-9]\d)-\d+\b', r["discipline"])
        if m:
            place = int(m.group(1))
            place_rows = query("SELECT coefficient FROM place_coefficients WHERE place=?", (place,))
            place_coef = place_rows[0]["coefficient"] if place_rows else 0
            total = round(r["level_coefficient"] * place_coef, 2)
            execute("UPDATE results SET place=?, total_points=? WHERE id=?", (place, total, r["id"]))
            fixed.append(f"Место {place} из диапазона: {r['full_name']} ({r['competition_name']})")
        else:
            # Определить невозможно — требует ручного ввода
            needs_review.append({
                "athlete": r["full_name"],
                "competition": r["competition_name"],
                "discipline": r["discipline"][:50],
                "issue": "Нет места",
                "action": f"Укажи место для {r['full_name']} на {r['competition_name']}"
            })

    # 2б. Обогащаем needs_review данными из черновиков (файл + страница)
    drafts_cache = {}
    for item in needs_review:
        comp = item["competition"]
        if comp not in drafts_cache:
            draft_file = DRAFTS_DIR / f"{comp}.json"
            if draft_file.exists():
                with open(draft_file) as f:
                    drafts_cache[comp] = json.load(f).get("athletes", [])
            else:
                drafts_cache[comp] = []
        athletes_in_draft = drafts_cache[comp]
        # Ищем спортсмена в черновике по фамилии
        surname = item["athlete"].split()[0].lower() if item["athlete"] else ""
        for da in athletes_in_draft:
            if surname and surname in da.get("full_name", "").lower():
                item["source_file"] = da.get("source_file", "")
                item["source_page"] = da.get("source_page", "")
                break

    # 3. Проверяем подозрительные имена (OCR ошибки)
    bad_names = query(
        "SELECT DISTINCT a.full_name, a.id FROM athletes a "
        "JOIN results r ON r.athlete_id=a.id "
        "WHERE length(a.full_name) < 5 OR a.full_name GLOB '*[0-9]*' "
        "OR a.full_name LIKE '%|%' OR a.full_name LIKE '%[%'"
    )
    for a in bad_names:
        needs_review.append({
            "athlete": a["full_name"],
            "competition": "—",
            "discipline": "—",
            "issue": "Подозрительное имя (ошибка OCR)",
            "action": f"Проверь и исправь имя: {a['full_name']}"
        })

    # 4. Перепроверяем: считаем сколько ещё осталось проблем
    remaining_no_place = query("SELECT COUNT(*) as cnt FROM results WHERE place IS NULL")[0]["cnt"]
    remaining_no_coach = query(
        "SELECT COUNT(*) as cnt FROM results r JOIN athletes a ON r.athlete_id=a.id "
        "WHERE (r.coach IS NULL OR r.coach='') AND (a.coach IS NULL OR a.coach='')"
    )[0]["cnt"]

    return jsonify({
        "auto_fixed": len(fixed),
        "fixed_details": fixed[:20],
        "needs_review_count": len(needs_review),
        "needs_review": needs_review,
        "remaining_no_place": remaining_no_place,
        "remaining_no_coach": remaining_no_coach,
        "summary": f"Исправлено автоматически: {len(fixed)}. Требует вашего решения: {len(needs_review)} (мест нет: {remaining_no_place}, тренеров нет: {remaining_no_coach})"
    })


@app.route("/fix/missing_data", methods=["POST"])
def fix_missing_data():
    d = request.json or {}
    competition_name = d.get("competition_name", "")

    fixed_coach = 0
    fixed_city = 0

    if competition_name:
        results = query(
            "SELECT r.id, r.athlete_id, r.coach, a.coach as a_coach, a.city as a_city, a.full_name "
            "FROM results r JOIN athletes a ON r.athlete_id=a.id "
            "WHERE r.competition_name=? AND (r.coach='' OR r.coach IS NULL OR a.city IS NULL OR a.city='')",
            (competition_name,)
        )
    else:
        results = query(
            "SELECT r.id, r.athlete_id, r.coach, a.coach as a_coach, a.city as a_city, a.full_name "
            "FROM results r JOIN athletes a ON r.athlete_id=a.id "
            "WHERE r.coach='' OR r.coach IS NULL"
        )

    for r in results:
        if (not r["coach"]) and r["a_coach"]:
            execute("UPDATE results SET coach=? WHERE id=?", (r["a_coach"], r["id"]))
            fixed_coach += 1

    if competition_name:
        athletes_no_city = query(
            "SELECT a.id, a.city FROM athletes a "
            "JOIN results r ON r.athlete_id=a.id "
            "WHERE r.competition_name=? AND (a.city IS NULL OR a.city='')",
            (competition_name,)
        )
    else:
        athletes_no_city = query(
            "SELECT DISTINCT a.id, a.city FROM athletes a "
            "JOIN results r ON r.athlete_id=a.id "
            "WHERE a.city IS NULL OR a.city=''"
        )

    return jsonify({
        "fixed_coach": fixed_coach,
        "fixed_city": fixed_city,
        "message": f"Исправлено: тренеров={fixed_coach}, городов={fixed_city}"
    })


@app.route("/audit", methods=["GET"])
def audit():
    competitions = query(
        "SELECT DISTINCT competition_name, competition_date, level_code FROM results ORDER BY competition_date DESC"
    )
    result = []
    for comp in competitions:
        name = comp["competition_name"]
        rows = query(
            """SELECT r.place, r.discipline, r.total_points, a.full_name, a.coach, a.city
               FROM results r JOIN athletes a ON r.athlete_id=a.id
               WHERE r.competition_name=? ORDER BY r.discipline, r.place""",
            (name,)
        )
        issues = []
        for r in rows:
            if not r["coach"]:
                issues.append(f"{r['full_name']}: нет тренера")
            if not r["city"]:
                issues.append(f"{r['full_name']}: нет города")
            if r["place"] is None:
                issues.append(f"{r['full_name']}: нет места")
        result.append({
            "competition_name": name,
            "competition_date": comp["competition_date"],
            "level_code": comp["level_code"],
            "athletes_count": len(rows),
            "issues": issues,
            "results": rows
        })
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(port=5050, debug=False)
