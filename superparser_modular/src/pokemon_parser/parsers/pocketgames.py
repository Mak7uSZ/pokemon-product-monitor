from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import ClientResponseError

from pokemon_parser.config import AppConfig
from pokemon_parser.models import ActionTarget, AddToCartTarget, CheckoutTarget, ParsedItem
from pokemon_parser.parsers.base import BaseParser
from pokemon_parser.utils.text import clean_text, normalize_text

logger = logging.getLogger(__name__)


class PocketGamesParser(BaseParser):
    site = "pocketgames"
    base_url = "https://pocketgames.nl"
    handle = "pokemon"

    DEFAULT_SECTION_ID = "template--17386837573891__main"
    DEFAULT_SECTIONS_URL = "/collections/pokemon"

    async def fetch(self, session: aiohttp.ClientSession, cfg: AppConfig) -> list[ParsedItem]:
        all_products: list[dict] = []
        page = 1
        max_pages = self.max_pages(cfg)
        page_delay = self.page_delay_seconds(cfg)
        timeout_seconds = self.request_timeout_seconds(cfg)

        while True:
            if max_pages is not None and page > max_pages:
                break

            if page > 1 and page_delay > 0:
                await asyncio.sleep(page_delay)

            url = f"{self.base_url}/collections/{self.handle}/products.json?limit=250&page={page}"
            try:
                async with session.get(url, timeout=timeout_seconds) as response:
                    response.raise_for_status()
                    payload = await response.json()
            except ClientResponseError as exc:
                if exc.status in {401, 403}:
                    # deny: пусть пайплайн включит cooldown/circuit breaker
                    raise
                if exc.status == 404:
                    # 404 чаще про смену URL/структуры; можно не банить, но логировать
                    logger.warning("[pocketgames] not_found status=%s url=%s", exc.status, url)
                    return []
                raise


            products = payload.get("products", [])
            if not products:
                break

            all_products.extend(products)
            if len(products) < 250:
                break
            page += 1

        items: list[ParsedItem] = []

        for product in all_products:
            title = clean_text(product.get("title"))
            title_norm = normalize_text(title)

            handle = product.get("handle") or str(product.get("id"))
            product_id = product.get("id")
            product_url = f"{self.base_url}/products/{handle}"

            available_variant = next((variant for variant in product.get("variants", []) if variant.get("available")), None)
            is_available = available_variant is not None
            variant_id = available_variant.get("id") if available_variant else None

            price_value = None
            try:
                if product.get("variants"):
                    price_value = float(product["variants"][0]["price"])
            except Exception:
                price_value = None

            target = ActionTarget(
                site=self.site,
                external_id=handle,
                title=title,
                product_url=product_url,
                add_to_cart=AddToCartTarget(
                    type="shopify_variant",
                    quantity=1,
                    variant_id=variant_id,
                    product_id=product_id,
                    cart_add_url=f"{self.base_url}/cart/add",
                    cart_url=f"{self.base_url}/cart",
                    product_url=product_url,
                    section_id=self.DEFAULT_SECTION_ID,
                    sections_url=self.DEFAULT_SECTIONS_URL,
                ),
                checkout=CheckoutTarget(
                    type="shopify_cart",
                    cart_url=f"{self.base_url}/cart",
                    checkout_url=f"{self.base_url}/checkout",
                ),
                meta={
                    "product_id": product_id,
                    "variant_id": variant_id,
                    "handle": handle,
                },
            )

            items.append(
                ParsedItem(
                    site=self.site,
                    external_id=handle,
                    title=title,
                    title_norm=title_norm,
                    url=product_url,
                    price_value=price_value,
                    availability_text="available" if is_available else "unavailable",
                    is_available=is_available,
                    seller="pocketgames",
                    extra={"product_id": product_id, "variant_id": variant_id},
                    target=target,
                )
            )

        return items
