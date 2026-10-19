"""Official results: HTML table parsing and the download flow (network stubbed)."""

import http.client
import unittest

import support  # noqa: F401  (puts the app's modules on sys.path)
import results as RS


def S(pts, code=None, disc=False):
    return {"pts": pts, "code": code, "disc": disc}


class SailtiLikePage(unittest.TestCase):
    def setUp(self):
        self.res = RS.parse_results(support.fixture("sailti_like.html"))

    def test_columns_and_rows(self):
        self.assertEqual(self.res["races"], ["1", "2", "3"])
        self.assertEqual([r["sail"] for r in self.res["rows"]], ["200001", "200002", "300003", "200004"])
        self.assertEqual([r["rank"] for r in self.res["rows"]], [1, 2, 3, 4])

    def test_text_cells(self):
        r = self.res["rows"][0]
        self.assertEqual((r["sail_raw"], r["name"], r["club"], r["cat"], r["net"], r["total"]),
                         ("ESP 200001", "Ana ALFA BETA", "Club Nautico Uno", "ABS", 4.0, 9.0))

    def test_scores_codes_and_discards(self):
        rows = self.res["rows"]
        self.assertEqual(rows[0]["scores"], [S(1.0), S(5.0, disc=True), S(3.0)])
        self.assertEqual(rows[1]["scores"], [S(2.0), S(3.0), S(7.0, "DNC", True)])
        self.assertEqual(rows[2]["scores"], [S(3.0), S(7.0, "UFD", True), S(7.0, "DNF")])
        self.assertEqual(rows[3]["scores"], [S(5.0, "ZFP"), S(6.0), S(7.0, disc=True)])


class TableLayouts(unittest.TestCase):
    """Layouts found while reviewing the parser."""

    def parse(self, html):
        return RS.parse_results(html)

    def test_unclosed_hidden_cell_does_not_hide_the_row(self):
        t = ('<table><tr><th>#<th>Sail<th>Name<th>1<th>2</tr>'
             '<tr><td style="display:none">k<td>ESP 1<td>A<td>3<td>4</tr>'
             '<tr><td>2<td>ESP 2<td>B<td>1<td>2</table>')
        rows = self.parse(t)["rows"]
        self.assertEqual([(r["sail"], r["name"]) for r in rows], [("1", "A"), ("2", "B")])
        self.assertEqual([s["pts"] for s in rows[0]["scores"]], [3.0, 4.0])

    def test_hidden_row_is_skipped(self):
        t = '<table><tr><th>Sail<th>1<tr style="display:none"><td>ESP 9<td>1<tr><td>ESP 3<td>2</table>'
        self.assertEqual([r["sail"] for r in self.parse(t)["rows"]], ["3"])

    def test_nested_table_in_a_cell(self):
        t = ('<table><tr><th>Sail</th><th>Name</th><th>1</th></tr><tr><td>ESP 5</td>'
             '<td><table><tr><td>x</td></tr></table>Bo</td><td>7</td></tr></table>')
        r = self.parse(t)["rows"][0]
        self.assertEqual((r["sail"], r["name"], r["scores"][0]["pts"]), ("5", "Bo", 7.0))

    def test_hidden_table_is_ignored(self):
        vis = '<table><tr><th>Sail<th>Nombre<th>1</tr><tr><td>ESP 1<td>Ana<td>1</tr></table>'
        hid = ('<div style="display:none"><table><tr><th>Sail<th>Nombre<th>1</tr>'
               + "".join(f"<tr><td>ESP {i}<td>X<td>{i}</tr>" for i in range(10, 20)) + "</table></div>")
        rows = self.parse(hid + vis)["rows"]
        self.assertEqual([(r["sail"], r["name"]) for r in rows], [("1", "Ana")])

    def test_grouped_headers_keep_labels_unique(self):
        t = ('<table><tr><th></th><th>Q</th><th>Q</th><th>F</th><th>F</th></tr>'
             '<tr><th>Sail</th><th>1</th><th>2</th><th>1</th><th>2</th></tr>'
             '<tr><td>ESP 1</td><td>1</td><td>2</td><td>3</td><td>4</td></tr></table>')
        self.assertEqual(self.parse(t)["races"], ["1", "2", "1 (2)", "2 (2)"])

    def test_prefixed_race_labels_are_kept(self):
        t = '<table><tr><th>Sail</th><th>Q1</th><th>F 1</th></tr><tr><td>ESP 1</td><td>1</td><td>2</td></tr></table>'
        self.assertEqual(self.parse(t)["races"], ["Q1", "F1"])

    def test_colspan_sail_header_joins_flag_and_number(self):
        t = ('<table><tr><th>#</th><th colspan="2">Sail</th><th>Name</th><th>1</th></tr>'
             '<tr><td>1</td><td>ESP</td><td>212517</td><td>Ana</td><td>3</td></tr></table>')
        r = self.parse(t)["rows"][0]
        self.assertEqual((r["sail_raw"], r["sail"]), ("ESP 212517", "212517"))

    def test_no_rank_column_when_first_column_is_the_sail(self):
        t = ('<table><tr><th>Sail<th>Name<th>R1<th>R2<th>Net</tr>'
             '<tr><td>ESP 212517<td>A<td>1<td>2<td>3</tr><tr><td>ITA 1<td>B<td>2<td>DNF<td>9</tr></table>')
        res = self.parse(t)
        self.assertEqual(res["races"], ["R1", "R2"])
        self.assertEqual([r["rank"] for r in res["rows"]], [None, None])
        self.assertEqual(res["rows"][1]["scores"][1], S(None, "DNF"))

    def test_spanish_name_headers(self):
        for head in ("Nombre", "Patrón", "Timonel", "Regatistas", "Tripulantes"):
            t = f'<table><tr><th>Vela<th>{head}<th>1</tr><tr><td>ESP 1<td>Ana<td>1</tr></table>'
            self.assertEqual(self.parse(t)["rows"][0]["name"], "Ana", head)

    def test_longest_digit_run_is_the_sail(self):
        t = '<table><tr><th>Sail<th>1</tr><tr><td>ESP-2 212517<td>1</tr></table>'
        self.assertEqual(self.parse(t)["rows"][0]["sail"], "212517")

    def test_empty_cell_is_no_score(self):
        t = '<table><tr><th>Sail<th>1<th>2</tr><tr><td>ESP 1<td>1<td></tr></table>'
        self.assertEqual(self.parse(t)["rows"][0]["scores"], [S(1.0), None])

    def test_no_results_table(self):
        self.assertIsNone(self.parse("<table><tr><th>A<th>B</tr><tr><td>1<td>2</tr></table>"))


TABLE = "<table><tr><th>Sail<th>1</tr><tr><td>ESP 3<td>2</table>"


class FetchFlow(unittest.TestCase):
    """fetch_results with the network stubbed (_fetch returns (text, final URL))."""

    def setUp(self):
        self.real, self.calls = RS._fetch, []

    def tearDown(self):
        RS._fetch = self.real

    def stub(self, fn):
        def fake(u):
            self.calls.append(u)
            return fn(u)
        RS._fetch = fake

    def test_sailti_follow_up_request(self):
        self.stub(lambda u: (TABLE, u) if "resultsajax" in u else
                  ("<script>var u='/es/resultsajax?id=1&amp;allResults=1';</script>", u))
        self.assertEqual(len(RS.fetch_results("https://ex.org/es/page")["rows"]), 1)
        self.assertEqual(self.calls[-1], "https://ex.org/es/resultsajax?id=1&allResults=1")

    def test_follow_up_resolved_against_the_redirected_page(self):
        self.stub(lambda u: (TABLE, u) if "resultsajax" in u else
                  ("<script>var u='/es/resultsajax?id=1&allResults=1';</script>", "https://www.ex.org/es/page"))
        RS.fetch_results("http://ex.org/es/page")
        self.assertEqual(self.calls[-1], "https://www.ex.org/es/resultsajax?id=1&allResults=1")

    def test_other_site_link_falls_back_to_the_page_table(self):
        self.stub(lambda u: ("<script>var u='https://other.org/resultsajax?id=1';</script>" + TABLE, u))
        self.assertEqual(len(RS.fetch_results("https://ex.org/r")["rows"]), 1)
        self.assertEqual(len(self.calls), 1)

    def test_other_site_link_without_table_is_an_error(self):
        self.stub(lambda u: ("<script>var u='https://other.org/resultsajax?id=1';</script>", u))
        with self.assertRaisesRegex(ValueError, "another site"):
            RS.fetch_results("https://ex.org/r")

    def test_failed_follow_up_falls_back_to_the_page_table(self):
        def fn(u):
            if "resultsajax" in u:
                raise http.client.IncompleteRead(b"x")
            return "<script>var u='resultsajax?id=1';</script>" + TABLE, u
        self.stub(fn)
        self.assertEqual(len(RS.fetch_results("https://ex.org/r")["rows"]), 1)

    def test_no_table_anywhere(self):
        self.stub(lambda u: ("<p>nothing</p>", u))
        with self.assertRaisesRegex(ValueError, "No results table"):
            RS.fetch_results("https://ex.org/r")

    def test_download_errors_become_value_errors(self):
        def fn(u):
            raise OSError("boom")
        self.stub(fn)
        with self.assertRaisesRegex(ValueError, "Could not download the results page"):
            RS.fetch_results("https://ex.org/r")

    def test_only_http_links(self):
        for url in ("ftp://ex.org/r", "file:///etc/passwd", "ex.org/r", ""):
            with self.assertRaises(ValueError):
                RS.fetch_results(url)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
