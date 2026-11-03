import re
from urllib.parse import urlsplit


def parse(url: str, raw: dict) -> tuple[str, str, str] | None:
    note_id = str(raw.get("id") or "")
    if not re.fullmatch(r"[a-fA-F0-9]{24}", note_id):
        match = re.search(
            r"/(?:explore|discovery/item)/([a-fA-F0-9]{24})(?:/|$)", urlsplit(url).path
        )
        note_id = match[1] if match else ""
    if note_id:
        # Canonical identity URL only. Keep signed access URL separately, unchanged.
        return note_id, "", f"https://www.xiaohongshu.com/explore/{note_id}"
    return None
