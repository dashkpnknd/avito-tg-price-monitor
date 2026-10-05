from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GOOGLE_EXPORT = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?gid={gid}&tqx=out:csv"
AVITO_API = "https://api.avito.ru"
UTC = timezone.utc


class MonitorError(RuntimeError):
    pass


@dataclass(frozen=True)
class SheetSource:
    name: str
    spreadsheet_id: str
    gid: str


@dataclass(frozen=True)
class Config:
    project_name: str
    client_sources: Tuple[SheetSource, ...]
    autoload_sources: Tuple[SheetSource, ...]
    telegram_token: str
    telegram_chat_id: str
    avito_client_id: str
    avito_client_secret: str
    state_path: Path
    check_interval_minutes: int
    mismatch_grace: timedelta
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
                    result.append(SheetSource(str(value["name"]), str(value["spreadsheet_id"]), str(value["gid"])))
                except (KeyError, TypeError) as error:
                    raise MonitorError("Invalid entry in config.%s" % name) from error
            return tuple(result)

        return cls(
            project_name=str(raw.get("project_name") or "Авито"),
            client_sources=sources("client_sources"),
            autoload_sources=sources("autoload_sources"),
            telegram_token=required("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=required("TELEGRAM_CHAT_ID"),
            avito_client_id=required("AVITO_CLIENT_ID"),
            avito_client_secret=required("AVITO_CLIENT_SECRET"),
            state_path=Path(os.getenv("STATE_PATH", "data/state.json")),
            check_interval_minutes=positive_int("CHECK_INTERVAL_MINUTES", 60),
            mismatch_grace=timedelta(hours=positive_int("MISMATCH_GRACE_HOURS", 5)),
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
        return self.parameters.get("title", "")


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


def fetch_sheet(source: SheetSource) -> List[List[str]]:
    url = GOOGLE_EXPORT.format(sheet_id=source.spreadsheet_id, gid=source.gid)
    text = http_text(url)
    return [list(row) for row in csv.reader(io.StringIO(text))]


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9+]", "", text)


def header_name(value: object) -> str:
    value = normalized(value)
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
        "akb": "akb", "акб": "akb", "состояниеаккумулятораiphone": "akb", "batteryhealth": "akb",
        "title": "title", "название": "title", "name": "title", "наименование": "title",
    }
    if value in aliases:
        return aliases[value]
    # Autoload templates expose friendly Russian labels plus their technical
    # Avito field name, e.g. "Заголовок объявления Title". Treat both forms
    # exactly as the same field instead of requiring the wording to match.
    if "title" in value or "заголовокобъявлен" in value or "наименование" in value:
        return "title"
    if "model" in value or "модель" in value:
        return "model"
    if "memorysize" in value or "встроеннаяпамять" in value:
        return "memorysize"
    if "ramsize" in value or "оперативнаяпамять" in value:
        return "ramsize"
    if "simconfig" in value or "simкарт" in value:
        return "simconfig"
    if "color" in value or "colour" in value or value.startswith("цвет"):
        return "color"
    if "аккумулятор" in value or "batteryhealth" in value:
        return "akb"
    return value


def is_price_header(value: object) -> bool:
    return "price" in normalized(value)


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
    return str(row[index]).strip() if index < len(row) and row[index] is not None else ""


def product_key(source: str, row: int, parameters: Dict[str, str]) -> str:
    payload = json.dumps([source, row, sorted(parameters.items())], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def extract_products(source: SheetSource, rows: Sequence[Sequence[str]]) -> List[Product]:
    header_row = find_header_row(rows)
    if header_row is None:
        raise MonitorError("No Price header found in client sheet: %s" % source.name)
    headers = rows[header_row]
    price_columns = [index for index, value in enumerate(headers) if is_price_header(value)]
    products: List[Product] = []
    for position, price_column in enumerate(price_columns):
        start = price_columns[position - 1] + 1 if position else 0
        fields = [(index, header_name(headers[index])) for index in range(start, price_column) if cell(headers, index)]
        for row_number, row in enumerate(rows[header_row + 1 :], start=header_row + 2):
            price = parse_price(cell(row, price_column))
            if price is None:
                continue
            parameters = {name: cell(row, index) for index, name in fields}
            if not any(parameters.values()):
                continue
            products.append(Product(product_key(source.name, row_number, parameters), source.name, row_number, parameters, price))
    return products


def extract_ad_id(headers: Sequence[str], row: Sequence[str]) -> Tuple[Optional[str], str]:
    for index, value in enumerate(headers):
        name = normalized(value)
        item = cell(row, index)
        if not item:
            continue
        if "ссылка" in name and "объявлен" in name:
            match = re.search(r"(?:_|/)(\d{5,})(?:[/?#]|$)", item)
            if match:
                return match.group(1), item
        if name in {"номеробъявления", "объявлениеid", "avitoid"} and re.fullmatch(r"\d{5,}", item):
            return item, ""
    return None, ""


def extract_autoload_rows(source: SheetSource, rows: Sequence[Sequence[str]]) -> List[AutoloadRow]:
    header_row = find_header_row(rows)
    if header_row is None:
        logging.warning("Skipping autoload sheet without Price header: %s", source.name)
        return []
    headers = rows[header_row]
    price_column = next((index for index, value in enumerate(headers) if is_price_header(value)), None)
    if price_column is None:
        return []
    fields = [(index, header_name(value)) for index, value in enumerate(headers) if index != price_column and cell(headers, index)]
    result: List[AutoloadRow] = []
    for row_number, row in enumerate(rows[header_row + 1 :], start=header_row + 2):
        parameters = {name: cell(row, index) for index, name in fields}
        if not any(parameters.values()):
            continue
        ad_id, url = extract_ad_id(headers, row)
        result.append(AutoloadRow(source.name, row_number, parameters, parse_price(cell(row, price_column)), ad_id, url))
    return result


def strict_equal(left: str, right: str) -> bool:
    return normalized(left) == normalized(right)


def product_matches(product: Product, row: AutoloadRow) -> bool:
    for name, value in product.parameters.items():
        if not value:
            continue
        if name not in row.parameters or not strict_equal(value, row.parameters[name]):
            return False
    # Mirrors the existing synchronisation: AKB is a critical extra field.
    for name, value in row.parameters.items():
        if name == "akb" and value and (not product.parameters.get(name) or not strict_equal(product.parameters[name], value)):
            return False
    return True


def get_avito_token(config: Config) -> str:
    data = urlencode({"grant_type": "client_credentials", "client_id": config.avito_client_id, "client_secret": config.avito_client_secret}).encode()
    payload = json.loads(http_text(AVITO_API + "/token", method="POST", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}))
    token = payload.get("access_token")
    if not token:
        raise MonitorError("Avito token response did not contain access_token")
    return str(token)


def fetch_active_avito_items(config: Config) -> Dict[str, AvitoItem]:
    token = get_avito_token(config)
    headers = {"Authorization": "Bearer " + token}
    result: Dict[str, AvitoItem] = {}
    for page in range(1, config.max_avito_pages + 1):
        payload = json.loads(http_text(AVITO_API + "/core/v1/items?page=" + str(page), headers=headers))
        resources = payload.get("resources", [])
        if not isinstance(resources, list):
            raise MonitorError("Unexpected Avito item list response")
        for item in resources:
            try:
                parsed = AvitoItem(str(item["id"]), str(item.get("title", "")), int(item["price"]), str(item.get("status", "")), str(item.get("url", "")))
            except (KeyError, TypeError, ValueError) as error:
                raise MonitorError("Unexpected Avito item format") from error
            if parsed.status == "active":
                result[parsed.item_id] = parsed
        if len(resources) < int(payload.get("meta", {}).get("per_page", 25)):
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


def send_telegram(config: Config, text: str) -> None:
    if config.dry_run:
        logging.info("DRY RUN Telegram notification:\n%s", text)
        return
    payload = urlencode({"chat_id": config.telegram_chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    response = json.loads(http_text("https://api.telegram.org/bot%s/sendMessage" % config.telegram_token, method="POST", data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}))
    if not response.get("ok"):
        raise MonitorError("Telegram rejected notification")


class Monitor:
    def __init__(self, config: Config):
        self.config = config

    def run_once(self) -> RunSummary:
        client: List[Product] = []
        for source in self.config.client_sources:
            client.extend(extract_products(source, fetch_sheet(source)))
        autoload: List[AutoloadRow] = []
        for source in self.config.autoload_sources:
            autoload.extend(extract_autoload_rows(source, fetch_sheet(source)))
        active = fetch_active_avito_items(self.config)
        issues = self._find_issues(client, autoload, active)
        alerts, resolved = self._notify(issues)
        return RunSummary(len(client), len(autoload), len(active), len(issues), alerts, resolved)

    def _find_issues(self, products: Sequence[Product], rows: Sequence[AutoloadRow], active: Dict[str, AvitoItem]) -> List[Issue]:
        issues: List[Issue] = []
        for product in products:
            matched = [row for row in rows if product_matches(product, row)]
            if not matched:
                issues.append(Issue("%s:missing-autoload" % product.key, product, None, None, ("Нет соответствующей строки в автозагрузке",)))
                continue
            for row in matched:
                avito = active.get(row.ad_id or "")
                reasons: List[str] = []
                if row.price != product.price:
                    reasons.append("цена не дошла из клиентской таблицы в автозагрузку")
                if not row.ad_id:
                    reasons.append("у строки автозагрузки нет ID объявления Авито")
                elif avito is None:
                    reasons.append("объявление не активно или не найдено в профиле Авито")
                elif row.price != avito.price:
                    reasons.append("цена не дошла из автозагрузки в Авито")
                if reasons:
                    identifier = row.ad_id or (row.source_name + ":" + str(row.row))
                    issues.append(Issue("%s:%s" % (product.key, identifier), product, row, avito, tuple(reasons)))
        return issues

    def _notify(self, issues: Sequence[Issue]) -> Tuple[int, int]:
        state = load_state(self.config.state_path)
        current = now_utc()
        current_keys = {issue.key for issue in issues}
        sent = 0
        resolved = 0
        for issue in issues:
            entry = state.get(issue.key)
            if not entry:
                entry = {"first_seen": current.isoformat(), "notified": "false"}
                state[issue.key] = entry
            elapsed = current - parse_timestamp(entry["first_seen"])
            if entry.get("notified") != "true" and elapsed >= self.config.mismatch_grace:
                send_telegram(self.config, issue.message(elapsed))
                entry["notified"] = "true"
                sent += 1
        for key in list(state):
            if key in current_keys:
                continue
            entry = state[key]
            if entry.get("notified") == "true":
                send_telegram(self.config, "✅ Расхождение цен устранено\nПроект: %s\nПроверка: %s" % (self.config.project_name, key))
                resolved += 1
            del state[key]
        save_state(self.config.state_path, state)
        return sent, resolved
