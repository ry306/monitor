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

# DM26-EX3、DM26EX3、DM26_EX03などを認識する。
PRODUCT_CODE_RE = re.compile(
    r"(?<![a-z0-9])dm[\s_-]*(\d{2})[\s_-]*ex[\s_-]*(\d+)(?!\d)",
    re.IGNORECASE,
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
    """文字列を比較しやすい形式へ正規化する。"""
    text = html.unescape(text or "")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("％", "%")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def extract_product_codes(text: str) -> set[tuple[int, int]]:
    """文章中のDMxx-EXxx形式の商品コードを取得する。"""
    value = normalize(text)
    codes: set[tuple[int, int]] = set()

    for series, number in PRODUCT_CODE_RE.findall(value):
        try:
            codes.add((int(series), int(number)))
        except ValueError:
            continue

    return codes


def looks_like_target(title: str, url: str = "") -> bool:
    """
    商品名またはURLがDM26-EX3そのものか判定する。

    商品名にDM26-EX2などの別コードが明記されている場合は除外する。
    商品コードがない場合は商品名の特徴で判定する。
    """
    target_code = (26, 3)

    title_codes = extract_product_codes(title)

    if title_codes:
        # 複数の商品コードが混ざる曖昧なタイトルも除外する。
        return title_codes == {target_code}

    url_codes = extract_product_codes(
        urllib.parse.unquote(url)
    )

    if url_codes:
        return url_codes == {target_code}

    value = normalize(title)

    return (
        "文化祭" in value
        and "ドラ娘" in value
        and (
            "100%" in value
            or "100パーセント" in value
        )
    )


def classify_status(text: str) -> str:
    """ページ上の文言から予約・在庫状態を分類する。"""
    value = normalize(text)

    negative = any(
        normalize(word) in value
        for word in NEGATIVE_WORDS
    )
    positive = any(
        normalize(word) in value
        for word in POSITIVE_WORDS
    )

    # 「予約受付終了」には「予約」が含まれるため、
    # 在庫なし・受付終了を優先する。
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
        re.IGNORECASE,
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
        re.search(pattern, url, re.IGNORECASE)
        for pattern in patterns
    )


def link_label(anchor) -> str:
    """
    商品リンク自身に含まれる商品名を取得する。

    隣の商品情報が混ざらないよう、
    リンク本文、title、aria-label、画像altだけを使用する。
    """
    parts = [
        anchor.get_text(" ", strip=True),
        anchor.get("title", ""),
        anchor.get("aria-label", ""),
    ]

    for image in anchor.find_all("img"):
        parts.append(image.get("alt", ""))

    unique_parts: list[str] = []

    for part in parts:
        part = str(part or "").strip()

        if part and part not in unique_parts:
            unique_parts.append(part)

    return " ".join(unique_parts)


def surrounding_text(anchor) -> str:
    """
    商品リンク周辺の価格・在庫文言を取得する。

    大きな検索結果コンテナまで遡ると隣の商品が混ざるため、
    800文字を超える親要素は使用しない。
    """
    best_text = link_label(anchor)
    parent = anchor.parent

    for _ in range(4):
        if parent is None:
            break

        text = parent.get_text(" ", strip=True)

        if not text:
            parent = parent.parent
            continue

        if len(text) > 800:
            break

        best_text = text
        parent = parent.parent

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

        # 商品名を取得できないリンクは誤検出防止のため除外する。
        if not title:
            continue

        # 商品リンク自身の名前とURLで対象商品を判定する。
        if not looks_like_target(title, raw_url):
            continue

        context = surrounding_text(anchor)
        url = canonicalize_url(raw_url)
        price = extract_price(context)

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

        # 説明文に偶然DM26-EX3が含まれていても通さない。
        # 検索結果のタイトルとURLだけで対象商品を判定する。
        if not looks_like_target(title, url):
            continue

        context = f"{title} {description}"

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
                price=extract_price(context),
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

    実行日時は入れず、監視結果が同じなら
    状態ファイルの内容も変わらないようにする。
    """
    return {
        "source": candidate.source,
        "title": candidate.title,
        "status": candidate.status,
        "price": candidate.price,
    }


def build_listing_warning(
    title: str,
    price: int | None,
) -> str:
    """
    販売数量や価格が怪しい場合の警告文を作る。

    対象商品自体は除外せず、通知に注意書きを付ける。
    """
    value = normalize(title)
    warnings: list[str] = []

    if "カートン" in value:
        warnings.append(
            "⚠ 数量注意：カートン表記があります。"
            "実際の販売数量を確認してください。"
        )

    box_count = re.search(
        r"(?<!\d)(\d+)\s*(?:box|ボックス)",
        value,
        re.IGNORECASE,
    )

    if (
        box_count
        and int(box_count.group(1)) >= 2
    ):
        warnings.append(
            "⚠ 数量注意："
            f"{box_count.group(1)}BOX表記があります。"
            "実際の販売数量を確認してください。"
        )

    single_pack_words = (
        "1パック",
        "単品パック",
        "バラパック",
        "ばらパック",
        "パック単品",
        "1 pack",
        "single pack",
    )

    if any(
        word in value
        for word in single_pack_words
    ):
        warnings.append(
            "⚠ 単品注意："
            "1BOXではなく単品パックの可能性があります。"
        )

    if (
        price is not None
        and price < OFFICIAL_BOX_PRICE * 0.5
    ):
        warnings.append(
            "⚠ 価格注意："
            "1BOX価格としては低すぎます。"
            "単品価格、送料、または価格誤取得の可能性があります。"
        )

    if (
        price is not None
        and price > OFFICIAL_BOX_PRICE * 1.25
    ):
        warnings.append(
            "⚠ 高額注意："
            f"希望小売価格1BOX {OFFICIAL_BOX_PRICE:,}円の"
            "125％を超えています。"
            "複数BOX商品、送料、販売元を確認してください。"
        )

    if not warnings:
        return ""

    return "\n".join(warnings) + "\n"


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

    warning = build_listing_warning(
        candidate.title,
        candidate.price,
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
                # 次回実行時に再度通知する。
                continue

        new_item = candidate_state(candidate)

        if previous != new_item:
            old_items[url] = new_item
            state_changes += 1

    # 毎回変化する実行日時やエラー履歴は保存しない。
    # 商品情報が同じなら状態ファイルの内容も変わらない。
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

    # 一部サイトでアクセス拒否やHTML変更が起きても、
    # ほかのサイトの監視を続けるため正常終了とする。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
