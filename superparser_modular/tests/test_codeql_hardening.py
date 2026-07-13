from types import SimpleNamespace

import pokemon_parser.api.services.filters_manager as filters_manager_module
import pokemon_parser.api.services.watchlist_manager as watchlist_manager_module
from pokemon_parser.api.services.filters_manager import FiltersManager
from pokemon_parser.api.services.watchlist_manager import WatchlistManager
from pokemon_parser.parsers.mediamarkt import MediaMarktParser
from pokemon_parser.workers.bol_worker import BolWorkerCase
from pokemon_parser.workers.mediamarkt_worker import MediaMarktWorkerCase


def test_bol_login_detection_requires_an_allowed_https_host():
    assert BolWorkerCase._is_login_page(SimpleNamespace(current_url="https://login.bol.com/login")) is True
    assert BolWorkerCase._is_login_page(SimpleNamespace(current_url="https://www.bol.com/wsp/login")) is True
    assert BolWorkerCase._is_login_page(SimpleNamespace(current_url="https://login.bol.com.evil.test/login")) is False
    assert BolWorkerCase._is_login_page(SimpleNamespace(current_url="https://evil.test/wsp/login")) is False
    assert BolWorkerCase._is_login_page(SimpleNamespace(current_url="http://login.bol.com/login")) is False


def test_mediamarkt_payment_detection_requires_the_computop_https_host():
    assert (
        MediaMarktWorkerCase._is_external_payment_page(
            SimpleNamespace(current_url="https://www.computop-paygate.com/payssl.aspx")
        )
        is True
    )
    assert (
        MediaMarktWorkerCase._is_external_payment_page(
            SimpleNamespace(current_url="https://computop-paygate.com.evil.test/payssl.aspx")
        )
        is False
    )
    assert (
        MediaMarktWorkerCase._is_external_payment_page(
            SimpleNamespace(current_url="https://evil.test/payssl.aspx")
        )
        is False
    )
    assert (
        MediaMarktWorkerCase._is_external_payment_page(
            SimpleNamespace(current_url="http://www.computop-paygate.com/payssl.aspx")
        )
        is False
    )


def test_mediamarkt_html_to_text_removes_malformed_hidden_tags():
    text = MediaMarktParser()._html_to_text(
        "<script>private-script-text</script ><style>private-style-text</style >Visible <b>Pokemon</b>"
    )

    assert text == "Visible Pokemon"


def test_legacy_filter_metadata_does_not_expose_exception_or_absolute_path(monkeypatch):
    path = SimpleNamespace(name="filters.json", exists=lambda: True)

    def fail_to_load(_path):
        raise RuntimeError("C:\\private\\operator-name\\filters.json")

    monkeypatch.setattr(filters_manager_module, "load_filters_from_json", fail_to_load)
    metadata = FiltersManager(config_manager=None)._legacy_json_metadata(SimpleNamespace(filters_json_path=path))

    assert metadata["path"] == "filters.json"
    assert metadata["error"] == "Legacy filters.json could not be read."
    assert "operator-name" not in str(metadata)


def test_watchlist_validation_does_not_expose_exception_text(monkeypatch):
    class Connection:
        def close(self):
            return None

    manager = WatchlistManager(config_manager=object())
    monkeypatch.setattr(manager, "_storage", lambda: (Connection(), object()))

    def reject_url(_site, _url):
        raise ValueError("C:\\private\\operator-name\\browser-profile")

    monkeypatch.setattr(watchlist_manager_module, "validate_retailer_url", reject_url)
    result = manager.add_manual(
        {"site": "bol", "product_key": "synthetic-product", "url": "https://www.bol.com/test"}
    )

    assert result == {"ok": False, "message": "The retailer URL is invalid for the selected site."}
