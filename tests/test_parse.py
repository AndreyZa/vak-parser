# -*- coding: utf-8 -*-
"""Тесты — на синтетических данных, без сети и без PDF.

Проверяется то, что ломается молча:

* привязка строки специальности к управляющему ею блоку сроков и отсечение
  закрытых блоков — ошибка здесь не падает, а даёт правдоподобный список не
  тех журналов;
* выбор источника: официальный сайт идёт первым, а переход на зеркало обязан
  быть громким и попадать в выдачу — молчаливая подмена первоисточника
  выглядит как нормальный ответ.
"""
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vak_parser as vp  # noqa: E402

AS_OF = date(2026, 8, 19)


def rec(spec, dates, num=1, name="Журнал", issn="1234-5678"):
    return {"num": num, "page": 1, "name": [name], "issn": [issn],
            "spec": spec, "date": dates}


class TestColumns(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(vp.col_of(40.0), "num")
        self.assertEqual(vp.col_of(76.8), "name")
        self.assertEqual(vp.col_of(243.7), "issn")
        self.assertEqual(vp.col_of(306.2), "spec")
        self.assertEqual(vp.col_of(515.5), "date")
        self.assertIsNone(vp.col_of(10.0))

    def test_name_boundary_is_below_768(self):
        # рубеж 77 склеивал бы номер и название в одну ячейку
        self.assertEqual(vp.col_of(70.0), "name")


class TestDateBlocks(unittest.TestCase):
    def test_two_blocks_in_order(self):
        blocks = vp.date_blocks([
            (100.0, "с 28.12.2018 по 16.10.2022"),
            (200.0, "с 01.02.2022"),
        ])
        self.assertEqual(blocks, [[100.0, "28.12.2018", "16.10.2022"],
                                  [200.0, "01.02.2022", None]])

    def test_closing_date_attaches_to_last_block(self):
        blocks = vp.date_blocks([(100.0, "с 01.01.2020"), (112.0, "по 31.12.2024")])
        self.assertEqual(blocks, [[100.0, "01.01.2020", "31.12.2024"]])


class TestSpecialties(unittest.TestCase):
    def test_open_block_is_active(self):
        r = rec(spec=[(200.0, "2.3.8")], dates=[(200.0, "с 01.02.2022")])
        hits = vp.specialties(r, "2.3.8", AS_OF)
        self.assertEqual([h[3] for h in hits], [True])

    def test_closed_block_is_not_active(self):
        r = rec(spec=[(100.0, "2.3.8")],
                dates=[(100.0, "с 28.12.2018 по 16.10.2022")])
        self.assertEqual([h[3] for h in vp.specialties(r, "2.3.8", AS_OF)], [False])

    def test_specialty_governed_by_its_own_block(self):
        # Ровно тот случай, ради которого написан разбор по координатам:
        # закрытый блок сверху, открытый снизу — код из закрытого НЕ действует.
        r = rec(spec=[(100.0, "17.00.04"), (200.0, "2.3.8")],
                dates=[(100.0, "с 28.12.2018 по 16.10.2022"),
                       (200.0, "с 01.02.2022")])
        self.assertEqual([h[3] for h in vp.specialties(r, "2.3.8", AS_OF)], [True])
        self.assertEqual([h[3] for h in vp.specialties(r, "17.00.04", AS_OF)], [False])

    def test_edge_tolerance(self):
        # строка специальности ниже начала блока на пол-строки — тот же блок
        r = rec(spec=[(206.0, "2.3.8")], dates=[(200.0, "с 01.02.2022")])
        self.assertEqual([h[3] for h in vp.specialties(r, "2.3.8", AS_OF)], [True])

    def test_as_of_matters(self):
        r = rec(spec=[(100.0, "2.3.8")], dates=[(100.0, "с 01.01.2020 по 01.08.2026")])
        self.assertTrue(vp.specialties(r, "2.3.8", date(2026, 7, 1))[0][3])
        self.assertFalse(vp.specialties(r, "2.3.8", date(2026, 8, 19))[0][3])


class TestSelect(unittest.TestCase):
    def test_only_active_records_selected(self):
        live = rec(spec=[(200.0, "2.3.8")], dates=[(200.0, "с 01.02.2022")], num=1)
        dead = rec(spec=[(100.0, "2.3.8")],
                   dates=[(100.0, "с 01.01.2019 по 16.10.2022")], num=2)
        got = vp.select([live, dead], "2.3.8", AS_OF)
        self.assertEqual([g["num"] for g in got], [1])

    def test_adjacent_codes_collected(self):
        r = rec(spec=[(200.0, "2.3.8 2.3.5 1.2.2")], dates=[(200.0, "с 01.02.2022")])
        got = vp.select([r], "2.3.8", AS_OF)
        self.assertEqual(got[0]["codes"], ["1.2.2", "2.3.5", "2.3.8"])


class TestMirrorDiscovery(unittest.TestCase):
    def test_picks_newest_by_link_text(self):
        html = ('<a href="/f/a.pdf">Перечень журналов ВАК на 11.02.2026</a>'
                '<a href="/f/b.pdf">Перечень журналок ВАК на 15.06.2026</a>'
                '<a href="/f/c.pdf">Перечень журналов ВАК на 30.03.2026</a>'
                ' по состоянию на 15.06.2026 ')
        got = vp.mirror_from_html(html, "https://example.invalid/index")
        self.assertEqual(got["kind"], "mirror")
        self.assertEqual(got["date"], "15.06.2026")
        self.assertEqual(got["stated"], "15.06.2026")
        self.assertEqual([i["date"] for i in got["all"]],
                         ["15.06.2026", "30.03.2026", "11.02.2026"])

    def test_mismatch_between_prose_and_links_is_reported(self):
        html = ('<a href="/f/b.pdf">Перечень журналов ВАК на 15.06.2026</a>'
                ' по состоянию на 30.03.2026 ')
        got = vp.mirror_from_html(html, "https://example.invalid/index")
        self.assertEqual(got["date"], "15.06.2026")
        self.assertEqual(got["stated"], "30.03.2026")   # расхождение видно вызывающему

    def test_empty_page_is_an_error(self):
        with self.assertRaises(vp.SourceError):
            vp.mirror_from_html("<p>ничего тут нет</p>", "https://example.invalid/")


# Ответ API сайта ВАК: ссылка на PDF лежит в редакторском rich-text (поле info,
# JSON строкой), заголовок записи называет ту же дату отдельно.
def _official_payload(link_date="29.06.2026", title_date="29.06.2026",
                      href="https://vak.gisnauka.ru/s3-files/x/a58506ec.pdf"):
    info = [{"type": "p", "attributes": {}, "children": [
        {"type": "a", "attributes": {"href": href, "target": "_blank"},
         "children": ["Перечень рецензируемых научных изданий, в которых "
                      "должны быть опубликованы основные научные результаты "
                      f"диссертаций (по состоянию на {link_date})"]}]}]
    return {"count": 2, "results": [
        {"name": "Письмо Минобрнауки России о наборе в состав совета",
         "info": "[]", "files": []},
        {"name": "Перечень рецензируемых научных изданий, в которых должны "
                 "быть опубликованы основные научные результаты диссертаций "
                 f"(по состоянию на {title_date} г.)",
         "info": json.dumps(info, ensure_ascii=False), "files": []}]}


class TestOfficialDiscovery(unittest.TestCase):
    def test_takes_pdf_link_and_date_from_rich_text(self):
        got = vp.official_from_json(_official_payload())
        self.assertEqual(got["kind"], "official")
        self.assertEqual(got["date"], "29.06.2026")
        self.assertEqual(got["stated"], "29.06.2026")
        self.assertTrue(got["url"].endswith("a58506ec.pdf"))

    def test_other_records_are_skipped(self):
        # первая запись в ленте — не перечень; берётся не она
        self.assertTrue(vp.official_from_json(_official_payload())["url"])

    def test_link_date_wins_over_title_date_but_mismatch_is_visible(self):
        got = vp.official_from_json(_official_payload(link_date="29.06.2026",
                                                      title_date="15.06.2026"))
        self.assertEqual(got["date"], "29.06.2026")
        self.assertEqual(got["stated"], "15.06.2026")

    def test_missing_record_is_an_error(self):
        with self.assertRaises(vp.SourceError):
            vp.official_from_json({"results": [{"name": "Новости", "info": "[]"}]})


class TestSourceChain(unittest.TestCase):
    """auto: официальный первым, зеркало — только если официальный молчит."""

    def _run(self, official_ok):
        log = io.StringIO()
        payload = json.dumps(_official_payload()).encode()
        html = '<a href="/f/b.pdf">Перечень журналов ВАК на 15.06.2026</a>'

        def fake_fetch(url, timeout=120):
            if url.startswith(vp.OFFICIAL_HOST):
                if not official_ok:
                    raise OSError("timed out")
                return payload
            return html.encode()

        orig, vp.fetch = vp.fetch, fake_fetch
        try:
            return vp.discover("auto", "https://example.invalid/i", log=log), log
        finally:
            vp.fetch = orig

    def test_official_used_silently_when_alive(self):
        src, log = self._run(official_ok=True)
        self.assertEqual(src["kind"], "official")
        self.assertEqual(log.getvalue(), "")

    def test_mirror_fallback_is_loud(self):
        src, log = self._run(official_ok=False)
        self.assertEqual(src["kind"], "mirror")
        said = log.getvalue()
        self.assertIn("ОФИЦИАЛЬНЫЙ ИСТОЧНИК НЕДОСТУПЕН", said)
        self.assertIn("ЗЕРКАЛО", said)

    def test_mirror_marked_in_generated_files(self):
        # выдача переживает прогон — в ней должно быть видно, что это зеркало
        self.assertIn("ЗЕРКАЛО", vp.source_note({"kind": "mirror", "site": "m"}))
        self.assertIn("официальный", vp.source_note({"kind": "official", "site": "v"}))


class TestCacheFallback(unittest.TestCase):
    """Официальный молчит: зеркало и уже скачанный кэш соревнуются ДАТОЙ
    РЕДАКЦИИ. Иначе заранее скачанная свежая официальная редакция молча
    проигрывала бы более старой зеркальной."""

    def _run(self, cached_dates, mirror_ok=True, mirror_date="15.06.2026"):
        log = io.StringIO()
        html = f'<a href="/f/b.pdf">Перечень журналов ВАК на {mirror_date}</a>'

        def fake_fetch(url, timeout=120):
            if url.startswith(vp.OFFICIAL_HOST):
                raise OSError("timed out")
            if not mirror_ok:
                raise OSError("mirror down")
            return html.encode()

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            for d in cached_dates:
                (cache / f"vak-{d.replace('.', '-')}.pdf").write_bytes(b"%PDF-")
            orig, vp.fetch = vp.fetch, fake_fetch
            try:
                src = vp.discover("auto", "https://example.invalid/i",
                                  cache=cache, log=log)
            finally:
                vp.fetch = orig
        return src, log.getvalue()

    def test_newer_cached_edition_beats_older_mirror(self):
        src, said = self._run(["29.06.2026"])
        self.assertEqual(src["kind"], "cache")
        self.assertEqual(src["date"], "29.06.2026")
        self.assertIn("из кэша", said)

    def test_newer_mirror_beats_older_cache(self):
        src, _ = self._run(["11.02.2026"])
        self.assertEqual(src["kind"], "mirror")
        self.assertEqual(src["date"], "15.06.2026")

    def test_same_edition_prefers_cache_over_redownload(self):
        src, _ = self._run(["15.06.2026"])
        self.assertEqual(src["kind"], "cache")

    def test_cache_serves_when_everything_is_down(self):
        src, said = self._run(["15.06.2026"], mirror_ok=False)
        self.assertEqual(src["kind"], "cache")
        self.assertIn("тоже недоступно", said)

    def test_newest_of_several_cached_wins(self):
        src, _ = self._run(["11.02.2026", "29.06.2026"], mirror_ok=False)
        self.assertEqual(src["date"], "29.06.2026")

    def test_nothing_anywhere_is_an_error(self):
        with self.assertRaises(vp.SourceError):
            self._run([], mirror_ok=False)

    def test_official_alive_ignores_cache_silently(self):
        # кэш — только запасной выход; живой официальный берётся без баннера
        log = io.StringIO()
        payload = json.dumps(_official_payload()).encode()
        orig, vp.fetch = vp.fetch, lambda url, timeout=120: payload
        try:
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "vak-01-01-2020.pdf").write_bytes(b"%PDF-")
                src = vp.discover("auto", "https://example.invalid/i",
                                  cache=Path(td), log=log)
        finally:
            vp.fetch = orig
        self.assertEqual(src["kind"], "official")
        self.assertEqual(log.getvalue(), "")


class TestReuse(unittest.TestCase):
    """Готовая выдача переиспользуется, только если совпали и PDF (sha256),
    и дата оценки — у специальностей истекают сроки, вчерашний ответ сегодня
    может быть уже неверным."""

    SHA = "d" * 64
    AS_OF = date(2026, 8, 19)

    def _write(self, out, code="2.3.8", sha=None, as_of="2026-08-19", n=2):
        (out / f"vak-{code}.json").write_text(json.dumps(
            {"code": code, "sha256": sha or self.SHA, "as_of": as_of,
             "found": [{"num": i} for i in range(n)]}), encoding="utf-8")
        (out / f"vak-{code}.md").write_text("# x", encoding="utf-8")

    def _counts(self, codes, prepare):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            prepare(out)
            return vp.reusable_counts(out, codes, self.SHA, self.AS_OF)

    def test_matching_outputs_are_reused(self):
        got = self._counts(["2.3.8"], lambda o: self._write(o))
        self.assertEqual(got, {"2.3.8": 2})

    def test_other_pdf_forces_recompute(self):
        got = self._counts(["2.3.8"], lambda o: self._write(o, sha="e" * 64))
        self.assertIsNone(got)

    def test_other_as_of_forces_recompute(self):
        got = self._counts(["2.3.8"],
                           lambda o: self._write(o, as_of="2026-08-18"))
        self.assertIsNone(got)

    def test_any_missing_code_forces_recompute(self):
        got = self._counts(["2.3.8", "2.3.5"], lambda o: self._write(o))
        self.assertIsNone(got)

    def test_missing_md_forces_recompute(self):
        def prepare(o):
            self._write(o)
            (o / "vak-2.3.8.md").unlink()
        self.assertIsNone(self._counts(["2.3.8"], prepare))

    def test_empty_out_dir_forces_recompute(self):
        self.assertIsNone(self._counts(["2.3.8"], lambda o: None))


class TestCli(unittest.TestCase):
    """Ошибки вызова заканчиваются кодом 2 ДО обращения к сети."""

    def setUp(self):
        # сеть в тестах запрещена: любой fetch — провал теста
        def no_net(url, timeout=120):
            raise AssertionError(f"тест полез в сеть: {url}")
        self._orig, vp.fetch = vp.fetch, no_net
        self._err, sys.stderr = sys.stderr, io.StringIO()

    def tearDown(self):
        vp.fetch = self._orig
        sys.stderr = self._err

    def test_gibberish_code_is_rejected_before_any_download(self):
        self.assertEqual(vp.main(["ерунда"]), vp.EX_USAGE)

    def test_bad_as_of_is_rejected_before_any_download(self):
        self.assertEqual(vp.main(["2.3.8", "--as-of", "вчера"]), vp.EX_USAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
