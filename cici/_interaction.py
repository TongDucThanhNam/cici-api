"""Pure policy for interpreting Cici/Dola bot interaction messages."""
from __future__ import annotations

import re


DEFAULT_REFUSAL_PATTERNS = [
    "bảo vệ bản quyền",
    "bản quyền",
    "to protect copyright",
    "copyright",
]

DEFAULT_CONFIRM_PATTERNS = [
    'reply "confirm"',
    "reply \u201cconfirm\u201d",
    "reply 'confirm'",
    "please confirm",
    "hãy xác nhận",
    "xác nhận để",
    "请确认",
]


class InteractionPolicy:
    """Classify bot messages and choose bounded automatic replies."""

    REPLY_TOKEN_RE = re.compile(
        r"(?:reply|trả lời|回复)\s*[\"'“‘]([^\"'”’]{1,40})[\"'”’]",
        re.IGNORECASE,
    )
    CHOICE_RE = re.compile(
        r"\b([A-Z])[.\):：]\s*(\d+)\s*(?:seconds?|giây|秒|s\b)"
    )

    def __init__(self, refusal_patterns: list[str], confirm_patterns: list[str]):
        self.refusal_patterns = refusal_patterns
        self.confirm_patterns = confirm_patterns

    def is_refusal(self, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(pattern.lower() in lowered for pattern in self.refusal_patterns)

    def is_confirm_request(self, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        if any(pattern.lower() in lowered for pattern in self.confirm_patterns):
            return True
        return self.is_choice_question(text)

    def extract_reply_token(self, text: str) -> str | None:
        if not text:
            return None
        match = self.REPLY_TOKEN_RE.search(text)
        return match.group(1).strip() if match else None

    def duration_choice(self, text: str, duration: str | None) -> str | None:
        if not text or not duration:
            return None
        match = re.match(r"(\d+)", duration.strip())
        if not match:
            return None
        wanted_seconds = int(match.group(1))
        for letter, seconds in self.CHOICE_RE.findall(text):
            if int(seconds) == wanted_seconds:
                return letter
        return None

    def is_choice_question(self, text: str) -> bool:
        if not text:
            return False
        return len({match[0] for match in self.CHOICE_RE.findall(text)}) >= 2

    def auto_reply_text(self, text: str, duration: str | None = None) -> str:
        letter = self.duration_choice(text, duration)
        if letter:
            return letter
        token = self.extract_reply_token(text)
        if token:
            return token
        if self.is_choice_question(text):
            return self.CHOICE_RE.findall(text)[0][0]
        return "confirm"
