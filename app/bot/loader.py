"""Bot/Dispatcher factory: middlewares + routers wired in one place."""
from __future__ import annotations

from aiogram import Bot, Dispatcher

from app.bot.handlers.admin import menu as admin_menu
from app.bot.handlers.admin import panel as admin_panel
from app.bot.handlers.admin import products as admin_products
from app.bot.handlers.admin import settings as admin_settings
from app.bot.handlers.admin import xui as admin_xui
from app.bot.handlers.user import language as user_language
from app.bot.handlers.user import orders as user_orders
from app.bot.handlers.user import products as user_products
from app.bot.handlers.user import rules as user_rules
from app.bot.handlers.user import start as user_start
from app.bot.middlewares.activity import ActivityMiddleware
from app.bot.middlewares.admin import AdminMiddleware
from app.bot.middlewares.blocked import BlockedMiddleware
from app.bot.middlewares.forcejoin import ForceJoinMiddleware
from app.bot.middlewares.language import LanguageMiddleware
from app.bot.middlewares.maintenance import MaintenanceMiddleware


def create_bot(token: str) -> Bot:
    return Bot(token=token)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    # Execution order per event: activity -> admin (role) -> language (lang + _)
    # -> blocked (block non-admin blocked users) -> maintenance -> forcejoin.
    for observer in (dp.message, dp.callback_query):
        observer.middleware(ActivityMiddleware())
        observer.middleware(AdminMiddleware())
        observer.middleware(LanguageMiddleware())
        observer.middleware(BlockedMiddleware())
        observer.middleware(MaintenanceMiddleware())
        observer.middleware(ForceJoinMiddleware())

    dp.include_router(admin_menu.router)
    dp.include_router(admin_panel.router)
    dp.include_router(admin_settings.router)
    dp.include_router(admin_products.router)
    dp.include_router(admin_xui.router)
    dp.include_router(user_language.router)
    dp.include_router(user_start.router)
    dp.include_router(user_rules.router)
    dp.include_router(user_products.router)
    dp.include_router(user_orders.router)
    return dp
