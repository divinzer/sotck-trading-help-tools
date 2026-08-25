"""RSS Reader: feeds.xml(OPML)에서 피드 목록을 읽어 RSS 데이터를 가져온다."""

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from readability import Document

OPML_FILE = "feeds.xml"
BLOG_DIR = Path("blog")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
PDF_EXT = ".pdf"


@dataclass
class Feed:
    title: str
    xml_url: str
    html_url: str
    category: str = ""


def load_feeds(opml_path: str = OPML_FILE) -> list[Feed]:
    """OPML 파일에서 RSS 피드 목록(카테고리 포함)을 파싱한다."""
    tree = ET.parse(opml_path)
    feeds: list[Feed] = []

    def walk(outline, category=""):
        for node in outline.findall("outline"):
            xml_url = node.get("xmlUrl")
            if xml_url:  # 실제 피드 항목
                feeds.append(
                    Feed(
                        title=node.get("title", ""),
                        xml_url=xml_url,
                        html_url=node.get("htmlUrl", ""),
                        category=category,
                    )
                )
            else:  # 카테고리 폴더
                walk(node, category=node.get("title", ""))

    walk(tree.getroot().find("body"))
    return feeds


def fetch_feed(feed: Feed, max_entries: int = 10,
               date_from: datetime | None = None,
               date_to: datetime | None = None) -> dict:
    """단일 RSS 피드를 가져와 최신 글 목록을 반환한다.

    date_from ~ date_to 기간(둘 다 inclusive)으로 발행일을 필터링한다.
    """
    parsed = feedparser.parse(feed.xml_url)
    entries = []
    for entry in parsed.entries[:max_entries]:
        dt = parse_date(entry.get("published", ""))
        if date_from and dt < date_from:
            continue
        if date_to and dt > date_to:
            continue
        link = entry.get("link", "")
        # 항상 링크에서 전체 본문을 가져온다 (실패 시 RSS 요약으로 폴백).
        content = fetch_full_body(link) if link else ""
        if not content:
            content = _entry_content(entry)
        entries.append(
            {
                "title": entry.get("title", ""),
                "link": link,
                "published": entry.get("published", ""),
                "content": content,
            }
        )
    return {
        "feed": feed,
        "blog_title": parsed.feed.get("title", feed.title),
        "entries": entries,
    }


def _entry_content(entry) -> str:
    """entry의 전체 본문(content)이 있으면 반환, 없으면 summary."""
    contents = entry.get("content")
    if contents:
        return contents[0].get("value", "")
    return entry.get("summary", "")


def fetch_full_body(link: str) -> str:
    """글 링크에서 전체 본문을 가져온다. 실패 시 빈 문자열."""
    try:
        # 네이버 블로그는 모바일 URL(m.blog.naver.com)이 본문 추출에 유리하다.
        fetch_url = link
        m = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", link)
        if m:
            fetch_url = f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
        resp = requests.get(fetch_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        doc = Document(resp.text)
        summary_html = doc.summary(html_partial=True)
        text = re.sub(r"<br\s*/?>", "\n", summary_html, flags=re.I)
        text = re.sub(r"</p>|</div>|</li>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    except Exception as e:
        print(f"    [본문 가져오기 실패] {link}: {e}")
        return ""


def parse_date(published: str) -> datetime:
    """발행일 문자열을 파싱해 naive datetime(한국시간)으로 반환 (실패 시 현재 시각)."""
    from zoneinfo import ZoneInfo

    kst = ZoneInfo("Asia/Seoul")
    try:
        dt = parsedate_to_datetime(published)
    except Exception:
        try:
            dt = datetime.fromisoformat(published)
        except Exception:
            return datetime.now()
    if dt.tzinfo is not None:
        dt = dt.astimezone(kst).replace(tzinfo=None)
    return dt


def sanitize_name(name: str) -> str:
    """파일/디렉토리 이름으로 쓸 수 없는 문자를 제거한다."""
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip().rstrip(".")
    return name or "untitled"


def download_file(url: str, save_dir: Path) -> str | None:
    """URL에서 이미지/PDF를 다운로드해 저장하고, md용 상대 경로를 반환한다."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"    [다운로드 실패] {url}: {e}")
        return None

    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in IMG_EXTS and ext != PDF_EXT:
        # URL에 확장자가 없으면 Content-Type으로 판단
        ctype = resp.headers.get("Content-Type", "")
        if "pdf" in ctype:
            ext = PDF_EXT
        elif "image/png" in ctype:
            ext = ".png"
        elif "image/gif" in ctype:
            ext = ".gif"
        else:
            ext = ".jpg"

    sub_dir = "assets" if ext in IMG_EXTS else "pdf"
    digest = hashlib.md5(url.encode()).hexdigest()[:8]
    filename = f"{digest}{ext}"
    save_path = save_dir / sub_dir / filename
    if not save_path.exists():
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(resp.content)
    return f"{sub_dir}/{filename}"


def html_to_markdown(html_text: str, blog_dir: Path) -> tuple[str, list[str]]:
    """HTML 본문을 간단한 markdown 텍스트로 변환하고 미디어를 로컬에 저장한다.

    Returns: (markdown 본문, 로컬 미디어 경로 목록)
    """
    media_paths: list[str] = []

    def replace_img(m):
        src = m.group(1)
        local = download_file(src, blog_dir)
        if local:
            media_paths.append(local)
            return f"\n![첨부 이미지]({local})\n"
        return ""

    def replace_link(m):
        href, text = m.group(1), m.group(2)
        if href.lower().endswith(PDF_EXT):
            local = download_file(href, blog_dir)
            if local:
                media_paths.append(local)
                return f"[{text}]({local})"
        return f"[{text}]({href})"

    text = html_text
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<img[^>]+src=[\"']([^\"']+)[\"'][^>]*>", replace_img, text, flags=re.I)
    text = re.sub(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", replace_link, text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</div>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), media_paths


def load_existing_links(md_path: Path) -> set[str]:
    """기존 md 파일에 이미 저장된 글 링크 목록을 읽어온다."""
    links: set[str] = set()
    if not md_path.exists():
        return links
    for line in md_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- 링크: "):
            # 쿼리스트링 없이 순수 글 URL 기준으로 비교
            m = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", line)
            links.add(m.group(2) if m else line[len("- 링크: "):])
    return links


def save_feed_markdown(result: dict) -> list[Path]:
    """피드 하나를 blog/<블로그제목>/<YYYY-MM>.md 로 저장한다.

    기존 파일에 이미 저장된 글(링크 기준)은 스킵하고 새 글만 추가한다.
    """
    feed = result["feed"]
    blog_dir = BLOG_DIR / sanitize_name(feed.title)

    # 월별로 엔트리 그룹화
    by_month: dict[str, list[tuple[datetime, dict]]] = {}
    for e in result["entries"]:
        dt = parse_date(e["published"])
        by_month.setdefault(dt.strftime("%Y-%m"), []).append((dt, e))

    saved_files = []
    for month, items in by_month.items():
        out = blog_dir / f"{month}.md"
        existing = load_existing_links(out)

        new_items = []
        for dt, e in sorted(items, key=lambda x: x[0]):
            m = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", e["link"])
            key = m.group(2) if m else e["link"]
            if key in existing:
                print(f"  스킵(기존): {e['title']}")
                continue
            new_items.append((dt, e))

        if not new_items:
            print(f"  변경 없음: {out}")
            continue

        lines = []
        if not out.exists():
            lines = [f"# {result['blog_title']}", ""]
        for dt, e in new_items:
            body, _ = html_to_markdown(e["content"], blog_dir)
            lines += [
                f"## {e['title']}",
                "",
                f"- 발행일: {dt.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- 링크: {e['link']}",
                "",
                body,
                "",
                "---",
                "",
            ]
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines))
        saved_files.append(out)
        print(f"  저장: {out} (신규 {len(new_items)}건)")
    return saved_files


def main():
    # 기간을 코드에 직접 지정한다 (None이면 필터 없음).
    date_from = datetime(2026, 8, 20, 0, 0, 0)
    date_to = datetime.now().replace(hour=23, minute=59, second=59)

    if date_from or date_to:
        print(f"기간 필터: {date_from} ~ {date_to}\n")

    feeds = load_feeds()
    print(f"총 {len(feeds)}개 피드를 찾았습니다.\n")

    ok, fail = 0, 0
    for feed in feeds:
        try:
            result = fetch_feed(feed, date_from=date_from, date_to=date_to)
        except Exception as e:
            print(f"[실패] [{feed.category}] {feed.title}: {e}")
            fail += 1
            continue

        print(f"=== [{feed.category}] {result['blog_title']} ===")
        if not result["entries"]:
            print("  (가져온 글 없음)")
        for e in result["entries"]:
            print(f"  - {e['published']} | {e['title']}")
        save_feed_markdown(result)
        print()
        ok += 1

    print(f"완료: 성공 {ok}개 / 실패 {fail}개")


if __name__ == "__main__":
    main()
