from __future__ import annotations

import asyncio
import random
import re
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

from pokemon_parser.config import AppConfig
from pokemon_parser.models import ActionTarget, AddToCartTarget, CheckoutTarget, ParsedItem
from pokemon_parser.parsers.base import BaseParser
from pokemon_parser.utils.text import clean_text, normalize_text, parse_price_eur


class BolParser(BaseParser):
    site = "bol"
    category_url = "https://www.bol.com/nl/nl/l/trading-cards/20303/4278866641/"

    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    PRODUCT_HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    async def _fetch_text(
        self,
        session: aiohttp.ClientSession,
        url: str,
        cfg: AppConfig,
        *,
        headers: dict[str, str],
        retries: int | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """
        Async HTTP fetch with small bounded retry budget.

        Policy:
        - no retry on 403/429
        - short retry on transient transport / 5xx
        - preserve ClientResponseError so pipeline/antiban can see real status
        """
        last_exc: Exception | None = None
        retries = self.max_retries(cfg) if retries is None else max(0, retries)
        timeout_seconds = self.request_timeout_seconds(cfg) if timeout_seconds is None else timeout_seconds

        for attempt in range(retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                async with session.get(url, headers=headers, timeout=timeout) as response:
                    if response.status in (403, 429):
                        response.raise_for_status()

                    if 500 <= response.status < 600:
                        response.raise_for_status()

                    response.raise_for_status()
                    return await response.text()

            except aiohttp.ClientResponseError as exc:
                last_exc = exc
                if exc.status in (403, 429):
                    raise
                if not (500 <= exc.status < 600):
                    raise

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc

            if attempt < retries:
                await self.sleep_retry(cfg, attempt)

        assert last_exc is not None
        raise last_exc

    async def fetch_page(self, session: aiohttp.ClientSession, cfg: AppConfig, page: int) -> str:
        url = f"{self.category_url}?page={page}"
        return await self._fetch_text(
            session,
            url,
            cfg,
            headers=self.DEFAULT_HEADERS,
        )

    async def fetch_product(self, session: aiohttp.ClientSession, cfg: AppConfig, product_url: str) -> str:
        return await self._fetch_text(
            session,
            product_url,
            cfg,
            headers=self.PRODUCT_HEADERS,
        )

    def _normalize_url(self, href: str) -> str:
        href = clean_text(href)
        if not href:
            return ""

        if href.startswith(("http://", "https://")):
            url = href
        else:
            url = f"https://www.bol.com{href}"

        return url.split("#", 1)[0].split("?", 1)[0]

    def _extract_product_id_from_url(self, product_url: str) -> Optional[str]:
        match = re.search(r"/p/[^/]+/(\d+)(?:/)?$", product_url)
        return match.group(1) if match else None

    def _build_add_to_cart_url(self, product_id: str, offer_uid: str, quantity: int = 1) -> str:
        return (
            "https://www.bol.com/nl/order/basket/addItems.html"
            f"?productId={product_id}&offerUid={offer_uid}&quantity={int(quantity)}"
        )

    def _extract_offer_uid(self, html: str) -> Optional[str]:
        patterns = [
            r'"offerUid"\s*:\s*"([a-f0-9\-]{36})"',
            r"offerUid=([a-f0-9\-]{36})",
            r'data-offer-uid="([a-f0-9\-]{36})"',
            r'"offerId"\s*:\s*"([a-f0-9\-]{36})"',
            r'"defaultOfferId"\s*:\s*"([a-f0-9\-]{36})"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _extract_title_url(self, card) -> tuple[str | None, str | None]:
        candidates: list[tuple[str, str]] = []

        for anchor in card.select("a[href*='/p/']"):
            href = self._normalize_url(anchor.get("href") or "")
            text = clean_text(anchor.get_text(" ", strip=True))
            if href and text and len(text) >= 8:
                candidates.append((text, href))

        for text, href in candidates:
            if "pok" in text.lower():
                return text, href

        return candidates[0] if candidates else (None, None)

    def _extract_price(self, card) -> Optional[float]:
        html = str(card)
        text = clean_text(card.get_text(" ", strip=True))

        price_sentence_patterns = [
            r"De prijs van dit product is\s*'?\s*(\d+)\s*'?\s*euro\s*en\s*'?\s*(\d{2})\s*'?\s*cent",
            r"De prijs van dit product is\s*(\d+)\s*euro\s*en\s*(\d{2})\s*cent",
        ]

        for source in (html, text):
            for pattern in price_sentence_patterns:
                match = re.search(pattern, source, flags=re.IGNORECASE)
                if match:
                    euros, cents = match.groups()
                    try:
                        return float(f"{int(euros)}.{cents}")
                    except Exception:
                        pass

        candidates: list[float] = []

        selectors = [
            "[data-test='price']",
            "[data-test*='price']",
            ".promo-price",
            ".price-block",
            ".product-prices",
            ".price",
        ]

        for selector in selectors:
            for node in card.select(selector):
                node_text = clean_text(node.get_text(" ", strip=True))
                if not node_text:
                    continue
                for raw in re.findall(r"\b(\d+[.,]\d{2})\b", node_text):
                    value = parse_price_eur(raw)
                    if value is not None:
                        candidates.append(value)

        for raw in re.findall(r"(?:€|&euro;)\s*([0-9]+(?:[.,][0-9]{2}))", html, flags=re.IGNORECASE):
            value = parse_price_eur(raw)
            if value is not None:
                candidates.append(value)

        cleaned: list[float] = []
        for value in candidates:
            rounded = round(value, 2)
            if rounded < 1 or rounded > 10000:
                continue
            cleaned.append(rounded)

        if not cleaned:
            return None

        return min(cleaned)

    def _extract_seller(self, card) -> Optional[str]:
        text = clean_text(card.get_text(" ", strip=True))
        for pattern in [
            r"Verkoop door\s+(.+?)(?:Wat je kan verwachten|Nieuw & Goedkoper|Select|In winkelwagen|Op voorraad|Niet leverbaar|$)",
            r"Sold by\s+(.+?)(?:What you can expect|Add to cart|In stock|Unavailable|$)",
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                seller = clean_text(match.group(1))
                if seller:
                    return seller
        return None

    def _extract_availability(self, card) -> tuple[Optional[str], bool]:
        text = clean_text(card.get_text(" ", strip=True))
        html = str(card)

        if "Huidige voorraad bijna op" in text:
            return "Huidige voorraad bijna op", True
        if "Op voorraad" in text:
            return "Op voorraad", True
        if "Tijdelijk niet leverbaar" in text:
            return "Tijdelijk niet leverbaar", False
        if "Niet leverbaar" in text:
            return "Niet leverbaar", False

        eta = re.search(r"(Uiterlijk .*? in huis)", text)
        if eta:
            return clean_text(eta.group(1)), True

        if "offerUid" in html or "addItems.html" in html:
            return "possible_available", True

        return None, False

    def parse_products(self, html: str, cfg: AppConfig, page_num: int) -> list[ParsedItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[ParsedItem] = []
        seen: set[str] = set()

        for card in soup.select("li, article, div"):
            try:
                title, product_url = self._extract_title_url(card)
                if not title or not product_url:
                    continue

                title = clean_text(title)
                title_norm = normalize_text(title)
                card_text = clean_text(card.get_text(" ", strip=True))

                if "trading cards" not in card_text.lower() and "pok" not in title.lower():
                    continue

                price_value = self._extract_price(card)
                seller = self._extract_seller(card) or "bol"
                availability_text, is_available = self._extract_availability(card)
                product_id = self._extract_product_id_from_url(product_url)
                offer_uid = self._extract_offer_uid(str(card))
                add_to_cart_url = (
                    self._build_add_to_cart_url(product_id, offer_uid, 1)
                    if product_id and offer_uid
                    else None
                )

                # The numeric Bol product id is stable across slug, locale, and
                # canonical-URL changes. Fall back to the historical URL key only
                # for malformed/legacy cards where Bol exposes no product id.
                external_id = product_id or re.sub(
                    r"[^a-zA-Z0-9]+", "_", product_url.lower()
                ).strip("_")
                if not external_id or external_id in seen:
                    continue
                seen.add(external_id)

                target = ActionTarget(
                    site=self.site,
                    external_id=external_id,
                    title=title,
                    product_url=product_url,
                    add_to_cart=AddToCartTarget(
                        type="direct_url",
                        quantity=1,
                        add_to_cart_url=add_to_cart_url,
                        product_id=product_id,
                        offer_uid=offer_uid,
                        product_url=product_url,
                    ),
                    checkout=CheckoutTarget(
                        type="url",
                        checkout_url=cfg.bol_buy_now_url,
                    ),
                    meta={
                        "seller": seller,
                        "page": page_num,
                        "product_id": product_id,
                        "offer_uid": offer_uid,
                    },
                )

                items.append(
                    ParsedItem(
                        site=self.site,
                        external_id=external_id,
                        title=title,
                        title_norm=title_norm,
                        url=product_url,
                        price_value=price_value,
                        availability_text=availability_text,
                        is_available=is_available,
                        seller=seller,
                        extra={
                            "page": page_num,
                            "product_id": product_id,
                            "offer_uid": offer_uid,
                            "add_to_cart_url": add_to_cart_url,
                        },
                        target=target,
                    )
                )
            except Exception:
                continue

        return items

    async def fetch(self, session: aiohttp.ClientSession, cfg: AppConfig) -> list[ParsedItem]:
        all_items: list[ParsedItem] = []
        found_any = False
        max_pages = self.max_pages(cfg) or 20
        page_delay = self.page_delay_seconds(cfg)

        for page in range(1, max_pages + 1):
            if page > 1 and page_delay > 0:
                await asyncio.sleep(page_delay)

            html = await self.fetch_page(session, cfg, page)
            items = self.parse_products(html, cfg, page)

            if items:
                all_items.extend(items)
                found_any = True
            elif found_any or page >= 3:
                break

        return all_items

    async def enrich(self, session: aiohttp.ClientSession, item: ParsedItem, cfg: AppConfig) -> ParsedItem:
        if item.target is None or item.target.add_to_cart is None:
            return item

        if item.target.add_to_cart.add_to_cart_url:
            return item

        product_url = item.target.product_url
        product_id = item.target.add_to_cart.product_id
        if not product_url or not product_id:
            return item

        html = await self.fetch_product(session, cfg, product_url)
        offer_uid = self._extract_offer_uid(html)
        add_to_cart_url = self._build_add_to_cart_url(product_id, offer_uid) if offer_uid else None

        target = ActionTarget(
            site=item.target.site,
            external_id=item.target.external_id,
            title=item.target.title,
            product_url=item.target.product_url,
            add_to_cart=AddToCartTarget(
                type="direct_url",
                quantity=1,
                add_to_cart_url=add_to_cart_url,
                product_id=product_id,
                offer_uid=offer_uid,
                product_url=product_url,
            ),
            checkout=item.target.checkout,
            meta={**dict(item.target.meta), "offer_uid": offer_uid},
        )

        return ParsedItem(
            site=item.site,
            external_id=item.external_id,
            title=item.title,
            title_norm=item.title_norm,
            url=item.url,
            price_value=item.price_value,
            availability_text=item.availability_text,
            is_available=item.is_available,
            seller=item.seller,
            extra={**dict(item.extra), "offer_uid": offer_uid, "add_to_cart_url": add_to_cart_url},
            target=target,
        )
