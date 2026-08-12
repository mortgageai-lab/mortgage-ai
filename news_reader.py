"""Read configured public Telegram channels through a local Telegram session."""

import asyncio
import logging
import os
from datetime import timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events

from news_monitor import NewsStore


load_dotenv()
ROOT = Path(__file__).resolve().parent
STORE = NewsStore(ROOT / "data" / "mortgage_news.sqlite3")


def configured_channels() -> list[str]:
    return [item.strip().removeprefix("@").removeprefix("https://t.me/")
            for item in os.getenv("NEWS_CHANNELS", "").split(",") if item.strip()]


async def main() -> None:
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    channels = configured_channels()
    if not api_id or not api_hash or not channels:
        raise RuntimeError("Заполните TELEGRAM_API_ID, TELEGRAM_API_HASH и NEWS_CHANNELS в .env")

    client = TelegramClient(str(ROOT / "data" / "news_reader"), int(api_id), api_hash)
    await client.start()

    async def save(message, channel: str) -> None:
        if not message.text or not message.date:
            return
        count = STORE.save_post(
            channel, message.id, message.date.astimezone(timezone.utc), message.text,
            f"https://t.me/{channel}/{message.id}",
        )
        if count:
            logging.info("Found %s rate mention(s) in @%s/%s", count, channel, message.id)

    for channel in channels:
        async for message in client.iter_messages(channel, limit=100):
            await save(message, channel)

    @client.on(events.NewMessage(chats=channels))
    async def new_post(event) -> None:
        username = getattr(await event.get_chat(), "username", None)
        if username:
            await save(event.message, username)

    logging.info("Monitoring channels: %s", ", ".join("@" + c for c in channels))
    await client.run_until_disconnected()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
