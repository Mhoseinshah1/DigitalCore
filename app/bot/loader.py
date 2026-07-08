"""Bot/Dispatcher factory: middlewares + routers wired in one place."""
from __future__ import annotations

from aiogram import Bot, Dispatcher

from app.bot.handlers.admin import menu as admin_menu
from app.bot.handlers.admin import panel as admin_panel
from app.bot.handlers.admin import products as admin_products
from app.bot.handlers.admin import receipt_actions as admin_receipt_actions
from app.bot.handlers.admin import settings as admin_settings
from app.bot.handlers.admin import tickets as admin_tickets
from app.bot.handlers.admin import wallet as admin_wallet
from app.bot.handlers.admin import xui as admin_xui
from app.bot.handlers.user import account as user_account
from app.bot.handlers.user import language as user_language
from app.bot.handlers.user import orders as user_orders
from app.bot.handlers.user import products as user_products
from app.bot.handlers.user import referral as user_referral
from app.bot.handlers.user import rules as user_rules
from app.bot.handlers.user import services as user_services
from app.bot.handlers.user import start as user_start
from app.bot.handlers.user import tickets as user_tickets
from app.bot.handlers.user import tutorials as user_tutorials
from app.bot.handlers.user import wallet as user_wallet
from app.bot.middlewares.activity import ActivityMiddleware
from app.bot.middlewares.admin import AdminMiddleware
from app.bot.middlewares.blocked import BlockedMiddleware
from app.bot.middlewares.forcejoin import ForceJoinMiddleware
from app.bot.middlewares.language import LanguageMiddleware
from app.bot.middlewares.maintenance import MaintenanceMiddleware
from app.bot.middlewares.restricted import RestrictedMiddleware


def create_bot(token: str) -> Bot:
    return Bot(token=token)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    # Execution order per event: activity -> admin (role) -> language (lang + _)
    # -> blocked -> restricted (gate purchase actions) -> maintenance -> forcejoin.
    for observer in (dp.message, dp.callback_query):
        observer.middleware(ActivityMiddleware())
        observer.middleware(AdminMiddleware())
        observer.middleware(LanguageMiddleware())
        observer.middleware(BlockedMiddleware())
        observer.middleware(RestrictedMiddleware())
        observer.middleware(MaintenanceMiddleware())
        observer.middleware(ForceJoinMiddleware())

    dp.include_router(admin_menu.router)
    dp.include_router(admin_panel.router)
    dp.include_router(admin_settings.router)
    dp.include_router(admin_products.router)
    dp.include_router(admin_xui.router)
    dp.include_router(admin_receipt_actions.router)
    dp.include_router(admin_wallet.router)
    dp.include_router(admin_tickets.router)
    dp.include_router(user_language.router)
    dp.include_router(user_start.router)
    dp.include_router(user_rules.router)
    dp.include_router(user_account.router)
    dp.include_router(user_products.router)
    # Wallet + tickets before orders: their state-filtered photo/document handlers
    # must win over the orders stateless receipt handler.
    dp.include_router(user_wallet.router)
    dp.include_router(user_tickets.router)
    dp.include_router(user_orders.router)
    dp.include_router(user_services.router)
    dp.include_router(user_tutorials.router)
    dp.include_router(user_referral.router)
    return dp
