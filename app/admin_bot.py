from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode

from .monitor import Config, MonitorError, http_text, load_state, save_state
from .projects import load_projects, project_id, save_projects


def spreadsheet_id(value: str) -> Optional[str]:
    value = value.strip()
    matched = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", value)
    if matched:
        return matched.group(1)
    return value if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", value) else None


class AdminBot:
    """A small, private Telegram wizard for connecting monitoring projects."""

    def __init__(self, config: Config):
        self.config = config
        self.admin_ids = {
            value.strip() for value in os.getenv("TELEGRAM_ADMIN_USER_IDS", "").split(",") if value.strip()
        }
        self.state_path = config.state_path.parent / "admin-bot-state.json"
        self.sessions_path = config.state_path.parent / "admin-bot-sessions.json"

    @property
    def enabled(self) -> bool:
        return bool(self.admin_ids)

    def _api(self, method: str, payload: Optional[Dict[str, str]] = None) -> Dict[str, object]:
        body = urlencode(payload or {}).encode() if payload else None
        raw = http_text(
            "https://api.telegram.org/bot%s/%s" % (self.config.telegram_token, method),
            method="POST" if body else "GET",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"} if body else None,
        )
        response = json.loads(raw)
        if not response.get("ok"):
            raise MonitorError("Telegram admin interface request was rejected")
        return response

    def _sessions(self) -> Dict[str, Dict[str, object]]:
        state = load_state(self.sessions_path)
        return {key: value for key, value in state.items() if isinstance(value, dict)}

    def _save_sessions(self, sessions: Dict[str, Dict[str, object]]) -> None:
        save_state(self.sessions_path, sessions)

    def _send(self, chat_id: str, text: str, keyboard: Optional[Dict[str, object]] = None) -> None:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if keyboard:
            payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
        self._api("sendMessage", payload)

    @staticmethod
    def _menu() -> Dict[str, object]:
        return {"inline_keyboard": [
            [{"text": "➕ Добавить проект", "callback_data": "add"}],
            [{"text": "📋 Подключённые проекты", "callback_data": "list"}],
        ]}

    @staticmethod
    def _cancel() -> Dict[str, object]:
        return {"inline_keyboard": [[{"text": "Отмена", "callback_data": "cancel"}]]}

    @staticmethod
    def _delays() -> Dict[str, object]:
        return {"inline_keyboard": [
            [{"text": "3 часа", "callback_data": "delay:3"}, {"text": "5 часов", "callback_data": "delay:5"}],
            [{"text": "8 часов", "callback_data": "delay:8"}],
            [{"text": "Отмена", "callback_data": "cancel"}],
        ]}

    def _allowed(self, sender: Dict[str, object], chat: Dict[str, object]) -> bool:
        return chat.get("type") == "private" and str(sender.get("id", "")) in self.admin_ids

    def _prompt(self, chat_id: str, session: Dict[str, object]) -> None:
        step = session.get("step")
        prompts = {
            "name": "Напиши название проекта и город. Например: <b>LimeStore Гатчина</b>",
            "client_sheet": "Отправь ссылку на клиентскую Google-таблицу.",
            "autoload_sheet": "Отправь ссылку на Google-таблицу автозагрузки.",
            "client_id": "Отправь <b>Client ID Авито</b>.",
            "client_secret": "Отправь <b>Client Secret Авито</b>.",
        }
        self._send(chat_id, prompts.get(str(step), "Выбери действие."), self._cancel())

    def _show_confirmation(self, chat_id: str, session: Dict[str, object]) -> None:
        data = session["data"]
        text = (
            "<b>Проверь проект</b>\n\n"
            "🏪 %s\n"
            "👤 Клиентская таблица: подключена\n"
            "📥 Автозагрузка: подключена\n"
            "🕐 Задержка: %s ч.\n\n"
            "После подтверждения бот начнёт проверять его в течение часа."
        ) % (html.escape(str(data["name"])), data["delay_hours"])
        keyboard = {"inline_keyboard": [
            [{"text": "✅ Подключить", "callback_data": "confirm"}],
            [{"text": "Отмена", "callback_data": "cancel"}],
        ]}
        self._send(chat_id, text, keyboard)

    def _handle_callback(self, callback: Dict[str, object], sessions: Dict[str, Dict[str, object]]) -> None:
        sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        callback_id = str(callback.get("id", ""))
        if callback_id:
            self._api("answerCallbackQuery", {"callback_query_id": callback_id})
        if not self._allowed(sender, chat):
            return
        user_id, chat_id = str(sender.get("id")), str(chat.get("id"))
        action = str(callback.get("data", ""))
        if action == "add":
            sessions[user_id] = {"step": "name", "data": {}}
            self._prompt(chat_id, sessions[user_id])
        elif action == "list":
            projects = load_projects(self.config)
            if projects:
                text = "<b>Подключённые проекты</b>\n\n" + "\n".join(
                    "• %s, задержка %s ч." % (html.escape(str(item.get("name", "Без названия"))), item.get("delay_hours", 5))
                    for item in projects
                )
            else:
                text = "Пока подключён только основной проект, заданный при запуске бота."
            self._send(chat_id, text, self._menu())
        elif action.startswith("delay:") and user_id in sessions:
            hours = action.split(":", 1)[1]
            if hours in {"3", "5", "8"}:
                sessions[user_id]["data"]["delay_hours"] = int(hours)
                sessions[user_id]["step"] = "confirm"
                self._show_confirmation(chat_id, sessions[user_id])
        elif action == "confirm" and user_id in sessions:
            data = dict(sessions[user_id].get("data", {}))
            required = {"name", "client_spreadsheet_id", "autoload_spreadsheet_id", "avito_client_id", "avito_client_secret", "delay_hours"}
            if required.issubset(data):
                data["id"] = project_id(str(data["name"]), str(data["client_spreadsheet_id"]))
                projects = [item for item in load_projects(self.config) if item.get("id") != data["id"]]
                projects.append(data)
                save_projects(self.config, projects)
                del sessions[user_id]
                self._send(chat_id, "✅ <b>Проект подключён.</b> Первая проверка будет в течение часа.", self._menu())
        elif action == "cancel":
            sessions.pop(user_id, None)
            self._send(chat_id, "Добавление проекта отменено.", self._menu())

    def _handle_message(self, message: Dict[str, object], sessions: Dict[str, Dict[str, object]]) -> None:
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if not self._allowed(sender, chat):
            return
        user_id, chat_id = str(sender.get("id")), str(chat.get("id"))
        text = str(message.get("text") or "").strip()
        if text.startswith("/start") or text == "/menu":
            sessions.pop(user_id, None)
            self._send(chat_id, "<b>Мониторинг цен Авито</b>\nВыбери действие:", self._menu())
            return
        session = sessions.get(user_id)
        if not session:
            self._send(chat_id, "Нажми кнопку, чтобы начать.", self._menu())
            return
        data = session.setdefault("data", {})
        step = session.get("step")
        if step == "name" and text:
            data["name"] = text
            session["step"] = "client_sheet"
        elif step in {"client_sheet", "autoload_sheet"}:
            identifier = spreadsheet_id(text)
            if not identifier:
                self._send(chat_id, "Не вижу ссылку на Google-таблицу. Пришли ссылку ещё раз.", self._cancel())
                return
            data["%s_spreadsheet_id" % ("client" if step == "client_sheet" else "autoload")] = identifier
            session["step"] = "autoload_sheet" if step == "client_sheet" else "client_id"
        elif step == "client_id" and text:
            data["avito_client_id"] = text
            session["step"] = "client_secret"
        elif step == "client_secret" and text:
            data["avito_client_secret"] = text
            session["step"] = "delay"
            self._send(chat_id, "Выбери, через сколько часов сообщать о расхождении:", self._delays())
            return
        else:
            self._prompt(chat_id, session)
            return
        self._prompt(chat_id, session)

    def process_updates(self) -> None:
        if not self.enabled:
            return
        state = load_state(self.state_path)
        offset = int(state.get("offset", 0) or 0)
        response = self._api("getUpdates", {"offset": str(offset), "timeout": "0", "allowed_updates": json.dumps(["message", "callback_query"])})
        sessions = self._sessions()
        for update in response.get("result", []):
            if not isinstance(update, dict):
                continue
            update_id = int(update.get("update_id", 0) or 0)
            if isinstance(update.get("callback_query"), dict):
                self._handle_callback(update["callback_query"], sessions)
            elif isinstance(update.get("message"), dict):
                self._handle_message(update["message"], sessions)
            offset = max(offset, update_id + 1)
        save_state(self.state_path, {"offset": offset})
        self._save_sessions(sessions)
