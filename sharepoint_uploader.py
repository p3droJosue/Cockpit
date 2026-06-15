"""
sharepoint_uploader.py
-----------------------
Uploads CSV files to a SharePoint document library folder
using the Microsoft Graph API with app-only (client credentials) auth.

Required Azure AD app permissions (application, not delegated):
  - Sites.ReadWrite.All   (or Sites.Selected for least-privilege)
  - Files.ReadWrite.All
"""

import logging
from pathlib import Path

import requests
import yaml

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class SharePointUploader:
    def __init__(self, config: dict):
        self.cfg = config["sharepoint"]
        self._token = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_all(self, file_paths) -> list:
        """
        Upload every file in file_paths to the SharePoint folder.
        Returns a list of the remote URLs for the uploaded files.
        """
        self._token = self._get_token()
        site_id = self._get_site_id()
        drive_id = self._get_drive_id(site_id)
        remote_urls = []

        for path in file_paths:
            try:
                url = self._upload_file(drive_id, path)
                remote_urls.append(url)
                logger.info("Uploaded: %s → %s", path.name, url)
            except Exception as exc:
                logger.error("Failed to upload %s: %s", path.name, exc)

        return remote_urls

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Obtain an OAuth 2.0 access token via client credentials flow."""
        url = TOKEN_URL.format(tenant_id=self.cfg["tenant_id"])
        resp = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.cfg["client_id"],
                "client_secret": self.cfg["client_secret"],
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        logger.debug("Access token obtained.")
        return token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    # ------------------------------------------------------------------
    # Graph API helpers
    # ------------------------------------------------------------------

    def _get_site_id(self) -> str:
        """Resolve the SharePoint site to its Graph site ID."""
        hostname = self.cfg["sharepoint_hostname"]
        resp = requests.get(
            f"{GRAPH_BASE}/sites/{hostname}:/sites/{self.cfg['site_name']}",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        site_id = resp.json()["id"]
        logger.debug("Resolved site ID: %s", site_id)
        return site_id

    def _get_drive_id(self, site_id: str) -> str:
        """Find the drive (document library) by name."""
        resp = requests.get(
            f"{GRAPH_BASE}/sites/{site_id}/drives",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        drives = resp.json().get("value", [])
        for drive in drives:
            if drive["name"].lower() == self.cfg["drive_name"].lower():
                logger.debug("Resolved drive ID: %s", drive["id"])
                return drive["id"]
        raise ValueError(
            f"Drive '{self.cfg['drive_name']}' not found in site. "
            f"Available drives: {[d['name'] for d in drives]}"
        )

    def _upload_file(self, drive_id: str, local_path: Path) -> str:
        """
        Upload a single file using the Graph upload session
        (works for files of any size). Returns the web URL.
        """
        remote_path = f"{self.cfg['folder_path']}/{local_path.name}"
        file_size = local_path.stat().st_size

        # Create an upload session
        session_url = (
            f"{GRAPH_BASE}/drives/{drive_id}/root:/{remote_path}:/createUploadSession"
        )
        session_resp = requests.post(
            session_url,
            headers=self._headers(),
            json={
                "item": {
                    "@microsoft.graph.conflictBehavior": "replace",
                    "name": local_path.name,
                }
            },
            timeout=30,
        )
        session_resp.raise_for_status()
        upload_url = session_resp.json()["uploadUrl"]

        # Upload the file in one chunk (fine for CSVs < 60 MB)
        with open(local_path, "rb") as f:
            data = f.read()

        upload_resp = requests.put(
            upload_url,
            data=data,
            headers={
                "Content-Length": str(file_size),
                "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
            },
            timeout=120,
        )
        upload_resp.raise_for_status()
        return upload_resp.json().get("webUrl", "")


# ------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    uploader = SharePointUploader(cfg)

    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        print("Usage: python sharepoint_uploader.py file1.csv file2.csv ...")
        sys.exit(1)

    urls = uploader.upload_all(paths)
    print(f"\nUploaded {len(urls)} file(s):")
    for u in urls:
        print(f"  {u}")