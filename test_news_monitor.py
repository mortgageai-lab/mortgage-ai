import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from news_monitor import NewsStore, extract_rate_mentions


class RateExtractionTests(unittest.TestCase):
    def test_extracts_mortgage_rate(self):
        result = extract_rate_mentions("Ставка по семейной ипотеке снижена до 6,2% годовых")
        self.assertEqual([(item.program, item.rate) for item in result], [("Семейная ипотека", 6.2)])

    def test_ignores_unrelated_percentage(self):
        self.assertEqual(extract_rate_mentions("Скидка на мебель 15%"), [])

    def test_ignores_implausible_rate(self):
        self.assertEqual(extract_rate_mentions("Ипотека: скидка 70%"), [])


class NewsStoreTests(unittest.TestCase):
    def test_duplicate_posts_are_not_inserted_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NewsStore(Path(directory) / "news.sqlite3")
            published = datetime.now(timezone.utc)
            store.save_post("bank", 1, published, "Ипотечная ставка 12%", None)
            store.save_post("bank", 1, published, "Ипотечная ставка 12%", None)
            self.assertEqual(len(store.latest_rates()), 1)


if __name__ == "__main__":
    unittest.main()
