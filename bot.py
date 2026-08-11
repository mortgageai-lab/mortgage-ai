"""Minimal local Telegram bot for the Mortgage AI project."""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from aiogram import Bot, Dispatcher
from aiogram import F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv


load_dotenv()

dp = Dispatcher()
PROJECT_DIRECTORY = Path(__file__).resolve().parent
DEALS_DIRECTORY = PROJECT_DIRECTORY / "Deals"
DATA_DIRECTORY = PROJECT_DIRECTORY / "data"
CHAT_DEALS_FILE = DATA_DIRECTORY / "chat_deals.json"


def create_deal_id() -> str:
    """Return a human-readable temporary ID without storing data locally."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid4().hex[:4].upper()
    return f"DEAL-{timestamp}-{suffix}"


def create_deal_folder(deal_id: str) -> Path:
    """Create and return the local folder for one deal."""
    deal_folder = DEALS_DIRECTORY / deal_id
    deal_folder.mkdir(parents=True, exist_ok=False)
    (deal_folder / "Documents").mkdir()
    return deal_folder


def load_chat_deals() -> dict[str, str]:
    """Load the local association between Telegram chats and deal IDs."""
    if not CHAT_DEALS_FILE.exists():
        return {}
    with CHAT_DEALS_FILE.open(encoding="utf-8") as file:
        return json.load(file)


def save_chat_deals(chat_deals: dict[str, str]) -> None:
    """Persist the association so documents work after a bot restart."""
    DATA_DIRECTORY.mkdir(exist_ok=True)
    with CHAT_DEALS_FILE.open("w", encoding="utf-8") as file:
        json.dump(chat_deals, file, ensure_ascii=False, indent=2)


def get_deal_id_for_chat(message: Message) -> Optional[str]:
    if message.chat is None:
        return None
    return load_chat_deals().get(str(message.chat.id))


def excluded_usernames() -> set[str]:
    """Return normalized usernames whose documents must not be stored."""
    usernames = os.getenv("EXCLUDED_USERNAMES", "")
    return {
        username.strip().removeprefix("@").lower()
        for username in usernames.split(",")
        if username.strip()
    }


def is_excluded_sender(message: Message) -> bool:
    """Check whether the message was sent by a team member to ignore."""
    if message.from_user is None or message.from_user.username is None:
        return False
    return message.from_user.username.lower() in excluded_usernames()


def unique_file_path(directory: Path, filename: str) -> Path:
    """Avoid overwriting a file when a client sends the same name twice."""
    safe_name = Path(filename).name or "document"
    destination = directory / safe_name
    if not destination.exists():
        return destination

    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    number = 2
    while True:
        destination = directory / f"{stem} ({number}){suffix}"
        if not destination.exists():
            return destination
        number += 1


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Здравствуйте! Я Mortgage AI.\n\n"
        "Добавьте меня в чат сделки и используйте /start_deal, "
        "чтобы создать временный номер сделки."
    )


@dp.message(Command("start_deal"))
async def start_deal_handler(message: Message) -> None:
    deal_id = create_deal_id()
    deal_folder = create_deal_folder(deal_id)
    chat_deals = load_chat_deals()
    chat_deals[str(message.chat.id)] = deal_id
    save_chat_deals(chat_deals)
    await message.answer(
        f"Сделка создана. ID: {deal_id}\n"
        f"Локальная папка создана: {deal_folder}\n"
        "Теперь документы из этого чата будут сохранены в папку Documents."
    )


@dp.message(Command("my_id"))
async def my_id_handler(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(f"Ваш технический Telegram ID: {message.from_user.id}")


@dp.message(F.document)
async def document_handler(message: Message) -> None:
    if is_excluded_sender(message):
        logging.info("Document from excluded user was skipped")
        return

    deal_id = get_deal_id_for_chat(message)
    if not deal_id:
        await message.answer("Сначала создайте сделку в этом чате: /start_deal")
        return

    document = message.document
    if document is None:
        return
    documents_directory = DEALS_DIRECTORY / deal_id / "Documents"
    documents_directory.mkdir(parents=True, exist_ok=True)
    destination = unique_file_path(documents_directory, document.file_name or "document")
    await message.bot.download(document, destination=destination)
    await message.answer(f"Документ сохранён: {destination.name}")


@dp.message(F.photo)
async def photo_handler(message: Message) -> None:
    if is_excluded_sender(message):
        logging.info("Photo from excluded user was skipped")
        return

    deal_id = get_deal_id_for_chat(message)
    if not deal_id:
        await message.answer("Сначала создайте сделку в этом чате: /start_deal")
        return

    photo = message.photo[-1]
    documents_directory = DEALS_DIRECTORY / deal_id / "Documents"
    documents_directory.mkdir(parents=True, exist_ok=True)
    filename = f"photo_{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
    destination = unique_file_path(documents_directory, filename)
    await message.bot.download(photo, destination=destination)
    await message.answer(f"Фотография сохранена: {destination.name}")


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN не найден. Создайте файл .env по образцу .env.example "
            "и добавьте в него токен от BotFather."
        )

    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
