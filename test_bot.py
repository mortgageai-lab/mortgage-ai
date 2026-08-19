import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from drive_storage import GoogleDriveStorage
from PIL import Image, ImageDraw


class DealFolderTests(unittest.TestCase):
    def test_new_deal_uses_requested_russian_names(self):
        with tempfile.TemporaryDirectory() as directory:
            deals_directory = Path(directory)
            with patch.object(bot, "DEALS_DIRECTORY", deals_directory):
                deal_folder = bot.create_deal_folder(
                    "DEAL-1", datetime(2026, 8, 19, 17, 52)
                )

            self.assertEqual(deal_folder.name, "Телеграмм 19.08 - 17:52")
            self.assertTrue((deal_folder / "Документы из чата").is_dir())
            metadata = json.loads((deal_folder / bot.DEAL_METADATA_FILE).read_text())
            self.assertEqual(metadata["deal_id"], "DEAL-1")

    def test_same_minute_gets_safe_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            deals_directory = Path(directory)
            moment = datetime(2026, 8, 19, 17, 52)
            with patch.object(bot, "DEALS_DIRECTORY", deals_directory):
                bot.create_deal_folder("DEAL-1", moment)
                second_folder = bot.create_deal_folder("DEAL-2", moment)

            self.assertEqual(second_folder.name, "Телеграмм 19.08 - 17:52 (2)")

    def test_existing_deal_keeps_legacy_documents_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            deal_folder = Path(directory)
            legacy_folder = deal_folder / "Documents"
            legacy_folder.mkdir()

            self.assertEqual(bot.documents_directory_for_deal(deal_folder), legacy_folder)


class RecognitionTests(unittest.TestCase):
    def test_crops_small_document_from_blank_scanner_page(self):
        image = Image.new("RGB", (1200, 900), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((700, 100, 1120, 430), fill=(210, 225, 200), outline="black", width=4)
        draw.point((50, 800), fill="black")

        cropped = bot.crop_to_primary_document(image)

        self.assertLess(cropped.width, 700)
        self.assertLess(cropped.height, 600)
        self.assertGreater(cropped.width, 350)

    def test_supported_document_names_remain_available(self):
        examples = {
            "ДОВЕРЕННОСТЬ паспорт гражданина Российской Федерации": "Доверенность",
            "ПАСПОРТ РОССИЙСКОЙ ФЕДЕРАЦИИ": "Паспорт",
            "СНИЛС страховой номер индивидуального лицевого счета": "СНИЛС",
            "Страховой номер индивидуального лицевого счёта": "СНИЛС",
            "СТРАХОВОЕ СВИДЕТЕЛЬСТВО ОБЯЗАТЕЛЬНОГО ПЕНСИОННОГО": "СНИЛС",
            "Справка о доходах и суммах налога физического лица НДФЛ": "Справка 2-НДФЛ",
            "СВИДЕТЕЛЬСТВО О ЗАКЛЮЧЕНИИ БРАКА": "Свидетельство о заключении брака",
            "СВИДЕТЕЛЬСТВО О РАСТОРЖЕНИИ БРАКА": "Свидетельство о расторжении брака",
            "СВИДЕТЕЛЬСТВО О РОЖДЕНИИ": "Свидетельство о рождении",
        }
        for text, expected_name in examples.items():
            with self.subTest(expected_name=expected_name):
                self.assertEqual(bot.recognized_document_name(text), expected_name)

    def test_power_of_attorney_title_overrides_embedded_passport_details(self):
        text = "ДОВЕРЕННОСТЬ паспорт гражданина РФ фамилия имя дата рождения"

        self.assertTrue(bot.is_russian_passport(text))
        self.assertEqual(bot.recognized_document_name(text), "Доверенность")

    def test_album_uses_title_before_passport_details(self):
        album_pages = [Path("page-1.jpg"), Path("page-2.jpg")]
        with patch.object(
            bot,
            "extract_document_text",
            side_effect=[
                "ДОВЕРЕННОСТЬ паспорт гражданина РФ",
                "фамилия имя дата рождения",
            ],
        ):
            result = bot.recognized_photo_album_name(album_pages)

        self.assertEqual(result, "Доверенность")


class GoogleDriveFolderTests(unittest.TestCase):
    def test_new_drive_deal_uses_russian_documents_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = GoogleDriveStorage(Path(directory), Path(directory) / "data")
            storage.enabled = True
            with patch.object(storage, "_root_folder_id", return_value="root"), patch.object(
                storage, "_create_folder", side_effect=["deal", "documents"]
            ) as create_folder:
                self.assertTrue(storage.ensure_deal_folders("DEAL-1", "Телеграмм 19.08 - 17:52"))

            self.assertEqual(
                create_folder.call_args_list[1].args,
                ("Документы из чата", "deal"),
            )


class FileLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_unsupported_format_is_saved_but_skips_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "archive.zip"
            file_path.write_bytes(b"zip")

            reason = bot.ocr_skip_reason(file_path)

        self.assertEqual(reason, "формат .zip не поддерживает распознавание")

    def test_file_over_25_mb_skips_ocr(self):
        reason = bot.ocr_skip_reason(Path("large.pdf"), bot.MAX_OCR_FILE_BYTES + 1)

        self.assertEqual(reason, "размер превышает 25 МБ")

    def test_image_over_25_megapixels_skips_ocr(self):
        image = MagicMock(width=6000, height=5000)
        image.__enter__.return_value = image
        with patch.object(bot.Image, "open", return_value=image):
            reason = bot.ocr_skip_reason(Path("large.jpg"), 1024)

        self.assertEqual(reason, "изображение превышает 25 мегапикселей")

    async def test_file_over_50_mb_is_rejected_before_download(self):
        telegram_bot = SimpleNamespace(download=AsyncMock())
        message = SimpleNamespace(
            chat=SimpleNamespace(type="supergroup", id=-1006, title="Тестовая сделка"),
            from_user=SimpleNamespace(id=999, username="client"),
            document=SimpleNamespace(
                file_name="large.pdf",
                file_size=bot.MAX_SAVE_FILE_BYTES + 1,
            ),
            bot=telegram_bot,
            answer=AsyncMock(),
        )

        with patch.object(bot, "get_deal_id_for_chat", return_value="DEAL-1"), patch.object(
            bot, "send_notification", new=AsyncMock()
        ) as notify:
            await bot.document_handler(message)

        telegram_bot.download.assert_not_awaited()
        notification = notify.await_args.args[1]
        self.assertIn('Документ "large.pdf" не сохранён', notification)
        self.assertIn("размер превышает 50 МБ", notification)

    async def test_file_between_limits_is_saved_without_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            deal_folder = Path(directory)
            (deal_folder / bot.CHAT_DOCUMENTS_FOLDER_NAME).mkdir()

            async def save_large_download(_, destination):
                with Path(destination).open("wb") as file:
                    file.truncate(bot.MAX_OCR_FILE_BYTES + 1)

            telegram_bot = SimpleNamespace(download=AsyncMock(side_effect=save_large_download))
            message = SimpleNamespace(
                chat=SimpleNamespace(type="supergroup", id=-1007, title="Тестовая сделка"),
                from_user=SimpleNamespace(id=999, username="client"),
                document=SimpleNamespace(
                    file_name="medium.pdf",
                    file_size=bot.MAX_OCR_FILE_BYTES + 1,
                ),
                bot=telegram_bot,
                answer=AsyncMock(),
            )

            with patch.object(bot, "get_deal_id_for_chat", return_value="DEAL-1"), patch.object(
                bot, "find_deal_folder", return_value=deal_folder
            ), patch.object(bot, "rename_recognized_document") as recognize, patch.object(
                bot.drive_storage, "upload_document", return_value=True
            ), patch.object(bot, "send_notification", new=AsyncMock()) as notify:
                await bot.document_handler(message)

        recognize.assert_not_called()
        notification = notify.await_args.args[1]
        self.assertIn('Документ "medium.pdf" сохранён без распознавания', notification)
        self.assertIn("размер превышает 25 МБ", notification)


class SilentChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_command_is_silent_in_client_group(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(type="supergroup"),
            from_user=SimpleNamespace(id=999, username="client"),
            answer=AsyncMock(),
        )

        await bot.start_handler(message, AsyncMock())

        message.answer.assert_not_awaited()

    async def test_adding_bot_creates_deal_without_client_reply(self):
        telegram_bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=42)))
        message = SimpleNamespace(
            chat=SimpleNamespace(type="supergroup", id=-1001),
            bot=telegram_bot,
            new_chat_members=[SimpleNamespace(id=42)],
            answer=AsyncMock(),
        )

        with patch.object(bot, "is_notification_chat", return_value=False), patch.object(
            bot, "is_authorized_team_user", return_value=True
        ), patch.object(bot, "create_deal_for_chat", new=AsyncMock()) as create_deal:
            await bot.bot_added_to_chat_handler(message)

        create_deal.assert_awaited_once_with(message)
        message.answer.assert_not_awaited()

    async def test_membership_update_creates_deal_for_new_chat(self):
        event = SimpleNamespace(
            chat=SimpleNamespace(type="supergroup", id=-1003),
            old_chat_member=SimpleNamespace(status=bot.ChatMemberStatus.LEFT),
            new_chat_member=SimpleNamespace(status=bot.ChatMemberStatus.MEMBER),
        )

        with patch.object(bot, "is_notification_chat", return_value=False), patch.object(
            bot, "is_authorized_team_user", return_value=True
        ), patch.object(bot, "create_deal_for_chat", new=AsyncMock()) as create_deal:
            await bot.bot_membership_updated_handler(event)

        create_deal.assert_awaited_once_with(event)

    async def test_unauthorized_addition_makes_bot_leave_without_creating_deal(self):
        telegram_bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=42)),
            leave_chat=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type="supergroup", id=-1004, title="Посторонний чат"),
            bot=telegram_bot,
            from_user=SimpleNamespace(id=999),
            new_chat_members=[SimpleNamespace(id=42)],
        )

        with patch.object(bot, "is_notification_chat", return_value=False), patch.object(
            bot, "is_authorized_team_user", return_value=False
        ), patch.object(bot, "send_notification", new=AsyncMock()), patch.object(
            bot, "create_deal_for_chat", new=AsyncMock()
        ) as create_deal:
            await bot.bot_added_to_chat_handler(message)

        telegram_bot.leave_chat.assert_awaited_once_with(-1004)
        create_deal.assert_not_awaited()

    async def test_only_manager_can_rebind_notification_chat(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(type="supergroup", id=-1005),
            from_user=SimpleNamespace(id=777),
            answer=AsyncMock(),
        )

        with patch.object(bot, "manager_user_id", return_value=111), patch.object(
            bot, "save_notification_chat_id"
        ) as save_chat:
            await bot.set_notifications_handler(message)

        save_chat.assert_not_called()
        message.answer.assert_not_awaited()

    async def test_document_result_goes_to_notifications_only(self):
        with tempfile.TemporaryDirectory() as directory:
            deal_folder = Path(directory)
            (deal_folder / bot.CHAT_DOCUMENTS_FOLDER_NAME).mkdir()
            async def save_download(_, destination):
                Path(destination).write_bytes(b"test")

            telegram_bot = SimpleNamespace(download=AsyncMock(side_effect=save_download))
            message = SimpleNamespace(
                chat=SimpleNamespace(type="supergroup", id=-1002, title="Тестовая сделка"),
                from_user=SimpleNamespace(id=999, username="client"),
                document=SimpleNamespace(file_name="test.pdf"),
                bot=telegram_bot,
                answer=AsyncMock(),
            )

            with patch.object(bot, "get_deal_id_for_chat", return_value="DEAL-1"), patch.object(
                bot, "find_deal_folder", return_value=deal_folder
            ), patch.object(bot, "rename_recognized_document", return_value=None), patch.object(
                bot.drive_storage, "upload_document", return_value=False
            ), patch.object(bot, "send_notification", new=AsyncMock()) as notify:
                await bot.document_handler(message)

        notify.assert_awaited_once()
        message.answer.assert_not_awaited()


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_rating_is_reported_immediately_with_client_identity(self):
        telegram_bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=321))
        )
        message = SimpleNamespace(
            text="1",
            from_user=SimpleNamespace(id=555, username="client", full_name="Тестовый клиент"),
            bot=telegram_bot,
            answer=AsyncMock(),
        )
        state = SimpleNamespace(
            update_data=AsyncMock(),
            set_state=AsyncMock(),
            clear=AsyncMock(),
        )

        with patch.object(bot, "load_notification_chat_id", return_value=-1001):
            await bot.feedback_rating_handler(message, state)

        sent_text = telegram_bot.send_message.await_args.args[1]
        self.assertIn("Оценка: 1/5", sent_text)
        self.assertIn("Тестовый клиент (@client)", sent_text)
        self.assertIn("Telegram ID: 555", sent_text)
        state.update_data.assert_awaited_once()
        state.set_state.assert_awaited_once_with(bot.Feedback.waiting_for_comment)

    async def test_comment_updates_existing_rating_notification(self):
        telegram_bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )
        message = SimpleNamespace(
            text="Нужно быстрее отвечать",
            bot=telegram_bot,
            answer=AsyncMock(),
        )
        state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={
                    "rating": "1",
                    "feedback_recipient_id": -1001,
                    "feedback_message_id": 321,
                    "feedback_notification_text": "Оценка: 1/5\nКомментарий: не оставлен",
                }
            ),
            clear=AsyncMock(),
        )

        await bot.feedback_comment_handler(message, state)

        telegram_bot.edit_message_text.assert_awaited_once_with(
            chat_id=-1001,
            message_id=321,
            text="Оценка: 1/5\nКомментарий: Нужно быстрее отвечать",
        )
        telegram_bot.send_message.assert_not_awaited()
        state.clear.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
