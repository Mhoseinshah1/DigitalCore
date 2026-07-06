"""Human-facing (bilingual) labels for order/payment statuses.

The canonical status *values* live on the models; this module only maps them to
display strings via the i18n catalog so the bot and the web panel render them
identically. Unknown values fall back to the raw value.
"""
from __future__ import annotations

from app.i18n import t


def order_status_label(status: str, lang: str = "fa") -> str:
    label = t(f"order.status.{status}", lang)
    return status if label == f"order.status.{status}" else label


def payment_status_label(status: str, lang: str = "fa") -> str:
    label = t(f"payment.status.{status}", lang)
    return status if label == f"payment.status.{status}" else label


def wallet_tx_type_label(tx_type: str, lang: str = "fa") -> str:
    label = t(f"wallet.tx.{tx_type}", lang)
    return tx_type if label == f"wallet.tx.{tx_type}" else label


def topup_status_label(status: str, lang: str = "fa") -> str:
    label = t(f"topup.status.{status}", lang)
    return status if label == f"topup.status.{status}" else label
