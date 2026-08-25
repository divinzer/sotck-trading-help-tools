"""RSS Reader: feeds.xml(OPML)에서 피드 목록을 읽어 RSS 데이터를 가져온다."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass

import feedparser

OPML_FILE = "feeds.xml"


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


def fetch_feed(feed: Feed, max_entries: int = 10) -> dict:
    """단일 RSS 피드를 가져와 최신 글 목록을 반환한다."""
    parsed = feedparser.parse(feed.xml_url)
    entries = []
    for entry in parsed.entries[:max_entries]:
        entries.append(
            {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": entry.get("summary", "")[:200],
            }
        )
    return {
        "feed": feed,
        "blog_title": parsed.feed.get("title", feed.title),
        "entries": entries,
    }


def main():
    feeds = load_feeds()
    print(f"총 {len(feeds)}개 피드를 찾았습니다.\n")

    ok, fail = 0, 0
    for feed in feeds:
        try:
            result = fetch_feed(feed)
        except Exception as e:
            print(f"[실패] [{feed.category}] {feed.title}: {e}")
            fail += 1
            continue

        print(f"=== [{feed.category}] {result['blog_title']} ===")
        if not result["entries"]:
            print("  (가져온 글 없음)")
        for e in result["entries"]:
            print(f"  - {e['published']} | {e['title']}")
            print(f"    {e['link']}")
        print()
        ok += 1

    print(f"완료: 성공 {ok}개 / 실패 {fail}개")


if __name__ == "__main__":
    main()
