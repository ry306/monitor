#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup


PRODUCT_CODE = "DM26-EX3"
PRODUCT_NAME = "文化祭だョ！全員集合!!ドラ娘100％パック"
OFFICIAL_BOX_PRICE = 6160
# 1BOX監視のため、希望小売価格の50％未満は単品パックや誤取得として除外する。
MIN_BOX_PRICE = OFFICIAL_BOX_PRICE // 2

TARGET_CODE_RE = re.compile(
    r"(?<![a-z0-9])dm[\s_-]*26[\s_-]*ex[\s_-]*0?3(?!\d)",
    re.IGNORECASE,
)
ANY_EX_CODE_RE = re.compile(
    r"(?<![a-z0-9])dm[\s_-]*(\d{2})[\s_-]*ex[\s_-]*(\d+)(?!\d)",
    re.IGNORECASE,
)

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NOTIFY_CANDIDATES = os.getenv("NOTIFY_CANDIDATES", "1") == "1"

TIMEOUT = 25
MAX_NOTIFICATIONS_PER_RUN = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.5",
}

POSITIVE_WORDS = (
    "予約受付中",
    "予約注文",
    "予約する",
    "予約商品",
    "カートに入れる",
    "購入する",
    "在庫あり",
    "販売中",
    "注文する",
)

NEGATIVE_WORDS = (
    "予約受付終了",
    "予約終了",
    "在庫切れ",
    "売り切れ",
    "品切れ",
    "販売終了",
    "入荷待ち",
    "注文できません",
    "取り扱い終了",
)

SEARCH_SOURCES = [
    {
        "name": "Amazon",
        "url": "https://www.amazon.co.jp/s?k=DM26-EX3",
        "allowed": (
            r"/dp/[A-Z0-9]{10}",
            r"/gp/product/[A-Z0-9]{10}",
        ),
    },
    {
        "name": "楽天市場",
        "url": "https://search.rakuten.co.jp/search/mall/DM26-EX3/",
        "allowed": (
            r"item\.rakuten\.co\.jp/",
        ),
    },
    {
        "name": "Yahoo!ショッピング",
        "url": "https://shopping.yahoo.co.jp/search?p=DM26-EX3",
        "allowed": (
            r"store\.shopping\.yahoo\.co\.jp/.+\.html",
        ),
    },
    {
        "name": "駿河屋",
        "url": "https://www.suruga-ya.jp/search?category=&search_word=DM26-EX3",
        "allowed": (
            r"suruga-ya\.jp/product/detail/",
        ),
    },
    {
        "name": "ヨドバシ",
        "url": "https://www.yodobashi.com/?word=DM26-EX3",
        "allowed": (
            r"yodobashi\.com/product/",
        ),
    },
    {
        "name": "ビックカメラ",
        "url": "https://www.biccamera.com/bc/category/?q=DM26-EX3",
        "allowed": (
            r"biccamera\.com/bc/item/",
        ),
    },
    {
        "name": "あみあみ",
        "url": "https://slist.amiami.jp/top/search/list?s_keywords=DM26-EX3",
        "allowed": (
            r"amiami\.jp/top/detail/detail\?gcode=",
        ),
    },
    {
        "name": "ホビーサーチ",
        "url": (
            "https://www.1999.co.jp/search"
            "?typ1_c=121&cat=&target=Series&searchkey=DM26-EX3"
        ),
        "allowed": (
            r"1999\.co\.jp/",
        ),
    },
]

BING_QUERIES = [
    '"DM26-EX3" 予約',
    '"文化祭だョ！全員集合!!ドラ娘100％パック" 予約',
]

BLOCKED_SEARCH_DOMAINS = (
    "dm.takaratomy.co.jp",
    "x.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "dmwiki.net",
    "fandom.com",
    "deneblog.jp",
    "supersolenoid.jp",
    "gachi-matome.com",
)


@dataclass(frozen=True)
class Candidate:
    source: str
    title: str
    url: str
    context: str
    status: str
    price: int | None


def normalize(text: str) -> str:
    """比較しやすい形式へ文字列を正規化する。"""
    text = html.unescape(text or "")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("％", "%")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def link_label(anchor) -> str:
    """
    商品リンク自身のラベルを取得する。

    検索結果一覧全体ではなく、リンク文字列・title・aria-label・
    画像のaltだけを使い、隣の商品が混ざるのを防ぐ。
    """
    parts = [
        anchor.get_text(" ", strip=True),
        anchor.get("title", ""),
        anchor.get("aria-label", ""),
    ]

    for image in anchor.find_all("img"):
        parts.append(image.get("alt", ""))

    values: list[str] = []
    for part in parts:
        value = str(part or "").strip()
        if value and value not in values:
            values.append(value)

    return " ".join(values)


def looks_like_target(title: str, url: str = "") -> bool:
    """
    商品リンクがDM26-EX3そのものか判定する。

    DM26-EX2など別の商品コードが含まれる場合は除外する。
    商品コードがない場合のみ、商品名の主要語で判定する。
    """
    value = normalize(f"{title} {url}")

    detected_codes = {
        (series, number.lstrip("0") or "0")
        for series, number in ANY_EX_CODE_RE.findall(value)
    }

    if detected_codes:
        # 別コードも同時に含む曖昧なリンクは通知しない。
        return detected_codes == {("26", "3")}

    if TARGET_CODE_RE.search(value):
        return True

    return (
        "文化祭" in value
        and "ドラ娘" in value
        and ("100%" in value or "100パーセント" in value)
    )


def is_unwanted_variant(title: str, price: int | None) -> bool:
    """1BOX監視に不要な単品パック・複数BOX・カートンを除外する。"""
    value = normalize(title)

    if "カートン" in value:
        return True

    box_count = re.search(r"(?<!\d)(\d+)\s*(?:box|ボックス)", value)
    if box_count and int(box_count.group(1)) >= 2:
        return True

    single_pack_words = (
        "1パック",
        "単品パック",
        "バラパック",
        "ばらパック",
        "パック単品",
        "1 pack",
        "single pack",
    )
    if any(word in value for word in single_pack_words):
        return True

    # DM26-EX3の1パックは440円。低価格表示は単品や周辺商品の
    # 価格を拾っている可能性が高いため、1BOX候補から除外する。
    if price is not None and price < MIN_BOX_PRICE:
        return True

    return False


def classify_status(text: str) -> str:
    """ページ上の文言から在庫状態を分類する。"""
    value = normalize(text)

    negative = any(
        normalize(word) in value
        for word in NEGATIVE_WORDS
    )
    positive = any(
        normalize(word) in value
        for word in POSITIVE_WORDS
    )

    # 「予約受付終了」の中にも「予約」が含まれるため、
    # 在庫なし・受付終了の判定を優先する。
    if negative:
        return "unavailable"

    if positive:
        return "available"

    return "candidate"


def extract_price(text: str) -> int | None:
    """ページ上の価格らしい数値を取得する。"""
    value = unicodedata.normalize("NFKC", text or "")

    patterns = (
        r"[¥￥]\s*([0-9][0-9,]*)",
        r"([0-9][0-9,]*)\s*円",
    )

    prices: list[int] = []

    for pattern in patterns:
        for match in re.findall(pattern, value):
            try:
                amount = int(match.replace(",", ""))
            except ValueError:
                continue

            if 100 <= amount <= 500000:
                prices.append(amount)

    return min(prices) if prices else None


def canonicalize_url(url: str) -> str:
    """追跡用パラメータなどを除去してURLを正規化する。"""
    parsed = urllib.parse.urlsplit(url)

    host = parsed.netloc.lower().replace("www.", "")
    path = re.sub(r"/+", "/", parsed.path)

    amazon_match = re.search(
        r"/(?:dp|gp/product)/([A-Z0-9]{10})",
        path,
        re.I,
    )

    if amazon_match and host.endswith("amazon.co.jp"):
        asin = amazon_match.group(1).upper()
        return f"https://www.amazon.co.jp/dp/{asin}"

    # 商品識別に必要そうなクエリだけ残す。
    keep_keys = {
        "gcode",
        "scode",
        "pid",
        "itemcode",
    }

    query = urllib.parse.parse_qsl(
        parsed.query,
        keep_blank_values=True,
    )

    kept = [
        (key, value)
        for key, value in query
        if key.lower() in keep_keys
    ]

    clean_query = urllib.parse.urlencode(kept)

    return urllib.parse.urlunsplit(
        (
            parsed.scheme or "https",
            parsed.netloc,
            path,
            clean_query,
            "",
        )
    )


def allowed_link(
    url: str,
    patterns: Iterable[str],
) -> bool:
    """通販サイトの商品URLとして許可された形式か判定する。"""
    return any(
        re.search(pattern, url, re.I)
        for pattern in patterns
    )


def surrounding_text(anchor) -> str:
    """
    商品カード内の在庫文言と価格を取得する。

    大きな親要素まで遡ると隣の商品が混ざるため、
    800文字以内の最も近い要素だけを採用する。
    """
    best_text = link_label(anchor)
    parent = anchor.parent

    for _ in range(4):
        if parent is None:
            break

        text = parent.get_text(" ", strip=True)
        parent = parent.parent

        if not text:
            continue

        if len(text) > 800:
            break

        best_text = text

        has_price = bool(
            re.search(r"(?:[¥￥]\s*[0-9]|[0-9][0-9,]*\s*円)", text)
        )
        has_status = any(
            normalize(word) in normalize(text)
            for word in POSITIVE_WORDS + NEGATIVE_WORDS
        )

        if has_price or has_status:
            break

    return best_text


def fetch_html(url: str) -> str:
    """HTMLを取得する。"""
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT,
    )

    response.raise_for_status()
    return response.text


def scan_store_source(source: dict) -> list[Candidate]:
    """通販サイトの検索結果を走査する。"""
    page = fetch_html(source["url"])
    soup = BeautifulSoup(page, "html.parser")

    found: dict[str, Candidate] = {}

    for anchor in soup.find_all("a", href=True):
        raw_url = urllib.parse.urljoin(
            source["url"],
            anchor["href"],
        )

        if not allowed_link(
            raw_url,
            source["allowed"],
        ):
            continue

        title = link_label(anchor)

        # 商品名を持たない画像・装飾リンクは誤検出防止のため除外する。
        if not title:
            continue

        # 周辺の商品説明ではなく、商品リンク自身の名前とURLで判定する。
        if not looks_like_target(title, raw_url):
            continue

        context = surrounding_text(anchor)
        price = extract_price(context)

        if is_unwanted_variant(title, price):
            continue

        url = canonicalize_url(raw_url)

        candidate = Candidate(
            source=source["name"],
            title=title[:180],
            url=url,
            context=context[:1200],
            status=classify_status(context),
            price=price,
        )

        found[url] = candidate

    return list(found.values())


def scan_bing_rss(query: str) -> list[Candidate]:
    """Bing検索のRSSから販売ページ候補を取得する。"""
    endpoint = (
        "https://www.bing.com/search?"
        + urllib.parse.urlencode(
            {
                "q": query,
                "format": "rss",
                "setlang": "ja-JP",
            }
        )
    )

    response = requests.get(
        endpoint,
        headers=HEADERS,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    root = ET.fromstring(response.content)
    found: list[Candidate] = []

    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        url = item.findtext("link") or ""
        description = item.findtext("description") or ""

        context = f"{title} {description}"

        # 検索結果の説明文ではなく、タイトルとURLで商品を特定する。
        if not looks_like_target(title, url):
            continue

        price = extract_price(context)
        if is_unwanted_variant(title, price):
            continue

        host = urllib.parse.urlsplit(
            url
        ).netloc.lower()

        if any(
            blocked in host
            for blocked in BLOCKED_SEARCH_DOMAINS
        ):
            continue

        status = classify_status(context)

        if (
            status == "candidate"
            and "予約" not in normalize(context)
        ):
            continue

        found.append(
            Candidate(
                source="Web検索",
                title=title[:180],
                url=canonicalize_url(url),
                context=context[:1200],
                status=status,
                price=price,
            )
        )

    return found


def load_state() -> dict:
    """前回の監視状態を読み込む。"""
    if not STATE_FILE.exists():
        return {
            "items": {},
        }

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8",
            )
        )

        if not isinstance(data, dict):
            raise ValueError(
                "state root is not an object"
            )

        if not isinstance(
            data.get("items"),
            dict,
        ):
            raise ValueError(
                "state items is not an object"
            )

        return data

    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(
            f"[WARN] 状態ファイルを読み込めません: {exc}",
            file=sys.stderr,
        )

        return {
            "items": {},
        }


def save_state(state: dict) -> None:
    """監視状態を安全に保存する。"""
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp_file = STATE_FILE.with_suffix(
        STATE_FILE.suffix + ".tmp"
    )

    tmp_file.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    tmp_file.replace(STATE_FILE)


def candidate_state(candidate: Candidate) -> dict:
    """
    state.jsonへ保存する候補情報を作る。

    実行日時は入れない。
    監視結果が変わらない場合に、
    Gitの差分が発生しないようにする。
    """
    return {
        "source": candidate.source,
        "title": candidate.title,
        "status": candidate.status,
        "price": candidate.price,
    }


def send_ntfy(candidate: Candidate) -> None:
    """ntfyへ通知を送信する。"""
    if not NTFY_TOPIC:
        raise RuntimeError(
            "環境変数 NTFY_TOPIC が設定されていません"
        )

    if candidate.price is not None:
        price_text = (
            f"検出価格: {candidate.price:,}円\n"
        )
    else:
        price_text = (
            "検出価格: ページで確認\n"
        )

    warning = ""

    high_price_limit = (
        OFFICIAL_BOX_PRICE * 1.25
    )

    if (
        candidate.price is not None
        and candidate.price > high_price_limit
    ):
        warning = (
            "⚠ 高額注意\n"
            f"希望小売価格1BOX "
            f"{OFFICIAL_BOX_PRICE:,}円の"
            "125％を超えています。\n"
            "複数BOX商品、送料、販売元を"
            "確認してください。\n"
        )

    status_label = {
        "available": "予約・購入可能の可能性",
        "candidate": "新しい販売ページ候補",
        "unavailable": "受付終了・在庫なし",
    }[candidate.status]

    payload = {
        "topic": NTFY_TOPIC,
        "title": (
            f"デュエマ予約通知："
            f"{status_label}"
        ),
        "message": (
            f"{candidate.source}\n"
            f"{candidate.title}\n"
            f"{price_text}"
            f"{warning}"
            f"{candidate.url}"
        ),
        "priority": 5,
        "tags": [
            "shopping_cart",
            "card_index",
        ],
        "click": candidate.url,
        "actions": [
            {
                "action": "view",
                "label": "商品ページを開く",
                "url": candidate.url,
                "clear": True,
            }
        ],
    }

    response = requests.post(
        NTFY_SERVER,
        json=payload,
        timeout=TIMEOUT,
    )

    response.raise_for_status()


def should_notify(
    previous: dict | None,
    candidate: Candidate,
) -> bool:
    """前回状態との比較から通知要否を判定する。"""
    if candidate.status == "unavailable":
        return False

    if candidate.status == "candidate":
        if not NOTIFY_CANDIDATES:
            return False

        # 候補通知は初めて見つけたときだけ送る。
        return previous is None

    if candidate.status == "available":
        if previous is None:
            return True

        # 候補または在庫なしから、
        # 予約受付中・在庫ありへ変化した場合に通知する。
        return (
            previous.get("status")
            != "available"
        )

    return False


def main() -> int:
    if not NTFY_TOPIC:
        print(
            "ERROR: NTFY_TOPIC を設定してください",
            file=sys.stderr,
        )
        return 2

    state = load_state()
    old_items: dict[str, dict] = state["items"]

    all_candidates: dict[str, Candidate] = {}
    errors: list[str] = []

    for source in SEARCH_SOURCES:
        try:
            candidates = scan_store_source(
                source
            )

            for candidate in candidates:
                all_candidates[
                    candidate.url
                ] = candidate

            print(
                f"[OK] {source['name']}: "
                f"{len(candidates)}件"
            )

        except Exception as exc:
            message = (
                f"{source['name']}: {exc}"
            )

            errors.append(message)

            print(
                f"[WARN] {message}",
                file=sys.stderr,
            )

    for query in BING_QUERIES:
        try:
            candidates = scan_bing_rss(
                query
            )

            for candidate in candidates:
                all_candidates.setdefault(
                    candidate.url,
                    candidate,
                )

            print(
                f"[OK] Web検索: "
                f"{query}: "
                f"{len(candidates)}件"
            )

        except Exception as exc:
            message = (
                f"Web検索 {query}: {exc}"
            )

            errors.append(message)

            print(
                f"[WARN] {message}",
                file=sys.stderr,
            )

    notifications = 0
    deferred = 0
    state_changes = 0

    for url, candidate in sorted(
        all_candidates.items()
    ):
        previous = old_items.get(url)

        notify_required = should_notify(
            previous,
            candidate,
        )

        if notify_required:
            if (
                notifications
                >= MAX_NOTIFICATIONS_PER_RUN
            ):
                deferred += 1

                print(
                    f"[DEFER] 次回通知へ持ち越し: "
                    f"{candidate.source}: "
                    f"{candidate.title}"
                )

                # 保存しないことで、
                # 次回も未通知として扱う。
                continue

            try:
                send_ntfy(candidate)

                notifications += 1

                print(
                    f"[NOTIFY] "
                    f"{candidate.source}: "
                    f"{candidate.title}"
                )

                time.sleep(1)

            except Exception as exc:
                message = (
                    f"通知失敗 {url}: {exc}"
                )

                errors.append(message)

                print(
                    f"[WARN] {message}",
                    file=sys.stderr,
                )

                # 通知に失敗した候補は保存しない。
                # 次回実行時に再度通知を試す。
                continue

        new_item = candidate_state(candidate)

        if previous != new_item:
            old_items[url] = new_item
            state_changes += 1

    # 毎回変化する実行日時やエラー履歴は保存しない。
    # 商品情報に変更がなければGit差分も発生しない。
    new_state = {
        "items": old_items,
    }

    save_state(new_state)

    print(
        f"[DONE] "
        f"candidates={len(all_candidates)} "
        f"notifications={notifications} "
        f"deferred={deferred} "
        f"state_changes={state_changes} "
        f"errors={len(errors)}"
    )

    # 一部サイトでアクセス拒否やHTML変更が発生しても、
    # 他サイトの監視を継続するため正常終了とする。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
