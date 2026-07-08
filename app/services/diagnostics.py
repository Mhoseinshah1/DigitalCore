"""Runtime bot diagnostics — used by ``scripts/debug_bot_state.py`` and the
admin ``/debug_bot_state`` command.

Its whole point is to survive a *stale schema*: if migration 0019 has not been
applied, ``products.category_id`` / the ``product_categories`` table are missing,
so a normal ``select(Product)`` (which lists every mapped column) would raise
``UndefinedColumn`` — exactly the failure that silently breaks the bot in
production. Every query here is guarded and count-only so the report always
renders and points at the real cause.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.product import Product
from app.models.product_category import ProductCategory


def _schema_probe(sync_conn) -> dict[str, bool]:
    insp = inspect(sync_conn)
    has_products = insp.has_table("products")
    product_cols = (
        {c["name"] for c in insp.get_columns("products")} if has_products else set()
    )
    return {
        "product_categories_table": insp.has_table("product_categories"),
        "products_category_id_column": "category_id" in product_cols,
    }


async def bot_state_report(session: AsyncSession) -> dict[str, Any]:
    report: dict[str, Any] = {}

    conn = await session.connection()
    schema = await conn.run_sync(_schema_probe)
    report["schema"] = schema
    report["migration_0019_applied"] = (
        schema["product_categories_table"] and schema["products_category_id_column"]
    )

    # Count-only queries: safe even when category_id is missing (they never select it).
    try:
        report["category_count"] = int(
            await session.scalar(select(func.count()).select_from(ProductCategory)) or 0
        )
        report["active_category_count"] = int(
            await session.scalar(
                select(func.count()).select_from(ProductCategory)
                .where(ProductCategory.is_active.is_(True))
            ) or 0
        )
    except Exception as exc:  # noqa: BLE001 - table may not exist yet
        report["category_count_error"] = f"{type(exc).__name__}: {exc}".splitlines()[0][:120]

    try:
        report["product_count"] = int(
            await session.scalar(select(func.count()).select_from(Product)) or 0
        )
        report["browsable_product_count"] = int(
            await session.scalar(
                select(func.count()).select_from(Product).where(
                    Product.is_active.is_(True),
                    Product.is_hidden.is_(False),
                    Product.applies_to_service.is_(False),
                )
            ) or 0
        )
        if report["migration_0019_applied"]:
            report["uncategorised_product_count"] = int(
                await session.scalar(
                    select(func.count()).select_from(Product)
                    .where(Product.category_id.is_(None))
                ) or 0
            )
    except Exception as exc:  # noqa: BLE001
        report["product_count_error"] = f"{type(exc).__name__}: {exc}".splitlines()[0][:120]

    svc = SettingsService(session)
    report["settings"] = {
        "license_section_title": await svc.get_str("license_section_title", ""),
        "online_gateway_enabled": await svc.get_bool("online_gateway_enabled", False),
        "wallet_enabled": await svc.get_bool("wallet_enabled", True),
        "wallet_payment_enabled": await svc.get_bool("wallet_payment_enabled", True),
        "card_to_card_enabled": await svc.get_bool("card_to_card_enabled", True),
    }
    return report


def format_report(report: dict[str, Any]) -> str:
    lines = ["DigitalCore — bot state diagnostic", ""]
    applied = report.get("migration_0019_applied")
    lines.append(f"migration 0019 (product categories) applied: {applied}")
    schema = report.get("schema", {})
    lines.append(f"  product_categories table:   {schema.get('product_categories_table')}")
    lines.append(f"  products.category_id column: {schema.get('products_category_id_column')}")
    if not applied:
        lines += [
            "",
            ">>> FIX: the schema is stale — run:",
            ">>>   docker compose exec -T backend alembic upgrade head",
            ">>> Without it every product/account query raises 'column does not exist'",
            ">>> and the bot's products / account / order flows fail silently.",
        ]
    lines.append("")
    lines.append(f"categories: total={report.get('category_count', report.get('category_count_error'))}"
                 f" active={report.get('active_category_count', '?')}")
    lines.append(f"products: total={report.get('product_count', report.get('product_count_error'))}"
                 f" browsable={report.get('browsable_product_count', '?')}"
                 f" uncategorised={report.get('uncategorised_product_count', '?')}")
    if report.get("category_count", 0) == 0 and applied:
        lines += [
            "",
            "NOTE: no categories exist yet — the bot shows the flat product list "
            "(the «سایر محصولات» fallback). Create categories in the admin panel "
            "(/admin/product-categories) and assign products to see the category picker.",
        ]
    lines.append("")
    lines.append("settings:")
    for k, v in report.get("settings", {}).items():
        lines.append(f"  {k}: {v!r}")
    return "\n".join(lines)
