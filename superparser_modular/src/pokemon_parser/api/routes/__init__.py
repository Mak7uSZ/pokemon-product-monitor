from pokemon_parser.api.routes.credentials import router as credentials_router
from pokemon_parser.api.routes.filters import router as filters_router
from pokemon_parser.api.routes.health import router as health_router
from pokemon_parser.api.routes.logs import router as logs_router
from pokemon_parser.api.routes.parsers import router as parsers_router
from pokemon_parser.api.routes.proxy import router as proxy_router
from pokemon_parser.api.routes.products import router as products_router
from pokemon_parser.api.routes.runtime import router as runtime_router
from pokemon_parser.api.routes.scan_settings import router as scan_settings_router
from pokemon_parser.api.routes.settings_backup import router as settings_backup_router
from pokemon_parser.api.routes.settings import router as settings_router
from pokemon_parser.api.routes.status import router as status_router
from pokemon_parser.api.routes.system import router as system_router
from pokemon_parser.api.routes.telegram import router as telegram_router
from pokemon_parser.api.routes.watchlist import router as watchlist_router

__all__ = [
    "credentials_router",
    "filters_router",
    "health_router",
    "logs_router",
    "parsers_router",
    "proxy_router",
    "products_router",
    "runtime_router",
    "scan_settings_router",
    "settings_backup_router",
    "settings_router",
    "status_router",
    "system_router",
    "telegram_router",
    "watchlist_router",
]
