from __future__ import annotations

from urllib.parse import urlsplit

RETAILER_HOSTS = {
    "bol": {"bol.com", "www.bol.com"},
    "mediamarkt": {"mediamarkt.nl", "www.mediamarkt.nl"},
    "dreamland": {"dreamland.nl", "www.dreamland.nl"},
    "pocketgames": {"pocketgames.nl", "www.pocketgames.nl"},
}


def validate_retailer_url(site: str, url: str) -> str:
    normalized_site = str(site or "").strip().lower()
    allowed_hosts = RETAILER_HOSTS.get(normalized_site)
    if not allowed_hosts:
        raise ValueError(f"Unsupported retailer site: {site!r}")
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid retailer URL.") from exc
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() != "https":
        raise ValueError("Retailer URL must use HTTPS.")
    if parsed.username or parsed.password:
        raise ValueError("Retailer URL must not contain credentials.")
    if port not in {None, 443}:
        raise ValueError("Retailer URL must use the standard HTTPS port.")
    if hostname not in allowed_hosts:
        raise ValueError(f"URL host is not valid for {normalized_site}.")
    return parsed.geturl()
