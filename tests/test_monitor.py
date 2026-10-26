import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.admin_bot import spreadsheet_id
from app.monitor import (
    AvitoItem,
    AutoloadRow,
    Config,
    Issue,
    Monitor,
    Product,
    SheetSource,
    extract_autoload_rows,
    extract_products,
    notification_batches,
    parse_phone_title,
    product_matches,
    load_state,
)


class MatchingTests(unittest.TestCase):
    def notification_config(self, directory: str) -> Config:
        return Config(
            project_name="Тестовый проект",
            client_sources=(),
            client_spreadsheet_ids=(),
            autoload_sources=(),
            autoload_spreadsheet_ids=(),
            telegram_token="test-token",
            telegram_chat_id="-1001",
            avito_client_id="client",
            avito_client_secret="secret",
            state_path=Path(directory) / "state.json",
            check_interval_minutes=60,
            mismatch_grace=timedelta(hours=5),
            notification_repeat=timedelta(hours=24),
            max_avito_pages=1,
            dry_run=False,
        )

    @staticmethod
    def notification_issue() -> Issue:
        product = Product("product", "Телефоны (авито)", 2, {"title": "iPhone"}, 100)
        autoload = AutoloadRow("Avito", 2, {"title": "iPhone"}, 90, "123", "")
        avito = AvitoItem("123", "iPhone", 90, "active", "")
        return Issue("product:123", product, autoload, avito, ("цена не дошла из клиентской таблицы в автозагрузку",))

    def test_reuses_one_telegram_card_for_an_ongoing_issue(self):
        issue = self.notification_issue()
        started = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            monitor = Monitor(self.notification_config(directory))
            with patch("app.monitor.now_utc", return_value=started):
                monitor._notify([issue])
            with patch("app.monitor.now_utc", return_value=started + timedelta(hours=5)), patch(
                "app.monitor.send_telegram", return_value=42
            ) as sent:
                monitor._notify([issue])
                sent.assert_called_once()
            with patch("app.monitor.now_utc", return_value=started + timedelta(hours=29)), patch(
                "app.monitor.send_telegram"
            ) as sent, patch("app.monitor.edit_telegram") as edited:
                monitor._notify([issue])
                sent.assert_not_called()
                edited.assert_called_once()
                self.assertEqual(42, edited.call_args.args[1])
            self.assertEqual("42", load_state(monitor.config.state_path)["__monitor__"]["report_message_id"])

    def test_removes_the_card_when_the_issue_is_resolved(self):
        issue = self.notification_issue()
        started = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            monitor = Monitor(self.notification_config(directory))
            with patch("app.monitor.now_utc", return_value=started):
                monitor._notify([issue])
            with patch("app.monitor.now_utc", return_value=started + timedelta(hours=5)), patch(
                "app.monitor.send_telegram", return_value=42
            ):
                monitor._notify([issue])
            with patch("app.monitor.now_utc", return_value=started + timedelta(hours=6)), patch(
                "app.monitor.delete_telegram"
            ) as deleted:
                monitor._notify([])
                deleted.assert_called_once_with(monitor.config, 42)
            self.assertNotIn("report_message_id", load_state(monitor.config.state_path)["__monitor__"])

    def test_extracts_google_spreadsheet_id_for_admin_wizard(self):
        self.assertEqual(
            "1abc_DEF-123456789012345",
            spreadsheet_id("https://docs.google.com/spreadsheets/d/1abc_DEF-123456789012345/edit#gid=0"),
        )
        self.assertIsNone(spreadsheet_id("https://example.test/not-a-sheet"))

    def test_extracts_title_price_products(self):
        rows = [["Title", "Price"], ["Apple Watch SE 3 (2025) 40mm Midnight", "24 990"]]
        products = extract_products(SheetSource("Часы", "id", "gid"), rows)
        self.assertEqual(1, len(products))
        self.assertEqual(24990, products[0].price)
        self.assertEqual("Apple Watch SE 3 (2025) 40mm Midnight", products[0].parameters["title"])

    def test_requires_all_client_parameters(self):
        product = Product("key", "Клиент", 2, {"model": "iPhone 16", "memorysize": "128 ГБ", "color": "Черный"}, 59990)
        matching = AutoloadRow("Авито", 2, {"model": "iPhone 16", "memorysize": "128ГБ", "color": "черный", "akb": ""}, 59990, "123456", "")
        different_memory = AutoloadRow("Авито", 3, {"model": "iPhone 16", "memorysize": "256 ГБ", "color": "черный"}, 59990, "123457", "")
        self.assertTrue(product_matches(product, matching))
        self.assertFalse(product_matches(product, different_memory))

    def test_extracts_avito_id_from_url(self):
        rows = [
            ["Заголовок объявления Title", "Цена Price", "Ссылка на объявление"],
            ["Apple Watch SE 3", "24990", "https://www.avito.ru/gatchina/x_8479075727"],
        ]
        parsed = extract_autoload_rows(SheetSource("Авито", "id", "gid"), rows)
        self.assertEqual("8479075727", parsed[0].ad_id)
        self.assertEqual(24990, parsed[0].price)

    def test_parses_limestore_phone_title(self):
        self.assertEqual(
            {"model": "iPhone 16E", "memorysize": "128 гб", "color": "черный"},
            parse_phone_title("iPhone 16E 128Gb black"),
        )
        self.assertEqual(
            {"model": "iPhone 14 plus", "memorysize": "128 гб", "color": "голубой"},
            parse_phone_title("14 plus 128 blue"),
        )

    def test_splits_long_telegram_notification(self):
        batches = notification_batches(["x" * 2000, "y" * 2000, "z" * 2000], "Заголовок", limit=3900)
        self.assertEqual(3, len(batches))
        self.assertTrue(all(len(batch) <= 3900 for batch in batches))

    def test_keeps_phone_title_for_a_readable_digest_without_matching_on_it(self):
        rows = [["НОВЫЙ", "Lime Store Наличка"], ["iPhone 16E 128Gb black", "57 990"]]
        product = extract_products(
            SheetSource("Телефоны", "id", "gid", "phone_cash", "НОВЫЙ", "Lime Store Наличка"), rows
        )[0]
        matching = AutoloadRow(
            "Авито", 2, {"model": "iPhone 16E", "memorysize": "128 ГБ", "color": "черный"}, 57490, "1", ""
        )
        self.assertEqual("iPhone 16E 128Gb black", product.title)
        self.assertTrue(product_matches(product, matching))

    def test_ignores_an_active_avito_listing_missing_from_autoload(self):
        issues = Monitor(None)._find_issues(
            [], [], {"123": AvitoItem("123", "iPhone 18 Pro", 290990, "active", "https://example.test/ad")}
        )
        self.assertEqual([], issues)

    def test_ignores_second_hand_autoload_row(self):
        product = Product("key", "Телефоны (авито)", 2, {"title": "Used phone"}, 100)
        used = AutoloadRow("Автозагрузка", 5, {"title": "Used phone", "condition": "Хорошее"}, 90, "123", "")
        active = {"123": AvitoItem("123", "Used phone", 90, "active", "https://example.test/ad")}
        self.assertEqual([], Monitor(None)._find_issues([product], [used], active))

    def test_ignores_client_product_with_only_an_archived_autoload_row(self):
        product = Product("key", "Клиент", 2, {"title": "Archived phone"}, 100)
        archived = AutoloadRow("Автозагрузка", 5, {"title": "Archived phone"}, 100, "123", "")
        self.assertEqual([], Monitor(None)._find_issues([product], [archived], {}))


if __name__ == "__main__":
    unittest.main()
