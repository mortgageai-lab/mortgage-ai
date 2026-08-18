"""One-time Google Drive authorization for the server deployment."""

from pathlib import Path

from dotenv import load_dotenv

from drive_storage import authorize_google_drive


PROJECT_DIRECTORY = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIRECTORY / ".env")


if __name__ == "__main__":
    authorize_google_drive(PROJECT_DIRECTORY)
