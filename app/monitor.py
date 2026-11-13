from __future__ import annotations

import csv
import fcntl
import hashlib
import html
import io
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GOOGLE_EXPORT = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?gid={gid}&tqx=out:csv"
GOOGLE_XLSX_EXPORT = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
AVITO_API = "https://api.avito.ru"
UTC = timezone.utc
XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class MonitorError(RuntimeError):
    pass


@dataclass(frozen=True)
class SheetSource:
    name: str
    spreadsheet_id: str
    gid: str
    kind: str = "strict_price"
    title_column: str = ""
    price_column: str = ""


@dataclass(frozen=True)
class Config:
    project_name: str
    client_sources: Tuple[SheetSource, ...]
    client_spreadsheet_ids: Tuple[str, ...]
    autoload_sources: Tuple[SheetSource, ...]
    autoload_spreadsheet_ids: Tuple[str, ...]
    telegram_token: str
    telegram_chat_id: str
    avito_client_id: str
    avito_client_secret: str
    state_path: Path
    check_interval_minutes: int
    mismatch_grace: timedelta
    notification_repeat: timedelta
    max_avito_pages: int
    dry_run: bool

    @classmethod
    def from_environment(cls) -> "Config":
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise MonitorError("Missing required environment variable: %s" % name)
            return value

        path = Path(required("MONITOR_CONFIG_PATH"))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise MonitorError("Monitor config is missing: %s" % path) from error
        except json.JSONDecodeError as error:
            raise MonitorError("Monitor config is not valid JSON: %s" % error) from error

        def sources(name: str) -> Tuple[SheetSource, ...]:
            values = raw.get(name)
            if not isinstance(values, list) or not values:
                raise MonitorError("config.%s must contain at least one sheet" % name)
            result: List[SheetSource] = []
            for value in values:
                try:
                    result.append(
                        SheetSource(
                            str(value["name"]),
                            str(value["spreadsheet_id"]),
                            str(value["gid"]),
                            str(value.get("kind", "strict_price")),
                            str(value.get("title_column", "")),
                            str(value.get("price_column", "")),
                        )
                    )
                except (KeyError, TypeError) as error:
                    raise MonitorError("Invalid entry in config.%s" % name) from error
            return tuple(result)

        client_spreadsheet_id = str(raw.get("client_spreadsheet_id") or "").strip()
        autoload_spreadsheet_id = str(raw.get("autoload_spreadsheet_id") or "").strip()
        # New projects only need the workbook itself.  The monitor discovers
        # every tab marked '(авито)' at each check.  client_sources is kept as
        # a backwards-compatible input for already configured projects.
        client_sources = (
            (SheetSource("Все листы (авито)", client_spreadsheet_id, ""),)
            if client_spreadsheet_id
            else sources("client_sources")
        )
        autoload_sources = (
            (SheetSource("Все листы Avito", autoload_spreadsheet_id, ""),)
            if autoload_spreadsheet_id
            else sources("autoload_sources")
        )
        # The client workbook also contains source and operational tabs.  Only
        # the prepared Avito-template tabs are a part of this monitor.
        invalid_client_sources = [source.name for source in client_sources if "(авито)" not in source.name.lower()]
        if invalid_client_sources:
            raise MonitorError(
                "Each client source must be an Avito template tab marked '(авито)': %s"
                % ", ".join(invalid_client_sources)
            )
        client_spreadsheet_ids = tuple(dict.fromkeys(source.spreadsheet_id for source in client_sources))
        autoload_spreadsheet_ids = tuple(dict.fromkeys(source.spreadsheet_id for source in autoload_sources))

        return cls(
            project_name=str(raw.get("project_name") or "Авито"),
            client_sources=client_sources,
            client_spreadsheet_ids=client_spreadsheet_ids,
            autoload_sources=autoload_sources,
            autoload_spreadsheet_ids=autoload_spreadsheet_ids,
            telegram_token=required("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=required("TELEGRAM_CHAT_ID"),
            avito_client_id=required("AVITO_CLIENT_ID"),
            avito_client_secret=required("AVITO_CLIENT_SECRET"),
            state_path=Path(os.getenv("STATE_PATH", "data/state.json")),
            check_interval_minutes=positive_int("CHECK_INTERVAL_MINUTES", 60),
            mismatch_grace=timedelta(hours=positive_int("MISMATCH_GRACE_HOURS", 5)),
            notification_repeat=timedelta(hours=positive_int("NOTIFICATION_REPEAT_HOURS", 5)),
            max_avito_pages=positive_int("MAX_AVITO_PAGES", 100),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        )


def positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise MonitorError("%s must be an integer" % name) from error
    if parsed <= 0:
        raise MonitorError("%s must be greater than zero" % name)
    return parsed


@dataclass(frozen=True)
class Product:
    key: str
    source_name: str
    row: int
    parameters: Dict[str, str]
    price: int

    @property
    def title(self) -> str:
        return self.parameters.get("title", "") or self.parameters.get("_display", "")


@dataclass(frozen=True)
class AutoloadRow:
    source_name: str
    row: int
    parameters: Dict[str, str]
    price: Optional[int]
    ad_id: Optional[str]
    url: str

    @property
    def title(self) -> str:
        return self.parameters.get("title", "")


@dataclass(frozen=True)
class AvitoItem:
    item_id: str
    title: str
    price: int
    status: str
    url: str


@dataclass(frozen=True)
class Issue:
    key: str
    product: Product
    autoload: Optional[AutoloadRow]
    avito: Optional[AvitoItem]
    reasons: Tuple[str, ...]
    match_note: str = ""
    match_category: str = "price_mismatch"

    def message(self, elapsed: timedelta) -> str:
        expected = format_price(self.product.price)
        autoload_price = format_price(self.autoload.price if self.autoload else None)
        avito_price = format_price(self.avito.price if self.avito else None)
        title = self.product.title or format_parameters(self.product.parameters)
        lines = [
            "⚠️ Расхождение цен Авито",
            "Проект: %s" % self.product.source_name,
            "Товар: %s" % title,
            "Клиентская таблица: %s" % expected,
            "Автозагрузка: %s" % autoload_price,
            "Авито: %s" % avito_price,
            "Проблема: %s" % "; ".join(self.reasons),
            "Сохраняется: %s" % format_duration(elapsed),
            "Источник: %s, строка %s" % (self.product.source_name, self.product.row),
        ]
        if self.autoload:
            lines.append("Автозагрузка: %s, строка %s" % (self.autoload.source_name, self.autoload.row))
            if self.autoload.url:
                lines.append("Объявление: %s" % self.autoload.url)
        elif self.avito:
            lines.append("Объявление: %s" % self.avito.url)
        return "\n".join(lines)


@dataclass(frozen=True)
class RunSummary:
    client_products: int
    autoload_rows: int
    active_avito_items: int
    issues: int
    alerts_sent: int
    resolved_sent: int


def http_text(url: str, method: str = "GET", data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None) -> str:
    request = Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:500]
        raise MonitorError("HTTP %s for %s: %s" % (error.code, url, body)) from error
    except URLError as error:
        raise MonitorError("Network error for %s: %s" % (url, error.reason)) from error


def http_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "avito-price-monitor/1.0"})
    try:
        with urlopen(request, timeout=60) as response:
            return response.read()
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:500]
        raise MonitorError("HTTP %s for %s: %s" % (error.code, url, body)) from error
    except URLError as error:
        raise MonitorError("Network error for %s: %s" % (url, error.reason)) from error


def fetch_sheet(source: SheetSource) -> List[List[str]]:
    url = GOOGLE_EXPORT.format(sheet_id=source.spreadsheet_id, gid=source.gid)
    text = http_text(url)
    return [list(row) for row in csv.reader(io.StringIO(text))]


def xlsx_column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    result = 0
    for character in letters:
        result = result * 26 + ord(character.upper()) - ord("A") + 1
    return result - 1


def xlsx_text(element: object) -> str:
    if element is None:
        return ""
    return "".join(
        getattr(node, "text", "") or ""
        for node in element.iter()
        if getattr(node, "tag", "").endswith("}t")
    )


def parse_xlsx_workbook(payload: bytes) -> List[Tuple[str, List[List[str]]]]:
    """Read a Google Sheets XLSX export without an extra runtime dependency."""
    import xml.etree.ElementTree as ET

    namespaces = {"x": XLSX_MAIN_NS, "r": XLSX_REL_NS, "pr": XLSX_PACKAGE_REL_NS}
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        shared: List[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [xlsx_text(item) for item in shared_root.findall("x:si", namespaces)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as error:
        raise MonitorError("Could not read Google Sheets workbook export") from error

    targets = {
        relation.attrib.get("Id", ""): relation.attrib.get("Target", "")
        for relation in relationships.findall("pr:Relationship", namespaces)
    }
    result: List[Tuple[str, List[List[str]]]] = []
    for sheet in workbook.findall("x:sheets/x:sheet", namespaces):
        name = sheet.attrib.get("name", "")
        relationship_id = sheet.attrib.get("{%s}id" % XLSX_REL_NS, "")
        target = targets.get(relationship_id, "")
        if not name or not target:
            continue
        try:
            root = ET.fromstring(archive.read("xl/" + target.lstrip("/")))
        except (KeyError, ET.ParseError) as error:
            raise MonitorError("Could not read worksheet '%s'" % name) from error
        target_path = target.lstrip("/")
        directory, filename = target_path.rsplit("/", 1)
        relation_path = "xl/%s/_rels/%s.rels" % (directory, filename)
        hyperlink_targets: Dict[str, str] = {}
        if relation_path in archive.namelist():
            relation_root = ET.fromstring(archive.read(relation_path))
            hyperlink_targets = {
                relation.attrib.get("Id", ""): relation.attrib.get("Target", "")
                for relation in relation_root.findall("pr:Relationship", namespaces)
            }
        hyperlinks = {
            hyperlink.attrib.get("ref", ""): (
                hyperlink.attrib.get("location", "")
                or hyperlink_targets.get(hyperlink.attrib.get("{%s}id" % XLSX_REL_NS, ""), "")
            )
            for hyperlink in root.findall("x:hyperlinks/x:hyperlink", namespaces)
        }
        values_by_row: Dict[int, Dict[int, str]] = {}
        for row in root.findall("x:sheetData/x:row", namespaces):
            row_number = int(row.attrib.get("r", "0") or 0)
            if not row_number:
                continue
            values = values_by_row.setdefault(row_number - 1, {})
            for node in row.findall("x:c", namespaces):
                column = xlsx_column_index(node.attrib.get("r", ""))
                kind = node.attrib.get("t", "")
                raw = node.findtext("x:v", default="", namespaces=namespaces)
                if kind == "s" and raw.isdigit() and int(raw) < len(shared):
                    values[column] = shared[int(raw)]
                elif kind == "inlineStr":
                    values[column] = xlsx_text(node.find("x:is", namespaces))
                else:
                    # SpreadsheetApp converts numeric 400.0 to "400". Preserve
                    # literal text, but give XLSX numeric cells the same form.
                    if kind in {"", "n"} and re.fullmatch(r"[+-]?\d+\.0+", raw):
                        values[column] = raw[:raw.index(".")]
                    else:
                        values[column] = raw
                hyperlink = hyperlinks.get(node.attrib.get("r", ""), "")
                if hyperlink.startswith(("https://", "http://")):
                    values[column] = hyperlink
        output: List[List[str]] = []
        for row_number in range(max(values_by_row, default=-1) + 1):
            values = values_by_row.get(row_number, {})
            output.append([values.get(index, "") for index in range(max(values, default=-1) + 1)])
        result.append((name, output))
    return result


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9+]", "", text)


def header_name(value: object) -> str:
    """The exact ``normalizeParamName`` contract of updatePricesFromPriceToAvito.

    Do not add heuristic header detection here.  The price-to-Avito Apps Script
    is the source of truth for which columns constitute one SKU.
    """
    value = re.sub(r"[^a-zа-я0-9]", "", str(value or "").lower()).replace("ё", "е")
    aliases = {
        "model": "model", "модель": "model",
        "memorysize": "memorysize", "встроеннаяпамять": "memorysize",
        "color": "color", "colour": "color", "цвет": "color",
        "ramsize": "ramsize", "оперативнаяпамять": "ramsize", "озу": "ramsize",
        "simconfig": "simconfig", "simкарты": "simconfig", "sim": "simconfig",
        "imei": "imei", "imeiтелефона": "imei",
        "devicehistory": "devicehistory", "историясмартфона": "devicehistory",
        "set": "set", "комплект": "set",
        "boxsealed": "boxsealed", "состояниекоробкителефона": "boxsealed",
        "akb": "akb", "акб": "akb", "состояниеаккумулятораiphone": "akb", "состояниеаккумулятора": "akb", "batteryhealth": "akb",
        "title": "title", "название": "title", "name": "title",
    }
    return aliases.get(value, value)


def is_price_header(value: object) -> bool:
    return "price" in str(value or "").lower()


def parse_price(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        candidate = float(value)
    else:
        matched = re.search(r"\d[\d\s]*[.,]?\d*", str(value))
        if not matched:
            return None
        candidate = float(matched.group(0).replace(" ", "").replace(",", "."))
    result = round(candidate)
    return result if 0 < result < 100_000_000 else None


def find_header_row(rows: Sequence[Sequence[str]]) -> Optional[int]:
    technical = {"model", "memorysize", "color", "simconfig", "ramsize", "imei", "devicehistory", "set", "boxsealed", "akb"}
    for index, row in enumerate(rows[:15]):
        if any(is_price_header(cell) for cell in row) and sum(header_name(cell) in technical for cell in row) >= 3:
            return index
    for index, row in enumerate(rows[:15]):
        if any(is_price_header(cell) for cell in row):
            return index
    return None


def cell(row: Sequence[str], index: int) -> str:
    if index >= len(row) or row[index] is None:
        return ""
    value = row[index]
    # SpreadsheetApp.getValues().toString() emits an integer as "400", not
    # Python's "400.0".  Keeping that display form is required for an exact
    # reproduction of the Apps Script comparison.
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def product_key(source: str, row: int, parameters: Dict[str, str]) -> str:
    payload = json.dumps([source, row, sorted(parameters.items())], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def extract_products(source: SheetSource, rows: Sequence[Sequence[str]]) -> List[Product]:
    if source.kind == "phone_cash":
        return extract_phone_cash_products(source, rows)
    if source.kind != "strict_price":
        raise MonitorError("Unsupported client sheet kind %s for %s" % (source.kind, source.name))
    header_row = find_header_row(rows)
    if header_row is None:
        raise MonitorError("No Price header found in client sheet: %s" % source.name)
    headers = rows[header_row]
    price_columns = [index for index, value in enumerate(headers) if is_price_header(value)]
    products: List[Product] = []
    for position, price_column in enumerate(price_columns):
        start = price_columns[position - 1] + 1 if position else 0
        fields = [
            (index, name)
            for index in range(start, price_column)
            if cell(headers, index)
            for name in (header_name(headers[index]),)
            if name not in CLIENT_METADATA_FIELDS
        ]
        for row_number, row in enumerate(rows[header_row + 1 :], start=header_row + 2):
            price = parse_price(cell(row, price_column))
            if price is None:
                continue
            parameters = {name: cell(row, index) for index, name in fields}
            if not any(parameters.values()):
                continue
            products.append(Product(product_key(source.name, row_number, parameters), source.name, row_number, parameters, price))
    return products


def fetch_avito_template_products(spreadsheet_id: str) -> List[Product]:
    """Return every client product from tabs explicitly marked '(авито)'."""
    payload = http_bytes(GOOGLE_XLSX_EXPORT.format(sheet_id=spreadsheet_id))
    products: List[Product] = []
    for name, rows in parse_xlsx_workbook(payload):
        if "(авито)" not in name.lower():
            continue
        products.extend(extract_products(SheetSource(name, spreadsheet_id, name), rows))
    return products


def named_tab_source(source: SheetSource) -> bool:
    """A registry project can opt into an explicitly named legacy tab."""
    return source.gid.startswith("tab:")


def fetch_named_client_products(sources: Sequence[SheetSource]) -> List[Product]:
    by_workbook: Dict[str, List[SheetSource]] = {}
    for source in sources:
        by_workbook.setdefault(source.spreadsheet_id, []).append(source)
    products: List[Product] = []
    for spreadsheet_id, workbook_sources in by_workbook.items():
        tabs = {name.casefold(): values for name, values in parse_xlsx_workbook(http_bytes(GOOGLE_XLSX_EXPORT.format(sheet_id=spreadsheet_id)))}
        for source in workbook_sources:
            tab_name = source.gid[4:]
            values = tabs.get(tab_name.casefold())
            if values is None:
                raise MonitorError("Configured client tab was not found: %s" % tab_name)
            products.extend(extract_products(source, values))
    return products


def is_avito_sheet_name(name: str) -> bool:
    value = name.lower()
    return "avito" in value or "авито" in value


def fetch_avito_autoload_rows(spreadsheet_id: str) -> List[AutoloadRow]:
    """Return every autoload row from tabs explicitly marked Avito/Авито."""
    payload = http_bytes(GOOGLE_XLSX_EXPORT.format(sheet_id=spreadsheet_id))
    rows: List[AutoloadRow] = []
    for name, values in parse_xlsx_workbook(payload):
        if not is_avito_sheet_name(name):
            continue
        rows.extend(extract_autoload_rows(SheetSource(name, spreadsheet_id, name), values))
    return rows


def fetch_named_autoload_rows(sources: Sequence[SheetSource]) -> List[AutoloadRow]:
    by_workbook: Dict[str, List[SheetSource]] = {}
    for source in sources:
        by_workbook.setdefault(source.spreadsheet_id, []).append(source)
    rows: List[AutoloadRow] = []
    for spreadsheet_id, workbook_sources in by_workbook.items():
        tabs = {name.casefold(): values for name, values in parse_xlsx_workbook(http_bytes(GOOGLE_XLSX_EXPORT.format(sheet_id=spreadsheet_id)))}
        for source in workbook_sources:
            tab_name = source.gid[4:]
            values = tabs.get(tab_name.casefold())
            if values is None:
                raise MonitorError("Configured autoload tab was not found: %s" % tab_name)
            rows.extend(extract_autoload_rows(source, values))
    return rows


COLOR_ALIASES = {
    "black": "черный",
    "white": "белый",
    "pink": "розовый",
    "green": "зеленый",
    "yellow": "желтый",
    "red": "красный",
    "gray": "серый",
    "grey": "серый",
    "violet": "фиолетовый",
    "purple": "фиолетовый",
    "teal": "голубой",
    "ultramarine": "голубой",
    "midnight": "черный",
    "starlight": "белый",
    "dtitanium": "золотистый",
    "ntitanium": "серый",
}


MATCH_FIELDS = ("model", "memorysize", "ramsize", "color", "simconfig", "akb")
FIELD_LABELS = {
    "model": "Модель",
    "memorysize": "Память",
    "ramsize": "RAM",
    "color": "Цвет",
    "simconfig": "SIM",
    "akb": "АКБ",
    "title": "Название",
}
UNKNOWN_VALUES = {"", "незнаю", "unknown", "na", "n/a"}
# This column explains why a price is blank in the client template. It is
# operational metadata, not a product parameter exported to Avito.
CLIENT_METADATA_FIELDS = {
    "причинабезцены", "причинаотсутствияцены",
    "reasonwithoutprice", "missingpricereason",
}


def is_unknown(value: object) -> bool:
    return normalized(value) in UNKNOWN_VALUES


def canonical_memory(value: object) -> str:
    """Normalise only an unambiguous storage capacity; keep arbitrary text strict."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(гб|gb|тб|tb)", text, re.IGNORECASE)
    if not match:
        return normalized(text)
    amount = float(match.group(1).replace(",", "."))
    if match.group(2).casefold() in {"тб", "tb"}:
        amount *= 1024
    return "%sgb" % (int(amount) if amount.is_integer() else amount)


def canonical_sim(value: object) -> str:
    text = normalized(value)
    if text in UNKNOWN_VALUES:
        return ""
    if text in {"2sim", "dualsim", "двесим"}:
        return "2sim"
    if text in {"simesim", "simплюсesim"}:
        return "sim+esim"
    if text in {"esim", "толькоesim"}:
        return "esim"
    return text


def canonical_model(value: object, vendor: object = "") -> str:
    model = normalized(value)
    brand = normalized(vendor)
    if brand and model.startswith(brand) and len(model) > len(brand):
        model = model[len(brand):]
    # These are established model aliases, not a fuzzy title comparison.
    return {"galaxys26+": "galaxys26plus", "iphoneair": "iphone17air"}.get(model, model)


def canonical_color(value: object, model: object = "") -> str:
    """Translate only known spelling/language aliases; never merge distinct finishes."""
    return phone_color(str(value or ""), str(model or ""))


def canonical_parameter(name: str, value: object, model: object = "", vendor: object = "") -> str:
    if name == "model":
        return canonical_model(value, vendor)
    if name in {"memorysize", "ramsize"}:
        return canonical_memory(value)
    if name == "simconfig":
        return canonical_sim(value)
    if name == "color":
        return canonical_color(value, model)
    return normalized(value)


def describe_difference(name: str, client_value: str, autoload_value: str) -> str:
    label = FIELD_LABELS.get(name, name)
    if not autoload_value:
        return "%s: у клиента «%s», в автозагрузке пусто" % (label, client_value)
    return "%s: «%s» ↔ «%s»" % (label, client_value, autoload_value)


MATCH_CATEGORY_LABELS = {
    "different_sim": "Товар найден, отличается SIM",
    "different_key_parameter": "Товар найден, отличается ключевой параметр",
    "ambiguous_match": "Неоднозначное сопоставление",
    "algorithm_error": "Не воспроизведено правилами автозагрузки",
}


def classify_unmatched_client_row(row: AutoloadRow, products: Sequence[Product]) -> Tuple[str, str]:
    """Diagnose a miss using the same raw fields and strict equality as source.

    This never creates a substitute SKU.  A miss is a failure to reproduce the
    autoload matcher, not evidence that the client product is absent.
    """
    fields_without_sim = ("vendor", "model", "memorysize", "ramsize", "color")
    nearby = [p for p in products if all(
        name in p.parameters and name in row.parameters and strict_equal(p.parameters[name], row.parameters[name])
        for name in fields_without_sim
    )]
    if len(nearby) > 1:
        locations = ", ".join("%s:%s" % (p.source_name, p.row) for p in nearby[:3])
        return "ambiguous_match", "Неоднозначное сопоставление: найдено %s кандидата (%s)." % (len(nearby), locations)
    if len(nearby) == 1:
        candidate = nearby[0]
        client_sim = candidate.parameters.get("simconfig", "")
        row_sim = row.parameters.get("simconfig", "")
        location = "лист «%s», строка %s" % (candidate.source_name, candidate.row)
        if not strict_equal(client_sim, row_sim):
            return "different_sim", "Товар найден: %s. Отличается SIM: «%s» ↔ «%s»." % (location, client_sim or "пусто", row_sim or "пусто")
        return "algorithm_error", "Найден кандидат %s со всеми ключевыми параметрами; требуется проверка алгоритма." % location
    return "algorithm_error", "Строка не воспроизведена правилами автозагрузки; это не означает отсутствия товара у клиента."


def unmatched_client_note(row: AutoloadRow, products: Sequence[Product]) -> str:
    """Explain a failed strict match without selecting or changing a client row.

    The result is deliberately diagnostic: even one semantically identical row
    remains an unconfirmed candidate until the strict matcher agrees.
    """
    row_model = row.parameters.get("model", "")
    if row_model:
        candidates = [
            product for product in products
            if product.parameters.get("model")
            and canonical_model(product.parameters["model"], product.parameters.get("vendor", ""))
            == canonical_model(row_model, row.parameters.get("vendor", ""))
        ]
    else:
        row_title = row.parameters.get("title", "")
        candidates = [
            product for product in products
            if row_title and product.parameters.get("title")
            and normalized(product.parameters["title"]) == normalized(row_title)
        ]
    if not candidates:
        subject = "модель «%s»" % row_model if row_model else "название «%s»" % row.parameters.get("title", "")
        return "Кандидаты в клиентской таблице не найдены: %s." % subject

    ranked = []
    for product in candidates:
        differences = []
        raw_only = []
        for name, client_value in product.parameters.items():
            if name.startswith("_") or not client_value:
                continue
            autoload_value = row.parameters.get(name, "")
            if strict_equal(client_value, autoload_value):
                continue
            if canonical_parameter(name, client_value, product.parameters.get("model", ""), product.parameters.get("vendor", "")) == canonical_parameter(
                name, autoload_value, row.parameters.get("model", ""), row.parameters.get("vendor", "")
            ):
                raw_only.append(name)
            else:
                differences.append(describe_difference(name, client_value, autoload_value))
        ranked.append((len(differences), len(raw_only), product, differences, raw_only))

    ranked.sort(key=lambda item: (item[0], item[1], item[2].source_name, item[2].row))
    best_difference_count, _, best, differences, raw_only = ranked[0]
    equally_close = [item for item in ranked if item[:2] == ranked[0][:2]]
    location = "лист «%s», строка %s" % (best.source_name, best.row)
    if len(equally_close) > 1:
        locations = ", ".join("%s:%s" % (item[2].source_name, item[2].row) for item in equally_close[:3])
        return "Найдено %s одинаково близких кандидата (%s); автоматически не выбрано." % (len(equally_close), locations)
    if best_difference_count:
        return "Возможный кандидат: %s. Отличается: %s. Автоматически не сопоставлено." % (
            location, "; ".join(differences[:3])
        )
    if raw_only:
        labels = ", ".join(FIELD_LABELS.get(name, name) for name in raw_only[:3])
        return "Возможный кандидат: %s. SKU совпадает после нормализации (%s); автоматически не сопоставлено." % (
            location, labels
        )
    return "Возможный кандидат: %s; автоматически не сопоставлено." % location


def phone_color(raw: str, model: str) -> str:
    raw = normalized(raw)
    if raw == "blue":
        # The Avito templates call blue of ordinary iPhones "голубой" and
        # blue of Pro models "синий".
        generation = re.search(r"iphone(\d+)", normalized(model))
        legacy_blue = generation is not None and int(generation.group(1)) <= 13
        return "синий" if "pro" in normalized(model) or legacy_blue else "голубой"
    return COLOR_ALIASES.get(raw, raw)


def parse_phone_title(title: str) -> Optional[Dict[str, str]]:
    """Turn LimeStore's compact title into the fields of an Avito phone row."""
    value = re.sub(r"\s+", " ", title).strip()
    if not value:
        return None
    tokens = value.split(" ")
    color = tokens[-1]
    without_color = " ".join(tokens[:-1])
    memory_match = re.search(r"(?:/|\s)(\d{2,4})\s*(gb|гб|tb|тб)?$", without_color, re.IGNORECASE)
    if not memory_match:
        return None
    amount, unit = memory_match.group(1), (memory_match.group(2) or "gb").casefold()
    memory = str(int(amount) * 1024) + " гб" if unit in {"tb", "тб"} else str(int(amount)) + " гб"
    model = without_color[: memory_match.start()].rstrip(" /,-")
    if not model:
        return None
    if normalized(model).startswith("phone"):
        model = "i" + model
    if re.match(r"^\d", model):
        model = "iPhone " + model
    return {"model": model, "memorysize": memory, "color": phone_color(color, model)}


def find_column(headers: Sequence[str], requested: str) -> Optional[int]:
    wanted = normalized(requested)
    return next((index for index, header in enumerate(headers) if normalized(header) == wanted), None)


def extract_phone_cash_products(source: SheetSource, rows: Sequence[Sequence[str]]) -> List[Product]:
    if not rows:
        return []
    headers = rows[0]
    title_column = find_column(headers, source.title_column or "НОВЫЙ")
    price_column = find_column(headers, source.price_column or "Lime Store Наличка")
    if title_column is None or price_column is None:
        raise MonitorError("Cash layout columns are missing in client sheet: %s" % source.name)
    products: List[Product] = []
    for row_number, row in enumerate(rows[1:], start=2):
        title = cell(row, title_column)
        price = parse_price(cell(row, price_column))
        parameters = parse_phone_title(title)
        if price is None or parameters is None:
            continue
        # Store the compact client title only for readable notifications.  It
        # must not participate in the strict match against Avito's own title.
        parameters["_display"] = title
        products.append(Product(product_key(source.name, row_number, parameters), source.name, row_number, parameters, price))
    return products


def extract_ad_id(headers: Sequence[str], row: Sequence[str]) -> Tuple[Optional[str], str]:
    fallback_id: Optional[str] = None
    for index, value in enumerate(headers):
        name = normalized(value)
        item = cell(row, index)
        if not item:
            continue
        if "ссылка" in name and ("объявлен" in name or "обьявлен" in name):
            match = re.search(r"(?:_|/)(\d{5,})(?:[/?#]|$)", item)
            if match:
                return match.group(1), item
        if name in {"номеробъявления", "номеробьявления", "объявлениеid", "обьявлениеid", "avitoid"}:
            # Google XLSX exports numeric IDs in scientific notation. Decimal
            # converts them without floating-point rounding of identifiers.
            from decimal import Decimal, InvalidOperation
            try:
                number = Decimal(item)
                if number.is_finite() and number == number.to_integral_value() and 10000 <= number < 10 ** 20:
                    fallback_id = str(int(number))
            except (InvalidOperation, ValueError, OverflowError):
                pass
    return fallback_id, ""


def extract_autoload_rows(source: SheetSource, rows: Sequence[Sequence[str]]) -> List[AutoloadRow]:
    header_row = find_header_row(rows)
    if header_row is None:
        logging.warning("Skipping autoload sheet without Price header: %s", source.name)
        return []
    headers = rows[header_row]
    price_column = next((index for index, value in enumerate(headers) if is_price_header(value)), None)
    if price_column is None:
        return []
    fields = [
        (index, name)
        for index, value in enumerate(headers)
        if index != price_column and cell(headers, index)
        for name in (header_name(value),)
        if name not in CLIENT_METADATA_FIELDS
    ]
    result: List[AutoloadRow] = []
    for row_number, row in enumerate(rows[header_row + 1 :], start=header_row + 2):
        parameters = {name: cell(row, index) for index, name in fields}
        if not any(parameters.values()):
            continue
        ad_id, url = extract_ad_id(headers, row)
        # Avito exports have a technical header row (Price, avitoid, …) below
        # a friendly Russian row containing "Ссылка на объявление".  Keep the
        # technical row for matching, but use the friendly row to recover the
        # clickable listing link from an XLSX export.
        if not ad_id and header_row > 0:
            ad_id, url = extract_ad_id(rows[header_row - 1], row)
        result.append(AutoloadRow(source.name, row_number, parameters, parse_price(cell(row, price_column)), ad_id, url))
    return result


def strict_equal(left: str, right: str) -> bool:
    """Exact equivalent of the Apps Script's compareStrictValues."""
    def script_value(value: object) -> str:
        # normalizeValue in the existing price-to-Avito script does not use
        # Unicode compatibility folding or replace ё before its character
        # filter. Keep this comparison separate from diagnostic normalization.
        return re.sub(r"[^a-zа-я0-9+]", "", str(value or "").lower().strip())

    return script_value(left) == script_value(right)


def product_matches(product: Product, row: AutoloadRow) -> bool:
    """Apply the two checks in updatePricesFromPriceToAvito."""
    for name, value in product.parameters.items():
        if name.startswith("_") or name in CLIENT_METADATA_FIELDS:
            continue
        # The source script checks every price-table parameter, including an
        # empty one: a missing autoload column is not the same as an empty cell.
        if name not in row.parameters:
            return False
        if not strict_equal(value, row.parameters[name]):
            return False
    # Its second pass treats only a non-empty AKB from the Avito row as extra
    # critical data that must also be present in the price table.
    for name, value in row.parameters.items():
        if name == "akb" and value and (not product.parameters.get(name) or not strict_equal(product.parameters[name], value)):
            return False
    return True


def semantic_product_matches(product: Product, row: AutoloadRow) -> bool:
    """Match equivalent SKU values when the source script's text differs.

    Every client field must still be present, and a non-empty Avito AKB must
    agree.  A brand prefix in Model is ignored only if Vendor agrees on both
    sides.  This is used only after the source script's exact match fails.
    """
    vendor = product.parameters.get("vendor", "")
    row_vendor = row.parameters.get("vendor", "")
    same_vendor = bool(vendor and row_vendor and normalized(vendor) == normalized(row_vendor))
    if not product.parameters.get("model") or not row.parameters.get("model"):
        return False
    for name, value in product.parameters.items():
        if name.startswith("_") or name in CLIENT_METADATA_FIELDS:
            continue
        if name not in row.parameters:
            return False
        if name == "model" and not same_vendor:
            left = canonical_model(value)
            right = canonical_model(row.parameters[name])
        else:
            brand = vendor if same_vendor else ""
            left = canonical_parameter(name, value, product.parameters.get("model", ""), brand)
            right = canonical_parameter(name, row.parameters[name], row.parameters.get("model", ""), brand)
        if left != right:
            return False
    akb = row.parameters.get("akb", "")
    if akb and (not product.parameters.get("akb") or canonical_parameter("akb", product.parameters["akb"]) != canonical_parameter("akb", akb)):
        return False
    return True


def matching_client_products(row: AutoloadRow, products: Sequence[Product]) -> List[Product]:
    """Prefer the source script's match, then one unambiguous equivalent SKU."""
    exact = [product for product in products if product_matches(product, row)]
    if exact:
        return exact
    equivalent = [product for product in products if semantic_product_matches(product, row)]
    return equivalent if len(equivalent) == 1 else []


def is_used_item(row: AutoloadRow) -> bool:
    """Return False for manually published second-hand listings.

    The autoload uses these condition values for second-hand devices.  A blank
    condition is retained: some supported categories do not expose this field.
    """
    condition = row.parameters.get("condition", "") or row.parameters.get("состояние", "")
    return normalized(condition) not in {"удовлетворительное", "хорошее", "отличное"}


def get_avito_token(config: Config) -> str:
    data = urlencode({"grant_type": "client_credentials", "client_id": config.avito_client_id, "client_secret": config.avito_client_secret}).encode()
    payload = json.loads(http_text(AVITO_API + "/token", method="POST", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}))
    token = payload.get("access_token")
    if not token:
        raise MonitorError("Avito token response did not contain access_token")
    return str(token)


def avito_json(url: str, headers: Dict[str, str]) -> Dict[str, object]:
    """Fetch an Avito response without turning a temporary rate limit into an alert storm."""
    last_error: Optional[MonitorError] = None
    for attempt in range(5):
        try:
            payload = json.loads(http_text(url, headers=headers))
            if not isinstance(payload, dict):
                raise MonitorError("Unexpected Avito response")
            return payload
        except (json.JSONDecodeError, MonitorError) as error:
            last_error = error if isinstance(error, MonitorError) else MonitorError("Invalid JSON from Avito")
            # The public API returns 429 occasionally when pages are requested
            # too quickly.  Wait and retry rather than reporting every listing
            # as absent.
            if "HTTP 429" not in str(last_error) or attempt == 4:
                raise last_error
            retry_after = re.search(r"retry after\s*(\d+)", str(last_error), re.IGNORECASE)
            time.sleep(min(int(retry_after.group(1)) if retry_after else 2 ** attempt, 30))
    raise last_error or MonitorError("Avito request failed")


def fetch_active_avito_items(config: Config) -> Dict[str, AvitoItem]:
    token = get_avito_token(config)
    headers = {"Authorization": "Bearer " + token}
    result: Dict[str, AvitoItem] = {}
    incomplete_items = 0
    for page in range(1, config.max_avito_pages + 1):
        if page > 1:
            # Keep the regular hourly scan well under the listing endpoint's
            # burst limit.
            time.sleep(1)
        payload = avito_json(AVITO_API + "/core/v1/items?page=" + str(page), headers)
        resources = payload.get("resources", [])
        if not isinstance(resources, list):
            raise MonitorError("Unexpected Avito item list response")
        for item in resources:
            if not isinstance(item, dict):
                incomplete_items += 1
                continue
            try:
                parsed = AvitoItem(str(item["id"]), str(item.get("title", "")), int(item["price"]), str(item.get("status", "")), str(item.get("url", "")))
            except (KeyError, TypeError, ValueError) as error:
                # Avito occasionally returns a partial resource without a
                # price.  It cannot be compared reliably, so ignore only that
                # resource instead of aborting monitoring for the whole city.
                incomplete_items += 1
                logging.warning("Skipping Avito resource without comparable price: %s", error)
                continue
            if parsed.status == "active":
                result[parsed.item_id] = parsed
        if len(resources) < int(payload.get("meta", {}).get("per_page", 25)):
            if incomplete_items:
                logging.warning("Skipped %s incomplete Avito resources", incomplete_items)
            return result
    raise MonitorError("Avito pagination exceeded MAX_AVITO_PAGES; no notifications sent")


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def load_state(path: Path) -> Dict[str, Dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise MonitorError("State file is invalid JSON: %s" % error) from error
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: Dict[str, Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def format_price(value: Optional[int]) -> str:
    return ("{:,}".format(value).replace(",", " ") + " ₽") if value is not None else "не найдено"


def format_duration(value: timedelta) -> str:
    hours = int(value.total_seconds() // 3600)
    minutes = int((value.total_seconds() % 3600) // 60)
    return "%s ч %s мин" % (hours, minutes)


def format_parameters(values: Dict[str, str]) -> str:
    return ", ".join("%s: %s" % pair for pair in values.items() if pair[1])


def telegram_request(config: Config, method: str, payload: Dict[str, str]) -> object:
    """Call Telegram without allowing the bot token into diagnostic logs."""
    try:
        raw = http_text(
            "https://api.telegram.org/bot%s/%s" % (config.telegram_token, method),
            method="POST",
            data=urlencode(payload).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except MonitorError as error:
        raise MonitorError(str(error).replace(config.telegram_token, "<redacted>")) from error
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MonitorError("Telegram returned an invalid response") from error
    if not response.get("ok"):
        raise MonitorError("Telegram rejected %s" % method)
    return response.get("result")


def send_telegram(config: Config, text: str) -> Optional[int]:
    if config.dry_run:
        logging.info("DRY RUN Telegram notification:\n%s", text)
        return None
    # Use the project-specific state-file name as a stable project identity.
    # Fail closed if a project has no topic rather than posting to General.
    mapping_path = config.state_path.parent / "telegram-topics.json"
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        topic_id = mapping.get(config.state_path.name)
    except (OSError, json.JSONDecodeError, AttributeError) as error:
        raise MonitorError("Telegram project topic mapping is unavailable") from error
    if not isinstance(topic_id, int) or isinstance(topic_id, bool) or topic_id <= 0:
        raise MonitorError("Telegram topic is not configured for %s" % config.project_name)
    result = telegram_request(config, "sendMessage", {
        "chat_id": config.telegram_chat_id,
        "message_thread_id": str(topic_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
        raise MonitorError("Telegram did not return a message ID")
    return result["message_id"]


def delete_telegram(config: Config, message_id: int) -> None:
    if config.dry_run:
        logging.info("DRY RUN Telegram message deletion %s", message_id)
        return
    telegram_request(config, "deleteMessage", {
        "chat_id": config.telegram_chat_id,
        "message_id": str(message_id),
    })


def issue_example(issue: Issue) -> str:
    """A scan-friendly example card within a project digest."""
    title = issue.product.title or format_parameters(issue.product.parameters)
    lines = ["<b>%s</b>" % html.escape(title)]
    if issue.product.source_name == "Авито":
        lines.extend((
            "📥 Автозагрузка: <b>строка не найдена</b>",
            "📣 Авито: <b>%s</b>" % html.escape(format_price(issue.avito.price if issue.avito else None)),
        ))
    elif issue.product.source_name == "Автозагрузка":
        parameters = issue.autoload.parameters if issue.autoload else {}
        color = parameters.get("color", "")
        sim_config = parameters.get("simconfig", "")
        if color:
            lines.append("🎨 Цвет: <b>%s</b>" % html.escape(color))
        if sim_config:
            lines.append("📱 SIM: <b>%s</b>" % html.escape(sim_config))
        lines.extend((
            "👤 Клиентская таблица: <b>подходящая строка не сопоставлена</b>",
            "📥 Автозагрузка: <b>%s</b>" % html.escape(format_price(issue.autoload.price if issue.autoload else None)),
            "📣 Авито: <b>%s</b>" % html.escape(format_price(issue.avito.price if issue.avito else None)),
        ))
        if issue.match_note:
            lines.append("🔎 %s" % html.escape(issue.match_note))
        if issue.match_category != "price_mismatch":
            lines.append("🏷 <b>%s</b>" % html.escape(MATCH_CATEGORY_LABELS.get(issue.match_category, issue.match_category)))
    else:
        lines.extend((
            "👤 Клиент: <b>%s</b>" % html.escape(format_price(issue.product.price)),
            "📥 Автозагрузка: <b>%s</b>" % html.escape(format_price(issue.autoload.price if issue.autoload else None)),
            "📣 Авито: <b>%s</b>" % html.escape(format_price(issue.avito.price if issue.avito else None)),
        ))
    url = (issue.autoload.url if issue.autoload and issue.autoload.url else issue.avito.url if issue.avito else "")
    if url.startswith(("https://", "http://")):
        lines.append('🔗 <a href="%s">Открыть объявление</a>' % html.escape(url, quote=True))
    return "\n".join(lines)


def position_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "позиция"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return "позиции"
    return "позиций"


def issue_digests(
    project_name: str,
    issues: Sequence[Issue],
    grace: timedelta,
    checked_at: datetime,
    limit: int = 3900,
) -> List[str]:
    """Return the complete project report, split only at Telegram's hard limit."""
    client_to_autoload = sum(
        "цена не дошла из клиентской таблицы в автозагрузку" in issue.reasons for issue in issues
    )
    autoload_to_avito = sum(
        "цена не дошла из автозагрузки в Авито" in issue.reasons for issue in issues
    )
    missing = sum(any(
        marker in reason.casefold()
        for reason in issue.reasons
        for marker in ("не найдено", "нет id", "не активно", "нет соответствующей")
    ) for issue in issues)
    lines = [
        "⚠️ <b>Проверь цены</b>",
        "🏪 <b>%s</b>" % html.escape(project_name),
        "🕐 <b>Проверка:</b> %s МСК" % checked_at.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m, %H:%M"),
        "",
        "<b>%s %s</b> не синхронизировались за %s." % (len(issues), position_word(len(issues)), format_duration(grace)),
    ]
    stages: List[str] = []
    if client_to_autoload:
        stages.append("👤 Клиентская таблица → автозагрузка: <b>%s</b>" % client_to_autoload)
    if autoload_to_avito:
        stages.append("📥 Автозагрузка → Авито: <b>%s</b>" % autoload_to_avito)
    if missing:
        stages.append("🔗 Нет строки или активного объявления: <b>%s</b>" % missing)
    if stages:
        lines.extend(("", "📍 <b>Где не обновилось</b>", *stages))
    categories = {}
    for issue in issues:
        if issue.match_category != "price_mismatch":
            categories[issue.match_category] = categories.get(issue.match_category, 0) + 1
    if categories:
        lines.extend(("", "🧭 <b>Несопоставленные товары</b>"))
        for category, count in sorted(categories.items()):
            lines.append("• %s: <b>%s</b>" % (MATCH_CATEGORY_LABELS.get(category, category), count))
    lines.extend(("", "📋 <b>Что проверить</b>"))
    heading = "\n".join(lines)
    continuation = "\n".join((
        "⚠️ <b>Проверь цены</b>",
        "🏪 <b>%s</b>" % html.escape(project_name),
        "",
        "📋 <b>Что проверить, продолжение</b>",
    ))
    batches: List[str] = []
    current = heading
    for issue in issues:
        item = issue_example(issue)
        if len(current) + len(item) + 2 > limit and current != heading:
            batches.append(current)
            current = continuation + "\n\n" + item
        else:
            current += "\n\n" + item
    batches.append(current)
    return batches


def issue_signature(issues: Sequence[Issue]) -> str:
    """A stable report identity which intentionally excludes the check time."""
    items = []
    for issue in issues:
        items.append((
            issue.key,
            issue.product.price,
            issue.autoload.price if issue.autoload else None,
            issue.avito.price if issue.avito else None,
            issue.reasons,
            issue.match_note,
        ))
    encoded = json.dumps(sorted(items), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def report_message_ids(meta: Dict[str, str]) -> List[int]:
    """Read both the current list format and the former single-card format."""
    raw_ids = meta.get("report_message_ids")
    values = raw_ids if isinstance(raw_ids, list) else [meta.get("report_message_id", "")]
    result: List[int] = []
    for value in values:
        try:
            message_id = int(str(value))
        except (TypeError, ValueError):
            continue
        if message_id > 0:
            result.append(message_id)
    return result


def replace_telegram_report(
    config: Config,
    previous_ids: Sequence[int],
    messages: Sequence[str],
) -> Tuple[List[int], int]:
    """Send a fresh report, then remove its previous version where permitted."""
    active_ids = []
    for text in messages:
        message_id = send_telegram(config, text)
        if message_id is not None:
            active_ids.append(message_id)
    for previous_id in previous_ids:
        try:
            delete_telegram(config, previous_id)
        except MonitorError as error:
            logging.info("Could not remove previous Telegram report card %s: %s", previous_id, error)
    return active_ids, len(active_ids)


class Monitor:
    def __init__(self, config: Config):
        self.config = config

    def run_once(self, force_notify: bool = False) -> RunSummary:
        named_client_sources = [source for source in self.config.client_sources if named_tab_source(source)]
        if named_client_sources:
            client = fetch_named_client_products(named_client_sources)
        else:
            client = []
            for spreadsheet_id in self.config.client_spreadsheet_ids:
                client.extend(fetch_avito_template_products(spreadsheet_id))
        named_autoload_sources = [source for source in self.config.autoload_sources if named_tab_source(source)]
        if named_autoload_sources:
            autoload = fetch_named_autoload_rows(named_autoload_sources)
        else:
            autoload = []
            for spreadsheet_id in self.config.autoload_spreadsheet_ids:
                autoload.extend(fetch_avito_autoload_rows(spreadsheet_id))
        active = fetch_active_avito_items(self.config)
        issues = self._find_issues(client, autoload, active)
        alerts, resolved = self._notify(issues, force_notify=force_notify)
        return RunSummary(len(client), len(autoload), len(active), len(issues), alerts, resolved)

    def _find_issues(self, products: Sequence[Product], rows: Sequence[AutoloadRow], active: Dict[str, AvitoItem]) -> List[Issue]:
        issues: List[Issue] = []
        unmatched_count = 0
        unmatched_examples: List[str] = []
        monitored_rows = [row for row in rows if is_used_item(row)]
        # A client price can legitimately remain for an archived or unpublished
        # product. Only autoload rows confirmed active by Avito are checked.
        active_autoload = {row.ad_id: row for row in monitored_rows if row.ad_id and row.ad_id in active}
        for item in active.values():
            row = active_autoload.get(item.item_id)
            if row is None:
                continue
            matches = matching_client_products(row, products)
            if matches:
                product = matches[0]
                reasons: List[str] = []
                if row.price != product.price:
                    reasons.append("цена не дошла из клиентской таблицы в автозагрузку")
                if row.price != item.price:
                    reasons.append("цена не дошла из автозагрузки в Авито")
                if reasons:
                    issues.append(Issue("%s:%s" % (product.key, item.item_id), product, row, item, tuple(reasons)))
            else:
                # An unlinked row does not establish a price discrepancy.
                # Keep a diagnostic in server logs instead of telling the
                # client that a product or price is missing from their table.
                unmatched_count += 1
                if len(unmatched_examples) < 3:
                    note = unmatched_client_note(row, products)
                    unmatched_examples.append("%s:%s (Avito %s): %s" % (row.source_name, row.row, item.item_id, note))
        if unmatched_count:
            logging.warning("Skipped %s active listings without a confident client match; examples: %s", unmatched_count, " | ".join(unmatched_examples))
        return issues

    def _notify(self, issues: Sequence[Issue], force_notify: bool = False) -> Tuple[int, int]:
        # A scheduled check and a manually requested report can finish at the
        # same time.  Serialise only the state/update phase so they cannot
        # both read the same prior report and send duplicates.
        lock_path = self.config.state_path.with_suffix(self.config.state_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return self._notify_locked(issues, force_notify=force_notify)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _notify_locked(self, issues: Sequence[Issue], force_notify: bool = False) -> Tuple[int, int]:
        state = load_state(self.config.state_path)
        current = now_utc()
        meta = state.setdefault("__monitor__", {})
        if not isinstance(meta, dict):
            meta = {}
            state["__monitor__"] = meta
        current_keys = {issue.key for issue in issues}
        due: List[Tuple[Issue, Dict[str, str], timedelta]] = []
        for issue in issues:
            entry = state.get(issue.key)
            if not entry:
                entry = {"first_seen": current.isoformat(), "notified": "false"}
                state[issue.key] = entry
            elapsed = current - parse_timestamp(entry["first_seen"])
            # Once a card has been deliberately sent (for example, for a
            # requested manual check), keep it live during the usual grace
            # period too.  Otherwise the very next routine check would remove
            # a card that was just sent early.
            if force_notify or entry.get("notified") == "true" or elapsed >= self.config.mismatch_grace:
                due.append((issue, entry, elapsed))

        # One project owns one complete current report. It may need several
        # Telegram messages solely because Telegram limits each one to 4,096
        # characters. The IDs let us delete its previous full report later.
        previous_message_ids = report_message_ids(meta)
        previous_signature = str(meta.get("report_signature", ""))
        last_digest_value = str(meta.get("last_digest_at", ""))
        last_digest = parse_timestamp(last_digest_value) if last_digest_value else None

        alerts_sent = 0
        if due:
            signature = issue_signature([issue for issue, _, _ in due])
            # Manual delivery is useful for a one-off requested check, but a
            # second click immediately afterwards must not duplicate a full
            # multi-message report.
            manual_refresh = force_notify and (
                last_digest is None or current - last_digest >= timedelta(minutes=5)
            )
            needs_refresh = (
                manual_refresh
                or
                previous_signature != signature
                or last_digest is None
                or current - last_digest >= self.config.notification_repeat
            )
            # A message created by a version before this change has no stored
            # ID, so leave it alone until its normal repeat window expires.
            if not previous_message_ids and last_digest is not None and current - last_digest < self.config.notification_repeat and not force_notify:
                needs_refresh = False
            if needs_refresh:
                messages = issue_digests(
                    self.config.project_name,
                    [issue for issue, _, _ in due],
                    self.config.mismatch_grace,
                    current,
                )
                active_ids, new_messages = replace_telegram_report(self.config, previous_message_ids, messages)
                if active_ids:
                    meta["report_message_ids"] = [str(message_id) for message_id in active_ids]
                    meta.pop("report_message_id", None)
                meta["report_signature"] = signature
                meta["last_digest_at"] = current.isoformat()
                alerts_sent += new_messages
            for _, entry, _ in due:
                entry["notified"] = "true"

        resolved_keys: List[str] = []
        for key in list(state):
            if key == "__monitor__":
                continue
            if key in current_keys:
                continue
            entry = state[key]
            if entry.get("notified") == "true":
                resolved_keys.append(key)
            del state[key]

        # When the problem is fixed, remove its last report if Telegram allows
        # it. An old message that cannot be deleted is intentionally left as
        # is; the bot never rewrites historical reports.
        if not due and previous_message_ids:
            for previous_message_id in previous_message_ids:
                try:
                    delete_telegram(self.config, previous_message_id)
                except MonitorError as error:
                    logging.info(
                        "Could not delete resolved Telegram card for %s: %s",
                        self.config.project_name,
                        error,
                    )
            meta.pop("report_message_ids", None)
            meta.pop("report_message_id", None)
            meta.pop("report_signature", None)
            meta.pop("last_digest_at", None)
        save_state(self.config.state_path, state)
        return alerts_sent, len(resolved_keys)
