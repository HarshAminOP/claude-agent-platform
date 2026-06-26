"""Cross-server message passing via atomic file writes.

One server drops JSONL messages into another server's inbox directory.
The owning server polls its inbox, processes messages, and acks or nacks them.
Dead-letter after 3 failed attempts.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("platform.inbox")

_MAX_ATTEMPTS = 3


def drop_message(
    target: str,
    source: str,
    message_type: str,
    payload: dict,
    inbox_base: Path,
) -> str:
    message_id = str(uuid.uuid4())
    inbox_dir = inbox_base / target
    inbox_dir.mkdir(parents=True, exist_ok=True)

    message = {
        "id": message_id,
        "source": source,
        "target": target,
        "type": message_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attempts": 0,
    }

    tmp_path = inbox_dir / f"{message_id}.tmp"
    final_path = inbox_dir / f"{message_id}.jsonl"

    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(message, f)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, final_path)

    logger.debug(
        "Dropped message %s from %s → %s (type=%s)",
        message_id, source, target, message_type,
    )
    return message_id


def poll_inbox(target: str, inbox_base: Path) -> list[dict]:
    inbox_dir = inbox_base / target
    if not inbox_dir.exists():
        return []

    messages = []
    for path in sorted(inbox_dir.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as f:
                message = json.load(f)
            message["_path"] = str(path)
            messages.append(message)
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read inbox message: %s", path)

    return messages


def ack_message(message_path: Path) -> None:
    try:
        message_path.unlink()
        logger.debug("Acked message: %s", message_path.name)
    except FileNotFoundError:
        pass


def nack_message(message_path: Path, error: str, inbox_base: Path) -> None:
    try:
        with message_path.open("r", encoding="utf-8") as f:
            message = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.error("Could not read message for nack: %s", message_path)
        return

    attempts = message.get("attempts", 0) + 1
    message["attempts"] = attempts

    logger.warning(
        "Nack message %s (attempt %d/%d): %s",
        message.get("id", message_path.name),
        attempts,
        _MAX_ATTEMPTS,
        error,
    )

    if attempts >= _MAX_ATTEMPTS:
        dead_letter_dir = inbox_base / "dead_letter"
        dead_letter_dir.mkdir(parents=True, exist_ok=True)
        dest = dead_letter_dir / message_path.name
        os.replace(message_path, dest)
        logger.error(
            "Message %s moved to dead letter after %d attempts",
            message.get("id", message_path.name),
            attempts,
        )
        return

    # Rewrite in place with updated attempt count — use tmp+replace for durability.
    tmp_path = message_path.parent / f"{message_path.stem}.tmp"
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(message, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, message_path)
