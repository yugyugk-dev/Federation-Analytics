#!/usr/bin/env python3
"""
Пайплайн: Яндекс.Диск / локальный PDF → OCR → черновик → БД

Использование:
  python3 pipeline.py fetch  <yadisk_url>          # скачать протоколы с Я.Диска
  python3 pipeline.py scan   <pdf_or_folder>        # OCR + найти ростовских
  python3 pipeline.py show   <competition_name>     # показать черновик
  python3 pipeline.py import <competition_name>     # импорт в БД
  python3 pipeline.py all     <yadisk_url>           # fetch + scan + show за один раз
  python3 pipeline.py coaches                       # пересчитать кэш тренеров
"""

import os, sys, json, re, sqlite3, urllib.parse, warnings
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime

warnings.filterwarnings("ignore")  # подавить SSL warning

# ── конфиг ───────────────────────────────────────────────────────────────────
DB_PATH      = Path.home() / "Library/Application Support/federation-analytics/federation.db"
DRAFTS_DIR   = Path(__file__).parent / "drafts"
PDFS_DIR     = Path(__file__).parent / "pdfs"
REGION       = "Ростовская область"
# Нужны только ОК, СЗ, ПК протоколы (не сводки и не судьи)
WANTED_PREFIXES = ("ОК-", "СЗ-", "ПК-")

DRAFTS_DIR.mkdir(exist_ok=True)
PDFS_DIR.mkdir(exist_ok=True)


# ════════════════════════════════════════════════════════════════════════════
# 1. ЗАГРУЗКА С ЯНДЕКС.ДИСКА
# ════════════════════════════════════════════════════════════════════════════

def fetch_from_yadisk(public_url: str, competition_name: str = None):
    """Скачивает ОК/СЗ/ПК протоколы из публичной папки Я.Диска.
    Поддерживает одноуровневые подпапки (Чемпионат / Первенство).
    """
    import requests

    api_list = "https://cloud-api.yandex.net/v1/disk/public/resources"
    api_dl   = "https://cloud-api.yandex.net/v1/disk/public/resources/download"

    def _list(path="/"):
        r = requests.get(api_list,
                         params={"public_key": public_url,
                                 "path": path, "limit": 100},
                         verify=False)
        r.raise_for_status()
        return r.json().get("_embedded", {}).get("items", []), r.json()

    def _download(yadisk_path: str, dest: Path):
        dl = requests.get(api_dl,
                          params={"public_key": public_url, "path": yadisk_path},
                          verify=False)
        href = dl.json().get("href")
        if not href:
            print(f"  [!] нет ссылки для {yadisk_path}")
            return False
        file_bytes = requests.get(href, verify=False).content
        dest.write_bytes(file_bytes)
        print(f"{len(file_bytes)//1024} КБ")
        return True

    root_items, root_data = _list("/")

    if not competition_name:
        competition_name = root_data.get("name", "unknown")

    comp_dir = PDFS_DIR / _safe(competition_name)
    comp_dir.mkdir(exist_ok=True)

    downloaded = []

    def _process_items(items, folder_path="/"):
        for item in items:
            name = item.get("name", "")
            itype = item.get("type", "")

            if itype == "dir":
                # Спускаемся в подпапку (один уровень вглубь)
                sub_items, _ = _list(f"{folder_path}/{name}".replace("//", "/"))
                _process_items(sub_items,
                               f"{folder_path}/{name}".replace("//", "/"))
                continue

            if not any(name.startswith(p) for p in WANTED_PREFIXES):
                continue

            dest = comp_dir / name
            if dest.exists():
                print(f"  [=] уже есть: {name}")
                downloaded.append(dest)
                continue

            yadisk_path = f"{folder_path}/{name}".replace("//", "/")
            print(f"  [↓] скачиваю {name} ...", end=" ", flush=True)
            if _download(yadisk_path, dest):
                downloaded.append(dest)

    _process_items(root_items, "/")

    print(f"\nСкачано файлов: {len(downloaded)} → {comp_dir}")
    return comp_dir, competition_name, downloaded


# ════════════════════════════════════════════════════════════════════════════
# 2. OCR + ПАРСИНГ
# ════════════════════════════════════════════════════════════════════════════

def ocr_page(img_path: str):
    """OCR одной страницы, возвращает список (text, x, y, w, h)."""
    from ocrmac import ocrmac
    results = ocrmac.OCR(img_path, language_preference=["ru-RU"]).recognize()
    return [(r[0], r[2][0], r[2][1], r[2][2], r[2][3]) for r in results]


def rows_from_ocr(ocr_items):
    """Группирует OCR-элементы в строки по Y-координате."""
    if not ocr_items:
        return []
    # Сортируем по убыванию Y (сверху вниз)
    items = sorted(ocr_items, key=lambda i: -i[2])
    rows = []
    current_row = [items[0]]
    for item in items[1:]:
        # Если Y близко к предыдущей строке — та же строка
        if abs(item[2] - current_row[-1][2]) < 0.018:
            current_row.append(item)
        else:
            rows.append(sorted(current_row, key=lambda i: i[1]))  # сорт по X
            current_row = [item]
    rows.append(sorted(current_row, key=lambda i: i[1]))
    return rows


def _parse_results_section(ocr_items, discipline: str) -> list:
    """
    Парсит секцию «Результаты» на страницах турнирных сеток.

    Формат секции:
        Результаты
        Фамилия Имя Отчество (Регион)   ← место 1
        Фамилия Имя Отчество (Регион)   ← место 2
        ...

    Спортсмены перечислены в порядке занятых мест — позиция = место.
    """
    # Ищем заголовок «Результаты» в левой половине страницы
    headers = sorted(
        [item for item in ocr_items
         if re.search(r'[Рр]езульт', item[0]) and item[1] < 0.55],
        key=lambda i: -i[2]  # сверху вниз
    )
    if not headers:
        return []

    athletes = []

    for header in headers:
        header_y = header[2]

        # Собираем items ниже заголовка (y < header_y) в пределах 0.22 единиц,
        # в левой части страницы (x < 0.50)
        below = sorted(
            [item for item in ocr_items
             if 0 < (header_y - item[2]) < 0.22 and item[1] < 0.50],
            key=lambda i: -i[2]  # сверху вниз
        )

        place_num = 0
        for item in below:
            text = item[0].strip()

            # Пропускаем строку-заголовок таблицы
            if any(w in text for w in ('Фамилия', 'Регион', 'Отчество', 'Занятое', 'Место')):
                continue

            # Паттерн: необязательный «[» + Фамилия + Имя + [Отчество] + (Регион)
            # [^)]+ захватывает весь регион до «)»; закрывающая скобка необязательна
            m = re.match(
                r'^\[?\s*([А-ЯЁ][а-яёА-ЯЁ\-]+)\s+([А-ЯЁ][а-яё]+)'
                r'(?:\s+[А-ЯЁ][а-яё]+)?\s*\(([^)]+)\)?',
                text
            )
            if not m:
                # Если строка не начинается с кириллической заглавной — конец секции
                if not re.match(r'^[\[А-ЯЁ]', text):
                    break
                continue

            place_num += 1
            surname = _clean_name(m.group(1))
            fname   = _clean_name(m.group(2))
            region  = m.group(3)

            # Пропускаем строку-заголовок вида "Фамипия Имя Отчество (Регион)"
            if surname.lower() in ('фамилия', 'фамипия', 'фамилис') or fname.lower() in ('имя', 'отчество'):
                place_num -= 1  # не считаем заголовок как место
                continue

            if _is_patronymic(fname):
                fname = ""
            if not surname:
                continue

            if 'Ростов' in region or 'ростов' in region.lower():
                athletes.append({
                    "full_name":  " ".join(filter(None, [surname, fname])),
                    "discipline": discipline,
                    "place_str":  str(place_num),
                    "grade":      "",
                    "raw_row":    f"results_section | place={place_num} | {text[:70]}",
                })

    return athletes


def find_rostov_athletes_on_page(ocr_items, discipline: str):
    """
    Извлекает ростовских спортсменов из страницы итогового протокола.

    Алгоритм: positional matching.
    Колонка фамилий (x 0.07-0.20) и колонка регионов (x 0.60-0.77)
    имеют одинаковое количество строк в одном порядке (сверху вниз).
    Если регион[i] = "Ростовская область", то фамилия[i] = нужный спортсмен.
    """
    full_text = " ".join(i[0] for i in ocr_items)
    if REGION not in full_text and "ростовская" not in full_text.lower() \
            and "гостовская" not in full_text.lower():
        return []

    # ── Колонки ─────────────────────────────────────────────────────────────
    # Фамилии — x ≈ 0.07-0.17 (семьи выровнены по левому краю)
    col_family  = sorted(
        [i for i in ocr_items if 0.07 <= i[1] <= 0.17 and _is_name_token(i[0])],
        key=lambda i: -i[2]  # сверху вниз (y убывает)
    )
    # Имена — x ≈ 0.17-0.26
    col_fname   = sorted(
        [i for i in ocr_items if 0.17 < i[1] <= 0.26 and _is_name_token(i[0])],
        key=lambda i: -i[2]
    )
    # Регионы
    col_region  = sorted(
        [i for i in ocr_items if 0.60 <= i[1] <= 0.77 and len(i[0]) > 4],
        key=lambda i: -i[2]
    )
    # Места
    col_place   = sorted(
        [i for i in ocr_items if 0.87 <= i[1] <= 0.97],
        key=lambda i: -i[2]
    )

    athletes = []

    for reg_idx, reg_item in enumerate(col_region):
        if "Ростовская" not in reg_item[0]:
            continue

        reg_y = reg_item[2]

        # ── Фамилия: positional match ─────────────────────────────────────
        # Нужно знать, сколько регионов выше по странице, и взять фамилию того же номера.
        # Но количество семей и регионов может не совпадать из-за OCR-пропусков.
        # Используем Y-proximity как первичный метод, positional — как запасной.

        # Выбираем фамилию по комбинированному score:
        #   score = Y-расстояние_до_региона + 0.5 * Y-расстояние_до_ближайшего_имени
        # Это позволяет найти правильную строку даже если фамилия чуть ниже/выше региона.
        fam = _best_family(col_family, col_fname, reg_y, tol=0.040)
        if fam is None and reg_idx < len(col_family):
            fam = col_family[reg_idx]  # запасной: по позиции

        # Имя берём ближайшее к найденной фамилии (не к региону)
        ref_y  = fam[2] if fam else reg_y
        fname  = _closest_above_or_below(col_fname, ref_y, prefer_above=False, tol=0.025)

        surname    = _clean_name(fam[0])   if fam   else ""
        first_name = _clean_name(fname[0]) if fname else ""

        # Отчество в колонке имён — отбрасываем
        if _is_patronymic(first_name):
            first_name = ""

        if not surname:
            continue

        # ── Место ─────────────────────────────────────────────────────────
        place_str = _find_place(col_place, reg_y, tol=0.040)

        # ── Разряд ────────────────────────────────────────────────────────
        grade = ""
        for gi in ocr_items:
            if 0.45 <= gi[1] <= 0.55 and abs(gi[2] - reg_y) <= 0.040:
                if re.search(r"(КМС|МС|[1-3]\s*спорт|[1-3]\s*юн)", gi[0], re.I):
                    grade = gi[0].strip()
                    break

        full_name = " ".join(filter(None, [surname, first_name]))
        athletes.append({
            "full_name":  full_name,
            "discipline": discipline,
            "place_str":  place_str,
            "grade":      grade,
            "raw_row":    f"y={reg_y:.3f} | fam_y={fam[2]:.3f} | {surname} {first_name}"
        })

    # ── Bracket-парсер: дополнить/обновить места ──────────────────────────
    # Запускаем всегда: bracket может добавить место к спортсменам без места.
    bracket = _parse_bracket_athletes(ocr_items, discipline)
    existing_keys = {(a["full_name"], a["discipline"]): a for a in athletes}
    for ba in bracket:
        key = (ba["full_name"], ba["discipline"])
        if key in existing_keys:
            if not existing_keys[key].get("place_str") and ba.get("place_str"):
                existing_keys[key]["place_str"] = ba["place_str"]
        else:
            athletes.append(ba)
            existing_keys[key] = ba

    # ── Результаты-парсер: наиболее надёжный источник итоговых мест ────────
    # Секция «Результаты» на bracket-страницах даёт окончательный порядок мест.
    # Перезаписывает места от bracket-парсера (bracket-позиции менее надёжны).
    def _disc_prefix(d: str) -> str:
        """Вернуть prefix дисциплины: 'ОК', 'СЗ' или 'ПК'."""
        return d[:2] if d else ""

    for rs in _parse_results_section(ocr_items, discipline):
        key = (rs["full_name"], rs["discipline"])
        rs_surname = rs["full_name"].split()[0].lower()
        rs_prefix  = _disc_prefix(rs["discipline"])
        matched = False

        # 1) Точное совпадение ключа
        if key in existing_keys:
            existing_keys[key]["place_str"] = rs["place_str"]
            existing_keys[key]["raw_row"]   = rs["raw_row"]
            matched = True

        # 2) Совпадение по фамилии (точное/префикс/fuzzy) + префиксу дисциплины
        if not matched:
            best_ek, best_ratio = None, 0.0
            for ek_key, ek_val in existing_keys.items():
                ek_surname = ek_key[0].split()[0].lower()
                ek_prefix  = _disc_prefix(ek_key[1])
                if ek_prefix != rs_prefix:
                    continue
                if ek_surname == rs_surname:
                    best_ek, best_ratio = ek_val, 2.0
                    break
                if ek_surname.startswith(rs_surname) or rs_surname.startswith(ek_surname):
                    if 1.9 > best_ratio:
                        best_ek, best_ratio = ek_val, 1.9
                else:
                    ratio = SequenceMatcher(None, ek_surname, rs_surname).ratio()
                    if ratio >= 0.88 and ratio > best_ratio:
                        best_ek, best_ratio = ek_val, ratio
            if best_ek is not None:
                best_ek["place_str"] = rs["place_str"]
                best_ek["raw_row"]   = rs["raw_row"]
                if len(rs["full_name"]) > len(best_ek.get("full_name", "")):
                    best_ek["full_name"] = rs["full_name"]
                matched = True

        if not matched:
            athletes.append(rs)
            existing_keys[key] = rs

    return athletes


def _parse_bracket_athletes(ocr_items, discipline: str) -> list:
    """
    Парсит страницы в формате «турнирная сетка» (поединки).

    В таких PDF место + фамилия + имя + (регион) идут в одном OCR-блоке:
      "4 Потапова Варвара (Ростовская область)"
      "[ Волоховская Альбина (Ростовская область)"  ← [ = 1
      "з Потапова Варвара (Ростовская область"       ← з = 3
      "Ляшков Артем (Ростовская область)"            ← без номера места

    Только x ≈ 0.04-0.16 (левая колонка итоговых мест).
    """
    athletes = []

    # Нормализация символов, похожих на цифры
    _PLACE_NORM = {"[": "1", "з": "3", "З": "3"}

    for text, x, y, pw, ph in ocr_items:
        # Только левая колонка
        if not (0.04 <= x <= 0.16):
            continue
        text = text.strip()

        # Паттерн 1: "{место} Фамилия Имя [(Отчество)] (Ростов...)"
        m = re.match(
            r'^([\d\[зЗ]+)\s+([А-ЯЁ][а-яёА-ЯЁ\-]+)\s+([А-ЯЁ][а-яё]+)'
            r'(?:\s+[А-ЯЁ][а-яё]+)?\s*\((?:[Рр]остов)',
            text
        )
        if m:
            raw_place = m.group(1)
            place_str = _PLACE_NORM.get(raw_place, raw_place if raw_place.isdigit() else "")
            surname   = _clean_name(m.group(2))
            fname     = _clean_name(m.group(3))
            if _is_patronymic(fname):
                fname = ""
            if surname:
                athletes.append({
                    "full_name":  " ".join(filter(None, [surname, fname])),
                    "discipline": discipline,
                    "place_str":  place_str,
                    "grade":      "",
                    "raw_row":    f"y={y:.3f} | bracket | {text[:60]}",
                })
            continue

        # Паттерн 2: "Фамилия Имя [(Отчество)] (Ростов...)" — без номера места
        m2 = re.match(
            r'^([А-ЯЁ][а-яёА-ЯЁ\-]+)\s+([А-ЯЁ][а-яё]+)'
            r'(?:\s+[А-ЯЁ][а-яё]+)?\s*\((?:[Рр]остов)',
            text
        )
        if m2:
            surname = _clean_name(m2.group(1))
            fname   = _clean_name(m2.group(2))
            if _is_patronymic(fname):
                fname = ""
            if surname and len(surname) >= 3:
                athletes.append({
                    "full_name":  " ".join(filter(None, [surname, fname])),
                    "discipline": discipline,
                    "place_str":  "",
                    "grade":      "",
                    "raw_row":    f"y={y:.3f} | bracket_noplace | {text[:60]}",
                })

    return athletes


def _is_name_token(token: str) -> bool:
    """True если токен похож на имя (кириллица, ≥ 3 букв, не число)."""
    t = _clean_name(token)
    return bool(t) and len(t) >= 3 and not re.match(r"^\d", t) and \
           t not in ("КМС", "МС", "ОК", "СЗ", "ПК", "Главный", "Вид", "Дата",
                     "Разряд", "Тренер", "Регион", "Команда", "Занятое", "Место",
                     "Всероссийские", "Московская", "Санкт")


def _best_family(col_family: list, col_fname: list, reg_y: float, tol: float = 0.040):
    """
    Двухуровневая стратегия выбора фамилии:

    1. Приоритет: фамилия ВЫШЕ региона (y_fam >= reg_y) с допуском 0.013.
       Типичная ситуация: текст фамилии в ячейке рендерится чуть выше текста региона.
    2. Резерв: если ничего не найдено выше, берём абсолютно ближайшую по Y.
       Так работает для страниц, где фамилия оказывается чуть ниже региона.
    """
    TOL_ABOVE = 0.013   # тесный допуск для «выше»

    # Уровень 1: фамилии, которые выше региона (или на одном уровне) ± TOL_ABOVE
    above = [i for i in col_family if 0 <= (i[2] - reg_y) <= TOL_ABOVE]
    if above:
        # Среди них — с лучшим combined score
        return _score_best(above, col_fname, reg_y, tol)

    # Уровень 2: все в пределах tol — берём абсолютно ближайшую
    candidates = [i for i in col_family if abs(i[2] - reg_y) <= tol]
    if not candidates:
        return None
    return _score_best(candidates, col_fname, reg_y, tol)


def _score_best(candidates: list, col_fname: list, reg_y: float, tol: float):
    """Из кандидатов выбирает фамилию с минимальным combined score."""
    if len(candidates) == 1:
        return candidates[0]
    best, best_score = None, float("inf")
    for fam in candidates:
        y_prox = abs(fam[2] - reg_y)
        fname_diffs = [abs(fn[2] - fam[2]) for fn in col_fname
                       if abs(fn[2] - reg_y) <= tol + 0.02]
        fname_min = min(fname_diffs) if fname_diffs else 0.05
        score = y_prox + 0.5 * fname_min
        if score < best_score:
            best_score = score
            best = fam
    return best


def _closest_above_or_below(col: list, y_ref: float, prefer_above: bool = True,
                             tol: float = 0.035) -> tuple | None:
    """
    Возвращает токен из col ближайший по Y к y_ref (в пределах tol).
    prefer_above=True → сначала смотрим выше (y >= y_ref), только потом ниже.
    prefer_above=False → берём абсолютно ближайший независимо от направления.
    """
    candidates = [i for i in col if abs(i[2] - y_ref) <= tol]
    if not candidates:
        return None
    if not prefer_above:
        return min(candidates, key=lambda i: abs(i[2] - y_ref))
    above = [i for i in candidates if i[2] >= y_ref]
    below = [i for i in candidates if i[2] <  y_ref]
    if above:
        return min(above, key=lambda i: i[2] - y_ref)
    return min(below, key=lambda i: y_ref - i[2])


def _find_place(col_place: list, y_ref: float, tol: float) -> str:
    """Ищет токен с местом (диапазон или число) рядом с y_ref."""
    candidates = sorted(
        [i for i in col_place if abs(i[2] - y_ref) <= tol],
        key=lambda i: abs(i[2] - y_ref)
    )
    for pm in candidates:
        m = re.search(r"(\d{1,2})[-–](\d{1,3})", pm[0])
        if m:
            return m.group(0)
        if re.match(r"^\d{1,2}$", pm[0]) and int(pm[0]) <= 64:
            return pm[0]
    return ""


_PATRONYMIC_RE = re.compile(
    r"(?:ович|евич|ич|овна|евна|ична|инична)$", re.I
)


def _is_patronymic(token: str) -> bool:
    """True если токен похож на отчество (оканчивается на -ович/-евна и т.п.)."""
    return bool(_PATRONYMIC_RE.search(token))


def _clean_name(token: str) -> str:
    """Очищает токен имени от OCR-мусора (ж, [, _, . и т.п.)."""
    t = re.sub(r"^[^А-ЯЁа-яёA-Za-z]+", "", token)
    t = re.sub(r"[^А-ЯЁа-яёA-Za-z]+$", "", t).strip()
    # Артефакт: одиночная заглавная буква приклеена к следующей заглавной
    # (напр. "ГРоманкулова" → "Романкулова"). В рус. фамилиях два подряд
    # заглавных в начале — нетипичная ситуация.
    if re.match(r"^[А-ЯЁ]{2}[а-яё]", t):
        t = t[1:]
    # OCR иногда читает имя со строчной буквы — нормализуем
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    if re.match(r"^[А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z\-]{1,}$", t):
        return t
    return ""


def scan_pdf(pdf_path: Path, competition_name: str, comp_date: str, level: str):
    """OCR всего PDF, возвращает список найденных ростовских спортсменов."""
    import pdfplumber

    discipline_from_filename = pdf_path.stem  # "ОК-поединки", "СЗ-ката" и т.д.
    athletes = []
    seen = set()

    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        print(f"  {pdf_path.name}: {total} стр.", flush=True)

        for page_num, page in enumerate(pdf.pages):
            img = page.to_image(resolution=150)
            img_path = f"/tmp/ocr_page_{page_num}.png"
            img.save(img_path)

            ocr_items = ocr_page(img_path)
            full_text = " ".join(i[0] for i in ocr_items)

            # Быстрая проверка наличия региона
            if REGION not in full_text and "ростовская" not in full_text.lower() \
                    and "гостовская" not in full_text.lower():
                if (page_num + 1) % 20 == 0:
                    print(f"    стр {page_num+1}/{total}...", flush=True)
                continue

            print(f"    [!] стр {page_num+1}: найдена Ростовская область", flush=True)

            # Определяем дисциплину из заголовка страницы
            discipline = _extract_discipline(full_text, discipline_from_filename)

            found = find_rostov_athletes_on_page(ocr_items, discipline)
            # Индекс для обновления места у уже найденных спортсменов
            seen_map = {(a["full_name"], a["discipline"]): a for a in athletes}

            def _disc_pfx(d: str) -> str:
                return d[:2] if d else ""

            def _find_existing(full_name: str, disc: str):
                """Ищет существующую запись по точному ключу, затем по фамилии+префикс+fuzzy."""
                key = (full_name, disc)
                if key in seen_map:
                    return seen_map[key]
                new_sur = full_name.split()[0].lower()
                new_pfx = _disc_pfx(disc)
                best_match, best_score = None, 0.0
                for (en, ed), ev in seen_map.items():
                    ex_sur = en.split()[0].lower()
                    ex_pfx = _disc_pfx(ed)
                    if ex_pfx != new_pfx:
                        continue
                    # Точное совпадение фамилии
                    if ex_sur == new_sur:
                        return ev
                    # Одна является префиксом другой (OCR-обрезание)
                    if ex_sur.startswith(new_sur) or new_sur.startswith(ex_sur):
                        return ev
                    # Нечёткое совпадение (OCR-ошибка типа Новохатченко/Новожатченко)
                    ratio = SequenceMatcher(None, ex_sur, new_sur).ratio()
                    if ratio > best_score:
                        best_score = ratio
                        best_match = ev
                if best_score >= 0.88:
                    return best_match
                return None

            for a in found:
                key = (a["full_name"], discipline)
                existing = _find_existing(a["full_name"], discipline)
                if existing is None:
                    seen.add(key)
                    a["competition_name"] = competition_name
                    a["competition_date"] = comp_date
                    a["level_code"] = level
                    a["source_file"] = pdf_path.name
                    a["source_page"] = page_num + 1
                    athletes.append(a)
                    seen_map[key] = a
                else:
                    if a.get("place_str") and not existing.get("place_str"):
                        existing["place_str"] = a["place_str"]
                    # Обновляем имя если у новой записи оно полнее
                    if len(a["full_name"]) > len(existing.get("full_name", "")):
                        existing["full_name"] = a["full_name"]

    return athletes


def _extract_discipline(text: str, fallback: str) -> str:
    """Извлекает название дисциплины из текста протокола.

    Ищет первое вхождение ОК/СЗ/ПК и берёт текст до следующего такого же
    маркера или до явного OCR-мусора (латиница, повтор, числа-результаты).
    """
    # Разбиваем на фрагменты по маркеру ОК/СЗ/ПК
    # Пример: "...ОК - весовая категория 65 кг, юн. 14-15 лет ОК - 65 кг OK..."
    m = re.search(r"(ОК|СЗ|ПК)\s*[-–]\s*(.+?)(?=\s+(?:ОК|СЗ|ПК|OK|СЗ)\s*[-–]|$)", text)
    if m:
        disc = m.group(2).strip()
        # Обрезаем по первому вхождению латиницы (OCR-повтор на другом языке)
        disc = re.split(r"[A-Za-z]{2,}", disc)[0].strip()
        # Обрезаем по двойному пробелу (граница между блоками OCR)
        disc = re.split(r"\s{2,}", disc)[0].strip()
        # Обрезаем на номере участника: "ката-ренгокай 16 Иванов..." → "ката-ренгокай"
        disc = re.sub(r"\s+\d{1,3}\s+[А-ЯЁ].*$", "", disc).strip()
        # Обрезаем на разряде/звании в тексте дисциплины
        disc = re.sub(r"\s+(?:\d\s*спорт|\d\s*юн|КМС|МС)\b.*$", "", disc, flags=re.I).strip()
        # Убираем хвостовые цифры-результаты и мусор в скобках
        disc = re.sub(r"\s*[\[\(].*$", "", disc).strip()
        disc = re.sub(r"\s+\d+[.,]\d+.*$", "", disc).strip()
        # После слов мужчины/женщины/юноши/девушки дисциплина заканчивается
        disc = re.sub(r"((?:мужчины|женщины|юноши|девушки|мальчики|девочки))\s+[А-ЯЁ•].*$", r"\1", disc).strip()
        # Убираем маркер • и всё что после него
        disc = re.sub(r"\s*•.*$", "", disc).strip()
        # Убираем слова-артефакты похожие на ФИО ("фамилис", "Барбин" и т.п.)
        disc = re.sub(r"\s+[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}.*$", "", disc).strip()
        # Убираем OCR-артефакт заголовка "Фамилия" → "фамилис" и подобные
        disc = re.sub(r"\s+фамили\w*.*$", "", disc, flags=re.I).strip()
        # Убираем хвостовые одиночные числа (номера участников без имени)
        disc = re.sub(r"\s+\d+\s*$", "", disc).strip()
        if len(disc) >= 3:
            return (m.group(1) + "-" + disc)[:60]
    return fallback


def _safe(s: str) -> str:
    """Безопасное имя для файловой системы."""
    return re.sub(r"[^\w\-,. ]", "_", s).strip()


# ════════════════════════════════════════════════════════════════════════════
# 2б. РЕГИОНАЛЬНЫЙ ПАРСЕР (Первенство/Чемпионат РО)
# ════════════════════════════════════════════════════════════════════════════

def _parse_reg_protocol(ocr_items, fallback_disc: str, page_num: int) -> list:
    """
    Парсит ПРОТОКОЛ РЕГИСТРАЦИИ региональных соревнований.
    Фамилия: x ≈ 0.07-0.21, Имя: x ≈ 0.21-0.28,
    Дисциплина: x ≈ 0.49-0.61, Место: x ≈ 0.78-0.97
    """
    athletes = []

    col_surname = sorted([i for i in ocr_items if 0.07 <= i[1] <= 0.21], key=lambda i: -i[2])
    col_fname   = sorted([i for i in ocr_items if 0.21 < i[1] <= 0.28],  key=lambda i: -i[2])
    col_disc    = sorted([i for i in ocr_items if 0.49 <= i[1] <= 0.61], key=lambda i: -i[2])
    col_place   = sorted([i for i in ocr_items if 0.78 <= i[1] <= 0.97], key=lambda i: -i[2])

    for sur_item in col_surname:
        raw = sur_item[0].strip()
        # Убираем ведущий номер и пол: "11 Муж Сергеев" → "Сергеев"
        cleaned = re.sub(r'^\d+\s*', '', raw)
        cleaned = re.sub(r'^(Муж|Жен|муж|жен)\s+', '', cleaned, flags=re.I)
        surname = _clean_name(cleaned.split()[0]) if cleaned.strip() else ""
        if not surname or len(surname) < 3:
            continue

        reg_y = sur_item[2]

        fname_item = _closest_above_or_below(col_fname, reg_y, prefer_above=False, tol=0.025)
        first_name = ""
        if fname_item:
            fn = _clean_name(fname_item[0])
            if fn and not _is_patronymic(fn):
                first_name = fn

        disc_item = _closest_above_or_below(col_disc, reg_y, prefer_above=False, tol=0.025)
        discipline = fallback_disc
        if disc_item:
            raw_d = disc_item[0].strip().lstrip('[').lstrip('Г').strip()
            m = re.search(r'(ОК|СЗ|ПК)\s*[-–]\s*(.+)', raw_d)
            if m:
                discipline = (m.group(1) + "-" + m.group(2).strip())[:60]

        place_str = _find_place(col_place, reg_y, tol=0.030)

        athletes.append({
            "full_name":  " ".join(filter(None, [surname, first_name])),
            "discipline": discipline,
            "place_str":  place_str,
            "grade":      "",
            "raw_row":    f"page={page_num} y={reg_y:.3f} | reg | {raw[:50]}",
        })
    return athletes


def _parse_итог_protocol(ocr_items, fallback_disc: str, page_num: int) -> list:
    """
    Парсит ИТОГОВЫЙ ПРОТОКОЛ (формат СЗ).
    Место задаётся порядком строк сверху вниз.
    Фамилия: x ≈ 0.10-0.18, Имя: x ≈ 0.21-0.28.
    """
    athletes = []

    # Заголовок дисциплины
    disc_headers = sorted(
        [i for i in ocr_items if 0.04 <= i[1] <= 0.20 and re.search(r'[ЖМ]\s*\d{2}', i[0])],
        key=lambda i: -i[2]
    )
    if disc_headers:
        dh = disc_headers[0]
        discipline = _extract_discipline(" " + dh[0], fallback_disc)
        header_y = dh[2]
    else:
        discipline = fallback_disc
        header_y = 1.0

    col_surname = sorted(
        [i for i in ocr_items if 0.10 <= i[1] <= 0.18 and _is_name_token(i[0])],
        key=lambda i: -i[2]
    )
    col_fname = sorted(
        [i for i in ocr_items if 0.21 < i[1] <= 0.28 and _is_name_token(i[0])],
        key=lambda i: -i[2]
    )

    for place_num, sur_item in enumerate(
            [s for s in col_surname if s[2] < header_y], 1):
        surname = _clean_name(sur_item[0])
        if not surname or len(surname) < 3:
            continue
        reg_y = sur_item[2]
        fname_item = _closest_above_or_below(col_fname, reg_y, prefer_above=False, tol=0.025)
        first_name = ""
        if fname_item:
            fn = _clean_name(fname_item[0])
            if fn and not _is_patronymic(fn):
                first_name = fn
        athletes.append({
            "full_name":  " ".join(filter(None, [surname, first_name])),
            "discipline": discipline,
            "place_str":  str(place_num),
            "grade":      "",
            "raw_row":    f"page={page_num} y={reg_y:.3f} | итог | place={place_num}",
        })
    return athletes


def scan_regional_pdf(pdf_path: Path, competition_name: str,
                      comp_date: str, level: str) -> list:
    """OCR регионального протокола — берём всех спортсменов (все свои)."""
    import pdfplumber
    athletes = []
    seen: dict = {}

    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        print(f"  {pdf_path.name}: {total} стр.", flush=True)

        for page_num, page in enumerate(pdf.pages):
            img = page.to_image(resolution=150)
            img_path = f"/tmp/ocr_page_{page_num}.png"
            img.save(img_path)
            ocr_items = ocr_page(img_path)
            full_text  = " ".join(i[0] for i in ocr_items).upper()

            fallback = pdf_path.stem

            if "ИТОГОВЫЙ" in full_text:
                found = _parse_итог_protocol(ocr_items, fallback, page_num + 1)
            else:
                found = _parse_reg_protocol(ocr_items, fallback, page_num + 1)

            if found:
                print(f"    стр {page_num+1}: {len(found)} спортсменов", flush=True)

            for a in found:
                key = (a["full_name"], a["discipline"])
                if key not in seen:
                    a["competition_name"] = competition_name
                    a["competition_date"] = comp_date
                    a["level_code"]       = level
                    a["source_file"]      = pdf_path.name
                    a["source_page"]      = page_num + 1
                    athletes.append(a)
                    seen[key] = a
                else:
                    if a.get("place_str") and not seen[key].get("place_str"):
                        seen[key]["place_str"] = a["place_str"]
    return athletes


# ════════════════════════════════════════════════════════════════════════════
# 3. ЧЕРНОВИК
# ════════════════════════════════════════════════════════════════════════════

def save_draft(competition_name: str, athletes: list):
    path = DRAFTS_DIR / f"{_safe(competition_name)}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"competition": competition_name,
                   "created": datetime.now().isoformat()[:19],
                   "athletes": athletes}, f, ensure_ascii=False, indent=2)
    print(f"\nЧерновик сохранён: {path}")
    return path


def load_draft(competition_name: str) -> dict:
    path = DRAFTS_DIR / f"{_safe(competition_name)}.json"
    if not path.exists():
        # попробуем частичное совпадение
        matches = list(DRAFTS_DIR.glob(f"*{_safe(competition_name)[:10]}*.json"))
        if matches:
            path = matches[0]
        else:
            print(f"Черновик не найден: {path}")
            sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def show_draft(competition_name: str):
    data = load_draft(competition_name)
    athletes = data["athletes"]
    print(f"\n{'='*60}")
    print(f"Соревнование: {data['competition']}")
    print(f"Создан: {data['created']}")
    print(f"Найдено спортсменов: {len(athletes)}")
    print(f"{'='*60}")
    for i, a in enumerate(athletes, 1):
        place = a.get("place_str") or "—"
        grade = a.get("grade") or ""
        print(f"{i:3}. {a['full_name']:<35} | {a['discipline'][:30]:<30} | место: {place:<8} | {grade}")
    print(f"\nДля редактирования: {DRAFTS_DIR / (_safe(competition_name) + '.json')}")


# ════════════════════════════════════════════════════════════════════════════
# 4. ТРЕНЕРЫ
# ════════════════════════════════════════════════════════════════════════════

def _split_coaches(coach_str: str) -> list:
    """
    Разбивает строку тренеров на список отдельных тренеров.
    'Полуян К.М. Верведа А.С.' → ['Полуян К.М.', 'Верведа А.С.']
    'Болгова Е.В.' → ['Болгова Е.В.']
    """
    if not coach_str or not coach_str.strip():
        return []
    coaches = re.findall(r'[А-ЯЁ][а-яёА-ЯЁ\-]+\s+[А-ЯЁ]\.[А-ЯЁ]\.', coach_str.strip())
    return coaches if coaches else [coach_str.strip()]


def refresh_coaches_cache():
    """
    Пересчитывает coaches_cache:
    - Если у спортсмена 2 тренера, рейтинг начисляется каждому.
    - Каждый тренер — уникальный индивид с единым суммарным рейтингом.
    """
    from datetime import date
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    cur.execute("""
        SELECT a.id, a.full_name, a.coach, a.birth_date, a.city,
               COALESCE(ar.rating, 0.0) as rating
        FROM athletes a
        LEFT JOIN athlete_ratings ar ON ar.athlete_id = a.id
        WHERE a.coach IS NOT NULL AND a.coach != ''
    """)
    athlete_rows = cur.fetchall()

    cur.execute("""
        SELECT athlete_id, COUNT(*) FROM results
        WHERE place BETWEEN 1 AND 3
        GROUP BY athlete_id
    """)
    medals_by_athlete = {r[0]: r[1] for r in cur.fetchall()}

    coaches = {}
    current_year = date.today().year

    for athlete_id, full_name, coach_str, birth_date, city, rating in athlete_rows:
        coach_list = _split_coaches(coach_str)
        share = 1.0 / len(coach_list) if len(coach_list) > 1 else 1.0
        for coach_name in coach_list:
            coach_name = coach_name.strip()
            if not coach_name:
                continue
            if coach_name not in coaches:
                coaches[coach_name] = {'total_rating': 0.0, 'athletes': [], 'medals': 0}
            coaches[coach_name]['total_rating'] += rating * share
            coaches[coach_name]['athletes'].append((full_name, rating * share, birth_date, city))
            coaches[coach_name]['medals'] += medals_by_athlete.get(athlete_id, 0) * share

    cur.execute("DELETE FROM coaches_cache")

    for coach_name, data in coaches.items():
        ath = data['athletes']
        if not ath:
            continue
        athlete_count = len(ath)
        total_rating  = round(data['total_rating'], 4)
        avg_rating    = round(total_rating / athlete_count, 4)
        best          = max(ath, key=lambda x: x[1])
        best_athlete  = best[0]
        best_rating   = round(best[1], 4)

        ages = []
        for _, _, bd, _ in ath:
            if not bd:
                continue
            try:
                yr = int(str(bd).split('.')[2]) if '.' in str(bd) else int(str(bd)[:4])
                ages.append(current_year - yr)
            except (ValueError, IndexError):
                pass
        avg_age      = round(sum(ages) / len(ages), 1) if ages else 0.0
        cities_count = len({c for _, _, _, c in ath if c})

        cur.execute("""
            INSERT OR REPLACE INTO coaches_cache
              (name, athlete_count, avg_rating, best_athlete, best_rating,
               total_medals, avg_age, cities_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (coach_name, athlete_count, avg_rating, best_athlete, best_rating,
              data['medals'], avg_age, cities_count))

    conn.commit()
    conn.close()

    print(f"\nКэш тренеров обновлён: {len(coaches)} тренеров")
    for name, data in sorted(coaches.items(),
                              key=lambda x: x[1]['total_rating'], reverse=True)[:15]:
        print(f"  {name:<28} | {len(data['athletes'])} спорт. | "
              f"рейтинг: {data['total_rating']:.1f}")


# ════════════════════════════════════════════════════════════════════════════
# 5. ИМПОРТ В БД
# ════════════════════════════════════════════════════════════════════════════

def _place_to_int(place_str: str):
    """Конвертирует "9-16" → 9, "5" → 5, "" → None."""
    if not place_str:
        return None
    m = re.match(r"(\d+)", str(place_str))
    return int(m.group(1)) if m else None


def _find_athlete_id(cur, name: str) -> int | None:
    """
    Ищет спортсмена по имени с тремя уровнями:
    1. Точное совпадение: "Салов Демьян Алексеевич"
    2. Префикс: "Салов Демьян" → "Салов Демьян Алексеевич"
    3. Нечёткое: "Романкупова Арина" → "Романкулова Арина Зралиевна" (OCR-ошибка)
    Возвращает id или None.
    """
    from difflib import SequenceMatcher

    cur.execute("SELECT id FROM athletes WHERE full_name=?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]

    parts = name.strip().split()
    if len(parts) < 2:
        return None

    prefix = parts[0] + " " + parts[1]

    # Префиксный поиск: "Фамилия Имя" → "Фамилия Имя Отчество"
    cur.execute(
        "SELECT id FROM athletes WHERE full_name LIKE ? ORDER BY id LIMIT 1",
        (prefix + " %",)
    )
    row = cur.fetchone()
    if row:
        return row[0]

    # Нечёткое совпадение: ищем по похожей "Фамилия Имя" (порог 0.87)
    # Помогает при OCR-ошибке в фамилии (купова vs кулова)
    cur.execute("SELECT id, full_name FROM athletes ORDER BY id")
    best_score, best_id = 0.0, None
    for aid, aname in cur.fetchall():
        aparts = aname.strip().split()
        if len(aparts) < 2:
            continue
        candidate = aparts[0] + " " + aparts[1]
        score = SequenceMatcher(None, prefix, candidate).ratio()
        if score >= 0.87 and score > best_score:
            best_score = score
            best_id = aid

    return best_id


def _migrate_results_coach(cur):
    """Добавляет колонку coach в results и заполняет из athletes.coach."""
    try:
        cur.execute("ALTER TABLE results ADD COLUMN coach TEXT DEFAULT ''")
    except Exception:
        return  # уже есть
    cur.execute("""
        UPDATE results SET coach = (
            SELECT COALESCE(coach,'') FROM athletes WHERE id = results.athlete_id
        ) WHERE coach = '' OR coach IS NULL
    """)


def import_to_db(competition_name: str, dry_run: bool = False):
    data = load_draft(competition_name)
    athletes = data["athletes"]

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    if not dry_run:
        _migrate_results_coach(cur)

    # Уровень соревнования
    level = athletes[0].get("level_code", "E") if athletes else "E"
    cur.execute(
        "SELECT coefficient, participation_points FROM competition_levels WHERE code=?",
        (level,)
    )
    row = cur.fetchone()
    level_coef, participation_pts = (row[0], row[1]) if row else (1.0, 0)

    # Все коэффициенты за место в словарь {place: coef}
    cur.execute("SELECT place, coefficient FROM place_coefficients")
    place_coefs = {r[0]: r[1] for r in cur.fetchall()}

    added_athletes = 0
    added_results  = 0
    skipped        = 0

    for a in athletes:
        name = a["full_name"].strip()
        if not name:
            continue

        comp_name  = a["competition_name"]
        comp_date  = a["competition_date"]
        discipline = a["discipline"]
        place      = _place_to_int(a.get("place_str"))

        # total_points = place_coef × level_coef × participation_points
        place_coef   = place_coefs.get(place, 0.0) if place else 0.0
        total_points = round(place_coef * level_coef * participation_pts, 4)

        # Найти или создать спортсмена (нечёткий поиск по префиксу)
        athlete_id = _find_athlete_id(cur, name)
        if athlete_id is None:
            if not dry_run:
                cur.execute(
                    "INSERT INTO athletes (full_name, grade) VALUES (?, ?)",
                    (name, a.get("grade", ""))
                )
                athlete_id = cur.lastrowid
                added_athletes += 1
            else:
                print(f"  [dry] НОВЫЙ спортсмен: {name}")
                athlete_id = -(len(athletes))  # уникальный фиктивный id

        # Проверка дубликата результата
        cur.execute(
            "SELECT id FROM results WHERE athlete_id=? AND competition_name=? AND discipline=?",
            (athlete_id, comp_name, discipline)
        )
        if cur.fetchone():
            skipped += 1
            continue

        if not dry_run:
            cur.execute("SELECT COALESCE(coach,'') FROM athletes WHERE id=?", (athlete_id,))
            r = cur.fetchone()
            athlete_coach = r[0] if r else ""
            cur.execute("""
                INSERT INTO results
                  (athlete_id, competition_date, competition_name, discipline,
                   place, level_code, level_coefficient,
                   participation_points, total_points, coach)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (athlete_id, comp_date, comp_name, discipline,
                  place, level, level_coef,
                  float(participation_pts), total_points, athlete_coach))
            added_results += 1
        else:
            pts_str = f"{total_points:.1f}" if total_points else "0"
            print(f"  [dry] {name:<30} | {discipline[:35]:<35} | место: {str(place):<4} | очки: {pts_str}")

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\n{'[dry-run] ' if dry_run else ''}Импорт завершён:")
    print(f"  Новых спортсменов: {added_athletes}")
    print(f"  Новых результатов: {added_results}")
    print(f"  Пропущено (дубли): {skipped}")

    if not dry_run:
        refresh_coaches_cache()


# ════════════════════════════════════════════════════════════════════════════
# 5. ТОЧКА ВХОДА
# ════════════════════════════════════════════════════════════════════════════

def cmd_fetch(args):
    if len(args) < 1:
        print("Использование: pipeline.py fetch <yadisk_url> [имя соревнования] [дата YYYY-MM-DD] [уровень]")
        sys.exit(1)
    url  = args[0]
    name = args[1] if len(args) > 1 else None
    comp_dir, comp_name, files = fetch_from_yadisk(url, name)
    print(f"Папка: {comp_dir}")
    return comp_dir, comp_name, files


def _load_competitions_config() -> dict:
    """Возвращает {name: {date, level}} из competitions_2026.json."""
    cfg_path = Path(__file__).parent / "competitions_2026.json"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {c["name"]: c for c in data}
    return data


def cmd_scan(args):
    if len(args) < 1:
        print("Использование: pipeline.py scan <папка_или_pdf> <имя_соревнования> <дата> <уровень> [--all-pdfs]")
        sys.exit(1)
    all_pdfs  = "--all-pdfs"  in args
    regional  = "--regional"  in args
    args = [a for a in args if not a.startswith("--")]

    path = Path(args[0])
    comp_name = args[1] if len(args) > 1 else path.name

    # Ищем дату и уровень в competitions_2026.json по названию
    comps_cfg = _load_competitions_config()
    cfg = comps_cfg.get(comp_name, {})
    comp_date = args[2] if len(args) > 2 else cfg.get("date", "2026-01-01")
    level     = args[3] if len(args) > 3 else cfg.get("level", "E")

    if path.is_file():
        pdfs = [path]
    else:
        pdfs = sorted(path.glob("*.pdf"))
        if not all_pdfs:
            pdfs = [p for p in pdfs if any(p.name.startswith(pr) for pr in WANTED_PREFIXES)]
        # Исключаем судейские списки
        pdfs = [p for p in pdfs if not any(w in p.name.lower() for w in ("судь", "список"))]
    print(f"PDF к обработке: {len(pdfs)}")

    all_athletes = []
    for pdf in pdfs:
        if regional:
            found = scan_regional_pdf(pdf, comp_name, comp_date, level)
            print(f"  → {pdf.name}: {len(found)} спортсменов")
        else:
            found = scan_pdf(pdf, comp_name, comp_date, level)
            print(f"  → {pdf.name}: {len(found)} ростовских")
        all_athletes.extend(found)

    save_draft(comp_name, all_athletes)
    return comp_name, all_athletes


def cmd_all(args):
    """fetch + scan + show за один шаг."""
    if len(args) < 4:
        print("Использование: pipeline.py all <yadisk_url> <имя_соревнования> <дата YYYY-MM-DD> <уровень E/K/L>")
        sys.exit(1)
    url, name, date, level = args[0], args[1], args[2], args[3]

    print(f"\n=== ШАग 1: скачиваю протоколы ===")
    comp_dir, _, files = fetch_from_yadisk(url, name)

    print(f"\n=== ШАГ 2: OCR + поиск ростовских ===")
    pdfs = sorted(comp_dir.glob("*.pdf"))
    pdfs = [p for p in pdfs if any(p.name.startswith(pr) for pr in WANTED_PREFIXES)]
    print(f"Файлов для OCR: {len(pdfs)}")

    all_athletes = []
    for pdf in pdfs:
        found = scan_pdf(pdf, name, date, level)
        print(f"  → {pdf.name}: {len(found)} ростовских")
        all_athletes.extend(found)

    draft_path = save_draft(name, all_athletes)
    print(f"\n=== РЕЗУЛЬТАТ ===")
    show_draft(name)
    print(f"\nПроверь черновик, при необходимости отредактируй места:")
    print(f"  {draft_path}")
    print(f"\nЗатем импортируй в БД:")
    print(f"  python3 pipeline.py import \"{name}\"")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd  = sys.argv[1].lower()
    rest = sys.argv[2:]

    if cmd == "fetch":
        cmd_fetch(rest)
    elif cmd == "scan":
        cmd_scan(rest)
    elif cmd == "show":
        show_draft(rest[0] if rest else "")
    elif cmd == "import":
        dry = "--dry" in rest
        name = next((r for r in rest if not r.startswith("--")), "")
        import_to_db(name, dry_run=dry)
    elif cmd == "coaches":
        refresh_coaches_cache()
    elif cmd == "all":
        cmd_all(rest)
    else:
        print(f"Неизвестная команда: {cmd}")
        print(__doc__)
        sys.exit(1)
