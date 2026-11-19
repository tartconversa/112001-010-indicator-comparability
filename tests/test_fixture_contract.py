from pathlib import Path
import csv
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FixtureContractTests(unittest.TestCase):
    def test_csv_fixtures_have_headers_and_well_formed_rows(self):
        files = sorted((ROOT / "data").glob("*.csv"))
        self.assertTrue(files, "expected synthetic CSV fixtures")
        for path in files:
            with self.subTest(file=path.name):
                with path.open(encoding="utf-8", newline="") as stream:
                    rows = list(csv.reader(stream))
                self.assertGreaterEqual(len(rows), 2)
                self.assertTrue(all(column for column in rows[0]))
                self.assertTrue(all(len(row) == len(rows[0]) for row in rows[1:]))
