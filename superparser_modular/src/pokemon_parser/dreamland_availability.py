from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pokemon_parser.utils.text import clean_text, normalize_text


@dataclass(frozen=True)
class DreamlandAvailability:
    purchasable: bool
    status: str
    reason: str
    raw_text: str
    negative_signals: tuple[str, ...] = ()
    positive_signals: tuple[str, ...] = ()
    cta_texts: tuple[str, ...] = ()


STOCK_NEGATIVE_PHRASES = (
    "online tijdelijk uitverkocht",
    "tijdelijk uitverkocht",
    "uitverkocht",
    "niet op voorraad",
    "niet beschikbaar",
    "geen voorraad",
    "voorraad komt er wellicht nog aan",
    "weet als eerste als dit product op voorraad is",
    "goed nieuws dit product is er snel",
    "beschikbaar vanaf",
)

NOTIFY_NEGATIVE_PHRASES = (
    "hou me op de hoogte",
    "houd me op de hoogte",
    "notify me",
)

WISHLIST_ONLY_PHRASES = (
    "toevoegen aan verlanglijstje",
    "verlanglijstje",
)

POSITIVE_TEXT_PHRASES = (
    "huidige levertijd",
    "online beschikbaar",
    "leverbaar",
    "op voorraad",
)

POSITIVE_CTA_PHRASES = (
    "levering aan huis",
    "bestel nu",
    "toevoegen aan winkelmandje",
    "voeg toe aan winkelmandje",
    "in winkelmandje",
)


def _contains_phrase(text_norm: str, phrase: str) -> bool:
    phrase_norm = normalize_text(phrase)
    return bool(phrase_norm and phrase_norm in text_norm)


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _signal_matches(text_norm: str, phrases: tuple[str, ...], *, prefix: str) -> list[str]:
    return [f"{prefix}:{phrase}" for phrase in phrases if _contains_phrase(text_norm, phrase)]


def _button_texts_from_soup(soup: Any) -> tuple[str, ...]:
    texts: list[str] = []
    for selector in ("button", "a", "input[type='submit']", "input[type='button']"):
        try:
            nodes = soup.select(selector)
        except Exception:
            continue
        for node in nodes:
            disabled = (
                node.has_attr("disabled")
                or str(node.get("aria-disabled", "")).lower() == "true"
                or "disabled" in normalize_text(" ".join(node.get("class", [])))
            )
            if disabled:
                continue
            if node.name == "input":
                text = clean_text(node.get("value") or node.get("aria-label") or "")
            else:
                text = clean_text(node.get_text(" ", strip=True) or node.get("aria-label") or "")
            if text:
                texts.append(text)
    return _dedupe(texts)


def _has_enabled_cart_form(soup: Any) -> bool:
    try:
        forms = soup.select('form[action*="/cart/add"], form[data-component="cart/add-to-cart-action-form"]')
    except Exception:
        return False

    for form in forms:
        buttons = form.select("button, input[type='submit'], input[type='button']")
        for button in buttons:
            disabled = (
                button.has_attr("disabled")
                or str(button.get("aria-disabled", "")).lower() == "true"
                or "disabled" in normalize_text(" ".join(button.get("class", [])))
            )
            if disabled:
                continue
            text = clean_text(
                button.get("value")
                if getattr(button, "name", "") == "input"
                else button.get_text(" ", strip=True)
            )
            text_norm = normalize_text(text)
            if any(_contains_phrase(text_norm, phrase) for phrase in NOTIFY_NEGATIVE_PHRASES + WISHLIST_ONLY_PHRASES):
                continue
            if any(_contains_phrase(text_norm, phrase) for phrase in POSITIVE_CTA_PHRASES):
                return True
    return False


def detect_dreamland_availability(
    *,
    text: str,
    html: str = "",
    cta_texts: tuple[str, ...] = (),
    has_enabled_cart_form: bool = False,
) -> DreamlandAvailability:
    raw_text = clean_text(text)
    text_norm = normalize_text(raw_text)

    normalized_ctas = tuple(clean_text(value) for value in cta_texts if clean_text(value))
    cta_norm = normalize_text(" ".join(normalized_ctas))

    stock_negatives = _signal_matches(text_norm, STOCK_NEGATIVE_PHRASES, prefix="text")
    notify_negatives = _signal_matches(text_norm, NOTIFY_NEGATIVE_PHRASES, prefix="text")
    wishlist_signals = _signal_matches(text_norm, WISHLIST_ONLY_PHRASES, prefix="text")

    positive_signals = _signal_matches(text_norm, POSITIVE_TEXT_PHRASES, prefix="text")
    positive_signals.extend(_signal_matches(cta_norm, POSITIVE_CTA_PHRASES, prefix="cta"))
    if has_enabled_cart_form and any(signal.startswith("cta:") for signal in positive_signals):
        positive_signals.append("dom:enabled_cart_form")

    strong_negatives = stock_negatives + notify_negatives
    if strong_negatives:
        return DreamlandAvailability(
            purchasable=False,
            status="unavailable",
            reason=strong_negatives[0],
            raw_text=raw_text,
            negative_signals=_dedupe(strong_negatives + wishlist_signals),
            positive_signals=_dedupe(positive_signals),
            cta_texts=normalized_ctas,
        )

    if positive_signals:
        return DreamlandAvailability(
            purchasable=True,
            status="purchasable",
            reason=positive_signals[0],
            raw_text=raw_text,
            negative_signals=_dedupe(wishlist_signals),
            positive_signals=_dedupe(positive_signals),
            cta_texts=normalized_ctas,
        )

    if wishlist_signals:
        return DreamlandAvailability(
            purchasable=False,
            status="unavailable",
            reason=wishlist_signals[0],
            raw_text=raw_text,
            negative_signals=_dedupe(wishlist_signals),
            positive_signals=(),
            cta_texts=normalized_ctas,
        )

    return DreamlandAvailability(
        purchasable=False,
        status="unknown",
        reason="unknown_no_positive_purchase_signal",
        raw_text=raw_text,
        negative_signals=(),
        positive_signals=(),
        cta_texts=normalized_ctas,
    )


def detect_dreamland_availability_from_soup(soup: Any) -> DreamlandAvailability:
    text = clean_text(soup.get_text(" ", strip=True))
    html = str(soup)
    return detect_dreamland_availability(
        text=text,
        html=html,
        cta_texts=_button_texts_from_soup(soup),
        has_enabled_cart_form=_has_enabled_cart_form(soup),
    )


def detect_dreamland_availability_from_driver(driver: Any) -> DreamlandAvailability:
    try:
        text = str(
            driver.execute_script(
                "return (document.body && document.body.innerText) ? document.body.innerText : '';"
            )
            or ""
        )
    except Exception:
        try:
            text = str(driver.page_source or "")
        except Exception:
            text = ""

    try:
        html = str(driver.page_source or "")
    except Exception:
        html = ""

    cta_texts: tuple[str, ...] = ()
    has_enabled_cart_form = False
    try:
        data = driver.execute_script(
            """
            const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && rect.width > 0
                    && rect.height > 0;
            };
            const controls = [...document.querySelectorAll('button,a,input[type="submit"],input[type="button"]')]
                .filter((el) => isVisible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
                .map((el) => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim())
                .filter(Boolean);
            const forms = [...document.querySelectorAll('form[action*="/cart/add"], form[data-component="cart/add-to-cart-action-form"]')];
            const hasCartForm = forms.some((form) =>
                [...form.querySelectorAll('button,input[type="submit"],input[type="button"]')]
                    .some((el) => isVisible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
            );
            return { controls, hasCartForm };
            """
        )
        if isinstance(data, dict):
            cta_texts = tuple(clean_text(value) for value in data.get("controls", []) if clean_text(value))
            has_enabled_cart_form = bool(data.get("hasCartForm"))
    except Exception:
        pass

    return detect_dreamland_availability(
        text=text,
        html=html,
        cta_texts=cta_texts,
        has_enabled_cart_form=has_enabled_cart_form,
    )
