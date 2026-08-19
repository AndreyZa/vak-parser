# -*- coding: utf-8 -*-
"""Перечень ВАК (PDF на ~1280 страниц) -> издания по заданной специальности.

Перечень рецензируемых научных изданий публикуется одним PDF без машинного
интерфейса, а вопрос к нему всегда один: «куда можно подать статью по
специальности X ПРЯМО СЕЙЧАС». Руками это — пролистать всю пятиколоночную
таблицу и не ошибиться в сроках.

Как устроено внутри — по разделам ниже. Коротко о неочевидном:

* плоский extract_text() для этой таблицы не годится: колонки перемешиваются.
  Разбираем по координатам слов, границы — из фактических краёв ячеек;

* у издания бывает НЕСКОЛЬКО блоков специальностей с разными сроками
  («17.00.04 ... с 28.12.2018 по 16.10.2022» и рядом «2.1.1 ... с 01.02.2022»),
  причём строка с датой выровнена по ПЕРВОЙ строке своего блока. Специальность
  засчитывается только если управляющий ею блок не закрыт — иначе в выдачу
  попадут издания, из перечня по этой специальности давно исключённые;

* «действует» считается на сегодня, а не на зашитую в код дату (--as-of
  фиксирует день оценки, если нужен воспроизводимый прогон);

* редакция перечня берётся с сайта, а не по зашитой ссылке: зашитая молча
  устаревает, и выдача начинает описывать прошлое, ничем этого не показывая.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import pdfplumber

# --------------------------------------------------------------------------
# Источники
# --------------------------------------------------------------------------
#
# Первоисточник — сайт ВАК. Сайт на React, HTML пустой, данные отдаёт его же
# API: рубрика «Рецензируемые издания» — это type=19 в общей ленте документов.
# Нужная запись — «Перечень рецензируемых научных изданий…»; ссылка на PDF
# лежит в её поле info (редакторский rich-text в JSON), а не в files: в files
# висят все прошлые редакции файлами с machine-именами, без дат.
OFFICIAL_HOST = "https://vak.gisnauka.ru"
OFFICIAL_API = OFFICIAL_HOST + "/api/news/news-list?type=19&page_size=100"
OFFICIAL_PAGE = OFFICIAL_HOST + "/documents/editions"
OFFICIAL_TITLE = "Перечень рецензируемых научных изданий"

# Запасной источник: страница-указатель ЦНПА МГТУ им. Баумана. Держит ссылки на
# все редакции и прозой называет актуальную. Нужен потому, что сайт ВАК
# открывается не из всех сетей (за пределами РФ обычно не открывается вовсе), а
# ещё он ощутимо отстаёт: зеркало — НЕ первоисточник и может отдать старую
# редакцию, поэтому переход на него объявляется громко и пишется в выдачу.
MIRROR_PAGE = "https://cpr.bmstu.ru/manual/perechen-zhurnalov-vak"

UA = "vak-parser (+https://github.com/AndreyZa/vak-parser)"

# Каталоги — относительно текущего каталога, а не установки: инструмент могут
# поставить через pipx, и писать в site-packages он не должен.
CACHE = Path("cache")
OUT = Path("out")

# Границы по фактическим x0: номер 40,0 | название 76,8 | ISSN 243,7 |
# специальности 306,2 | дата 515,5. Рубеж названия ОБЯЗАН быть ниже 76,8 —
# при 77 номер и название попадали в одну ячейку.
COLS = [(35, 70, "num"), (70, 240, "name"), (240, 300, "issn"),
        (300, 505, "spec"), (505, 600, "date")]
# Межстрочный шаг ~12,6 pt: округление к 6 разводит соседние строки и терпит
# дрожание базовой линии в пределах ±3 pt.
Y_TOL = 6.0

RE_NUM = re.compile(r"^(\d{1,5})\.$")
RE_FROM = re.compile(r"с\s+(\d{2}\.\d{2}\.\d{4})")
RE_TO = re.compile(r"по\s+(\d{2}\.\d{2}\.\d{4})")
RE_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
RE_CODE = re.compile(r"\b\d\.\d\.\d+\b")
# Дата в имени файла: кэш пишет vak-29-06-2026.pdf, а руками качают
# «... 29.06.2026.pdf» — --pdf должен узнавать редакцию в обоих видах.
RE_FILE_DATE = re.compile(r"(\d{2})[.-](\d{2})[.-](\d{4})")

EX_OK = 0          # выдача построена
EX_FAIL = 1        # источник недоступен / PDF не разобрался
EX_USAGE = 2       # неверный вызов (как у argparse)
EX_EMPTY = 3       # отработали штатно, но ни одного издания не нашли


class SourceError(RuntimeError):
    """Источник недоступен или отдал не то, что ожидалось."""


def _d(s: str) -> date:
    dd, mm, yy = s.split(".")
    return date(int(yy), int(mm), int(dd))


def fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------
# Поиск актуальной редакции: официальный сайт
# --------------------------------------------------------------------------

def _plain(node) -> str:
    """Текст из узла rich-text (строки, вложенные children)."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_plain(n) for n in node)
    if isinstance(node, dict):
        return _plain(node.get("children", []))
    return ""


def _links(node, acc: list | None = None) -> list[tuple[str, str]]:
    """[(href, текст)] из rich-text JSON, в порядке следования в документе."""
    acc = [] if acc is None else acc
    if isinstance(node, list):
        for n in node:
            _links(n, acc)
    elif isinstance(node, dict):
        if node.get("type") == "a":
            href = (node.get("attributes") or {}).get("href") or ""
            acc.append((href, re.sub(r"\s+", " ", _plain(node)).strip()))
        _links(node.get("children", []), acc)
    return acc


def official_from_json(payload: dict) -> dict:
    """Разобрать ответ API сайта ВАК -> описание актуальной редакции.

    Дата берётся из ТЕКСТА ссылки («…(по состоянию на 29.06.2026)»), а имя
    файла игнорируется: имена на сайте — машинные (UUID). Заголовок записи
    называет ту же дату независимо от ссылки — расхождение между ними значит,
    что выложили файл, не поправив заголовок (или наоборот), и смотреть надо
    глазами.
    """
    for rec in payload.get("results", []):
        name = re.sub(r"\s+", " ", rec.get("name") or "").strip()
        if not name.startswith(OFFICIAL_TITLE):
            continue
        try:
            info = json.loads(rec.get("info") or "[]")
        except json.JSONDecodeError:
            continue
        pdfs = [(h, t) for h, t in _links(info) if h.lower().endswith(".pdf")]
        if not pdfs:
            continue
        href, text = pdfs[0]
        d = RE_DATE.search(text) or RE_DATE.search(name)
        if not d:
            continue
        stated = RE_DATE.search(name)
        return {"kind": "official", "site": OFFICIAL_HOST,
                "index": OFFICIAL_PAGE, "url": href, "date": d.group(0),
                "stated": stated.group(0) if stated else None,
                "title": text or name, "all": []}
    raise SourceError(
        "в ленте документов сайта ВАК не нашлась запись «" + OFFICIAL_TITLE +
        "…» со ссылкой на PDF — рубрику переверстали, поправить official_from_json()")


# Сайт ВАК из-за пределов РФ обычно не отвечает вовсе — не коннект-отказ, а
# молчание. Ждать полную минуту, чтобы потом всё равно уйти на зеркало, незачем.
OFFICIAL_TIMEOUT = 20


def discover_official(timeout: int = OFFICIAL_TIMEOUT) -> dict:
    try:
        raw = fetch(OFFICIAL_API, timeout=timeout)
    except (urllib.error.URLError, OSError) as e:
        raise SourceError(f"{OFFICIAL_HOST} не отвечает: {e}") from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SourceError(f"{OFFICIAL_HOST} ответил не JSON: {e}") from e
    return official_from_json(payload)


# --------------------------------------------------------------------------
# Поиск актуальной редакции: зеркало
# --------------------------------------------------------------------------

def mirror_from_html(html: str, index_url: str = MIRROR_PAGE) -> dict:
    """Разобрать страницу-указатель зеркала -> актуальная редакция и история.

    Дату берём из ТЕКСТА ссылки («Перечень журналов ВАК на 15.06.2026»), а не
    из имени файла: имена там живут своей жизнью (у одной из редакций в имени
    опечатка «журналок»). Текст ссылки ведёт редакция руками, формат один.
    """
    items = []
    for m in re.finditer(r'href="([^"#]+\.pdf)"[^>]*>(.*?)</a>', html, re.S | re.I):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if "перечень" not in text.lower():
            continue
        href = urllib.parse.unquote(m.group(1))
        d = RE_DATE.search(text) or RE_DATE.search(href)
        if not d:
            continue
        items.append({"url": urllib.parse.urljoin(index_url, m.group(1)),
                      "date": d.group(0), "text": text})
    if not items:
        raise SourceError(f"на {index_url} не найдено ни одной ссылки на перечень "
                          "— страница переверстана, поправить mirror_from_html()")
    items.sort(key=lambda i: _d(i["date"]), reverse=True)
    newest = items[0]

    # Проза страницы («актуальный перечень ... по состоянию на 15.06.2026») —
    # независимая проверка того, что самая свежая ссылка и есть текущая
    # редакция. Расхождение значит, что читать надо глазами, а не брать max.
    m = re.search(r"по\s+состоянию\s+на\s+(\d{2}\.\d{2}\.\d{4})",
                  re.sub(r"<[^>]+>", " ", html))
    return {"kind": "mirror", "site": urllib.parse.urlsplit(index_url).netloc,
            "index": index_url, "url": newest["url"], "date": newest["date"],
            "stated": m.group(1) if m else None, "title": newest["text"],
            "all": [{"date": i["date"], "url": i["url"]} for i in items]}


def discover_mirror(index_url: str = MIRROR_PAGE, timeout: int = 60) -> dict:
    try:
        html = fetch(index_url, timeout=timeout).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise SourceError(f"{index_url} не отвечает: {e}") from e
    return mirror_from_html(html, index_url)


# --------------------------------------------------------------------------
# Выбор источника
# --------------------------------------------------------------------------

RE_CACHE_NAME = re.compile(r"^vak-(\d{2})-(\d{2})-(\d{4})\.pdf$")


def cached_editions(cache: Path) -> list[dict]:
    """Редакции, уже лежащие в кэше (по именам файлов), свежие первыми."""
    if not cache.is_dir():
        return []
    items = [{"date": ".".join(m.groups()), "path": p}
             for p in cache.iterdir()
             if (m := RE_CACHE_NAME.match(p.name))]
    items.sort(key=lambda i: _d(i["date"]), reverse=True)
    return items


def _loud(lines: list[str], log) -> None:
    # Громко: пользователь обязан знать, что читает не первоисточник.
    print("\n" + "!" * 72, file=log)
    for line in lines:
        print("! " + line, file=log)
    print("!" * 72 + "\n", file=log)


def discover(prefer: str = "auto", mirror_url: str = MIRROR_PAGE,
             cache: Path | None = None, timeout: int = 60,
             log=sys.stderr) -> dict:
    """Актуальная редакция: официальный сайт, при отказе — зеркало или кэш.

    Когда официальный сайт молчит, зеркало и уже скачанный кэш соревнуются
    ДАТОЙ РЕДАКЦИИ. Скачанная ранее официальная редакция обычно свежее
    зеркальной (зеркало отстаёт) — уходить с неё на зеркало значило бы
    отвечать по более старому перечню, чем уже лежит на диске.
    """
    if prefer == "mirror":
        return discover_mirror(mirror_url, timeout)
    try:
        # Короткий таймаут — только в авторежиме, где при молчании впереди
        # ждёт зеркало; явный prefer="official" значит «жди официальный».
        return discover_official(timeout if prefer == "official"
                                 else min(timeout, OFFICIAL_TIMEOUT))
    except SourceError as e:
        if prefer == "official":
            raise
        why = e

    have = cached_editions(cache) if cache is not None else []
    try:
        mirror = discover_mirror(mirror_url, timeout)
    except SourceError as e:
        mirror, mirror_why = None, e

    if mirror and (not have or _d(mirror["date"]) > _d(have[0]["date"])):
        _loud([f"ОФИЦИАЛЬНЫЙ ИСТОЧНИК НЕДОСТУПЕН: {why}",
               f"Беру ЗЕРКАЛО {mirror['site']} — это НЕ первоисточник.",
               f"Редакция на зеркале ({mirror['date']}) может отставать "
               "от официальной.",
               f"Проверить: {OFFICIAL_PAGE}"], log)
        return mirror
    if have:
        newest = have[0]
        _loud([f"ОФИЦИАЛЬНЫЙ ИСТОЧНИК НЕДОСТУПЕН: {why}",
               (f"Зеркало не свежее (там {mirror['date']})." if mirror
                else f"Зеркало тоже недоступно: {mirror_why}"),
               f"Беру ранее скачанную редакцию {newest['date']} из кэша.",
               "К этому моменту могла выйти более новая редакция.",
               f"Проверить: {OFFICIAL_PAGE}"], log)
        return {"kind": "cache", "site": None, "index": None,
                "url": newest["path"].resolve().as_uri(),
                "date": newest["date"], "stated": None,
                "title": newest["path"].name,
                "all": [{"date": i["date"], "url": i["path"].resolve().as_uri()}
                        for i in have],
                "path": newest["path"]}
    raise SourceError(f"официальный сайт недоступен ({why}), "
                      f"зеркало тоже ({mirror_why}), в кэше пусто")


def check_stated(src: dict, log=sys.stderr) -> None:
    """Сверка даты файла с датой, которую источник называет прозой."""
    if src["stated"] and src["stated"] != src["date"]:
        print(f"ВНИМАНИЕ: {src['site']} называет актуальной редакцию "
              f"{src['stated']}, а ссылка ведёт на {src['date']}. Беру "
              f"{src['date']}; проверить глазами: {src['index']}", file=log)


def ensure_pdf(src: dict, cache: Path = CACHE, refresh: bool = False,
               log=sys.stderr) -> Path:
    """Скачать редакцию в кэш (имя — по дате) и вернуть путь."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"vak-{src['date'].replace('.', '-')}.pdf"
    if path.exists() and not refresh:
        return path
    print(f"качаю перечень на {src['date']} -> {path}", file=log)
    try:
        path.write_bytes(fetch(src["url"]))
    except (urllib.error.URLError, OSError) as e:
        path.unlink(missing_ok=True)
        raise SourceError(f"не скачался {src['url']}: {e}") from e
    return path


# --------------------------------------------------------------------------
# Разбор PDF
# --------------------------------------------------------------------------

def col_of(x0: float) -> str | None:
    for lo, hi, name in COLS:
        if lo <= x0 < hi:
            return name
    return None


def page_lines(page):
    """[(top, {колонка: текст})] — строки страницы с разнесёнными колонками."""
    buckets: dict[int, dict[str, list]] = {}
    for w in page.extract_words():
        c = col_of(w["x0"])
        if not c:
            continue
        key = round(w["top"] / Y_TOL)
        buckets.setdefault(key, {}).setdefault(c, []).append((w["x0"], w["text"]))
    out = []
    for key in sorted(buckets):
        cells = {c: " ".join(t for _, t in sorted(ws))
                 for c, ws in buckets[key].items()}
        out.append((key * Y_TOL, cells))
    return out


def parse(path: Path, progress_every: int = 100, log=sys.stderr) -> list[dict]:
    records, cur = [], None
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, 1):
            if progress_every and pno % progress_every == 0:
                print(f"  ...{pno}/{total}", file=log, flush=True)
            # Каждая страница читается ровно один раз, а pdfplumber держит её
            # разобранную раскладку в кэше до конца работы — на ~1300 страницах
            # это гигабайты. Отпускаем страницу сразу (см. close() ниже).
            for top, cells in page_lines(page):
                m = RE_NUM.match(cells.get("num", "").strip())
                if m:
                    if cur:
                        records.append(cur)
                    cur = {"num": int(m.group(1)), "page": pno,
                           "name": [], "issn": [], "spec": [], "date": []}
                if cur is None:
                    continue
                for c in ("name", "issn"):
                    if cells.get(c):
                        cur[c].append(cells[c])
                # Сквозная координата: страница входит в ключ, иначе блок с
                # предыдущей страницы «управлял» бы строками следующей.
                for c in ("spec", "date"):
                    if cells.get(c):
                        cur[c].append((top + pno * 10000, cells[c]))
            page.close()
    if cur:
        records.append(cur)
    return records


def date_blocks(date_items) -> list[list]:
    """[(y_начала, дата_с, дата_по|None)] — блоки сроков в порядке следования."""
    blocks = []
    for y, txt in date_items:
        m_from, m_to = RE_FROM.search(txt), RE_TO.search(txt)
        if m_from:
            blocks.append([y, m_from.group(1), None])
        if m_to and blocks:
            blocks[-1][2] = m_to.group(1)
    return blocks


def specialties(rec: dict, code: str, as_of: date):
    """Вхождения кода: [(текст_строки, дата_с, дата_по, действует_ли)]."""
    blocks = date_blocks(rec["date"])
    hits = []
    for y, txt in rec["spec"]:
        if code not in txt:
            continue
        gov = None
        for b in blocks:
            if b[0] <= y + Y_TOL * 2:
                gov = b
        frm = gov[1] if gov else None
        to = gov[2] if gov else None
        active = (to is None) or (_d(to) > as_of)
        hits.append((txt, frm, to, active))
    return hits


def select(recs: list[dict], code: str, as_of: date) -> list[dict]:
    found = []
    for r in recs:
        hits = specialties(r, code, as_of)
        if not any(h[3] for h in hits):
            continue
        found.append({
            "num": r["num"], "page": r["page"],
            "name": re.sub(r"\s+", " ", " ".join(r["name"])).strip(),
            "issn": " ".join(r["issn"]).strip(),
            "codes": sorted({m for _, l in r["spec"] for m in RE_CODE.findall(l)}),
            "hits": [{"line": h[0], "from": h[1], "to": h[2]} for h in hits],
        })
    return found


# --------------------------------------------------------------------------
# Выдача
# --------------------------------------------------------------------------

def source_note(src: dict) -> str:
    if src["kind"] == "official":
        return f"официальный сайт ВАК ({src['site']})"
    if src["kind"] == "mirror":
        return (f"ЗЕРКАЛО {src['site']} — не первоисточник, "
                "официальный сайт был недоступен")
    if src["kind"] == "cache":
        return ("ранее скачанная редакция из кэша — источники были недоступны "
                "или не свежее её")
    return src.get("site") or "локальный файл"


def reusable_counts(out_dir: Path, codes: list[str], digest: str,
                    as_of: date) -> dict | None:
    """Счёт по кодам, если выдача для ВСЕХ кодов уже построена из этого же
    PDF (sha256) на эту же дату оценки; иначе None — нужен пересчёт.

    Дата оценки входит в ключ не для красоты: у специальностей истекают
    сроки, и вчерашняя выдача сегодня может быть уже неверной."""
    counts = {}
    for code in codes:
        try:
            d = json.loads((out_dir / f"vak-{code}.json")
                           .read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (d.get("sha256") != digest or d.get("as_of") != as_of.isoformat()
                or not isinstance(d.get("found"), list)
                or not (out_dir / f"vak-{code}.md").is_file()):
            return None
        counts[code] = len(d["found"])
    return counts


def emit_md(code: str, total: int, found: list[dict], src: dict,
            as_of: date, path: Path) -> None:
    lines = [f"# Издания перечня ВАК по специальности {code}", "",
             f"Сгенерировано `vak_parser.py` из перечня на **{src['date']}** "
             f"(оценка действия — на {as_of.strftime('%d.%m.%Y')}). "
             "Руками не править — правка не переживёт следующий прогон.", "",
             f"Источник: {source_note(src)}.", "",
             f"Записей в перечне: {total}. "
             f"Изданий с действующей {code}: {len(found)}.", "",
             "| № в перечне | Издание | ISSN | Смежные специальности |",
             "|---|---|---|---|"]
    for f in found:
        name = re.split(r"\(перевод|\(до |\(До ", f["name"])[0].strip()
        other = ", ".join(c for c in f["codes"] if c != code)
        lines.append(f'| {f["num"]} | {name} | {f["issn"]} | {other} |')
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vak-parser",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Издания перечня ВАК, у которых указанная специальность "
                    "действует на заданный день.",
        epilog="примеры:\n"
               "  vak-parser 2.3.8                 издания по специальности 2.3.8\n"
               "  vak-parser 2.3.8 5.2.3 --json    несколько кодов, JSON в stdout\n"
               "  vak-parser 1.2.2 --as-of 01.01.2026     как было на ту дату\n"
               "  vak-parser 1.2.2 --pdf vak.pdf   разобрать свой файл\n\n"
               "коды возврата: 0 — что-то найдено, 3 — не найдено ничего,\n"
               "1 — источник недоступен, 2 — неверный вызов\n\n"
               "Источник — официальный сайт ВАК (" + OFFICIAL_PAGE + "). Он\n"
               "открывается не из всех сетей; если не ответил, инструмент\n"
               "громко предупреждает и берёт зеркало.")
    ap.add_argument("codes", nargs="+", metavar="КОД",
                    help="коды специальностей, напр. 2.3.8 5.2.3")
    ap.add_argument("--as-of", metavar="ДД.ММ.ГГГГ",
                    help="день, на который проверяется действие (по умолчанию сегодня)")
    ap.add_argument("--source", choices=("auto", "official", "mirror"),
                    default="auto",
                    help="откуда брать перечень (по умолчанию auto: "
                         "официальный сайт, при отказе — зеркало)")
    ap.add_argument("--pdf", metavar="ФАЙЛ",
                    help="разобрать готовый файл вместо скачивания")
    ap.add_argument("--refresh", action="store_true",
                    help="перекачать PDF, даже если он уже в кэше")
    ap.add_argument("--cache", default=str(CACHE), metavar="КАТАЛОГ",
                    help="куда класть скачанные PDF (по умолчанию ./cache)")
    ap.add_argument("--out", default=str(OUT), metavar="КАТАЛОГ",
                    help="куда писать .md и .json (по умолчанию ./out)")
    ap.add_argument("--json", action="store_true",
                    help="выдать всё одним JSON в stdout и ничего не писать на диск")
    ap.add_argument("--quiet", action="store_true", help="без прогресса")
    return ap


def resolve_source(args, log) -> tuple[dict, Path]:
    """log — канал прогресса. Предупреждения об источнике идут в stderr всегда:
    --quiet глушит прогресс, но не право знать, что ответ построен на зеркале."""
    if args.pdf:
        path = Path(args.pdf)
        if not path.is_file():
            raise SourceError(f"нет файла {path}")
        m = RE_FILE_DATE.search(path.name)
        return ({"kind": "file", "site": None, "index": None,
                 "url": path.resolve().as_uri(),
                 "date": ".".join(m.groups()) if m else "неизвестна",
                 "stated": None, "title": path.name, "all": []}, path)
    src = discover(args.source, cache=Path(args.cache), log=sys.stderr)
    check_stated(src, log=sys.stderr)
    if src["kind"] == "cache":
        return src, src["path"]
    return src, ensure_pdf(src, Path(args.cache), args.refresh, log=log)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = open(os.devnull, "w") if args.quiet else sys.stderr

    # Опечатка в коде ловится ДО сети: иначе она обходится в 10 МБ трафика,
    # две минуты разбора и пустой, но правдоподобный ответ.
    bad = [c for c in args.codes if not re.fullmatch(r"[\d.]+", c)]
    if bad:
        print("это не похоже на коды специальностей: " + " ".join(bad) +
              " (нужен вид 2.3.8)", file=sys.stderr)
        return EX_USAGE

    try:
        as_of = _d(args.as_of) if args.as_of else date.today()
    except ValueError:
        print(f"--as-of: не разобрал дату {args.as_of!r}, нужен вид 31.12.2026",
              file=sys.stderr)
        return EX_USAGE

    try:
        src, path = resolve_source(args, log)
    except SourceError as e:
        print(f"источник недоступен: {e}", file=sys.stderr)
        return EX_FAIL

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"перечень на {src['date']} ({path.stat().st_size // 1024} КБ, "
          f"sha256 {digest[:12]}), источник: {source_note(src)}, "
          f"оценка на {as_of.strftime('%d.%m.%Y')}", file=log)

    if not args.json:
        ready = reusable_counts(Path(args.out), args.codes, digest, as_of)
        if ready is not None:
            print(f"выдача в {args.out}/ уже построена из этого перечня на "
                  "эту же дату оценки — пересчёт пропущен (пересобрать: "
                  "удалить файлы или сменить --out)", file=log)
            for code, n in ready.items():
                print(f"изданий с действующей {code}: {n}", file=log)
            return EX_OK if any(ready.values()) else EX_EMPTY

    try:
        recs = parse(path, progress_every=0 if args.quiet else 100, log=log)
    except Exception as e:
        print(f"не разобрался {path}: {e}", file=sys.stderr)
        return EX_FAIL
    if not recs:
        print(f"в {path} не нашлось ни одной записи перечня — это не тот PDF "
              "или его переверстали", file=sys.stderr)
        return EX_FAIL
    print(f"записей в перечне: {len(recs)}", file=log)

    results = {code: select(recs, code, as_of) for code in args.codes}
    for code, found in results.items():
        print(f"изданий с действующей {code}: {len(found)}", file=log)

    meta = {"list_date": src["date"], "stated_current": src["stated"],
            "source_kind": src["kind"], "source_url": src["url"],
            "source_index": src["index"], "sha256": digest,
            "as_of": as_of.isoformat(), "total_records": len(recs),
            "counts": {c: len(f) for c, f in results.items()}}

    if args.json:
        try:
            json.dump({**meta, "found": results}, sys.stdout,
                      ensure_ascii=False, indent=1)
            sys.stdout.write("\n")
            sys.stdout.flush()
        except BrokenPipeError:
            # Читатель закрыл трубу (vak-parser ... --json | head) — не ошибка.
            # stdout подменяется на devnull, иначе интерпретатор упадёт на
            # финальном flush уже после выхода из main().
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    else:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        for code, found in results.items():
            (out_dir / f"vak-{code}.json").write_text(
                json.dumps({"code": code, **meta, "found": found},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            emit_md(code, len(recs), found, src, as_of, out_dir / f"vak-{code}.md")
        (out_dir / "source.json").write_text(
            json.dumps({**meta, "known_editions": src["all"]},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"записано в {out_dir}/", file=log)

    return EX_OK if any(results.values()) else EX_EMPTY


if __name__ == "__main__":
    sys.exit(main())
