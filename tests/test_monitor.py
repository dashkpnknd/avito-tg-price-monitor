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
    MonitorError,
    Product,
    SheetSource,
    extract_autoload_rows,
    extract_products,
    fetch_active_avito_items,
    issue_example,
    issue_digests,
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

    def test_replaces_an_ongoing_report_with_a_new_message(self):
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
                "app.monitor.send_telegram", return_value=43
            ) as sent, patch("app.monitor.delete_telegram") as deleted:
                monitor._notify([issue])
                sent.assert_called_once()
                deleted.assert_called_once_with(monitor.config, 42)
            self.assertEqual(["43"], load_state(monitor.config.state_path)["__monitor__"]["report_message_ids"])

    def test_leaves_an_old_report_when_telegram_refuses_deletion(self):
        issue = self.notification_issue()
        started = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            monitor = Monitor(self.notification_config(directory))
            with patch("app.monitor.now_utc", return_value=started), patch("app.monitor.send_telegram", return_value=42):
                monitor._notify([issue], force_notify=True)
            with patch("app.monitor.now_utc", return_value=started + timedelta(hours=1)), patch(
                "app.monitor.send_telegram", return_value=43
            ), patch("app.monitor.delete_telegram", side_effect=MonitorError("too old")) as deleted:
                monitor._notify([issue], force_notify=True)
                deleted.assert_called_once_with(monitor.config, 42)
            self.assertEqual(["43"], load_state(monitor.config.state_path)["__monitor__"]["report_message_ids"])

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
            self.assertNotIn("report_message_ids", load_state(monitor.config.state_path)["__monitor__"])

    def test_manual_report_stays_live_before_the_normal_grace_period(self):
        issue = self.notification_issue()
        started = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            monitor = Monitor(self.notification_config(directory))
            with patch("app.monitor.now_utc", return_value=started), patch(
                "app.monitor.send_telegram", return_value=42
            ):
                monitor._notify([issue], force_notify=True)
            with patch("app.monitor.now_utc", return_value=started + timedelta(hours=1)), patch(
                "app.monitor.delete_telegram"
            ) as deleted:
                monitor._notify([issue])
                deleted.assert_not_called()

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

    def test_splits_a_full_report_without_dropping_items(self):
        issues = []
        for index in range(4):
            title = "Товар %s %s" % (index, "x" * 1100)
            product = Product("product-%s" % index, "Телефоны (авито)", index + 2, {"title": title}, 100)
            autoload = AutoloadRow("Avito", index + 2, {"title": title}, 90, str(index), "")
            avito = AvitoItem(str(index), title, 90, "active", "")
            issues.append(Issue("issue-%s" % index, product, autoload, avito, ("цена не дошла из клиентской таблицы в автозагрузку",)))
        batches = issue_digests("Тест", issues, timedelta(hours=5), datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(batch) <= 3900 for batch in batches))
        full_text = "\n".join(batches)
        for index in range(4):
            self.assertIn("Товар %s" % index, full_text)

    def test_skips_a_partial_avito_resource_without_stopping_the_project(self):
        with TemporaryDirectory() as directory:
            config = self.notification_config(directory)
            payload = {
                "resources": [
                    {"id": 1, "price": 100, "status": "active", "title": "Complete"},
                    {"id": 2, "status": "active", "title": "No price"},
                ],
                "meta": {"per_page": 25},
            }
            with patch("app.monitor.get_avito_token", return_value="token"), patch("app.monitor.avito_json", return_value=payload):
                items = fetch_active_avito_items(config)
        self.assertEqual(["1"], list(items))

    def test_explains_that_an_unmatched_client_row_is_not_a_missing_price(self):
        autoload = AutoloadRow(
            "Avito", 2,
            {"title": "iPhone 17 Pro Max, 1 ТБ", "color": "серебристый", "simconfig": "Только eSIM"},
            134499, "123", "",
        )
        issue = Issue(
            "missing-client", Product("autoload:123", "Автозагрузка", 2, {"title": "iPhone 17 Pro Max, 1 ТБ"}, 134499),
            autoload, AvitoItem("123", "iPhone 17 Pro Max, 1 ТБ", 134499, "active", ""),
            ("Нет соответствующей цены в клиентской таблице",),
        )
        card = issue_example(issue)
        self.assertIn("подходящая строка не сопоставлена", card)
        self.assertIn("Цвет: <b>серебристый</b>", card)
        self.assertIn("SIM: <b>Только eSIM</b>", card)

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
