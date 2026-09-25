#!/usr/bin/env python3
"""Проверка согласованности Markdown-документа ТЗ (навык embedded-tech-spec).

Проверяет:
  - повторяющиеся номера пунктов;
  - ссылки «п. X.Y.Z» и номера в матрице/таблицах изменений на несуществующие пункты;
  - тест-кейсы: упомянуты, но не определены; определены, но не встречаются в матрице;
  - нормативные пункты, отсутствующие в матрице прослеживаемости;
  - нормативные пункты без маркера происхождения [R]/[N]/[U];
  - ревизию в реквизитах против последней строки истории;
  - блоки «Изменения ревизии X.Y» для всех ревизий, кроме первой, и их порядок;
  - пометки (р.X.Y) ревизий, которых нет в истории.

Код возврата: 0 - нет ошибок (в режиме --strict ещё и нет предупреждений), 1 - иначе.
"""
import argparse
import re
import sys

NUM = r"\d+(?:\.\d+)+"
ITEM_RE = re.compile(rf"^({NUM})\.\s+(.*)$")
HEAD_RE = re.compile(r"^(#{2,4})\s+(?:Приложение\s+)?([A-Z]|\d+(?:\.\d+)*)\.?\s+(.*)$")
TC_ROW_RE = re.compile(r"^\|\s*(TC-\d+)\s*\|")
Q_ROW_RE = re.compile(rf"^\|\s*({NUM})\s*\|")
TC_RANGE_RE = re.compile(r"TC-(\d+)\s*(?:…|\.\.\.|–|-)\s*(?:TC-)?(\d+)")
TC_RE = re.compile(r"TC-(\d+)")
REF_RE = re.compile(r"п\.\s*((?:[\d.]+(?:\([а-я]\))?[\s,–\-…и]*)+)")
RANGE_RE = re.compile(rf"({NUM})\s*(?:–|—|…|\.\.\.|-)\s*({NUM})")
REV_MARK_RE = re.compile(r"\(р\.(\d+\.\d+)\)")
MARKER_RE = re.compile(r"\[(R|N|U)\]")


def strip_code(lines):
    out, fence, comment = [], False, False
    for ln in lines:
        s = ln.strip()
        if s.startswith("```"):
            fence = not fence
            out.append("")
            continue
        if "<!--" in s:
            comment = True
        if fence or comment:
            out.append("")
        else:
            out.append(ln)
        if "-->" in s:
            comment = False
    return out


def expand_nums(text):
    """Все номера пунктов в тексте, включая раскрытые диапазоны с общим префиксом."""
    nums = set(re.findall(NUM, text))
    for a, b in RANGE_RE.findall(text):
        pa, pb = a.split("."), b.split(".")
        if len(pa) == len(pb) and pa[:-1] == pb[:-1]:
            lo, hi = int(pa[-1]), int(pb[-1])
            if 0 <= hi - lo <= 200:
                for k in range(lo, hi + 1):
                    nums.add(".".join(pa[:-1] + [str(k)]))
    return nums


def expand_tcs(text):
    tcs = {int(x) for x in TC_RE.findall(text)}
    for a, b in TC_RANGE_RE.findall(text):
        lo, hi = int(a), int(b)
        if 0 <= hi - lo <= 500:
            tcs.update(range(lo, hi + 1))
    return tcs


def rev_key(r):
    return tuple(int(x) for x in r.split("."))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--strict", action="store_true", help="предупреждения тоже дают код 1")
    a = ap.parse_args()

    raw = open(a.file, encoding="utf-8").read().splitlines()
    lines = strip_code(raw)
    errors, warnings = [], []

    # --- структура: разделы, пункты, заголовки
    section = None          # номер раздела верхнего уровня (строка) или буква приложения
    section_title = {}
    items = {}              # номер -> (строка, текст, раздел)
    headings = set()
    questions = set()
    tc_defs = {}
    matrix_text, changes_text = [], []
    in_matrix = in_changes = in_history = in_questions = False
    history = []
    header_rev = None
    change_blocks = []

    last_item = None
    for i, ln in enumerate(lines, 1):
        mg = re.match(r"^(#{2,4})\s+(.*)$", ln)
        if mg:
            last_item = None
            level, title = mg.groups()
            m = HEAD_RE.match(ln)
            if m:
                headings.add(m.group(2))
            if level == "##":
                section = m.group(2) if m else None
                if section:
                    section_title[section] = m.group(3)
                in_matrix = "Матрица прослеживаемости" in title
                in_questions = "Допущения" in title
            in_history = level == "###" and "История ревизий" in title
            mc = re.match(r"Изменения ревизии (\d+\.\d+)", title) if level == "###" else None
            in_changes = bool(mc)
            if mc:
                change_blocks.append(mc.group(1))
            continue

        mr = re.match(r"^\|\s*\*\*Ревизия\*\*\s*\|\s*(\d+\.\d+)", ln)
        if mr:
            header_rev = mr.group(1)
        if in_history:
            mh = re.match(r"^\|\s*(\d+\.\d+)\s*\|", ln)
            if mh:
                history.append((mh.group(1), i))
        if in_matrix:
            matrix_text.append(ln)
        if in_changes and ln.startswith("|"):
            first = ln.split("|")[1] if ln.count("|") > 1 else ""
            changes_text.append((i, first))

        mt = TC_ROW_RE.match(ln)
        if mt and not in_matrix:
            n = int(mt.group(1)[3:])
            if n in tc_defs:
                errors.append(f"стр. {i}: тест-кейс {mt.group(1)} определён повторно (впервые стр. {tc_defs[n]})")
            tc_defs[n] = i
        mq = Q_ROW_RE.match(ln)
        if mq and in_questions:
            questions.add(mq.group(1))

        mi = ITEM_RE.match(ln)
        if mi and section is not None:
            num, text = mi.groups()
            if not num.startswith(section + ".") and num != section:
                # номер из чужого раздела: вероятно, опечатка или нумерованная ссылка
                warnings.append(f"стр. {i}: пункт {num} находится в разделе {section}")
            if num in items:
                errors.append(f"стр. {i}: номер пункта {num} повторяется (впервые стр. {items[num][0]})")
                last_item = None
            else:
                items[num] = (i, text, section)
                last_item = num
        elif last_item and ln.strip() and not ln.startswith("---"):
            # продолжение пункта (таблица, маркер на отдельной строке)
            li, lt, ls = items[last_item]
            items[last_item] = (li, lt + "\n" + ln, ls)

    defined = set(items) | headings | questions

    # --- нормативные разделы
    full = "\n".join(lines)
    norm = None
    mn = re.search(r"разделы\s+(\d+)\s*[–-]\s*(\d+)\s*—\s*нормативные", full)
    if mn:
        norm = set(str(k) for k in range(int(mn.group(1)), int(mn.group(2)) + 1))
    else:
        warnings.append("не найдено указание «разделы X–Y — нормативные» (п. 1.6); нормативными считаются все разделы, кроме 1, 2, матрицы и допущений")
        norm = {s for s, t in section_title.items() if s.isdigit() and s not in ("1", "2")
                and "Матрица" not in t and "Допущения" not in t and "Верификация" not in t}
    normative = {n: v for n, v in items.items() if v[2] in norm}

    # --- ссылки п. X.Y.Z
    for i, ln in enumerate(lines, 1):
        for grp in REF_RE.findall(ln):
            for n in set(re.findall(NUM, grp)):
                if n not in defined:
                    errors.append(f"стр. {i}: ссылка «п. {n}» на несуществующий пункт")

    # --- номера в матрице и таблицах изменений
    matrix_cover = set()
    for ln in matrix_text:
        if not ln.startswith("|"):
            continue
        cells = ln.split("|")
        if len(cells) < 3:
            continue
        first = cells[1]
        for n in re.findall(NUM, first):
            if n not in defined:
                errors.append(f"матрица: пункт {n} не существует")
        matrix_cover |= expand_nums(first)
    for i, first in changes_text:
        for n in re.findall(NUM, first):
            if n not in defined:
                warnings.append(f"стр. {i}: в таблице изменений указан несуществующий пункт {n}")

    for n, (i, text, _) in sorted(normative.items(), key=lambda kv: kv[1][0]):
        excluded = "исключено" in text
        if not excluded and n not in matrix_cover:
            # покрытие подразделом целиком (например, «5.6» в матрице)
            parent = ".".join(n.split(".")[:-1])
            if parent not in matrix_cover:
                warnings.append(f"стр. {i}: пункт {n} не указан в матрице прослеживаемости")
        if not excluded and not MARKER_RE.search(text):
            warnings.append(f"стр. {i}: пункт {n} без маркера происхождения [R]/[N]/[U]")

    # --- тест-кейсы
    mentioned = set()
    for ln in lines:
        if TC_ROW_RE.match(ln):
            ln = "|".join(ln.split("|")[2:])
        mentioned |= expand_tcs(ln)
    for n in sorted(mentioned - set(tc_defs)):
        errors.append(f"тест-кейс TC-{n:02d} упоминается, но не определён")
    matrix_tcs = expand_tcs("\n".join(matrix_text))
    for n in sorted(set(tc_defs) - matrix_tcs):
        warnings.append(f"стр. {tc_defs[n]}: тест-кейс TC-{n:02d} не упоминается в матрице")
    if tc_defs:
        missing = sorted(set(range(1, max(tc_defs) + 1)) - set(tc_defs))
        if missing:
            warnings.append("пропуски в нумерации тест-кейсов: " + ", ".join(f"TC-{n:02d}" for n in missing))

    # --- ревизии
    revs = [r for r, _ in history]
    if not revs:
        errors.append("не найдена таблица «История ревизий»")
    else:
        if revs != sorted(revs, key=rev_key):
            warnings.append("строки истории ревизий не по возрастанию")
        if len(set(revs)) != len(revs):
            errors.append("в истории ревизий повторяется номер ревизии")
        if header_rev is None:
            errors.append("в реквизитах не найдена строка «Ревизия»")
        elif header_rev != max(revs, key=rev_key):
            errors.append(f"ревизия в реквизитах {header_rev} не совпадает с последней в истории {max(revs, key=rev_key)}")
        first = min(revs, key=rev_key)
        for r in revs:
            if r != first and r not in change_blocks:
                warnings.append(f"нет блока «Изменения ревизии {r}»")
        for r in change_blocks:
            if r not in revs:
                errors.append(f"блок «Изменения ревизии {r}» есть, а в истории такой ревизии нет")
        if change_blocks != sorted(change_blocks, key=rev_key, reverse=True):
            warnings.append("блоки «Изменения ревизии» должны идти от новейшей к старейшей")
        marks = set(REV_MARK_RE.findall(full))
        for r in sorted(marks - set(revs), key=rev_key):
            errors.append(f"пометка (р.{r}) относится к ревизии, которой нет в истории")
        for r in revs:
            if r != first and r not in marks:
                warnings.append(f"в тексте нет ни одной пометки (р.{r})")

    # --- отчёт
    for w in warnings:
        print("ПРЕДУПРЕЖДЕНИЕ:", w)
    for e in errors:
        print("ОШИБКА:", e)
    print(f"Пунктов: {len(items)} (нормативных {len(normative)}); тест-кейсов: {len(tc_defs)}; "
          f"вопросов: {len(questions)}; ревизий: {len(revs)}. "
          f"Ошибок: {len(errors)}; предупреждений: {len(warnings)}.")
    return 1 if errors or (a.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
