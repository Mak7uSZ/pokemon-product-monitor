from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from pokemon_parser.config import AppConfig
from pokemon_parser.dreamland_availability import (
    DreamlandAvailability,
    detect_dreamland_availability,
    detect_dreamland_availability_from_soup,
)
from pokemon_parser.models import ActionTarget, AddToCartTarget, CheckoutTarget, ParsedItem
from pokemon_parser.parsers.base import BaseParser
from pokemon_parser.utils.text import clean_text, normalize_text, parse_price_eur

logger = logging.getLogger(__name__)


class DreamLandParserDeny(aiohttp.ClientResponseError):
    """
    Explicit parser-level deny signal for DreamLand.

    Raise this when we detect:
    - hard 403 / 429
    - challenge / blocked HTML
    - repeated access denial where pipeline should trigger antiban cooldown
    """
    pass


class DreamLandParser(BaseParser):
    site = "dreamland"
    base_url = "https://www.dreamland.nl"
    category_url = (
    "https://www.dreamland.nl/c/gezelschapspellen-en-puzzels/ruilkaarten/producten"
    "?SUBBRAND%5B%5D=POKEMON"
)

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) "
            "Gecko/20100101 Firefox/149.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    PRODUCT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) "
            "Gecko/20100101 Firefox/149.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    PDP_CONCURRENCY = 5
    MAX_PAGES = 25

    GENERIC_LISTING_TEXTS = {
        "filter producten",
        "alleen online",
        "bestel nu",
        "levering aan huis",
        "naar winkelmandje",
        "verder winkelen",
        "verder naar bestellen",
        "doorgaan naar betaalwijze",
        "naar besteloverzicht",
        "hou me op de hoogte",
        "huidige levertijd",
        "gratis verzending",
        "gratis afhalen",
        "retourneren binnen 30 dagen",
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
        last_exc: Exception | None = None
        retries = self.max_retries(cfg) if retries is None else max(0, retries)
        timeout_seconds = self.request_timeout_seconds(cfg) if timeout_seconds is None else timeout_seconds

        for attempt in range(retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                async with session.get(url, headers=headers, timeout=timeout) as response:
                    if response.status in (403, 429):
                        raise DreamLandParserDeny(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                            message="dreamland_rate_limited",
                            headers=response.headers,
                        )

                    response.raise_for_status()
                    html = await response.text()

                    if self._looks_like_block_page(html):
                        raise DreamLandParserDeny(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                            message="dreamland_block_page",
                            headers=response.headers,
                        )

                    return html

            except DreamLandParserDeny:
                raise

            except aiohttp.ClientResponseError as exc:
                last_exc = exc
                if exc.status in (403, 429):
                    raise DreamLandParserDeny(
                        request_info=exc.request_info,
                        history=exc.history,
                        status=exc.status,
                        message=f"dreamland_http_{exc.status}",
                        headers=exc.headers,
                    )
                if not (500 <= exc.status < 600):
                    raise

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc

            if attempt < retries:
                await self.sleep_retry(cfg, attempt)

        assert last_exc is not None
        raise last_exc

    def _looks_like_block_page(self, html: str) -> bool:
        if not html:
            return True

        lowered = html.lower()

        suspicious_markers = [
            "access denied",
            "temporarily blocked",
            "unusual traffic",
            "verify you are human",
            "captcha",
            "cloudflare",
            "attention required",
        ]

        return any(marker in lowered for marker in suspicious_markers)

    async def fetch_page(self, session: aiohttp.ClientSession, cfg: AppConfig, page: int) -> str:
        if page == 1:
            url = self.category_url
        else:
            url = f"{self.category_url}&page={page}"
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
            url = urljoin(self.base_url, href)

        parsed = urlparse(url)
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return normalized.rstrip("/")

    def _extract_listing_cards(self, html: str, page_num: int) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        anchors = soup.select('a[href*="/producten/"]')
        logger.info("[dreamland] listing page=%s raw_product_anchors=%s", page_num, len(anchors))

        for anchor in anchors:
            href = anchor.get("href") or ""
            product_url = self._normalize_url(href)
            if not product_url or product_url in seen_urls:
                continue

            # поднимаемся вверх до разумной карточки
            card = anchor
            for _ in range(8):
                if card.parent is None:
                    break
                card = card.parent

            card_text = clean_text(card.get_text(" ", strip=True))
            if not card_text or len(card_text) < 10:
                continue

            title = self._extract_listing_title_from_card(card, anchor, product_url)
            if not title:
                continue

            title_norm = normalize_text(f"{title} {card_text}")

            # mixed category safeguard, но мягче
            if "pokemon" not in title_norm:
                continue

            price_value = self._extract_price_from_node(card)
            image_url = self._extract_image_url(card)
            listing_availability = detect_dreamland_availability(text=card_text)

            results.append(
                {
                    "page": page_num,
                    "listing_title": title,
                    "product_url": product_url,
                    "listing_price_value": price_value,
                    "image_url": image_url,
                    "listing_text": card_text,
                    "listing_is_available_hint": listing_availability.purchasable,
                    "listing_availability_hint_text": listing_availability.reason,
                    "listing_availability_status": listing_availability.status,
                    "listing_negative_signals": list(listing_availability.negative_signals),
                    "listing_positive_signals": list(listing_availability.positive_signals),
                }
            )
            seen_urls.add(product_url)

        logger.info("[dreamland] listing page=%s parsed_cards=%s", page_num, len(results))
        return results

    def _is_generic_listing_text(self, text: str) -> bool:
        normalized = normalize_text(text)
        if not normalized:
            return True

        if normalized in self.GENERIC_LISTING_TEXTS:
            return True

        generic_prefixes = (
            "huidige levertijd",
            "gratis verzending",
            "gratis afhalen",
            "retourneren",
            "klanten geven",
            "op dit moment niet op te halen",
        )
        return any(normalized.startswith(prefix) for prefix in generic_prefixes)

    def _extract_listing_title_from_events(self, node) -> str:
        candidates = []

        if getattr(node, "attrs", None) and node.get("data-events"):
            candidates.append(node)

        try:
            candidates.extend(node.select("[data-events]"))
        except Exception:
            pass

        for candidate in candidates:
            raw_events = clean_text(candidate.get("data-events") or "")
            if not raw_events:
                continue

            try:
                events = json.loads(raw_events)
            except Exception:
                continue

            if not isinstance(events, list):
                continue

            for event in events:
                if not isinstance(event, dict):
                    continue

                data = event.get("data")
                if not isinstance(data, dict):
                    continue

                for key in ("name", "variant"):
                    text = clean_text(data.get(key) or "")
                    if text and not self._is_generic_listing_text(text):
                        return text

        return ""

    def _build_listing_item(self, card: dict[str, Any]) -> ParsedItem | None:
        title = clean_text(card.get("listing_title"))
        if not title:
            return None

        product_url = card["product_url"]
        external_id = self._extract_product_code(product_url, product_url)
        title_norm = normalize_text(title)

        is_available = bool(card.get("listing_is_available_hint", False))
        availability_text = card.get("listing_availability_hint_text")
        if not availability_text:
            availability_text = "listing_unknown"

        target = None
        if is_available:
            target = self._build_target(
                external_id=external_id,
                title=title,
                product_url=product_url,
                page=card["page"],
                image_url=card.get("image_url"),
                price_source="listing",
                availability_source="listing",
            )

        return ParsedItem(
            site=self.site,
            external_id=external_id,
            title=title,
            title_norm=title_norm,
            url=product_url,
            price_value=card.get("listing_price_value"),
            availability_text=availability_text,
            is_available=is_available,
            seller="dreamland",
            extra={
                "page": card["page"],
                "image_url": card.get("image_url"),
                "price_source": "listing",
                "availability_source": "listing",
                "availability_status": card.get("listing_availability_status") or "listing",
                "purchasable": is_available,
                "availability_reason": availability_text,
                "negative_signals": card.get("listing_negative_signals") or [],
                "positive_signals": card.get("listing_positive_signals") or [],
                "listing_price_value": card.get("listing_price_value"),
                "listing_is_available_hint": card.get("listing_is_available_hint", False),
            },
            target=target,
        )
    def _extract_listing_title_from_card(self, card, anchor, product_url: str) -> str:
        # 1. пробуем сам anchor
        event_title = self._extract_listing_title_from_events(card)
        if event_title:
            return event_title

        anchor_text = clean_text(anchor.get_text(" ", strip=True))
        if anchor_text and len(anchor_text) >= 6 and not self._is_generic_listing_text(anchor_text):
            return anchor_text

        # 2. ищем title/headings внутри карточки
        selectors = [
            "h1",
            "h2",
            "h3",
            "h4",
            '[data-testid*="title"]',
            '[class*="title"]',
            '[class*="name"]',
        ]
        for selector in selectors:
            for node in card.select(selector):
                text = clean_text(node.get_text(" ", strip=True))
                if text and len(text) >= 6 and not self._is_generic_listing_text(text):
                    return text

        # 3. fallback: берём slug из URL
        slug = product_url.rstrip("/").split("/")[-2] if "/" in product_url.rstrip("/") else ""
        slug = clean_text(slug.replace("-", " "))
        if slug:
            return slug

        return ""

    def _extract_image_url(self, node) -> str | None:
        img = node.find("img")
        if not img:
            return None

        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("srcset")
            or ""
        )
        src = clean_text(src)
        if not src:
            return None

        if " " in src and "," in src:
            src = src.split(",")[0].strip().split(" ")[0].strip()
        elif " " in src and src.startswith("http"):
            src = src.split(" ")[0].strip()

        return urljoin(self.base_url, src)

    def _extract_price_from_node(self, node) -> float | None:
        candidates: list[float] = []

        # 1. самый точный вариант: dedicated price node
        for price_node in node.select('[data-product-price], .product-pricing__price'):
            raw_text = clean_text(price_node.get_text(" ", strip=True))
            parsed = parse_price_eur(raw_text)
            if parsed is not None:
                candidates.append(parsed)

            # DreamLand split format: "18," + <span class="decimals">99</span>
            full_html = str(price_node)

            split_match = re.search(
                r'€?\s*(\d{1,4})\s*,\s*<span[^>]*class="[^"]*decimals[^"]*"[^>]*>\s*(\d{2})\s*</span>',
                full_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if split_match:
                euros, cents = split_match.groups()
                try:
                    candidates.append(float(f"{int(euros)}.{cents}"))
                except Exception:
                    pass

        # 2. analytics JSON in data-events on article/product-card
        article = node if getattr(node, "has_attr", lambda *_: False)("data-events") else node.find(attrs={"data-events": True})
        if article and article.get("data-events"):
            raw_events = article.get("data-events") or ""
            try:
                # BeautifulSoup usually already decodes &quot; into normal quotes
                events = json.loads(raw_events)
                if isinstance(events, list):
                    for event in events:
                        data = event.get("data", {}) if isinstance(event, dict) else {}
                        price = data.get("price")
                        if price is not None:
                            try:
                                candidates.append(round(float(price), 2))
                            except Exception:
                                pass
            except Exception:
                pass

        # 3. generic price selectors
        selectors = [
            ".product-pricing__prices",
            ".product-card__prices",
            '[class*="price"]',
            '[class*="Price"]',
            '[id*="price"]',
        ]
        for selector in selectors:
            for price_node in node.select(selector):
                raw_text = clean_text(price_node.get_text(" ", strip=True))
                parsed = parse_price_eur(raw_text)
                if parsed is not None:
                    candidates.append(parsed)

        # 4. cleanup
        cleaned: list[float] = []
        for value in candidates:
            try:
                rounded = round(float(value), 2)
            except Exception:
                continue
            if 0.5 <= rounded <= 1000:
                cleaned.append(rounded)

        if not cleaned:
            return None

        # for listing card actual sell price, minimal valid candidate is usually correct
        return min(cleaned)

    def _extract_json_ld(self, html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        blobs: list[dict[str, Any]] = []

        for script in soup.select('script[type="application/ld+json"]'):
            raw = script.string or script.get_text(strip=True) or ""
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue

            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        blobs.append(item)
            elif isinstance(payload, dict):
                blobs.append(payload)

        return blobs

    def _extract_product_code(self, url: str, html: str) -> str:
        tail = url.rstrip("/").split("/")[-1].strip()
        if tail:
            normalized_tail = re.sub(r"[^a-zA-Z0-9_]+", "_", tail).strip("_")
            if normalized_tail:
                return normalized_tail

        html_match = re.search(r"\b(\d{6,12}(?:_\d{3})?)\b", html)
        if html_match:
            return html_match.group(1)

        return re.sub(r"[^a-zA-Z0-9]+", "_", url.lower()).strip("_")

    def _parse_pdp_title(self, soup: BeautifulSoup, fallback: str) -> str:
        selectors = [
            "h1",
            '[data-testid="product-title"]',
            ".product-title",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                title = clean_text(node.get_text(" ", strip=True))
                if title and len(title) >= 4:
                    return title
        return fallback

    def _parse_pdp_price(self, html: str, soup: BeautifulSoup, fallback: float | None) -> tuple[float | None, str]:
        # 1. JSON-LD offers price
        for blob in self._extract_json_ld(html):
            price = self._extract_price_from_jsonld(blob)
            if price is not None:
                return price, "pdp_jsonld"

        # 2. visible DOM
        selectors = [
            '[data-testid*="price"]',
            '[class*="price"]',
            '[id*="price"]',
        ]
        candidates: list[float] = []
        for selector in selectors:
            for node in soup.select(selector):
                value = self._extract_price_from_node(node)
                if value is not None:
                    candidates.append(value)

        if candidates:
            return min(candidates), "pdp_dom"

        # 3. fallback to full page text
        page_text_price = parse_price_eur(clean_text(soup.get_text(" ", strip=True)))
        if page_text_price is not None:
            return page_text_price, "pdp_text"

        return fallback, "listing_fallback" if fallback is not None else "missing"

    def _extract_price_from_jsonld(self, node: Any) -> float | None:
        if isinstance(node, dict):
            node_type = str(node.get("@type", "")).lower()

            if node_type == "product":
                offers = node.get("offers")
                offer_price = self._extract_price_from_jsonld(offers)
                if offer_price is not None:
                    return offer_price

            if node_type in {"offer", "aggregateoffer"}:
                for key in ("price", "lowPrice", "highPrice"):
                    if key in node:
                        parsed = parse_price_eur(node.get(key))
                        if parsed is not None:
                            return parsed

            for value in node.values():
                parsed = self._extract_price_from_jsonld(value)
                if parsed is not None:
                    return parsed

        elif isinstance(node, list):
            for item in node:
                parsed = self._extract_price_from_jsonld(item)
                if parsed is not None:
                    return parsed

        return None

    def _parse_pdp_availability(self, soup: BeautifulSoup) -> DreamlandAvailability:
        return detect_dreamland_availability_from_soup(soup)

    def _parse_pdp_stock(self, soup: BeautifulSoup) -> tuple[bool, str, str]:
        availability = self._parse_pdp_availability(soup)
        return availability.purchasable, availability.reason, f"pdp_{availability.status}"

    def _build_target(
        self,
        external_id: str,
        title: str,
        product_url: str,
        page: int,
        image_url: str | None,
        price_source: str,
        availability_source: str,
    ) -> ActionTarget:
        return ActionTarget(
            site=self.site,
            external_id=external_id,
            title=title,
            product_url=product_url,
            add_to_cart=AddToCartTarget(
                type="ui_button",
                quantity=1,
                product_id=external_id,
                product_url=product_url,
                pdp_button_selector=(
                    'button[type="submit"], '
                    'button[name="add"], '
                    'button[data-testid*="add"], '
                    'button[class*="add"], '
                    'button[class*="cart"], '
                    'button[aria-label*="winkelmand"]'
                ),
            ),
            checkout=CheckoutTarget(
                type="ui_flow",
                cart_url=f"{self.base_url}/cart",
                checkout_url=f"{self.base_url}/checkout",
            ),
            meta={
                "page": page,
                "image_url": image_url,
                "price_source": price_source,
                "availability_source": availability_source,
                "site_hint": "dreamland",
            },
        )

    async def _enrich_one(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        card: dict[str, Any],
        cfg: AppConfig,
    ) -> ParsedItem | None:
        async with sem:
            product_url = card["product_url"]
            html = await self.fetch_product(session, cfg, product_url)
            soup = BeautifulSoup(html, "html.parser")

            title = self._parse_pdp_title(soup, card["listing_title"])
            title_norm = normalize_text(title)

            listing_norm = normalize_text(card.get("listing_title", ""))
            if "pokemon" not in title_norm and "pokemon" not in listing_norm:
                return None

            external_id = self._extract_product_code(product_url, html)
            price_value, price_source = self._parse_pdp_price(
                html,
                soup,
                card.get("listing_price_value"),
            )
            availability = self._parse_pdp_availability(soup)
            is_available = availability.purchasable
            availability_text = availability.reason
            availability_source = f"pdp_{availability.status}"

            canonical = soup.select_one('link[rel="canonical"]')
            final_url = self._normalize_url(canonical.get("href")) if canonical and canonical.get("href") else product_url

            target = None
            if is_available:
                target = self._build_target(
                    external_id=external_id,
                    title=title,
                    product_url=final_url,
                    page=card["page"],
                    image_url=card.get("image_url"),
                    price_source=price_source,
                    availability_source=availability_source,
                )

            if card.get("listing_is_available_hint") and not is_available:
                logger.info(
                    "[dreamland] listing hint ignored external_id=%s title=%r listing_hint=%s pdp_reason=%s negative_signals=%s",
                    external_id,
                    title,
                    card.get("listing_availability_hint_text"),
                    availability.reason,
                    list(availability.negative_signals),
                )

            logger.info(
                "[dreamland] product parsed external_id=%s title=%r price=%s purchasable=%s availability_status=%s reason=%s negative=%s positive=%s action_target=%s",
                external_id,
                title,
                price_value,
                is_available,
                availability.status,
                availability.reason,
                list(availability.negative_signals),
                list(availability.positive_signals),
                target is not None,
            )

            return ParsedItem(
                site=self.site,
                external_id=external_id,
                title=title,
                title_norm=title_norm,
                url=final_url,
                price_value=price_value,
                availability_text=availability_text,
                is_available=is_available,
                seller="dreamland",
                extra={
                    "page": card["page"],
                    "image_url": card.get("image_url"),
                    "price_source": price_source,
                    "availability_source": availability_source,
                    "availability_status": availability.status,
                    "purchasable": is_available,
                    "availability_reason": availability.reason,
                    "negative_signals": list(availability.negative_signals),
                    "positive_signals": list(availability.positive_signals),
                    "cta_texts": list(availability.cta_texts[:8]),
                    "availability_raw_text": availability.raw_text[:500],
                    "listing_price_value": card.get("listing_price_value"),
                    "listing_is_available_hint": card.get("listing_is_available_hint"),
                    "listing_availability_hint_text": card.get("listing_availability_hint_text"),
                },
                target=target,
            )

    async def fetch(self, session: aiohttp.ClientSession, cfg: AppConfig) -> list[ParsedItem]:
        all_cards: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        found_any = False
        max_pages = self.max_pages(cfg) or self.MAX_PAGES
        page_delay = self.page_delay_seconds(cfg)

        for page in range(1, max_pages + 1):
            html = await self.fetch_page(session, cfg, page)
            page_cards = self._extract_listing_cards(html, page)

            new_cards: list[dict[str, Any]] = []
            for card in page_cards:
                if card["product_url"] in seen_urls:
                    continue
                seen_urls.add(card["product_url"])
                new_cards.append(card)

            if new_cards:
                all_cards.extend(new_cards)
                logger.info("[dreamland] listing collected total_cards=%s", len(all_cards))
                found_any = True
                logger.info("[dreamland] listing page=%s cards=%s new=%s", page, len(page_cards), len(new_cards))
            elif found_any or page >= 3:
                logger.info("[dreamland] stop pagination page=%s reason=no_new_cards", page)
                break

            if page > 1 and page_delay > 0:
                await asyncio.sleep(page_delay)

        if not all_cards:
            return []

        # 1. базовые listing items
        listing_items_by_url: dict[str, ParsedItem] = {}
        for card in all_cards:
            item = self._build_listing_item(card)
            if item is None:
                continue
            listing_items_by_url[card["product_url"]] = item

        # 2. пробуем PDP enrich, но fail-soft
        sem = asyncio.Semaphore(self.PDP_CONCURRENCY)
        tasks = [self._enrich_one(session, sem, card, cfg) for card in all_cards]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("[dreamland] pdp results total=%s", len(results))

        final_items: list[ParsedItem] = []
        seen_ids: set[str] = set()

        for card, result in zip(all_cards, results):
            listing_item = listing_items_by_url.get(card["product_url"])

            # hard deny должен пробрасываться наверх в pipeline/antiban
            if isinstance(result, DreamLandParserDeny):
                raise result

            # обычные PDP ошибки не должны убивать товар
            if isinstance(result, Exception):
                logger.warning(
                    "[dreamland] pdp enrich failed type=%s error=%s url=%s",
                    type(result).__name__,
                    result,
                    card["product_url"],
                )
                result = None

            final_item = result if isinstance(result, ParsedItem) else listing_item
            if final_item is None:
                continue

            if final_item.external_id in seen_ids:
                logger.info(
                    "[dreamland] duplicate external_id=%s url=%s",
                    final_item.external_id,
                    final_item.url,
                )
                continue

            seen_ids.add(final_item.external_id)
            final_items.append(final_item)

        with_price = sum(1 for item in final_items if item.price_value is not None)
        without_price = sum(1 for item in final_items if item.price_value is None)
        available_count = sum(1 for item in final_items if item.is_available)

        listing_with_price = sum(1 for card in all_cards if card.get("listing_price_value") is not None)
        listing_without_price = sum(1 for card in all_cards if card.get("listing_price_value") is None)

        logger.info(
            "[dreamland] listing price stats total_cards=%s with_price=%s without_price=%s",
            len(all_cards),
            listing_with_price,
            listing_without_price,
        )
        logger.info(
            "[dreamland] final item stats total=%s available=%s unavailable=%s with_price=%s without_price=%s",
            len(final_items),
            available_count,
            len(final_items) - available_count,
            with_price,
            without_price,
        )
        return final_items
