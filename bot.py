"""Minimal local Telegram bot for the Mortgage AI project."""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

import fitz
import pytesseract
from aiogram import Bot, Dispatcher
from aiogram import F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from dotenv import load_dotenv
from PIL import Image, ImageOps

from drive_storage import GoogleDriveStorage
from news_monitor import NewsStore, format_rate_report


load_dotenv()

dp = Dispatcher()
PROJECT_DIRECTORY = Path(__file__).resolve().parent
DEALS_DIRECTORY = PROJECT_DIRECTORY / "Deals"
DATA_DIRECTORY = PROJECT_DIRECTORY / "data"
CHAT_DEALS_FILE = DATA_DIRECTORY / "chat_deals.json"
NEWS_DATABASE_FILE = DATA_DIRECTORY / "mortgage_news.sqlite3"
DEAL_METADATA_FILE = ".mortgage_ai_deal.json"
PASSPORT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
MAX_PDF_PAGES_TO_SCAN = 2
OCR_TIMEOUT_SECONDS = 12
photo_album_buffers: dict[tuple[int, str], list[tuple[int, object]]] = {}
photo_album_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
news_store = NewsStore(NEWS_DATABASE_FILE)
drive_storage = GoogleDriveStorage(PROJECT_DIRECTORY, DATA_DIRECTORY)


class Feedback(StatesGroup):
    waiting_for_rating = State()
    waiting_for_comment = State()


def create_deal_id() -> str:
    """Return a human-readable temporary ID without storing data locally."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid4().hex[:4].upper()
    return f"DEAL-{timestamp}-{suffix}"


def create_deal_folder(deal_id: str) -> Path:
    """Create a human-readable local folder and return its path."""
    folder_name = datetime.now().strftime("%d.%m.%Y %H-%M-%S")
    deal_folder = DEALS_DIRECTORY / folder_name
    deal_folder.mkdir(parents=True, exist_ok=False)
    (deal_folder / "Documents").mkdir()
    with (deal_folder / DEAL_METADATA_FILE).open("w", encoding="utf-8") as file:
        json.dump({"deal_id": deal_id}, file)
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


def find_deal_folder(deal_id: str) -> Optional[Path]:
    """Find a deal folder even if a manager renamed it in Finder."""
    legacy_folder = DEALS_DIRECTORY / deal_id
    if legacy_folder.is_dir():
        return legacy_folder

    if not DEALS_DIRECTORY.exists():
        return None
    for metadata_path in DEALS_DIRECTORY.rglob(DEAL_METADATA_FILE):
        try:
            with metadata_path.open(encoding="utf-8") as file:
                if json.load(file).get("deal_id") == deal_id:
                    return metadata_path.parent
        except (json.JSONDecodeError, OSError):
            logging.warning("Could not read deal metadata: %s", metadata_path)
    return None


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


def manager_user_id() -> Optional[int]:
    """Return the manager's Telegram ID configured only in the local .env."""
    raw_user_id = os.getenv("MANAGER_USER_ID")
    if raw_user_id and raw_user_id.isdigit():
        return int(raw_user_id)
    return None


def is_team_member(message: Message) -> bool:
    """Identify the manager or the mortgage manager in a private chat."""
    return (message.from_user is not None and message.from_user.id == manager_user_id()) or is_excluded_sender(message)


def is_client_private_message(message: Message) -> bool:
    return message.chat.type == "private" and not is_team_member(message)


async def offer_feedback(message: Message, state: FSMContext) -> None:
    """Start the short client-feedback flow in the bot's private chat."""
    await state.set_state(Feedback.waiting_for_rating)
    await message.answer(
        "Здравствуйте! Это бот отдела контроля качества Whitewill.\n\n"
        "Нам важно, чтобы взаимодействие с нами было комфортным. Здесь вы можете "
        "оценить работу ипотечного отдела и оставить комментарий. Ваш отзыв поступит "
        "в отдел контроля качества — мы обязательно его рассмотрим.\n\n"
        "Пожалуйста, поставьте оценку от 1 до 5."
    )


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


def extract_text_from_image(image: Image.Image) -> str:
    """Read text locally without sending a document to external services."""
    prepared_image = ImageOps.exif_transpose(image).convert("L")
    if prepared_image.width < 2600:
        scale = 2600 / prepared_image.width
        prepared_image = prepared_image.resize(
            (2600, int(prepared_image.height * scale)), Image.Resampling.LANCZOS
        )
    try:
        orientation_data = pytesseract.image_to_osd(
            prepared_image, config="--psm 0", timeout=OCR_TIMEOUT_SECONDS
        )
        rotation_match = re.search(r"Rotate: (\d+)", orientation_data)
        if rotation_match:
            rotation = int(rotation_match.group(1))
            if rotation:
                prepared_image = prepared_image.rotate(360 - rotation, expand=True)
    except pytesseract.TesseractError:
        # Some sparse or low-text pages cannot be auto-oriented; OCR still runs.
        pass
    text_blocks = [
        pytesseract.image_to_string(
            prepared_image, lang="rus+eng", config="--psm 6", timeout=OCR_TIMEOUT_SECONDS
        ),
        pytesseract.image_to_string(
            prepared_image, lang="rus+eng", config="--psm 12", timeout=OCR_TIMEOUT_SECONDS
        ),
    ]
    return "\n".join(text_blocks)


def extract_document_text(file_path: Path) -> str:
    """Extract text from an image or the first two pages of a PDF locally."""
    if file_path.suffix.lower() == ".pdf":
        document = fitz.open(file_path)
        try:
            text_parts = []
            for page_number in range(min(document.page_count, MAX_PDF_PAGES_TO_SCAN)):
                page = document[page_number]
                text_parts.append(page.get_text())
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                text_parts.append(extract_text_from_image(image))
            return "\n".join(text_parts)
        finally:
            document.close()

    with Image.open(file_path) as image:
        return extract_text_from_image(image)


def is_russian_passport(text: str) -> bool:
    """Identify a Russian internal passport by several independent OCR markers."""
    normalized_text = re.sub(r"[^А-ЯЁ]", "", text.upper())
    has_title = "ПАСПОРТ" in normalized_text and (
        "РОСС" in normalized_text or "ФЕДЕРАЦ" in normalized_text
    )
    has_personal_fields = sum(
        marker in normalized_text for marker in ("ФАМИЛ", "ИМЯ", "ДАТАРОЖД")
    ) >= 2
    has_passport_layout = (
        "РОСС" in normalized_text
        and any(marker in normalized_text for marker in ("УФМС", "МВД", "МИНИСТЕРСТ"))
    )
    return has_title or has_personal_fields or has_passport_layout


def is_snils(text: str) -> bool:
    """Identify modern and older forms of the Russian SNILS document."""
    normalized_text = re.sub(r"[^А-ЯЁ]", "", text.upper())
    has_modern_name = "СНИЛС" in normalized_text or (
        "СТРАХОВОЙНОМЕР" in normalized_text and "ЛИЦЕВОГ" in normalized_text
    )
    has_older_name = (
        "ПЕНСИОННОГ" in normalized_text
        and "СТРАХОВАН" in normalized_text
        and ("СВИДЕТЕЛ" in normalized_text or "РОСС" in normalized_text)
    )
    return has_modern_name or has_older_name


def is_2ndfl_certificate(text: str) -> bool:
    """Identify the 2-NDFL or income-and-tax certificate form."""
    normalized_text = re.sub(r"[^А-ЯЁ]", "", text.upper())
    has_ndfl = "НДФЛ" in normalized_text and (
        "СПРАВК" in normalized_text or "ДОХОД" in normalized_text
    )
    has_current_form = (
        "СПРАВК" in normalized_text
        and "ДОХОД" in normalized_text
        and "НАЛОГ" in normalized_text
        and "ФИЗИЧЕСК" in normalized_text
    )
    has_older_form = (
        "СПРАВК" in normalized_text
        and "ДОХОД" in normalized_text
        and "ФИЗИЧЕСК" in normalized_text
        and ("НАЛОГОВ" in normalized_text or "АГЕНТ" in normalized_text)
    )
    return has_ndfl or has_current_form or has_older_form


def is_marriage_certificate(text: str) -> bool:
    normalized_text = re.sub(r"[^А-ЯЁ]", "", text.upper())
    return all(marker in normalized_text for marker in ("СВИДЕТЕЛ", "ЗАКЛЮЧЕН", "БРАК"))


def is_divorce_certificate(text: str) -> bool:
    normalized_text = re.sub(r"[^А-ЯЁ]", "", text.upper())
    return all(marker in normalized_text for marker in ("СВИДЕТЕЛ", "РАСТОРЖ", "БРАК"))


def is_birth_certificate(text: str) -> bool:
    normalized_text = re.sub(r"[^А-ЯЁ]", "", text.upper())
    return "СВИДЕТЕЛ" in normalized_text and "РОЖДЕН" in normalized_text


def recognized_document_name(text: str) -> Optional[str]:
    """Return a safe, human-readable name for a recognized document type."""
    if is_russian_passport(text):
        return "Паспорт"
    if is_snils(text):
        return "СНИЛС"
    if is_2ndfl_certificate(text):
        return "Справка 2-НДФЛ"
    if is_marriage_certificate(text):
        return "Свидетельство о заключении брака"
    if is_divorce_certificate(text):
        return "Свидетельство о расторжении брака"
    if is_birth_certificate(text):
        return "Свидетельство о рождении"
    return None


def rename_recognized_document(file_path: Path) -> Optional[Path]:
    """Rename only files confidently identified as supported document types."""
    if file_path.suffix.lower() not in PASSPORT_EXTENSIONS:
        return None

    try:
        text = extract_document_text(file_path)
    except (OSError, RuntimeError, fitz.FileDataError, pytesseract.TesseractError) as error:
        logging.warning("Passport recognition failed for %s: %s", file_path.name, error)
        return None

    document_name = recognized_document_name(text)
    if not document_name:
        return None

    desired_name = f"{document_name}{file_path.suffix.lower()}"
    destination = unique_file_path(file_path.parent, desired_name)
    file_path.rename(destination)
    return destination


def is_passport_photo_album(file_paths: list[Path]) -> bool:
    """Identify a passport by considering all pages in one Telegram album."""
    for file_path in file_paths:
        try:
            if is_russian_passport(extract_document_text(file_path)):
                return True
        except (OSError, RuntimeError, fitz.FileDataError, pytesseract.TesseractError) as error:
            logging.warning("Could not read album page %s: %s", file_path.name, error)
    return False


async def process_photo_album(
    album_key: tuple[int, str], bot: Bot, documents_directory: Path, deal_id: str
) -> None:
    """Wait for an album to arrive, then save and recognize its pages together."""
    try:
        await asyncio.sleep(2)
        album_photos = photo_album_buffers.pop(album_key, [])
        photo_album_tasks.pop(album_key, None)
        if not album_photos:
            return

        saved_paths = []
        for page_number, (_, photo) in enumerate(sorted(album_photos), start=1):
            filename = f"album_{album_key[1]}_page_{page_number}.jpg"
            destination = unique_file_path(documents_directory, filename)
            await bot.download(photo, destination=destination)
            saved_paths.append(destination)

        if await asyncio.to_thread(is_passport_photo_album, saved_paths):
            renamed_paths = []
            for page_number, saved_path in enumerate(saved_paths, start=1):
                destination = unique_file_path(
                    documents_directory, f"Паспорт {page_number}.jpg"
                )
                saved_path.rename(destination)
                renamed_paths.append(destination)
            await bot.send_message(
                album_key[0], f"Паспорт сохранён: {len(renamed_paths)} стр."
            )
            for renamed_path in renamed_paths:
                await asyncio.to_thread(drive_storage.upload_document, deal_id, renamed_path)
        else:
            await bot.send_message(album_key[0], f"Фотографии сохранены: {len(saved_paths)} шт.")
            for saved_path in saved_paths:
                await asyncio.to_thread(drive_storage.upload_document, deal_id, saved_path)
    except Exception:
        logging.exception("Could not process photo album")


async def queue_photo_album(message: Message, documents_directory: Path, deal_id: str) -> None:
    """Collect all photos from one Telegram album before processing them."""
    if message.media_group_id is None or not message.photo:
        return
    album_key = (message.chat.id, message.media_group_id)
    photo_album_buffers.setdefault(album_key, []).append((message.message_id, message.photo[-1]))

    previous_task = photo_album_tasks.get(album_key)
    if previous_task:
        previous_task.cancel()
    photo_album_tasks[album_key] = asyncio.create_task(
        process_photo_album(album_key, message.bot, documents_directory, deal_id)
    )


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    if is_client_private_message(message):
        await offer_feedback(message, state)
        return
    await message.answer(
        "Здравствуйте! Я Mortgage AI.\n\n"
        "Добавьте меня в чат сделки и используйте /start_deal, "
        "чтобы создать временный номер сделки."
    )


@dp.message(Command("start_deal"))
async def start_deal_handler(message: Message, state: FSMContext) -> None:
    if is_client_private_message(message):
        await offer_feedback(message, state)
        return
    deal_id = create_deal_id()
    deal_folder = create_deal_folder(deal_id)
    chat_deals = load_chat_deals()
    chat_deals[str(message.chat.id)] = deal_id
    save_chat_deals(chat_deals)
    drive_ready = await asyncio.to_thread(
        drive_storage.ensure_deal_folders, deal_id, deal_folder.name
    )
    await message.answer(f'Сделка создана. Папка "{deal_folder.name}"')
    if drive_storage.enabled and not drive_ready:
        await message.answer("Локальная папка создана. Google Drive временно недоступен.")


@dp.message(Command("my_id"))
async def my_id_handler(message: Message) -> None:
    if not is_team_member(message):
        return
    if message.from_user is None:
        return
    await message.answer(f"Ваш технический Telegram ID: {message.from_user.id}")


@dp.message(Command("rates"))
async def rates_handler(message: Message) -> None:
    """Show the latest rate mentions collected from configured channels."""
    if not is_team_member(message):
        return
    report = await asyncio.to_thread(
        format_rate_report, news_store.latest_rates(), "Текущая ситуация по ставкам"
    )
    await message.answer(report, disable_web_page_preview=True)


@dp.message(Command("rate_changes"))
async def rate_changes_handler(message: Message) -> None:
    """Show rate mentions published during the last seven days."""
    if not is_team_member(message):
        return
    report = await asyncio.to_thread(
        format_rate_report, news_store.recent_changes(), "Изменения за последние 7 дней"
    )
    await message.answer(report, disable_web_page_preview=True)


@dp.message(Feedback.waiting_for_rating, F.text)
async def feedback_rating_handler(message: Message, state: FSMContext) -> None:
    rating = (message.text or "").strip()
    if rating not in {"1", "2", "3", "4", "5"}:
        await message.answer("Пожалуйста, отправьте одну цифру: 1, 2, 3, 4 или 5.")
        return
    await state.update_data(rating=rating)
    await state.set_state(Feedback.waiting_for_comment)
    await message.answer("Спасибо. Напишите, пожалуйста, ваш комментарий или пожелание.")


@dp.message(Feedback.waiting_for_comment, F.text)
async def feedback_comment_handler(message: Message, state: FSMContext) -> None:
    comment = (message.text or "").strip()
    if not comment:
        await message.answer("Пожалуйста, напишите комментарий текстом.")
        return

    recipient_id = manager_user_id()
    if recipient_id is None:
        logging.error("MANAGER_USER_ID is not configured")
        await message.answer("Спасибо! Сейчас отзыв нельзя отправить. Попробуйте позднее.")
        await state.clear()
        return

    feedback_data = await state.get_data()
    sender_name = message.from_user.full_name if message.from_user else "Неизвестный пользователь"
    username = f" (@{message.from_user.username})" if message.from_user and message.from_user.username else ""
    await message.bot.send_message(
        recipient_id,
        "Новый отзыв о работе ипотечной команды\n\n"
        f"От: {sender_name}{username}\n"
        f"Оценка: {feedback_data['rating']}/5\n"
        f"Комментарий: {comment}",
    )
    await state.clear()
    await message.answer("Спасибо за отзыв! Он направлен в отдел контроля качества.")


@dp.message(F.document)
async def document_handler(message: Message) -> None:
    if is_client_private_message(message):
        await message.answer("В личном чате можно оставить отзыв. Отправьте оценку от 1 до 5 текстом.")
        return
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
    deal_folder = find_deal_folder(deal_id)
    if not deal_folder:
        await message.answer("Папка этой сделки не найдена. Создайте новую сделку: /start_deal")
        return
    documents_directory = deal_folder / "Documents"
    documents_directory.mkdir(exist_ok=True)
    destination = unique_file_path(documents_directory, document.file_name or "document")
    await message.bot.download(document, destination=destination)
    renamed_path = await asyncio.to_thread(rename_recognized_document, destination)
    saved_path = renamed_path or destination
    uploaded = await asyncio.to_thread(drive_storage.upload_document, deal_id, saved_path)
    response = f"Документ сохранён: {saved_path.name}"
    if drive_storage.enabled:
        response += "\nКопия загружена в Google Drive." if uploaded else "\nЛокальная копия сохранена; Google Drive временно недоступен."
    await message.answer(response)


@dp.message(F.photo)
async def photo_handler(message: Message) -> None:
    if is_client_private_message(message):
        await message.answer("В личном чате можно оставить отзыв. Отправьте оценку от 1 до 5 текстом.")
        return
    if is_excluded_sender(message):
        logging.info("Photo from excluded user was skipped")
        return

    deal_id = get_deal_id_for_chat(message)
    if not deal_id:
        await message.answer("Сначала создайте сделку в этом чате: /start_deal")
        return

    photo = message.photo[-1]
    deal_folder = find_deal_folder(deal_id)
    if not deal_folder:
        await message.answer("Папка этой сделки не найдена. Создайте новую сделку: /start_deal")
        return
    documents_directory = deal_folder / "Documents"
    documents_directory.mkdir(exist_ok=True)
    if message.media_group_id:
        await queue_photo_album(message, documents_directory, deal_id)
        return

    filename = f"photo_{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
    destination = unique_file_path(documents_directory, filename)
    await message.bot.download(photo, destination=destination)
    renamed_path = await asyncio.to_thread(rename_recognized_document, destination)
    saved_path = renamed_path or destination
    uploaded = await asyncio.to_thread(drive_storage.upload_document, deal_id, saved_path)
    response = f"Фотография сохранена: {saved_path.name}"
    if drive_storage.enabled:
        response += "\nКопия загружена в Google Drive." if uploaded else "\nЛокальная копия сохранена; Google Drive временно недоступен."
    await message.answer(response)


@dp.message(F.text)
async def private_client_message_handler(message: Message, state: FSMContext) -> None:
    if is_client_private_message(message):
        await offer_feedback(message, state)


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
