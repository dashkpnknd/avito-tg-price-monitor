import unittest

from app.monitor import (
    AutoloadRow,
    Product,
    SheetSource,
    extract_autoload_rows,
    extract_products,
    notification_batches,
    parse_phone_title,
    product_matches,
)


class MatchingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
