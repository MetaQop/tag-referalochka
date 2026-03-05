"""
sheets.py — модуль для записи реферальных событий в Google Sheets.
Все вызовы обёрнуты в try/except, чтобы ошибки Sheets не ломали бота.
"""

import logging
from datetime import datetime
import pytz

log = logging.getLogger(__name__)

_gc = None
_spreadsheet = None


def _get_sheet(name: str):
    """Открыть лист по имени (с ленивой инициализацией)."""
    global _gc, _spreadsheet
    if _spreadsheet is None:
        import gspread
        from google.oauth2.service_account import Credentials
        from config import SHEETS_CREDS, SPREADSHEET_NAME

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(SHEETS_CREDS, scopes=scopes)
        _gc = gspread.authorize(creds)
        _spreadsheet = _gc.open(SPREADSHEET_NAME)

    return _spreadsheet.worksheet(name)


def _now_str() -> str:
    tz = pytz.timezone("Europe/Moscow")
    return datetime.now(tz).strftime("%d.%m.%Y %H:%M")


# ──────────────────────────────────────────────
# ПУБЛИЧНЫЕ ФУНКЦИИ (вызываются из bot.py)
# ──────────────────────────────────────────────

def log_referral_join(
    referred_id: int,
    referred_name: str,
    referred_username: str,
    referrer_id: int,
    referrer_name: str,
    referrer_username: str,
    referrer_total: int,
):
    """Новый участник вступил по реферальной ссылке → строка в 'Рефералки'."""
    try:
        ws = _get_sheet("🔗 Рефералки")
        ws.append_row(
            [
                _now_str(),
                str(referred_id),
                referred_name,
                f"@{referred_username}" if referred_username else "—",
                str(referrer_id),
                referrer_name,
                f"@{referrer_username}" if referrer_username else "—",
                referrer_total,
                "✅ Вступил",
            ],
            value_input_option="USER_ENTERED",
        )
        log.info(f"Sheets: referral join logged — {referred_name} via {referrer_name}")
    except Exception as e:
        log.error(f"Sheets log_referral_join error: {e}")


def log_referral_leave(
    referred_id: int,
    referred_name: str,
    referrer_id: int,
    referrer_name: str,
    referrer_total: int,
):
    """Участник вышел → обновить строку или добавить событие."""
    try:
        ws = _get_sheet("🔗 Рефералки")
        # Найти строку по referred_id и обновить статус
        cell = ws.find(str(referred_id), in_column=2)
        if cell:
            ws.update_cell(cell.row, 9, "❌ Вышел")
        else:
            ws.append_row(
                [
                    _now_str(),
                    str(referred_id),
                    referred_name,
                    "—",
                    str(referrer_id),
                    referrer_name,
                    "—",
                    referrer_total,
                    "❌ Вышел",
                ],
                value_input_option="USER_ENTERED",
            )
        log.info(f"Sheets: referral leave logged — {referred_name}")
    except Exception as e:
        log.error(f"Sheets log_referral_leave error: {e}")


def log_access_granted(
    user_id: int,
    user_name: str,
    username: str,
    invited_count: int,
    expiry_date: str,
):
    """Пользователь выполнил условие и получил доступ в группу."""
    try:
        ws = _get_sheet("🏆 Рефереры")
        # Проверим, есть ли уже строка для этого пользователя
        try:
            cell = ws.find(str(user_id), in_column=1)
            row = cell.row
            ws.update(f"E{row}:G{row}", [[invited_count, expiry_date, "✅ Активен"]])
        except Exception:
            ws.append_row(
                [
                    str(user_id),
                    user_name,
                    f"@{username}" if username else "—",
                    _now_str(),
                    invited_count,
                    expiry_date,
                    "✅ Активен",
                ],
                value_input_option="USER_ENTERED",
            )
        log.info(f"Sheets: access granted logged — {user_name}")
    except Exception as e:
        log.error(f"Sheets log_access_granted error: {e}")


def log_access_expired(user_id: int, user_name: str):
    """Подписка истекла → обновить статус в листе Рефереры."""
    try:
        ws = _get_sheet("🏆 Рефереры")
        cell = ws.find(str(user_id), in_column=1)
        if cell:
            ws.update_cell(cell.row, 7, "🔴 Истёк")
        log.info(f"Sheets: access expired logged — {user_name}")
    except Exception as e:
        log.error(f"Sheets log_access_expired error: {e}")
