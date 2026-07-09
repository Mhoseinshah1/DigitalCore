"""FSM states for the admin product editor."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ProductAddForm(StatesGroup):
    entering_title = State()
    entering_price = State()
    entering_duration = State()
    entering_traffic = State()
    # V2Ray products bind to an XUI server + inbound and may carry an optional
    # device limit / description before they are created.
    choosing_server = State()
    choosing_inbound = State()
    entering_ip_limit = State()
    entering_description = State()


class ProductEditForm(StatesGroup):
    entering_value = State()
