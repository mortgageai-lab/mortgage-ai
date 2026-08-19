"""Optional Google Drive mirror for Mortgage AI client documents.

The bot always saves a local copy first.  This module only adds a second copy
to Google Drive when it is explicitly enabled in the private .env file.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
CHAT_DOCUMENTS_FOLDER_NAME = "Документы из чата"


class GoogleDriveStorage:
    """Create deal folders and mirror documents to the dedicated Drive."""

    def __init__(self, project_directory: Path, data_directory: Path) -> None:
        self.project_directory = project_directory
        self.data_directory = data_directory
        self.enabled = os.getenv("GOOGLE_DRIVE_ENABLED", "false").lower() == "true"
        self.client_file = self._configured_path(
            "GOOGLE_DRIVE_CLIENT_FILE", "secrets/google-oauth-client.json"
        )
        self.token_file = self._configured_path(
            "GOOGLE_DRIVE_TOKEN_FILE", "secrets/google-oauth-token.json"
        )
        self.settings_file = data_directory / "google_drive_settings.json"
        self.deals_file = data_directory / "google_drive_deals.json"
        self.root_name = os.getenv("GOOGLE_DRIVE_ROOT_NAME", "Mortgage AI — Сделки (бот)")
        self._service = None

    def _configured_path(self, variable_name: str, default: str) -> Path:
        path = Path(os.getenv(variable_name, default))
        return path if path.is_absolute() else self.project_directory / path

    def _load_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            with path.open(encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            logging.warning("Could not read Google Drive settings file: %s", path)
            return {}

    def _save_json(self, path: Path, value: dict) -> None:
        self.data_directory.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)

    def _get_service(self):
        if not self.enabled:
            return None
        if self._service is not None:
            return self._service
        if not self.client_file.exists() or not self.token_file.exists():
            logging.warning("Google Drive is enabled but authorization files are incomplete")
            return None

        credentials = Credentials.from_authorized_user_file(self.token_file, [DRIVE_FILE_SCOPE])
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self.token_file.write_text(credentials.to_json(), encoding="utf-8")
        if not credentials.valid:
            logging.warning("Google Drive authorization is no longer valid")
            return None
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    def _create_folder(self, name: str, parent_id: Optional[str] = None) -> str:
        service = self._get_service()
        if service is None:
            raise RuntimeError("Google Drive is not authorized")
        metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            metadata["parents"] = [parent_id]
        response = service.files().create(body=metadata, fields="id").execute()
        return response["id"]

    def _root_folder_id(self) -> str:
        settings = self._load_json(self.settings_file)
        root_id = settings.get("root_folder_id")
        if root_id:
            return root_id
        root_id = self._create_folder(self.root_name)
        self._save_json(self.settings_file, {"root_folder_id": root_id})
        logging.info("Created Google Drive root folder: %s", self.root_name)
        return root_id

    def ensure_deal_folders(self, deal_id: str, deal_name: str) -> bool:
        """Create the matching Drive folders once, returning success status."""
        if not self.enabled:
            return False
        try:
            deals = self._load_json(self.deals_file)
            if deal_id in deals:
                return True
            deal_folder_id = self._create_folder(deal_name, self._root_folder_id())
            documents_folder_id = self._create_folder(CHAT_DOCUMENTS_FOLDER_NAME, deal_folder_id)
            deals[deal_id] = {
                "deal_folder_id": deal_folder_id,
                "documents_folder_id": documents_folder_id,
            }
            self._save_json(self.deals_file, deals)
            return True
        except (HttpError, OSError, RuntimeError) as error:
            logging.warning("Could not create Google Drive folders for %s: %s", deal_id, error)
            return False

    def upload_document(self, deal_id: str, file_path: Path) -> bool:
        """Upload the final local filename to the deal's chat-documents folder."""
        if not self.enabled:
            return False
        try:
            deals = self._load_json(self.deals_file)
            documents_folder_id = deals.get(deal_id, {}).get("documents_folder_id")
            if not documents_folder_id:
                return False
            from googleapiclient.http import MediaFileUpload

            media = MediaFileUpload(str(file_path), resumable=True)
            self._get_service().files().create(
                body={"name": file_path.name, "parents": [documents_folder_id]},
                media_body=media,
                fields="id",
            ).execute()
            return True
        except (HttpError, OSError, RuntimeError) as error:
            logging.warning("Could not upload %s to Google Drive: %s", file_path.name, error)
            return False


def authorize_google_drive(project_directory: Path) -> None:
    """Run the one-time OAuth flow and save its token outside Git."""
    client_file = project_directory / os.getenv(
        "GOOGLE_DRIVE_CLIENT_FILE", "secrets/google-oauth-client.json"
    )
    token_file = project_directory / os.getenv(
        "GOOGLE_DRIVE_TOKEN_FILE", "secrets/google-oauth-token.json"
    )
    if not client_file.exists():
        raise RuntimeError(f"Не найден файл OAuth: {client_file}")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(client_file, [DRIVE_FILE_SCOPE])
    credentials = flow.run_local_server(host="localhost", port=8080, open_browser=False)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    os.chmod(token_file, 0o600)
    print(f"Авторизация Google Drive завершена. Токен сохранён: {token_file}")
