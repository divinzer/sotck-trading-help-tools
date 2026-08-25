import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from readability import Document
from telethon import TelegramClient
from telethon.tl.types import Channel

# Telegram 인증정보는 소스 코드와 분리해 프로젝트 루트의 JSON 파일에서 읽는다.
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# Telegram 메시지의 시간은 한국 시간으로 통일해서 비교하고 저장한다.
KST = ZoneInfo("Asia/Seoul")
# 수집 기간은 시작일과 종료일을 모두 포함한다.
DATE_TO = date.today()
# DATE_FROM = DATE_TO - timedelta(days=6)
DATE_FROM = DATE_TO

# 메시지 안에서 HTTP/HTTPS URL만 찾기 위한 정규식이다.
URL_PATTERN = re.compile(r"https?://[^\s<>]+")
# 웹 페이지가 너무 큰 경우를 대비해 읽을 최대 바이트 수를 제한한다.
MAX_URL_BODY_BYTES = 2_000_000
# 응답하지 않는 웹 사이트 때문에 전체 수집이 멈추지 않도록 요청 시간을 제한한다.
URL_TIMEOUT_SECONDS = 10
# 채널별 Markdown과 첨부파일을 모두 messages 아래에 저장한다.
MEDIA_ROOT = Path("messages")


def load_config() -> dict[str, int | str]:
    """config.json에서 Telegram 인증정보를 읽고 필수 값의 형식을 검증한다."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"설정 파일을 찾을 수 없습니다: {CONFIG_PATH}\n"
            "config.json에 api_id, api_hash, phone을 입력하세요."
        )

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"config.json 형식이 올바르지 않습니다: {error}") from error

    if not isinstance(config, dict):
        raise ValueError("config.json의 최상위 값은 JSON 객체여야 합니다.")

    required_keys = {"api_id", "api_hash", "phone"}
    missing_keys = required_keys - config.keys()
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise ValueError(f"config.json에 필수 항목이 없습니다: {missing}")

    api_id = config["api_id"]
    api_hash = config["api_hash"]
    phone = config["phone"]
    if isinstance(api_id, bool) or not isinstance(api_id, int):
        raise ValueError("config.json의 api_id는 정수여야 합니다.")
    if not isinstance(api_hash, str) or not api_hash.strip():
        raise ValueError("config.json의 api_hash는 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(phone, str) or not phone.strip():
        raise ValueError("config.json의 phone은 비어 있지 않은 문자열이어야 합니다.")

    return {"api_id": api_id, "api_hash": api_hash, "phone": phone}


def to_kst(dt: datetime) -> datetime:
    """날짜 시간 객체를 한국 표준시로 변환한다."""
    if dt.tzinfo is None:
        # Telethon의 날짜에 시간대 정보가 없는 경우 UTC로 간주한다.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST)


def message_body(message) -> str:
    """Telegram 메시지에서 텍스트를 꺼내고, 텍스트가 없으면 미디어 표시를 반환한다."""
    text = (message.text or "").strip()
    if text:
        return text
    if message.photo:
        return "[photo]"
    if message.video:
        return "[video]"
    if message.document:
        return "[document]"
    return "[empty]"


class PageTextParser(HTMLParser):
    """HTML에서 화면에 표시될 텍스트만 모으는 간단한 파서."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        # 프로그램 코드나 스타일 정보는 본문으로 저장할 필요가 없으므로 무시한다.
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)


def extract_urls(text: str) -> list[str]:
    """메시지에서 유효한 URL을 중복 없이 추출한다."""
    urls = []
    for match in URL_PATTERN.findall(text):
        # 문장 끝에 붙은 구두점은 URL의 일부가 아니므로 제거한다.
        url = match.rstrip(".,;:!?)]}>")
        if url and urlparse(url).netloc and url not in urls:
            urls.append(url)
    return urls


def fetch_url_body(url: str) -> str:
    """URL의 HTML을 가져와 본문 텍스트만 반환한다."""
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=URL_TIMEOUT_SECONDS) as response:
        content_type = response.headers.get_content_type()
        if content_type != "text/html":
            return f"[HTML 본문을 지원하지 않는 콘텐츠: {content_type}]"
        html = response.read(MAX_URL_BODY_BYTES).decode(
            response.headers.get_content_charset() or "utf-8", errors="replace"
        )

    # 일반 웹 페이지는 readability로 기사 본문 영역을 우선 추출한다.
    parser = PageTextParser()
    if "youtu" not in url.lower():
        document = Document(html)
        parser.feed(document.summary())
        body = "\n".join(parser.parts).strip()
        if body:
            return body

    # YouTube URL 또는 readability가 본문을 찾지 못한 경우에는 HTML 전체를
    # 보조 방식으로 파싱한다. YouTube는 동영상 페이지 구조가 일반 기사와 달라
    # readability 결과가 부정확할 수 있어 의도적으로 적용하지 않는다.
    parser = PageTextParser()
    parser.feed(html)
    return "\n".join(parser.parts).strip() or "[본문을 찾을 수 없습니다]"


async def add_url_bodies(channels: list[dict]) -> None:
    """URL이 포함된 메시지에 웹 페이지 본문을 추가한다."""
    for channel in channels:
        for message in channel["messages"]:
            urls = extract_urls(message["text"])
            if not urls:
                continue

            fetched_bodies = []
            for url in urls:
                try:
                    # 동기 방식인 urllib 호출이 이벤트 루프를 막지 않도록 별도 스레드에서 실행한다.
                    body = await asyncio.to_thread(fetch_url_body, url)
                except Exception as error:
                    # 특정 URL의 실패가 다른 채널과 메시지 수집을 중단시키지 않게 한다.
                    body = f"[본문을 가져오지 못했습니다: {error}]"
                fetched_bodies.extend([f"URL: {url}", body])

            message["text"] += "\n\n" + "\n\n".join(fetched_bodies)


def safe_filename(name: str) -> str:
    """채널명을 Windows 파일명으로 사용할 수 있도록 정리한다."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(".")
    return name or "unnamed_channel"


def latest_saved_datetime(output_path: Path) -> datetime | None:
    """기존 Markdown에서 가장 최근에 저장된 메시지의 날짜와 시간을 읽는다."""
    if not output_path.exists():
        return None

    current_day = None
    latest = None
    # 날짜 제목과 시간 제목을 순서대로 읽어 최신 메시지의 시각을 계산한다.
    for line in output_path.read_text(encoding="utf-8").splitlines():
        day_match = re.fullmatch(r"## (\d{4}-\d{2}-\d{2})", line)
        if day_match:
            current_day = date.fromisoformat(day_match.group(1))
            continue

        time_match = re.fullmatch(r"### (\d{2}:\d{2})(?::(\d{2}))?", line)
        if current_day and time_match:
            timestamp = time_match.group(1)
            timestamp_format = "%H:%M"
            if time_match.group(2):
                timestamp += f":{time_match.group(2)}"
                timestamp_format = "%H:%M:%S"
            saved_at = datetime.combine(
                current_day,
                datetime.strptime(timestamp, timestamp_format).time(),
                tzinfo=KST,
            )
            if latest is None or saved_at > latest:
                latest = saved_at

    return latest


def media_extension(message) -> str | None:
    """지원하는 첨부파일이면 저장할 확장자를 반환한다."""
    if message.photo:
        return ".jpg"
    if not message.document:
        return None

    mime_type = (message.document.mime_type or "").lower()
    if mime_type == "application/pdf":
        return ".pdf"
    if mime_type.startswith("image/"):
        return (message.file.ext or ".bin").lower()
    return None


async def download_media(client: TelegramClient, message, channel_title: str) -> str | None:
    """이미지 또는 PDF를 채널 디렉토리에 다운로드하고 Markdown용 상대 경로를 반환한다."""
    extension = media_extension(message)
    if extension is None:
        return None

    # PDF와 이미지를 별도 디렉토리에 보관해 파일 종류를 쉽게 구분한다.
    media_dir = "pdf" if extension == ".pdf" else "assets"
    channel_dir = MEDIA_ROOT / safe_filename(channel_title) / media_dir
    channel_dir.mkdir(parents=True, exist_ok=True)
    # 같은 이름의 파일이 있어도 메시지 ID가 앞에 붙으므로 덮어쓰지 않는다.
    original_name = Path(message.file.name or "").stem if message.file else ""
    filename = safe_filename(original_name) if original_name else f"message_{message.id}"
    media_path = channel_dir / f"{message.id}_{filename}{extension}"

    downloaded_path = await message.download_media(file=str(media_path))
    if not downloaded_path:
        return None
    return media_path.relative_to(MEDIA_ROOT / safe_filename(channel_title)).as_posix()


async def fetch_messages_in_range(
    client: TelegramClient, entity, channel_title: str
) -> list[dict]:
    """지정한 날짜 범위의 메시지를 오래된 순서로 반환한다."""
    start = datetime.combine(DATE_FROM, time.min, tzinfo=KST)
    end = datetime.combine(DATE_TO + timedelta(days=1), time.min, tzinfo=KST)
    messages: list[dict] = []

    # Telethon은 기본적으로 최신 메시지부터 반환하므로 마지막에 reverse()한다.
    async for message in client.iter_messages(entity, offset_date=end):
        if message.date is None:
            continue
        msg_dt = to_kst(message.date)
        if msg_dt < start:
            # 시간순으로 내려오므로 시작일보다 과거가 되는 순간 더 볼 필요가 없다.
            break
        if msg_dt >= end:
            continue
        media_path = None
        media_error = None
        if message.photo or message.document:
            # 미디어 다운로드 실패는 메시지 자체의 저장을 막지 않는다.
            try:
                media_path = await download_media(client, message, channel_title)
            except Exception as error:
                media_error = f"[미디어를 다운로드하지 못했습니다: {error}]"

        message_data = {
            "id": message.id,
            "date": msg_dt,
            "text": message_body(message),
            "media_path": media_path,
            "media_error": media_error,
        }
        messages.append(message_data)

    messages.reverse()
    return messages


async def fetch_channels(config: dict[str, int | str]) -> list[dict]:
    """대화 목록에서 일반 채널만 골라 메시지를 수집한다."""
    async with TelegramClient(
        "session", config["api_id"], config["api_hash"]
    ) as client:
        await client.start(phone=config["phone"])

        channels: list[dict] = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            # 그룹/개인 대화는 제외하고 Broadcast 채널만 처리한다.
            if not isinstance(entity, Channel):
                continue
            if getattr(entity, "megagroup", False):
                continue

            channels.append(
                {
                    "id": entity.id,
                    "title": dialog.name,
                    "username": entity.username,
                    "participants": getattr(entity, "participants_count", None),
                    "messages": await fetch_messages_in_range(client, entity, dialog.name),
                }
            )

        return channels


def main() -> None:
    """메시지 수집, URL 본문 보강, 증분 필터링, Markdown 저장을 순서대로 실행한다."""
    config = load_config()
    channels = asyncio.run(fetch_channels(config))
    output_dir = Path("messages")
    output_dir.mkdir(exist_ok=True)

    # 이미 저장된 파일의 마지막 시각 이후 메시지만 남겨 중복 저장을 방지한다.
    for channel in channels:
        output_path = (
            output_dir
            / safe_filename(channel["title"])
            / f"{DATE_TO.year}-{DATE_TO.month:02d}.md"
        )
        latest = latest_saved_datetime(output_path)
        channel["messages"] = [
            message
            for message in channel["messages"]
            if latest is None or message["date"] > latest
        ]

    # 새 메시지에만 URL 본문을 추가해 불필요한 웹 요청을 줄인다.
    asyncio.run(add_url_bodies(channels))

    print(f"channels: {len(channels)}")
    print(f"date range: {DATE_FROM} ~ {DATE_TO} (KST)")
    for channel in channels:
        username = f"@{channel['username']}" if channel["username"] else "-"
        title = channel["title"]
        messages = channel["messages"]
        channel_dir = output_dir / safe_filename(title)
        channel_dir.mkdir(parents=True, exist_ok=True)
        output_path = channel_dir / f"{DATE_TO.year}-{DATE_TO.month:02d}.md"
        existing_content = (
            output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        )
        print(f"\n=== {title} ({username}) id={channel['id']} ===")

        if existing_content and not messages:
            print(f"no new messages: {output_path}")
            continue

        # 기존 파일은 유지하고, 새 파일일 때만 채널 메타데이터를 먼저 기록한다.
        markdown = [] if existing_content else [
            f"# {title}",
            "",
            f"- Username: {username}",
            f"- Channel ID: `{channel['id']}`",
            f"- Date range: {DATE_FROM} ~ {DATE_TO} (KST)",
            "",
        ]

        if not messages:
            print("(no messages)")
            markdown.append("메시지가 없습니다.")
        else:
            by_day: dict[str, list[dict]] = defaultdict(list)
            for message in messages:
                by_day[message["date"].strftime("%Y-%m-%d")].append(message)
            # 이미 날짜 제목이 있으면 중복으로 만들지 않고 메시지만 이어 붙인다.
            existing_days = set(
                re.findall(r"^## (\d{4}-\d{2}-\d{2})$", existing_content, re.MULTILINE)
            )

            for day in sorted(by_day):
                print(f"\n-- {day} --")
                if day not in existing_days:
                    markdown.extend([f"## {day}", ""])
                for message in by_day[day]:
                    timestamp = message["date"].strftime("%H:%M:%S")
                    print(f"[{day}{timestamp}] {message['text']}")
                    markdown.extend([f"### {timestamp}", "", message["text"], ""])
                    if message["media_path"]:
                        media_path = Path(message["media_path"])
                        if media_path.suffix.lower() == ".pdf":
                            markdown.extend(
                                [f"[PDF 파일]({media_path.as_posix()})", ""]
                            )
                        else:
                            markdown.extend(
                                [
                                    f"![첨부 이미지]({media_path.as_posix()})",
                                    "",
                                ]
                            )
                    if message["media_error"]:
                        markdown.extend([message["media_error"], ""])

        # 기존 내용 뒤에 새 Markdown만 추가한다. 파일이 새 파일이면 그대로 작성한다.
        new_content = "\n".join(markdown)
        if existing_content:
            new_content = existing_content.rstrip() + "\n\n" + new_content
        output_path.write_text(new_content, encoding="utf-8")
        print(f"saved: {output_path}")


if __name__ == "__main__":
    main()
