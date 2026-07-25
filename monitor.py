from __future__ import annotations

import html as html_lib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


STORE_BASE_URL = os.getenv("STORE_BASE_URL", "https://shop.spacex.com").rstrip("/")
SITEMAP_URL = os.getenv("SITEMAP_URL", f"{STORE_BASE_URL}/sitemap.xml")
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
SEND_INITIALIZED_MESSAGE = os.getenv("SEND_INITIALIZED_MESSAGE", "true").lower() in {
    "1",
    "true",
    "yes",
}
DISCORD_MENTION = os.getenv("DISCORD_MENTION", "").strip()
USER_AGENT = os.getenv(
    "USER_AGENT",
    "SpaceXStoreProductMonitor/1.0 (personal product availability monitor)",
)

REQUEST_TIMEOUT_SECONDS = 25
MAX_DISCORD_EMBEDS = 10
HEARTBEAT_DAYS = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def make_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = make_session()


def get_webhook_url() -> str:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL is missing. Add it as a secret or environment variable."
        )

    parsed = urlparse(webhook_url)
    allowed_hosts = {"discord.com", "www.discord.com", "discordapp.com", "www.discordapp.com"}
    if parsed.scheme != "https" or parsed.netloc.lower() not in allowed_hosts:
        raise RuntimeError("DISCORD_WEBHOOK_URL does not look like an official Discord webhook.")
    if not parsed.path.startswith("/api/webhooks/"):
        raise RuntimeError("DISCORD_WEBHOOK_URL is not a Discord incoming webhook URL.")

    return webhook_url


def fetch_text(url: str) -> str:
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"GET request failed with HTTP {response.status_code}: {url}")
    return response.text


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def direct_child_text(element: ET.Element, wanted_name: str) -> str | None:
    for child in list(element):
        if local_name(child.tag) == wanted_name and child.text:
            return child.text.strip()
    return None


def descendant_text(element: ET.Element, wanted_name: str) -> str | None:
    for child in element.iter():
        if local_name(child.tag) == wanted_name and child.text:
            return child.text.strip()
    return None


def normalize_product_url(raw_url: str) -> str | None:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"}:
        return None

    store_host = urlparse(STORE_BASE_URL).netloc.lower()
    if parsed.netloc.lower() != store_host:
        return None

    path = parsed.path.rstrip("/")
    if not path.startswith("/products/"):
        return None

    return urlunparse(("https", parsed.netloc.lower(), path, "", "", ""))


def fallback_title_from_url(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    words = slug.replace("-", " ").replace("_", " ")
    return html_lib.unescape(words).strip().title() or "New SpaceX Store Product"


def parse_product_sitemap(xml_text: str) -> dict[str, dict[str, str]]:
    root = ET.fromstring(xml_text)
    products: dict[str, dict[str, str]] = {}

    if local_name(root.tag) != "urlset":
        return products

    for url_node in list(root):
        if local_name(url_node.tag) != "url":
            continue

        raw_url = direct_child_text(url_node, "loc")
        if not raw_url:
            continue

        url = normalize_product_url(raw_url)
        if not url:
            continue

        title = descendant_text(url_node, "title") or fallback_title_from_url(url)
        image = descendant_text(url_node, "loc") or ""
        # The first descendant <loc> is normally the product URL. Find an image URL instead.
        for node in url_node.iter():
            if local_name(node.tag) == "loc" and node.text:
                candidate = node.text.strip()
                if candidate != raw_url and candidate.startswith("http"):
                    image = candidate
                    break

        products[url] = {
            "url": url,
            "title": title,
            "image": image,
            "lastmod": direct_child_text(url_node, "lastmod") or "",
        }

    return products


def fetch_current_products() -> dict[str, dict[str, str]]:
    root_text = fetch_text(SITEMAP_URL)
    root = ET.fromstring(root_text)
    root_type = local_name(root.tag)

    if root_type == "urlset":
        products = parse_product_sitemap(root_text)
    elif root_type == "sitemapindex":
        sitemap_urls: list[str] = []
        for sitemap_node in list(root):
            if local_name(sitemap_node.tag) != "sitemap":
                continue
            loc = direct_child_text(sitemap_node, "loc")
            if loc:
                sitemap_urls.append(loc)

        product_sitemaps = [
            url for url in sitemap_urls if "sitemap_products" in url.lower()
        ]
        if not product_sitemaps:
            # Safe fallback for an unusual Shopify sitemap naming scheme.
            product_sitemaps = sitemap_urls

        products: dict[str, dict[str, str]] = {}
        for product_sitemap_url in product_sitemaps:
            child_text = fetch_text(product_sitemap_url)
            products.update(parse_product_sitemap(child_text))
    else:
        raise RuntimeError(f"Unexpected sitemap root element: {root_type}")

    if not products:
        raise RuntimeError("No product URLs were found in the SpaceX Store sitemap.")

    return dict(sorted(products.items()))


def iter_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def is_product_schema(value: dict[str, Any]) -> bool:
    schema_type = value.get("@type")
    if isinstance(schema_type, str):
        return schema_type.lower() == "product"
    if isinstance(schema_type, list):
        return any(str(item).lower() == "product" for item in schema_type)
    return False


def humanize_availability(value: str) -> str:
    label = value.rsplit("/", 1)[-1]
    label = re.sub(r"(?<!^)(?=[A-Z])", " ", label)
    return label.strip().capitalize()


def format_price(price: Any, currency: str | None) -> str | None:
    if price in (None, ""):
        return None

    raw = str(price).strip()
    try:
        amount = float(raw)
        if currency == "USD":
            return f"${amount:,.2f}"
        if currency:
            return f"{currency} {amount:,.2f}"
        return f"{amount:,.2f}"
    except ValueError:
        return f"{currency} {raw}".strip() if currency else raw


def choose_offer(offers: Any) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    if isinstance(offers, dict):
        candidates.append(offers)
    elif isinstance(offers, list):
        candidates.extend(item for item in offers if isinstance(item, dict))

    if not candidates:
        return None

    for candidate in candidates:
        availability = str(candidate.get("availability", "")).lower()
        if availability.endswith("/instock") or availability == "instock":
            return candidate
    return candidates[0]


def fetch_product_details(entry: dict[str, str]) -> dict[str, str]:
    details = {
        "url": entry["url"],
        "title": entry.get("title") or fallback_title_from_url(entry["url"]),
        "image": entry.get("image", ""),
        "price": "",
        "availability": "",
    }

    page_html = fetch_text(entry["url"])
    soup = BeautifulSoup(page_html, "html.parser")

    product_schema: dict[str, Any] | None = None
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        if "ld+json" not in script_type:
            continue

        raw_json = script.string or script.get_text()
        if not raw_json.strip():
            continue

        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        for obj in iter_json_objects(parsed):
            if is_product_schema(obj):
                product_schema = obj
                break
        if product_schema:
            break

    if product_schema:
        if product_schema.get("name"):
            details["title"] = str(product_schema["name"]).strip()

        image = product_schema.get("image")
        if isinstance(image, str):
            details["image"] = image
        elif isinstance(image, list) and image:
            first = image[0]
            if isinstance(first, str):
                details["image"] = first
            elif isinstance(first, dict):
                details["image"] = str(first.get("url", details["image"]))
        elif isinstance(image, dict):
            details["image"] = str(image.get("url", details["image"]))

        offer = choose_offer(product_schema.get("offers"))
        if offer:
            details["price"] = format_price(
                offer.get("price") or offer.get("lowPrice"),
                str(offer.get("priceCurrency", "")).upper() or None,
            ) or ""
            if offer.get("availability"):
                details["availability"] = humanize_availability(
                    str(offer["availability"])
                )

    # Open Graph fallbacks.
    og_title = soup.find("meta", attrs={"property": "og:title"})
    og_image = soup.find("meta", attrs={"property": "og:image"})
    if og_title and og_title.get("content"):
        details["title"] = str(og_title["content"]).strip()
    if not details["image"] and og_image and og_image.get("content"):
        details["image"] = str(og_image["content"]).strip()

    return details


def build_product_embed(product: dict[str, str]) -> dict[str, Any]:
    embed: dict[str, Any] = {
        "title": product["title"][:256],
        "url": product["url"],
        "description": "A new product page was detected on the official SpaceX Store.",
        "timestamp": iso_now(),
        "footer": {"text": "SpaceX Store Monitor"},
    }

    fields: list[dict[str, Any]] = []
    if product.get("price"):
        fields.append({"name": "Price", "value": product["price"], "inline": True})
    if product.get("availability"):
        fields.append(
            {
                "name": "Availability",
                "value": product["availability"],
                "inline": True,
            }
        )
    if fields:
        embed["fields"] = fields

    if product.get("image"):
        embed["image"] = {"url": product["image"]}

    return embed


def execute_discord_webhook(payload: dict[str, Any]) -> None:
    webhook_url = get_webhook_url()
    separator = "&" if "?" in webhook_url else "?"
    request_url = f"{webhook_url}{separator}wait=true"

    for attempt in range(4):
        response = SESSION.post(
            request_url,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Content-Type": "application/json"},
        )

        if response.status_code == 429:
            try:
                retry_after = float(response.json().get("retry_after", 1))
            except (ValueError, TypeError, json.JSONDecodeError):
                retry_after = 1
            time.sleep(retry_after + 0.25)
            continue

        if 200 <= response.status_code < 300:
            return

        body = response.text.replace("\n", " ")[:300]
        raise RuntimeError(
            f"Discord webhook returned HTTP {response.status_code}. Response: {body}"
        )

    raise RuntimeError("Discord webhook remained rate-limited after multiple attempts.")


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "initialized": False,
        "initialized_at": None,
        "heartbeat_at": None,
        "seen": {},
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()

    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {STATE_PATH}: {exc}") from exc

    if not isinstance(state, dict):
        raise RuntimeError(f"{STATE_PATH} must contain a JSON object.")

    state.setdefault("version", 1)
    state.setdefault("initialized", False)
    state.setdefault("initialized_at", None)
    state.setdefault("heartbeat_at", None)
    state.setdefault("seen", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(STATE_PATH)


def heartbeat_due(state: dict[str, Any]) -> bool:
    raw = state.get("heartbeat_at")
    if not raw:
        return True
    try:
        previous = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    return utc_now() - previous >= timedelta(days=HEARTBEAT_DAYS)


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def send_initialized_message(product_count: int) -> None:
    payload = {
        "username": "SpaceX Store Monitor",
        "content": DISCORD_MENTION or None,
        "embeds": [
            {
                "title": "SpaceX Store monitor initialized",
                "description": (
                    f"Tracking **{product_count}** existing product pages. "
                    "Future newly published products will be posted here."
                ),
                "url": STORE_BASE_URL,
                "timestamp": iso_now(),
                "footer": {"text": "SpaceX Store Monitor"},
            }
        ],
        "allowed_mentions": {"parse": []},
    }
    if not payload["content"]:
        payload.pop("content")
    execute_discord_webhook(payload)


def send_new_product_batch(products: list[dict[str, str]]) -> None:
    payload: dict[str, Any] = {
        "username": "SpaceX Store Monitor",
        "embeds": [build_product_embed(product) for product in products],
        "allowed_mentions": {"parse": []},
    }

    if DISCORD_MENTION:
        payload["content"] = DISCORD_MENTION
        # A literal role/user mention can be enabled deliberately with DISCORD_MENTION.
        payload["allowed_mentions"] = {"parse": ["roles", "users"]}

    execute_discord_webhook(payload)


def main() -> int:
    # Validate before doing network work so configuration errors are immediate.
    get_webhook_url()

    current_products = fetch_current_products()
    state = load_state()
    seen: dict[str, Any] = state["seen"]

    if not state["initialized"]:
        if SEND_INITIALIZED_MESSAGE:
            send_initialized_message(len(current_products))

        state["initialized"] = True
        state["initialized_at"] = iso_now()
        state["heartbeat_at"] = iso_now()
        state["seen"] = current_products
        save_state(state)
        print(f"Initialized with {len(current_products)} existing products.")
        return 0

    new_urls = sorted(set(current_products) - set(seen))

    if not new_urls:
        if heartbeat_due(state):
            state["heartbeat_at"] = iso_now()
            save_state(state)
            print("No new products. Monthly state heartbeat updated.")
        else:
            print(f"No new products. Tracking {len(seen)} previously seen URLs.")
        return 0

    print(f"Detected {len(new_urls)} new product URL(s).")

    for url_batch in chunked(new_urls, MAX_DISCORD_EMBEDS):
        detailed_products: list[dict[str, str]] = []

        for url in url_batch:
            entry = current_products[url]
            try:
                details = fetch_product_details(entry)
            except Exception as exc:
                # The sitemap data is enough to alert even if the product page
                # temporarily fails while it is being published.
                print(
                    f"Warning: could not load product details for {url}: {exc}",
                    file=sys.stderr,
                )
                details = {
                    "url": entry["url"],
                    "title": entry.get("title") or fallback_title_from_url(url),
                    "image": entry.get("image", ""),
                    "price": "",
                    "availability": "",
                }
            detailed_products.append(details)

        send_new_product_batch(detailed_products)

        # Save after every successful Discord batch to minimize duplicate alerts
        # if a later batch fails.
        for url in url_batch:
            seen[url] = current_products[url]
        state["seen"] = seen
        state["heartbeat_at"] = iso_now()
        save_state(state)

    print(f"Sent Discord alert(s) for {len(new_urls)} new product(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
