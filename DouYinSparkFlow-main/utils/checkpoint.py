import json
import os
import tempfile
from datetime import date, datetime


CHECKPOINT_DIR = "checkpoints"
CHECKPOINT_FILE = "send-progress.json"


def _checkpoint_path():
    return os.getenv(
        "DYSF_CHECKPOINT_FILE",
        os.path.join(CHECKPOINT_DIR, CHECKPOINT_FILE),
    )


def _today_key():
    return date.today().isoformat()


def load_checkpoint():
    path = _checkpoint_path()
    if not os.path.exists(path):
        return {"version": 1, "days": {}}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"version": 1, "days": {}}

    if not isinstance(data, dict):
        return {"version": 1, "days": {}}

    data.setdefault("version", 1)
    data.setdefault("days", {})
    return data


def save_checkpoint(data):
    path = _checkpoint_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        prefix=".send-progress-",
        suffix=".tmp",
        dir=directory or ".",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception:
        raise


def get_completed_targets(data, unique_id):
    day = data.get("days", {}).get(_today_key(), {})
    account = day.get("accounts", {}).get(str(unique_id), {})
    completed = account.get("completed", {})
    if not isinstance(completed, dict):
        return set()
    return set(completed.keys())


def mark_target_completed(data, unique_id, username, target_name, message):
    day = data.setdefault("days", {}).setdefault(_today_key(), {})
    accounts = day.setdefault("accounts", {})
    account = accounts.setdefault(str(unique_id), {})
    account["username"] = username
    completed = account.setdefault("completed", {})
    completed[target_name] = {
        "sent_at": datetime.now().isoformat(timespec="seconds"),
        "message": message,
    }
    return data
