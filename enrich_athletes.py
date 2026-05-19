#!/usr/bin/env python3
"""
Enrich athletes DB from "Сборная РО 2026" DOC files.
Updates: gender, birth_date (normalize), grade, coach, city, in_team.
"""
import re
import sqlite3
import subprocess
import difflib

DB_PATH = "/Users/vladimirkopylov/Library/Application Support/federation-analytics/federation.db"
DOC1 = "/Users/vladimirkopylov/Desktop/1 Сборная РО 2026/списки сборной СДЕЛАЛ.doc"
DOC2 = "/Users/vladimirkopylov/Desktop/1 Сборная РО 2026/Дополнения в списки сборной ФВКР РО по ВК 2026\xa0— копия.doc"

DATE_RE = re.compile(r'^\d{1,2}\.\d{2}\.\d{4}$')
ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
GRADE_RE = re.compile(r'^(МСМК|МС|КМС|\d\s*[юЮ][нН]\.?|\d\s*сп\.?|\d\s*спорт\.?|\d)$', re.I)
SECTION_RE = re.compile(r'(мужчин|женщин|юниор|юноши|девушк|12.13|14.15|16.17|18.20|тренер|специалист)', re.I)

PATRONYMIC_M = ('ович', 'евич')
PATRONYMIC_F = ('овна', 'евна', 'ична', 'инична')
MALE_NAMES = {'Никита', 'Илья', 'Кузьма', 'Лука', 'Фома', 'Савва', 'Миша'}
FEMALE_NAMES = {'Мария', 'Анна', 'Варвара', 'Виктория', 'Вера', 'Ксения', 'Дарья',
                'Наталья', 'Полина', 'Арина', 'Алена', 'Кира', 'Алина', 'Мелина',
                'Оксана', 'Светлана', 'Валентина', 'Екатерина', 'Александра', 'Анастасия',
                'Мирослава', 'Малена', 'Вероника', 'Альбина', 'Патимат', 'Регина',
                'Мария', 'Лидия', 'Татьяна', 'Олеся', 'Эллада', 'Эсма', 'Ариана',
                'Ариадна', 'Виолетта', 'Маргарита', 'Дина', 'Кристина', 'Марина',
                'Людмила', 'Нина', 'Ольга', 'Надежда', 'Галина', 'Тамара', 'Зоя',
                'Вика', 'Даша', 'Катя', 'Соня', 'Саша', 'Таня', 'Аня', 'Юля',
                'Карина', 'Ирина', 'Юлия', 'Елена', 'Нелли', 'Белла'}

def detect_gender(full_name: str) -> str:
    parts = full_name.split()
    if len(parts) >= 3:
        pat = parts[2]
        if any(pat.endswith(s) for s in PATRONYMIC_M):
            return 'М'
        if any(pat.endswith(s) for s in PATRONYMIC_F):
            return 'Ж'
    if len(parts) >= 2:
        fn = parts[1]
        if fn in MALE_NAMES:
            return 'М'
        if fn in FEMALE_NAMES:
            return 'Ж'
        if fn.endswith(('а', 'я')) and fn not in MALE_NAMES:
            return 'Ж'
        return 'М'
    return ''

def normalize_date(d: str) -> str:
    """Convert YYYY-MM-DD → DD.MM.YYYY for consistency, keep DD.MM.YYYY as-is."""
    if ISO_DATE_RE.match(d.strip()):
        parts = d.strip().split('-')
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    if DATE_RE.match(d.strip()):
        return d.strip()
    return d.strip()

def is_name_line(line: str) -> bool:
    """True if line looks like a Russian full name (2–4 words, each capitalized)."""
    parts = line.strip().split()
    if not (2 <= len(parts) <= 4):
        return False
    for p in parts:
        if not p:
            continue
        if not re.match(r'^[А-ЯЁ][А-ЯЁа-яё\-]+$', p):
            return False
    return True

def read_doc(path: str) -> str:
    r = subprocess.run(['textutil', '-convert', 'txt', path, '-stdout'],
                       capture_output=True, text=True)
    return r.stdout

def parse_athletes(text: str) -> list:
    """Parse athlete records from converted DOC text."""
    athletes = []
    lines = [l.rstrip() for l in text.splitlines()]

    i = 0
    current_section = ''
    in_athlete_section = False

    while i < len(lines):
        line = lines[i].strip()

        # Detect section headers
        if SECTION_RE.search(line) and len(line) < 80:
            low = line.lower()
            if 'тренер' in low or 'специалист' in low:
                in_athlete_section = False
                current_section = 'coaches'
            else:
                in_athlete_section = True
                current_section = line
            i += 1
            continue

        # Skip table headers and empty lines
        if not line or line in ('№', 'п/п', 'Фамилия', 'Вид программы', 'Спортивная организация',
                                'Муниципальное образование', 'Тренер', 'Высший результат',
                                'имя, отчество', 'имя отчество', 'отчество',
                                'Дата', 'рождения', 'Спортивное', 'звание', 'Должность в команде',
                                'Спортивная дисциплина', 'Основное место работы',
                                'УТВЕРЖДАЮ', 'СОГЛАСОВАНО', 'СПИСОК', 'ДОПОЛНЕНИЯ В СПИСОК'):
            i += 1
            continue

        # Skip footer lines
        if 'Руководитель' in line or 'Главный тренер' in line or 'Директор' in line:
            i += 1
            continue

        # Skip if row number only
        if re.match(r'^\d+$', line) and int(line) < 100:
            i += 1
            continue

        if not in_athlete_section:
            i += 1
            continue

        # Try to detect a name line
        if is_name_line(line):
            record = {
                'full_name': line,
                'birth_date': '',
                'grade': '',
                'discipline': '',
                'org': '',
                'city': '',
                'coach': '',
                'section': current_section,
                'gender': detect_gender(line),
            }
            i += 1
            # Collect following lines as fields until next name or section
            fields_collected = []
            while i < len(lines):
                next_line = lines[i].strip()
                if not next_line:
                    i += 1
                    continue
                # Stop on next name, section header, or numeric row number
                if is_name_line(next_line) and len(next_line.split()) >= 2:
                    break
                if SECTION_RE.search(next_line) and len(next_line) < 80:
                    break
                if re.match(r'^\d+$', next_line) and int(next_line) < 100:
                    # Row number, skip but stop collecting for this athlete
                    i += 1
                    break
                fields_collected.append(next_line)
                i += 1

            # Parse fields_collected into record fields
            for field in fields_collected:
                if not record['birth_date'] and DATE_RE.match(field):
                    record['birth_date'] = field
                elif not record['grade'] and GRADE_RE.match(field.strip()):
                    record['grade'] = field.strip()
                elif not record['discipline'] and re.match(r'^(ОК|СЗ|СЭ)', field):
                    record['discipline'] = field
                elif not record['org'] and 'ФВКР' in field or 'МБУ' in field or 'МАУ' in field:
                    record['org'] = field
                elif not record['city'] and re.match(r'^[А-ЯЁ][а-яё]', field) and len(field) < 40:
                    # Could be city or coach - coach usually has initials
                    if re.search(r'[А-ЯЁ]\.[А-ЯЁ]\.', field):
                        if not record['coach']:
                            record['coach'] = field
                    elif not record['city']:
                        record['city'] = field

            athletes.append(record)
        else:
            i += 1

    return athletes

def fuzzy_match(name: str, candidates: list, threshold: float = 0.82) -> str:
    """Find best fuzzy match for name in candidates list."""
    # Try exact match first
    name_lower = name.lower()
    for c in candidates:
        if c.lower() == name_lower:
            return c
    # Try prefix match (surname + first name)
    name_parts = name.split()
    if len(name_parts) >= 2:
        prefix = ' '.join(name_parts[:2]).lower()
        for c in candidates:
            c_parts = c.split()
            if len(c_parts) >= 2:
                c_prefix = ' '.join(c_parts[:2]).lower()
                if c_prefix == prefix:
                    return c
    # Fuzzy match
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=threshold)
    return matches[0] if matches else None

def main():
    print("Читаем DOC файлы...")
    doc1_text = read_doc(DOC1)
    doc2_text = read_doc(DOC2)

    print("Парсим спортсменов из DOC...")
    athletes_doc = parse_athletes(doc1_text) + parse_athletes(doc2_text)

    # Deduplicate by name (keep last occurrence which may have more data)
    doc_by_name = {}
    for a in athletes_doc:
        name = a['full_name']
        if name not in doc_by_name or (a['birth_date'] and not doc_by_name[name]['birth_date']):
            doc_by_name[name] = a

    print(f"Найдено {len(doc_by_name)} спортсменов в DOC файлах")

    # Print sample
    for name, a in list(doc_by_name.items())[:5]:
        print(f"  {name}: дата={a['birth_date']}, разряд={a['grade']}, "
              f"тренер={a['coach']}, город={a['city']}, пол={a['gender']}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get all DB athletes
    cur.execute("SELECT id, full_name, birth_date, gender, grade, coach, city, in_team FROM athletes")
    db_athletes = cur.fetchall()
    db_names = [a['full_name'] for a in db_athletes]
    db_by_name = {a['full_name']: dict(a) for a in db_athletes}

    updated = 0
    not_found = []
    doc_names_list = list(doc_by_name.keys())

    for doc_name, doc_data in doc_by_name.items():
        matched_name = fuzzy_match(doc_name, db_names)
        if not matched_name:
            not_found.append(doc_name)
            continue

        db_rec = db_by_name[matched_name]
        updates = {}

        # Gender: update if empty
        if (not db_rec['gender']) and doc_data['gender']:
            updates['gender'] = doc_data['gender']

        # Birth date: update if empty, or normalize if ISO format
        if not db_rec['birth_date'] and doc_data['birth_date']:
            updates['birth_date'] = doc_data['birth_date']
        elif db_rec['birth_date'] and ISO_DATE_RE.match(db_rec['birth_date']):
            updates['birth_date'] = normalize_date(db_rec['birth_date'])

        # Grade: update only if empty
        if (not db_rec['grade']) and doc_data['grade']:
            updates['grade'] = doc_data['grade']

        # Coach: update if empty
        if (not db_rec['coach']) and doc_data['coach']:
            updates['coach'] = doc_data['coach']

        # City: update if empty
        if (not db_rec['city']) and doc_data['city']:
            updates['city'] = doc_data['city']

        # Mark as in_team if found in official list
        if not db_rec['in_team']:
            updates['in_team'] = 1

        if updates:
            set_clause = ', '.join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [db_rec['id']]
            cur.execute(f"UPDATE athletes SET {set_clause} WHERE id = ?", values)
            updated += 1
            if doc_name != matched_name:
                print(f"  ОБНОВЛЕНО (fuzzy '{doc_name}' → '{matched_name}'): {updates}")
            else:
                print(f"  ОБНОВЛЕНО '{matched_name}': {updates}")

    conn.commit()
    conn.close()

    print(f"\n=== ИТОГО ===")
    print(f"Обновлено записей: {updated}")
    print(f"Не найдено в базе: {len(not_found)}")
    if not_found:
        print("Не найденные:")
        for name in not_found:
            print(f"  - {name}")

if __name__ == '__main__':
    main()
