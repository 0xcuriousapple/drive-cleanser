"""Google Drive connector.

Safety model:
- Default OAuth scope is drive.readonly — indexing and analysis physically
  cannot modify anything in the account.
- Executing approved trash actions requires re-authenticating with the full
  drive scope (POST /api/sources/gdrive/connect {"write": true}); until then
  trash() raises with a clear message.
- trash() only ever sets trashed=true (Drive keeps trashed files 30 days);
  restore() sets it back. Nothing is permanently deleted by this app.
"""
import io
from pathlib import Path
from typing import Iterator

from .. import config
from .base import Source

READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/drive"

LIST_FIELDS = ("nextPageToken, files(id, name, mimeType, size, md5Checksum, createdTime, "
               "modifiedTime, parents, imageMediaMetadata(width,height,time,cameraMake,cameraModel,location), "
               "videoMediaMetadata(width,height,durationMillis))")


class GDriveSource(Source):
    name = "gdrive"

    def __init__(self):
        self._service = None

    # -- auth ---------------------------------------------------------------
    def connect(self, write: bool = False) -> dict:
        """Run the OAuth installed-app flow. Opens a browser tab."""
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not config.GDRIVE_CREDENTIALS.exists():
            raise RuntimeError(
                f"Missing {config.GDRIVE_CREDENTIALS}. Create an OAuth client (Desktop app) in "
                "Google Cloud Console with the Drive API enabled, download the JSON, and save it there.")
        scopes = [WRITE_SCOPE if write else READONLY_SCOPE]
        flow = InstalledAppFlow.from_client_secrets_file(str(config.GDRIVE_CREDENTIALS), scopes)
        creds = flow.run_local_server(port=0)
        config.GDRIVE_TOKEN.write_text(creds.to_json())
        self._service = None
        return {"scopes": scopes}

    def _creds(self):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        if not config.GDRIVE_TOKEN.exists():
            raise RuntimeError("Google Drive not connected — POST /api/sources/gdrive/connect first.")
        creds = Credentials.from_authorized_user_file(str(config.GDRIVE_TOKEN))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            config.GDRIVE_TOKEN.write_text(creds.to_json())
        return creds

    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build
            self._service = build("drive", "v3", credentials=self._creds(), cache_discovery=False)
        return self._service

    def status(self) -> dict:
        if not config.GDRIVE_TOKEN.exists():
            return {"connected": False, "write": False}
        creds = self._creds()
        return {"connected": True, "write": WRITE_SCOPE in (creds.scopes or [])}

    # -- read-only ------------------------------------------------------------
    def list_media(self) -> Iterator[dict]:
        svc = self.service()
        q = ("(mimeType contains 'image/' or mimeType contains 'video/') "
             "and trashed = false")
        token = None
        while True:
            resp = svc.files().list(q=q, fields=LIST_FIELDS, pageSize=1000,
                                    pageToken=token, spaces="drive").execute()
            for f in resp.get("files", []):
                img = f.get("imageMediaMetadata") or {}
                vid = f.get("videoMediaMetadata") or {}
                loc = img.get("location") or {}
                yield {
                    "source_id": f["id"],
                    "name": f.get("name"),
                    "path": f.get("name"),
                    "mime": f.get("mimeType"),
                    "size": int(f.get("size", 0) or 0),
                    "md5": f.get("md5Checksum"),
                    "created_time": f.get("createdTime"),
                    "modified_time": f.get("modifiedTime"),
                    "taken_time": img.get("time"),
                    "camera": " ".join(x for x in (img.get("cameraMake"), img.get("cameraModel")) if x) or None,
                    "gps_lat": loc.get("latitude"), "gps_lon": loc.get("longitude"),
                    "width": img.get("width") or vid.get("width"),
                    "height": img.get("height") or vid.get("height"),
                    "duration": (int(vid["durationMillis"]) / 1000.0) if vid.get("durationMillis") else None,
                }
            token = resp.get("nextPageToken")
            if not token:
                return

    def fetch_preview(self, source_id: str, dest: Path) -> Path | None:
        """Drive serves resizable previews via thumbnailLink (short-lived URL,
        so fetch it fresh). ~200-400KB at 1600px vs multi-MB originals — this
        is what makes scanning a 100k-photo library practical."""
        import re
        from google.auth.transport.requests import AuthorizedSession
        meta = self.service().files().get(fileId=source_id, fields="thumbnailLink").execute()
        link = meta.get("thumbnailLink")
        if not link:
            return None
        link = re.sub(r"=s\d+$", f"=s{config.PREVIEW_SIZE}", link)
        resp = AuthorizedSession(self._creds()).get(link, timeout=120)
        if resp.status_code != 200 or not resp.content:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest

    def stream_spec(self, source_id: str) -> tuple[str, dict] | None:
        """Drive supports ranged reads on alt=media, so ffmpeg can analyze
        videos straight off the API without a local download."""
        creds = self._creds()
        return (f"https://www.googleapis.com/drive/v3/files/{source_id}?alt=media",
                {"Authorization": f"Bearer {creds.token}"})

    def download(self, source_id: str, dest: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = self.service().files().get_media(fileId=source_id)
        with io.FileIO(dest, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
        return dest

    # -- write (only via execution engine, only with write scope) -------------
    def trash(self, source_id: str) -> dict:
        if not self.status().get("write"):
            raise RuntimeError(
                "Connected with read-only scope. To execute approved actions, reconnect with "
                'write access: POST /api/sources/gdrive/connect {"write": true}')
        self.service().files().update(fileId=source_id, body={"trashed": True}).execute()
        return {"method": "drive_trash"}

    def restore(self, source_id: str, undo_info: dict) -> None:
        self.service().files().update(fileId=source_id, body={"trashed": False}).execute()
