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
    cell,
    classify_unmatched_client_row,
    canonical_title,
    avito_json,
    fetch_active_avito_items,
    issue_example,
    issue_digests,
    parse_phone_title,
    product_matches,
    matching_client_products,
    unmatched_client_note,
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

    def test_second_manual_report_is_suppressed_for_five_minutes(self):
        issue = self.notification_issue()
        started = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            monitor = Monitor(self.notification_config(directory))
            with patch("app.monitor.now_utc", return_value=started), patch(
                "app.monitor.send_telegram", return_value=42
            ):
                monitor._notify([issue], force_notify=True)
            with patch("app.monitor.now_utc", return_value=started + timedelta(minutes=2)), patch(
                "app.monitor.send_telegram"
            ) as sent:
                monitor._notify([issue], force_notify=True)
                sent.assert_not_called()

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

    def test_storage_units_are_not_equated_by_source_matcher(self):
        product = Product(
            "client-1", "Телефоны (авито)", 42,
            {"model": "iPhone 17 Pro Max", "memorysize": "1 ТБ", "color": "серебристый"}, 134499,
        )
        row = AutoloadRow(
            "Авито", 5,
            {"model": "iPhone 17 Pro Max", "memorysize": "1024 ГБ", "color": "серебристый"},
            134499, "123", "",
        )
        self.assertFalse(product_matches(product, row))
        self.assertEqual([product], matching_client_products(row, [product]))

    def test_android_template_metadata_does_not_block_matching(self):
        rows = [
            ["Причина без цены", "Vendor", "Model", "SimConfig", "Color", "MemorySize", "RamSize", "Price"],
            ["", "Google", "Pixel 10", "SIM + eSIM", "синий", "256 ГБ", "12 ГБ", "73990"],
            ["", "Xiaomi", "17 Ultra", "SIM + eSIM", "белый", "512 ГБ", "16 ГБ", "109990"],
        ]
        products = extract_products(SheetSource("Телефоны", "id", "gid"), rows)
        self.assertNotIn("причинабезцены", products[0].parameters)
        pixel = AutoloadRow("Авито", 775, {
            "vendor": "Google", "model": "Pixel 10", "simconfig": "SIM + eSIM",
            "color": "синий", "memorysize": "256 ГБ", "ramsize": "12 ГБ",
        }, 73990, "123", "")
        xiaomi = AutoloadRow("Авито", 757, {
            "vendor": "Xiaomi", "model": "17 Ultra", "simconfig": "SIM + eSIM",
            "color": "белый", "memorysize": "512 ГБ", "ramsize": "16 ГБ",
        }, 109990, "456", "")
        active = {
            "123": AvitoItem("123", "Google Pixel 10", 73990, "active", ""),
            "456": AvitoItem("456", "Xiaomi 17 Ultra", 109990, "active", ""),
        }
        self.assertEqual([], Monitor(None)._find_issues(products, [pixel, xiaomi], active))

    def test_all_script_metadata_aliases_are_ignored_on_both_sides(self):
        for alias in ("Причина без цены", "Причина отсутствия цены", "Reason Without Price", "Missing Price Reason"):
            with self.subTest(alias=alias):
                product = extract_products(
                    SheetSource("Телефоны", "id", "gid"),
                    [[alias, "Model", "Color", "Price"], ["", "Pixel 10", "синий", "73990"]],
                )[0]
                row = extract_autoload_rows(
                    SheetSource("Avito", "id", "gid"),
                    [[alias, "Model", "Color", "Price", "Ссылка на объявление"],
                     ["", "Pixel 10", "синий", "73990", "https://www.avito.ru/x_123456"]],
                )[0]
                self.assertTrue(product_matches(product, row))
                self.assertEqual({"model", "color"}, set(product.parameters))
                self.assertNotIn(alias.lower().replace(" ", ""), row.parameters)

    def test_source_uses_first_exact_row_and_distinct_color_stays_distinct(self):
        products = [
            Product("one", "Телефоны", 10, {"model": "17 Ultra", "memorysize": "1024 ГБ", "color": "белый"}, 123990),
            Product("two", "Телефоны", 11, {"model": "17 Ultra", "memorysize": "1 ТБ", "color": "белый"}, 124990),
            Product("three", "Телефоны", 12, {"model": "17 Ultra", "memorysize": "1 ТБ", "color": "черный"}, 124990),
        ]
        row = AutoloadRow("Авито", 100, {"model": "17 Ultra", "memorysize": "1 ТБ", "color": "белый"}, 124990, "1", "")
        self.assertEqual([products[1]], matching_client_products(row, products))
        self.assertFalse(product_matches(products[2], row))

    def test_source_uses_first_row_for_duplicate_skus(self):
        products = [
            Product("one", "Телефоны", 10, {"model": "17 Ultra", "memorysize": "1 ТБ", "color": "белый"}, 123990),
            Product("two", "Телефоны", 11, {"model": "17 Ultra", "memorysize": "1 ТБ", "color": "белый"}, 124990),
        ]
        row = AutoloadRow("Авито", 100, {"model": "17 Ultra", "memorysize": "1 ТБ", "color": "белый"}, 123990, "1", "")
        active = {"1": AvitoItem("1", "Xiaomi 17 Ultra", 123990, "active", "")}
        issues = Monitor(None)._find_issues(products, [row], active)
        self.assertEqual([], issues)

    def test_live_log_xiaomi_1024gb_matches_client_without_false_alert(self):
        # The price-to-Avito log for KingStore Нижневартовск marks Avito R757
        # "Нет в прайсе" and client R302 "Нет в Авито" on 2026-11-13.
        client = Product("xiaomi", "Телефоны", 302, {
            "vendor": "Xiaomi", "model": "17 Ultra", "simconfig": "SIM + eSIM",
            "color": "белый", "memorysize": "1024 ГБ", "ramsize": "16 ГБ",
        }, 123990)
        row = AutoloadRow("Avito - Мобильные телефоны", 757, {
            "vendor": "Xiaomi", "model": "17 Ultra", "simconfig": "SIM + eSIM",
            "color": "белый", "memorysize": "1 ТБ", "ramsize": "16 ГБ",
        }, 123990, "8577805795", "")
        active = {row.ad_id: AvitoItem(row.ad_id, "Xiaomi 17 Ultra", 123990, "active", "")}
        issues = Monitor(None)._find_issues([client], [row], active)
        self.assertEqual([], issues)
        self.assertEqual([client], matching_client_products(row, [client]))
        active[row.ad_id] = AvitoItem(row.ad_id, "Xiaomi 17 Ultra", 124990, "active", "")
        issues = Monitor(None)._find_issues([client], [row], active)
        self.assertEqual(1, len(issues))
        self.assertEqual(client, issues[0].product)
        self.assertEqual(("цена не дошла из автозагрузки в Авито",), issues[0].reasons)

    def test_android_brand_prefix_and_ram_units_match_only_when_vendor_agrees(self):
        client = Product("pixel", "Телефоны", 42, {
            "vendor": "Google", "model": "Pixel 10", "memorysize": "256 ГБ",
            "ramsize": "12 GB", "color": "синий", "simconfig": "SIM + eSIM",
        }, 73990)
        row = AutoloadRow("Автозагрузка", 100, {
            "vendor": "Google", "model": "Google Pixel 10", "memorysize": "256 GB",
            "ramsize": "12 ГБ", "color": "синий", "simconfig": "SIM + eSIM",
        }, 73990, "123", "")
        self.assertEqual([client], matching_client_products(row, [client]))
        wrong_vendor = AutoloadRow(row.source_name, row.row, {**row.parameters, "vendor": "Xiaomi"}, row.price, row.ad_id, row.url)
        self.assertEqual([], matching_client_products(wrong_vendor, [client]))
        wrong_color = AutoloadRow(row.source_name, row.row, {**row.parameters, "color": "черный"}, row.price, row.ad_id, row.url)
        self.assertEqual([], matching_client_products(wrong_color, [client]))

    def test_ambiguous_equivalent_rows_do_not_claim_a_client_price(self):
        clients = [
            Product("one", "Телефоны", 42, {"model": "17 Ultra", "memorysize": "1024 ГБ", "color": "белый"}, 123990),
            Product("two", "Телефоны", 43, {"model": "17 Ultra", "memorysize": "1024 GB", "color": "белый"}, 124990),
        ]
        row = AutoloadRow("Автозагрузка", 100, {"model": "17 Ultra", "memorysize": "1 ТБ", "color": "белый"}, 123990, "123", "")
        self.assertEqual([], matching_client_products(row, clients))
        with self.assertLogs(level="WARNING") as logs:
            issues = Monitor(None)._find_issues(clients, [row], {"123": AvitoItem("123", "Xiaomi 17 Ultra", 123990, "active", "")})
        self.assertEqual([], issues)
        self.assertIn("without a confident client match", logs.output[0])

    def test_duplicate_equivalent_rows_with_the_same_price_use_first_row(self):
        products = [
            Product("first", "Телефоны", 42, {"model": "17 Ultra", "memorysize": "1024 ГБ", "color": "белый"}, 123990),
            Product("second", "Телефоны", 43, {"model": "17 Ultra", "memorysize": "1024 GB", "color": "белый"}, 123990),
        ]
        row = AutoloadRow("Автозагрузка", 100, {"model": "17 Ultra", "memorysize": "1 ТБ", "color": "белый"}, 123990, "123", "")
        self.assertEqual([products[0]], matching_client_products(row, products))

    def test_saved_ihub_report_links_unknown_client_sim_to_one_listing(self):
        # 13 Nov log: iHub iPhone 16 Plus R148 says "Нет в Авито" while
        # autoload R323 retains a price and says "Нет в прайсе".
        client = Product("ihub-148", "телефоны", 148, {
            "model": "iPhone 16 Plus", "memorysize": "128 ГБ", "color": "голубой",
            "simconfig": "Не знаю", "ramsize": "8 ГБ",
        }, 76990)
        row = AutoloadRow("Avito - Мобильные телефоны", 323, {
            "model": "iPhone 16 Plus", "memorysize": "128 ГБ", "color": "голубой",
            "simconfig": "SIM + eSIM", "ramsize": "8 ГБ",
        }, 78990, "8577523182", "")
        self.assertFalse(product_matches(client, row))
        self.assertEqual([client], matching_client_products(row, [client]))
        issues = Monitor(None)._find_issues([client], [row], {
            row.ad_id: AvitoItem(row.ad_id, "iPhone 16 Plus", 78990, "active", "")
        })
        self.assertEqual(1, len(issues))
        self.assertEqual(client, issues[0].product)
        self.assertEqual(("цена не дошла из клиентской таблицы в автозагрузку",), issues[0].reasons)

    def test_title_only_console_rows_ignore_sale_terms_but_keep_disc_variant(self):
        clients = [
            Product("disc", "пс", 3, {"title": "Sony PlayStation 5 Slim + Дисковод Рассрочка"}, 76990),
            Product("digital", "пс", 4, {"title": "Sony PlayStation 5 Slim Гарантия Рассрочка"}, 75990),
            Product("pro", "пс", 5, {"title": "Sony PlayStation 5 Pro Гарантия Рассрочка"}, 135990),
        ]
        titles = ["Sony PlayStation 5 Slim Дисковод", "Sony PlayStation 5 Slim", "Sony PlayStation 5 Pro"]
        for index, title in enumerate(titles):
            with self.subTest(title=title):
                row = AutoloadRow("Avito - Игровые приставки", index + 3, {
                    "title": title, "model": "PlayStation 5 Slim" if index < 2 else "PlayStation 5 Pro",
                }, [77990, 73990, 133990][index], str(index + 1), "")
                self.assertEqual([clients[index]], matching_client_products(row, clients))
        self.assertNotEqual(canonical_title(titles[0]), canonical_title(titles[1]))
        self.assertEqual("galaxys25+", canonical_title("Galaxy S25+ Рассрочка"))

    def test_saved_iphone_examples_do_not_merge_different_sim_or_capacity(self):
        kaluga_clients = [
            Product("sim", "Телефоны", 152, {
                "model": "iPhone 17", "simconfig": "SIM + eSIM", "memorysize": "256 ГБ", "color": "белый",
            }, 76490),
            Product("capacity", "Телефоны", 157, {
                "model": "iPhone 17", "simconfig": "Только eSIM", "memorysize": "512 ГБ", "color": "белый",
            }, 88490),
        ]
        kaluga_row = AutoloadRow("Avito - Мобильные телефоны", 383, {
            "model": "iPhone 17", "simconfig": "Только eSIM", "memorysize": "256 ГБ", "color": "белый",
        }, 74990, "383", "")
        self.assertEqual([], matching_client_products(kaluga_row, kaluga_clients))

        belgorod_clients = [
            Product("esim", "телефоны", 221, {
                "vendor": "Apple", "model": "iPhone 17 Pro", "simconfig": "Только eSIM",
                "memorysize": "512 ГБ", "color": "серебристый", "ramsize": "12 ГБ",
            }, 112900),
            Product("sim", "телефоны", 258, {
                "vendor": "Apple", "model": "iPhone 17 Pro", "simconfig": "SIM + eSIM",
                "memorysize": "512 ГБ", "color": "серебристый", "ramsize": "12 ГБ",
            }, 123900),
        ]
        belgorod_row = AutoloadRow("Avito - Мобильные телефоны", 396, {
            "vendor": "Apple", "model": "iPhone 17 Pro", "simconfig": "Не знаю",
            "memorysize": "512 ГБ", "color": "серебристый", "ramsize": "12 ГБ",
        }, 123900, "396", "")
        self.assertEqual([], matching_client_products(belgorod_row, belgorod_clients))

    def test_saved_kingstore_s25_plus_row_exists_with_blank_client_price(self):
        rows = [
            ["Vendor", "Model", "SimConfig", "Color", "MemorySize", "RamSize", "Price"],
            ["Samsung", "Galaxy S25+", "SIM + eSIM", "синий", "256 ГБ", "12 ГБ", ""],
        ]
        source = SheetSource("Телефоны", "id", "gid")
        self.assertEqual([], extract_products(source, rows))
        client = extract_products(source, rows, include_unpriced=True)
        self.assertEqual(1, len(client))
        self.assertIsNone(client[0].price)
        row = AutoloadRow("Avito - Мобильные телефоны", 472, {
            "vendor": "Samsung", "model": "Galaxy S25+", "simconfig": "SIM + eSIM",
            "color": "синий", "memorysize": "256 ГБ", "ramsize": "12 ГБ",
        }, 70990, "123", "")
        with self.assertLogs(level="INFO") as logs:
            issues = Monitor(None)._find_issues(client, [row], {"123": AvitoItem("123", "Galaxy S25+", 70990, "active", "")})
        self.assertEqual([], issues)
        self.assertTrue(any("client row but no client price" in line for line in logs.output))
        self.assertTrue(any("Телефоны:2" in line for line in logs.output))
        with self.assertLogs(level="INFO"):
            issues = Monitor(None)._find_issues(client, [row], {"123": AvitoItem("123", "Galaxy S25+", 71990, "active", "")})
        self.assertEqual(("цена не дошла из автозагрузки в Авито",), issues[0].reasons)
        self.assertEqual(client[0], issues[0].product)
        self.assertIn("Клиент: <b>нет текущей цены</b>", issue_example(issues[0]))

    def test_exact_blank_client_row_beats_fuzzy_priced_sim_candidate(self):
        exact_blank = Product("blank", "Телефоны", 10, {
            "model": "iPhone 17", "memorysize": "256 ГБ", "color": "белый", "simconfig": "Только eSIM"
        }, None)
        fuzzy_priced = Product("priced", "Телефоны", 11, {
            "model": "iPhone 17", "memorysize": "256 ГБ", "color": "белый", "simconfig": "Не знаю"
        }, 75990)
        row = AutoloadRow("Автозагрузка", 383, exact_blank.parameters, 74990, "123", "")
        self.assertEqual([exact_blank], matching_client_products(row, [fuzzy_priced, exact_blank]))
        with self.assertLogs(level="INFO"):
            issues = Monitor(None)._find_issues([fuzzy_priced, exact_blank], [row], {
                "123": AvitoItem("123", "iPhone 17", 74990, "active", "")
            })
        self.assertEqual([], issues)

    def test_unknown_sim_does_not_override_an_exact_variant_or_merge_two_known_variants(self):
        unknown = Product("unknown", "телефоны", 10, {"model": "Pixel 10", "memorysize": "256 ГБ", "color": "синий", "simconfig": "Не знаю"}, 73990)
        exact = Product("exact", "телефоны", 11, {"model": "Pixel 10", "memorysize": "256 ГБ", "color": "синий", "simconfig": "SIM + eSIM"}, 74990)
        row = AutoloadRow("Avito", 30, {"model": "Pixel 10", "memorysize": "256 ГБ", "color": "синий", "simconfig": "SIM + eSIM"}, 74990, "123", "")
        self.assertEqual([exact], matching_client_products(row, [unknown, exact]))
        different = AutoloadRow(row.source_name, row.row, {**row.parameters, "simconfig": "Только eSIM"}, row.price, row.ad_id, row.url)
        self.assertEqual([unknown], matching_client_products(different, [unknown, exact]))
        ambiguous = Product("unknown2", "телефоны", 12, unknown.parameters, 72990)
        self.assertEqual([], matching_client_products(different, [unknown, exact, ambiguous]))

    def test_numeric_sheet_cells_match_apps_script_display_form(self):
        self.assertEqual("400", cell([400.0], 0))

    def test_classifies_sim_without_creating_a_match(self):
        product = Product(
            "client-1", "Телефоны", 395,
            {"vendor": "HONOR", "model": "400", "memorysize": "256 ГБ", "ramsize": "12 ГБ", "color": "черный", "simconfig": ""}, 34990,
        )
        row = AutoloadRow(
            "Авито", 846,
            {"vendor": "HONOR", "model": "400", "memorysize": "256 ГБ", "ramsize": "12 ГБ", "color": "черный", "simconfig": "Только eSIM"}, 34990, "123", "",
        )
        self.assertFalse(product_matches(product, row))
        category, note = classify_unmatched_client_row(row, [product])
        self.assertEqual("different_sim", category)
        self.assertIn("SIM", note)

    def test_unmatched_note_names_the_field_that_prevents_a_match(self):
        product = Product(
            "client-1", "Телефоны (авито)", 42,
            {"model": "iPhone 17 Pro Max", "memorysize": "1024 ГБ", "color": "серебристый", "simconfig": "Только eSIM"}, 134499,
        )
        row = AutoloadRow(
            "Авито", 5,
            {"model": "iPhone 17 Pro Max", "memorysize": "1 ТБ", "color": "серебристый", "simconfig": "не знаю"},
            134499, "123", "",
        )
        note = unmatched_client_note(row, [product])
        self.assertIn("SIM: «Только eSIM» ↔ «не знаю»", note)
        self.assertNotIn("SKU совпадает", note)

    def test_unmatched_note_never_picks_between_equally_close_candidates(self):
        products = [
            Product("one", "Телефоны (авито)", 42, {"model": "iPhone 17", "memorysize": "128 ГБ", "color": "черный"}, 1),
            Product("two", "Телефоны (авито)", 43, {"model": "iPhone 17", "memorysize": "128 ГБ", "color": "белый"}, 1),
        ]
        row = AutoloadRow(
            "Авито", 5, {"model": "iPhone 17", "memorysize": "128 ГБ", "color": "красный"}, 1, "123", ""
        )
        note = unmatched_client_note(row, products)
        self.assertIn("2 одинаково близких кандидата", note)
        self.assertIn("автоматически не выбрано", note)

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

    def test_rate_limit_waits_for_the_retry_window_without_a_live_request(self):
        with patch("app.monitor.http_text", side_effect=[MonitorError("HTTP 429 for Avito"), '{"resources": []}']) as request, patch("app.monitor.time.sleep") as sleep:
            payload = avito_json("https://example.test/items", {})
        self.assertEqual([], payload["resources"])
        self.assertEqual(2, request.call_count)
        sleep.assert_called_once_with(60)

    def test_rate_limit_respects_retry_after_header(self):
        with patch("app.monitor.http_text", side_effect=[MonitorError("HTTP 429 for Avito: Retry-After: 90"), '{}']) as request, patch("app.monitor.time.sleep") as sleep:
            self.assertEqual({}, avito_json("https://example.test/items", {}))
        self.assertEqual(2, request.call_count)
        sleep.assert_called_once_with(90)

    def test_unlinked_client_row_does_not_claim_the_product_is_missing(self):
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
        self.assertIn("Клиентская цена: <b>не проверена</b>", card)
        self.assertNotIn("подходящая строка не сопоставлена", card)
        self.assertIn("Цвет: <b>серебристый</b>", card)
        self.assertIn("SIM: <b>Только eSIM</b>", card)

    def test_renders_diagnostic_note_for_an_unmatched_client_row(self):
        autoload = AutoloadRow("Avito", 2, {"title": "iPhone", "model": "iPhone 17"}, 100, "123", "")
        issue = Issue(
            "missing-client", Product("autoload:123", "Автозагрузка", 2, {"title": "iPhone"}, 100),
            autoload, AvitoItem("123", "iPhone", 100, "active", ""),
            ("Нет соответствующей цены в клиентской таблице",),
            "Возможный кандидат: лист «Телефоны», строка 42. Отличается: SIM.",
        )
        self.assertIn("Возможный кандидат", issue_example(issue))

    def test_unlinked_client_still_checks_autoload_price_against_avito(self):
        row = AutoloadRow("Avito - Мобильные телефоны", 777, {
            "model": "Pixel 10 Pro", "memorysize": "128 ГБ", "color": "бежевый"
        }, 79990, "123", "")
        active = {"123": AvitoItem("123", "Pixel 10 Pro", 81990, "active", "")}
        with self.assertLogs(level="WARNING"):
            issues = Monitor(None)._find_issues([], [row], active)
        self.assertEqual(1, len(issues))
        self.assertEqual(("цена не дошла из автозагрузки в Авито",), issues[0].reasons)
        self.assertIn("Автозагрузка: <b>79 990", issue_example(issues[0]))
        self.assertIn("Авито: <b>81 990", issue_example(issues[0]))
        self.assertNotIn("подходящая строка не сопоставлена", issue_example(issues[0]))

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
