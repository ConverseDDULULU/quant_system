"""파일 내용을 디스코드로 전송 (2000자 제한에 맞춰 나눠 보냄)

사용법: PYTHONPATH=src:. .venv/bin/python deploy/discord_send.py <파일>
"""

import os
import sys

import requests
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(PROJECT_ROOT, "config", ".env"))

LIMIT = 1900


def chunks(text: str) -> list[str]:
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > LIMIT:
            parts.append(current + line[: LIMIT - len(current)])
            line, current = line[LIMIT - len(current):], ""
        if len(current) + len(line) > LIMIT:
            parts.append(current)
            current = ""
        current += line
    if current.strip():
        parts.append(current)
    return parts


def main() -> int:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("DISCORD_WEBHOOK_URL 없음")
        return 1
    with open(sys.argv[1], encoding="utf-8") as f:
        text = f.read()
    for part in chunks(text):
        res = requests.post(webhook, json={"content": part}, timeout=10)
        if res.status_code >= 300:
            print(f"디스코드 전송 실패: HTTP {res.status_code}")
            return 1
    print(f"디스코드 전송 완료 ({len(text)}자)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
