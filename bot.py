"""Minimal local Telegram bot for the Mortgage AI project."""

import asyncio
import json
import logging
import os
import re
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from uuid import uuid4

import pymupdf as fitz
import pytesseract
from aiogram import Bot, Dispatcher
from aiogram import F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ChatMemberUpdated, Message
from dotenv import load_dotenv
from PIL import Image, ImageFilter, ImageOps

from drive_storage import GoogleDriveStorage
from news_monitor import NewsStore, format_rate_report


load_dotenv()

dp = Dispatcher()
PROJECT_DIRECTORY = Path(__file__).resolve().parent
DEALS_DIRECTORY = PROJECT_DIRECTORY / "Deals"
DATA_DIRECTORY = PROJECT_DIRECTORY / "data"
CHAT_DEALS_FILE = DATA_DIRECTORY / "chat_deals.json"
NOTIFICATION_CHAT_FILE = DATA_DIRECTORY / "notification_chat.json"
FEEDBACK_RATE_LIMIT_FILE = DATA_DIRECTORY / "feedback_rate_limits.json"
NEWS_DATABASE_FILE = DATA_DIRECTORY / "mortgage_news.sqlite3"
DEAL_METADATA_FILE = ".mortgage_ai_deal.json"
CHAT_DOCUMENTS_FOLDER_NAME = "Документы из чата"
LEGACY_DOCUMENTS_FOLDER_NAME = "Documents"
PASSPORT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
MAX_PDF_PAGES_TO_SCAN = 2
OCR_TIMEOUT_SECONDS = 12
MAX_OCR_FILE_BYTES = 25 * 1024 * 1024
MAX_SAVE_FILE_BYTES = 50 * 1024 * 1024
MAX_OCR_IMAGE_PIXELS = 25_000_000
MAX_FEEDBACKS_PER_WINDOW = 3
FEEDBACK_RATE_LIMIT_WINDOW = timedelta(hours=24)
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


def unique_directory_path(directory: Path, folder_name: str) -> Path:
    """Return a non-conflicting directory path without creating it."""
    destination = directory / folder_name
    if not destination.exists():
        return destination

    number = 2
    while True:
        destination = directory / f"{folder_name} ({number})"
        if not destination.exists():
            return destination
        number += 1


def create_deal_folder(deal_id: str, created_at: Optional[datetime] = None) -> Path:
    """Create a human-readable local folder and return its path."""
    created_at = created_at or datetime.now()
    folder_name = f"Телеграмм {created_at.strftime('%d.%m')} - {created_at.strftime('%H:%M')}"
    deal_folder = unique_directory_path(DEALS_DIRECTORY, folder_name)
    deal_folder.mkdir(parents=True, exist_ok=False)
    (deal_folder / CHAT_DOCUMENTS_FOLDER_NAME).mkdir()
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


def load_notification_chat_id() -> Optional[int]:
    """Load the private operations chat from env or local runtime data."""
    configured_id = os.getenv("NOTIFICATIONS_CHAT_ID", "").strip()
    if configured_id.lstrip("-").isdigit():
        return int(configured_id)
    if not NOTIFICATION_CHAT_FILE.exists():
        return None
    try:
        with NOTIFICATION_CHAT_FILE.open(encoding="utf-8") as file:
            stored_id = str(json.load(file).get("chat_id", ""))
        return int(stored_id) if stored_id.lstrip("-").isdigit() else None
    except (OSError, json.JSONDecodeError):
        logging.warning("Could not read the notification chat settings")
        return None


def save_notification_chat_id(chat_id: int) -> None:
    DATA_DIRECTORY.mkdir(exist_ok=True)
    with NOTIFICATION_CHAT_FILE.open("w", encoding="utf-8") as file:
        json.dump({"chat_id": chat_id}, file, ensure_ascii=False, indent=2)


def load_recent_feedbacks(now: Optional[datetime] = None) -> dict[str, list[float]]:
    """Load feedback timestamps and discard entries older than 24 hours."""
    now = now or datetime.now()
    cutoff = (now - FEEDBACK_RATE_LIMIT_WINDOW).timestamp()
    if not FEEDBACK_RATE_LIMIT_FILE.exists():
        return {}
    try:
        with FEEDBACK_RATE_LIMIT_FILE.open(encoding="utf-8") as file:
            stored_feedbacks = json.load(file)
        return {
            str(user_id): [
                float(timestamp)
                for timestamp in timestamps
                if isinstance(timestamp, (int, float)) and float(timestamp) > cutoff
            ]
            for user_id, timestamps in stored_feedbacks.items()
            if isinstance(timestamps, list)
        }
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        logging.warning("Could not read feedback rate limits")
        return {}


def save_recent_feedbacks(feedbacks: dict[str, list[float]]) -> None:
    DATA_DIRECTORY.mkdir(exist_ok=True)
    with FEEDBACK_RATE_LIMIT_FILE.open("w", encoding="utf-8") as file:
        json.dump(feedbacks, file, ensure_ascii=False, indent=2)


def feedback_limit_reached(user_id: int, now: Optional[datetime] = None) -> bool:
    return len(load_recent_feedbacks(now).get(str(user_id), [])) >= MAX_FEEDBACKS_PER_WINDOW


def record_feedback(user_id: int, now: Optional[datetime] = None) -> None:
    now = now or datetime.now()
    feedbacks = load_recent_feedbacks(now)
    feedbacks.setdefault(str(user_id), []).append(now.timestamp())
    save_recent_feedbacks(feedbacks)


def is_notification_chat(message: Message) -> bool:
    return load_notification_chat_id() == message.chat.id


async def send_notification(bot: Bot, text: str) -> bool:
    """Send an operational message outside the client chat."""
    chat_id = load_notification_chat_id()
    if chat_id is None:
        logging.warning("Notification chat is not configured: %s", text)
        return False
    try:
        await bot.send_message(chat_id, text)
        return True
    except Exception as error:
        logging.warning("Could not send notification: %s", error)
        return False


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


def documents_directory_for_deal(deal_folder: Path) -> Path:
    """Use the Russian folder for new deals and retain legacy deal support."""
    current_directory = deal_folder / CHAT_DOCUMENTS_FOLDER_NAME
    legacy_directory = deal_folder / LEGACY_DOCUMENTS_FOLDER_NAME
    if current_directory.exists() or not legacy_directory.exists():
        current_directory.mkdir(exist_ok=True)
        return current_directory
    return legacy_directory


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


def team_user_ids() -> set[int]:
    """Return stable Telegram IDs allowed to activate the bot in a group."""
    configured_ids = {
        int(value.strip())
        for value in os.getenv("TEAM_USER_IDS", "").split(",")
        if value.strip().isdigit()
    }
    configured_manager_id = manager_user_id()
    if configured_manager_id is not None:
        configured_ids.add(configured_manager_id)
    return configured_ids


def is_authorized_team_user(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in team_user_ids()


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


def exceeds_save_limit(file_size: Optional[int]) -> bool:
    return file_size is not None and file_size > MAX_SAVE_FILE_BYTES


def ocr_skip_reason(file_path: Path, file_size: Optional[int] = None) -> Optional[str]:
    """Explain why a saved file should bypass OCR."""
    actual_size = file_size if file_size is not None else file_path.stat().st_size
    if actual_size > MAX_OCR_FILE_BYTES:
        return "размер превышает 25 МБ"
    if file_path.suffix.lower() not in PASSPORT_EXTENSIONS:
        suffix = file_path.suffix.lower() or "без расширения"
        return f"формат {suffix} не поддерживает распознавание"
    if file_path.suffix.lower() != ".pdf":
        try:
            with Image.open(file_path) as image:
                if image.width * image.height > MAX_OCR_IMAGE_PIXELS:
                    return "изображение превышает 25 мегапикселей"
        except OSError:
            return "изображение не удалось прочитать"
    return None


def remove_oversized_download(file_path: Path) -> None:
    """Remove a just-downloaded file that exceeded the hard storage limit."""
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        logging.warning("Could not remove oversized download: %s", file_path)


def crop_to_primary_document(image: Image.Image) -> Image.Image:
    """Crop a small document from a mostly blank scanner page."""
    source_image = ImageOps.exif_transpose(image).convert("RGB")
    preview = source_image.convert("L")
    preview.thumbnail((500, 500), Image.Resampling.LANCZOS)
    foreground = preview.point(lambda pixel: 255 if pixel < 245 else 0)
    foreground = foreground.filter(ImageFilter.MaxFilter(9))
    width, height = foreground.size
    pixels = foreground.tobytes()
    visited = bytearray(width * height)
    largest_component: Optional[tuple[int, int, int, int, int]] = None

    for start_index, pixel in enumerate(pixels):
        if pixel == 0 or visited[start_index]:
            continue
        queue = deque([start_index])
        visited[start_index] = 1
        min_x = max_x = start_index % width
        min_y = max_y = start_index // width
        component_size = 0
        while queue:
            index = queue.popleft()
            x, y = index % width, index // width
            component_size += 1
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for neighbor in (index - 1, index + 1, index - width, index + width):
                if neighbor < 0 or neighbor >= width * height or visited[neighbor]:
                    continue
                neighbor_x = neighbor % width
                if abs(neighbor_x - x) > 1 or pixels[neighbor] == 0:
                    continue
                visited[neighbor] = 1
                queue.append(neighbor)
        component = (component_size, min_x, min_y, max_x + 1, max_y + 1)
        if largest_component is None or component_size > largest_component[0]:
            largest_component = component

    if largest_component is None or largest_component[0] < width * height * 0.01:
        return source_image
    _, left, top, right, bottom = largest_component
    component_area = (right - left) * (bottom - top)
    if component_area > width * height * 0.9:
        return source_image

    scale_x = source_image.width / width
    scale_y = source_image.height / height
    padding_x = max(10, int((right - left) * scale_x * 0.04))
    padding_y = max(10, int((bottom - top) * scale_y * 0.04))
    crop_box = (
        max(0, int(left * scale_x) - padding_x),
        max(0, int(top * scale_y) - padding_y),
        min(source_image.width, int(right * scale_x) + padding_x),
        min(source_image.height, int(bottom * scale_y) + padding_y),
    )
    return source_image.crop(crop_box)


def extract_text_from_image(image: Image.Image) -> str:
    """Read text locally without sending a document to external services."""
    prepared_image = crop_to_primary_document(image).convert("L")
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
    has_expanded_name = all(
        marker in normalized_text
        for marker in ("СТРАХОВ", "НОМЕР", "ИНДИВИДУАЛ", "ЛИЦЕВОГ", "СЧЕТ")
    )
    has_older_name = (
        "ПЕНСИОННОГ" in normalized_text
        and "СТРАХОВАН" in normalized_text
        and ("СВИДЕТЕЛ" in normalized_text or "РОСС" in normalized_text)
    )
    has_older_header = all(
        marker in normalized_text for marker in ("СТРАХОВО", "ОБЯЗАТЕЛЬН", "ПЕНСИОННОГ")
    )
    return has_modern_name or has_expanded_name or has_older_name or has_older_header


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


def is_power_of_attorney(text: str) -> bool:
    """Identify a power of attorney by its explicit document title."""
    normalized_text = re.sub(r"[^А-ЯЁ]", "", text.upper())
    return "ДОВЕРЕННОСТ" in normalized_text


def recognized_document_name(text: str) -> Optional[str]:
    """Return a safe, human-readable name for a recognized document type."""
    # A power of attorney often contains passport details, so its explicit
    # title must take priority over the embedded identity-document markers.
    if is_power_of_attorney(text):
        return "Доверенность"
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


def recognized_photo_album_name(file_paths: list[Path]) -> Optional[str]:
    """Classify a multi-page Telegram album using all readable pages together."""
    text_parts = []
    for file_path in file_paths:
        try:
            text_parts.append(extract_document_text(file_path))
        except (OSError, RuntimeError, fitz.FileDataError, pytesseract.TesseractError) as error:
            logging.warning("Could not read album page %s: %s", file_path.name, error)
    return recognized_document_name("\n".join(text_parts))


async def process_photo_album(
    album_key: tuple[int, str],
    bot: Bot,
    documents_directory: Path,
    deal_id: str,
    source_chat_title: str,
) -> None:
    """Wait for an album to arrive, then save and recognize its pages together."""
    try:
        await asyncio.sleep(2)
        album_photos = photo_album_buffers.pop(album_key, [])
        photo_album_tasks.pop(album_key, None)
        if not album_photos:
            return

        album_label = f"Альбом из {len(album_photos)} файлов"
        declared_size = sum(photo.file_size or 0 for _, photo in album_photos)
        if exceeds_save_limit(declared_size):
            await send_notification(
                bot,
                f'Документ "{album_label}" не сохранён: общий размер превышает 50 МБ\n'
                f"Чат: {source_chat_title}",
            )
            return

        saved_paths = []
        for page_number, (_, photo) in enumerate(sorted(album_photos), start=1):
            filename = f"album_{album_key[1]}_page_{page_number}.jpg"
            destination = unique_file_path(documents_directory, filename)
            await bot.download(photo, destination=destination)
            saved_paths.append(destination)

        actual_size = sum(path.stat().st_size for path in saved_paths)
        if exceeds_save_limit(actual_size):
            for saved_path in saved_paths:
                remove_oversized_download(saved_path)
            await send_notification(
                bot,
                f'Документ "{album_label}" не сохранён: общий размер превышает 50 МБ\n'
                f"Чат: {source_chat_title}",
            )
            return

        album_skip_reason = None
        if actual_size > MAX_OCR_FILE_BYTES:
            album_skip_reason = "общий размер превышает 25 МБ"
        elif any(
            getattr(photo, "width", 0) * getattr(photo, "height", 0) > MAX_OCR_IMAGE_PIXELS
            for _, photo in album_photos
        ):
            album_skip_reason = "одно из изображений превышает 25 мегапикселей"

        document_name = None
        if album_skip_reason is None:
            document_name = await asyncio.to_thread(recognized_photo_album_name, saved_paths)
        if document_name:
            renamed_paths = []
            for page_number, saved_path in enumerate(saved_paths, start=1):
                destination = unique_file_path(
                    documents_directory, f"{document_name} {page_number}.jpg"
                )
                saved_path.rename(destination)
                renamed_paths.append(destination)
            await send_notification(
                bot,
                f"{document_name} сохранён: {len(renamed_paths)} стр.\nЧат: {source_chat_title}",
            )
            for renamed_path in renamed_paths:
                await asyncio.to_thread(drive_storage.upload_document, deal_id, renamed_path)
        else:
            reason = album_skip_reason or "тип документа не определён"
            await send_notification(
                bot,
                f'Документ "{album_label}" сохранён без распознавания: {reason}\n'
                f"Чат: {source_chat_title}",
            )
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
        process_photo_album(
            album_key,
            message.bot,
            documents_directory,
            deal_id,
            message.chat.title or str(message.chat.id),
        )
    )


async def create_deal_for_chat(message: Message, replace_existing: bool = False) -> Optional[Path]:
    """Create and link a deal without writing anything in the client chat."""
    if load_notification_chat_id() is None:
        logging.error("Cannot create a deal before the notification chat is configured")
        return None

    existing_deal_id = get_deal_id_for_chat(message)
    if existing_deal_id and not replace_existing:
        existing_folder = find_deal_folder(existing_deal_id)
        if existing_folder:
            return existing_folder

    created_at = datetime.now()
    deal_id = create_deal_id()
    deal_folder = create_deal_folder(deal_id, created_at)
    chat_deals = load_chat_deals()
    chat_deals[str(message.chat.id)] = deal_id
    save_chat_deals(chat_deals)
    drive_ready = await asyncio.to_thread(
        drive_storage.ensure_deal_folders, deal_id, deal_folder.name
    )
    await send_notification(message.bot, f"Создал папку в {created_at.strftime('%H:%M')}")
    if drive_storage.enabled and not drive_ready:
        await send_notification(
            message.bot,
            f"Google Drive временно недоступен. Локальная папка сохранена: {deal_folder.name}",
        )
    return deal_folder


@dp.message(F.new_chat_members)
async def bot_added_to_chat_handler(message: Message) -> None:
    """Silently initialize a deal when this bot is added to a client group."""
    if message.chat.type not in {"group", "supergroup"}:
        return
    bot_user = await message.bot.get_me()
    if not any(member.id == bot_user.id for member in message.new_chat_members):
        return
    if is_notification_chat(message):
        return
    if not is_authorized_team_user(message):
        await send_notification(
            message.bot,
            f"Бот отклонил добавление в посторонний чат: {message.chat.title or message.chat.id}",
        )
        await message.bot.leave_chat(message.chat.id)
        return
    await create_deal_for_chat(message)


@dp.my_chat_member()
async def bot_membership_updated_handler(event: ChatMemberUpdated) -> None:
    """Handle the dedicated Bot API update emitted when this bot joins a chat."""
    if event.chat.type not in {"group", "supergroup"}:
        return
    joined_statuses = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
    departed_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    if (
        event.old_chat_member.status not in departed_statuses
        or event.new_chat_member.status not in joined_statuses
    ):
        return
    if is_notification_chat(event):
        return
    if not is_authorized_team_user(event):
        await send_notification(
            event.bot,
            f"Бот отклонил добавление в посторонний чат: {event.chat.title or event.chat.id}",
        )
        await event.bot.leave_chat(event.chat.id)
        return
    await create_deal_for_chat(event)


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    if is_client_private_message(message):
        await offer_feedback(message, state)
        return
    if message.chat.type in {"group", "supergroup"}:
        return
    await message.answer("Здравствуйте! Я Mortgage AI.")


@dp.message(Command("set_notifications"))
async def set_notifications_handler(message: Message) -> None:
    """Bind a private team group as the only operational notification chat."""
    if (
        message.chat.type not in {"group", "supergroup"}
        or message.from_user is None
        or message.from_user.id != manager_user_id()
    ):
        return
    save_notification_chat_id(message.chat.id)
    await message.answer("Чат уведомлений подключён.")


@dp.message(Command("start_deal"))
async def start_deal_handler(message: Message, state: FSMContext) -> None:
    if is_client_private_message(message):
        await offer_feedback(message, state)
        return
    if (
        message.chat.type not in {"group", "supergroup"}
        or not is_authorized_team_user(message)
        or is_notification_chat(message)
    ):
        return
    await create_deal_for_chat(message, replace_existing=True)


@dp.message(Command("my_id"))
async def my_id_handler(message: Message) -> None:
    if not is_team_member(message):
        return
    if message.from_user is None:
        return
    if message.chat.type in {"group", "supergroup"} and not is_notification_chat(message):
        return
    await message.answer(f"Ваш технический Telegram ID: {message.from_user.id}")


@dp.message(Command("rates"))
async def rates_handler(message: Message) -> None:
    """Show the latest rate mentions collected from configured channels."""
    if not is_team_member(message):
        return
    if message.chat.type in {"group", "supergroup"} and not is_notification_chat(message):
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
    if message.chat.type in {"group", "supergroup"} and not is_notification_chat(message):
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

    if message.from_user is None:
        await message.answer("Спасибо! Сейчас отзыв нельзя отправить. Попробуйте позднее.")
        await state.clear()
        return
    if feedback_limit_reached(message.from_user.id):
        await message.answer(
            "Спасибо! Ваши отзывы уже отправлены. Следующий отзыв можно будет оставить позднее."
        )
        await state.clear()
        return

    recipient_id = manager_user_id()
    if recipient_id is None:
        logging.error("MANAGER_USER_ID is not configured for private feedback")
        await message.answer("Спасибо! Сейчас отзыв нельзя отправить. Попробуйте позднее.")
        await state.clear()
        return

    sender_name = message.from_user.full_name if message.from_user else "Неизвестный пользователь"
    username = (
        f" (@{message.from_user.username})"
        if message.from_user and message.from_user.username
        else ""
    )
    sender_id = message.from_user.id if message.from_user else "неизвестен"
    notification_text = (
        "Новый отзыв о работе ипотечной команды\n\n"
        f"Клиент: {sender_name}{username}\n"
        f"Telegram ID: {sender_id}\n"
        "Источник: личный чат с ботом\n"
        f"Оценка: {rating}/5\n"
        "Комментарий: не оставлен"
    )
    try:
        notification_message = await message.bot.send_message(recipient_id, notification_text)
    except Exception as error:
        logging.warning("Could not send rating notification: %s", error)
        await message.answer("Спасибо! Сейчас отзыв нельзя отправить. Попробуйте позднее.")
        await state.clear()
        return

    record_feedback(message.from_user.id)

    await state.update_data(
        rating=rating,
        feedback_recipient_id=recipient_id,
        feedback_message_id=notification_message.message_id,
        feedback_notification_text=notification_text,
    )
    await state.set_state(Feedback.waiting_for_comment)
    await message.answer(
        "Спасибо! Оценка уже отправлена. Если хотите, напишите комментарий или пожелание."
    )


@dp.message(Feedback.waiting_for_comment, F.text)
async def feedback_comment_handler(message: Message, state: FSMContext) -> None:
    comment = (message.text or "").strip()
    if not comment:
        await message.answer("Пожалуйста, напишите комментарий текстом.")
        return

    feedback_data = await state.get_data()
    recipient_id = feedback_data.get("feedback_recipient_id")
    notification_message_id = feedback_data.get("feedback_message_id")
    notification_text = feedback_data.get("feedback_notification_text", "")
    updated_text = notification_text.rsplit("\nКомментарий:", 1)[0] + f"\nКомментарий: {comment}"
    try:
        if recipient_id is None or notification_message_id is None:
            raise RuntimeError("Rating notification metadata is missing")
        await message.bot.edit_message_text(
            chat_id=recipient_id,
            message_id=notification_message_id,
            text=updated_text,
        )
    except Exception as error:
        logging.warning("Could not update rating notification: %s", error)
        fallback_recipient_id = recipient_id or manager_user_id()
        if fallback_recipient_id is not None:
            await message.bot.send_message(
                fallback_recipient_id,
                updated_text or f"Комментарий к отзыву: {comment}",
            )
    await state.clear()
    await message.answer("Спасибо за комментарий! Отзыв дополнен.")


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
        await send_notification(
            message.bot,
            f"Документ не сохранён: для чата {message.chat.title or message.chat.id} нет папки сделки.",
        )
        return

    document = message.document
    if document is None:
        return
    original_filename = Path(document.file_name or "document").name or "document"
    if exceeds_save_limit(getattr(document, "file_size", None)):
        await send_notification(
            message.bot,
            f'Документ "{original_filename}" не сохранён: размер превышает 50 МБ\n'
            f"Чат: {message.chat.title or message.chat.id}",
        )
        return
    deal_folder = find_deal_folder(deal_id)
    if not deal_folder:
        await send_notification(
            message.bot,
            f"Документ не сохранён: папка сделки для чата {message.chat.title or message.chat.id} не найдена.",
        )
        return
    documents_directory = documents_directory_for_deal(deal_folder)
    destination = unique_file_path(documents_directory, original_filename)
    await message.bot.download(document, destination=destination)
    actual_size = destination.stat().st_size
    if exceeds_save_limit(actual_size):
        remove_oversized_download(destination)
        await send_notification(
            message.bot,
            f'Документ "{original_filename}" не сохранён: размер превышает 50 МБ\n'
            f"Чат: {message.chat.title or message.chat.id}",
        )
        return
    skip_reason = ocr_skip_reason(destination, actual_size)
    renamed_path = None
    if skip_reason is None:
        renamed_path = await asyncio.to_thread(rename_recognized_document, destination)
    saved_path = renamed_path or destination
    uploaded = await asyncio.to_thread(drive_storage.upload_document, deal_id, saved_path)
    if renamed_path:
        response = f"Документ сохранён: {saved_path.name}"
    else:
        reason = skip_reason or "тип документа не определён"
        response = f'Документ "{saved_path.name}" сохранён без распознавания: {reason}'
    response += f"\nЧат: {message.chat.title or message.chat.id}"
    if drive_storage.enabled:
        response += "\nКопия загружена в Google Drive." if uploaded else "\nЛокальная копия сохранена; Google Drive временно недоступен."
    await send_notification(message.bot, response)


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
        await send_notification(
            message.bot,
            f"Фотография не сохранена: для чата {message.chat.title or message.chat.id} нет папки сделки.",
        )
        return

    photo = message.photo[-1]
    filename = f"photo_{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
    if exceeds_save_limit(getattr(photo, "file_size", None)):
        await send_notification(
            message.bot,
            f'Документ "{filename}" не сохранён: размер превышает 50 МБ\n'
            f"Чат: {message.chat.title or message.chat.id}",
        )
        return
    deal_folder = find_deal_folder(deal_id)
    if not deal_folder:
        await send_notification(
            message.bot,
            f"Фотография не сохранена: папка сделки для чата {message.chat.title or message.chat.id} не найдена.",
        )
        return
    documents_directory = documents_directory_for_deal(deal_folder)
    if message.media_group_id:
        await queue_photo_album(message, documents_directory, deal_id)
        return

    destination = unique_file_path(documents_directory, filename)
    await message.bot.download(photo, destination=destination)
    actual_size = destination.stat().st_size
    if exceeds_save_limit(actual_size):
        remove_oversized_download(destination)
        await send_notification(
            message.bot,
            f'Документ "{filename}" не сохранён: размер превышает 50 МБ\n'
            f"Чат: {message.chat.title or message.chat.id}",
        )
        return
    skip_reason = ocr_skip_reason(destination, actual_size)
    renamed_path = None
    if skip_reason is None:
        renamed_path = await asyncio.to_thread(rename_recognized_document, destination)
    saved_path = renamed_path or destination
    uploaded = await asyncio.to_thread(drive_storage.upload_document, deal_id, saved_path)
    if renamed_path:
        response = f"Документ сохранён: {saved_path.name}"
    else:
        reason = skip_reason or "тип документа не определён"
        response = f'Документ "{saved_path.name}" сохранён без распознавания: {reason}'
    response += f"\nЧат: {message.chat.title or message.chat.id}"
    if drive_storage.enabled:
        response += "\nКопия загружена в Google Drive." if uploaded else "\nЛокальная копия сохранена; Google Drive временно недоступен."
    await send_notification(message.bot, response)


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
