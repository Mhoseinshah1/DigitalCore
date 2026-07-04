"""FSM states for the admin product editor."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ProductAddForm(StatesGroup):
    entering_title = State()
    entering_price = State()
    entering_duration = State()
    entering_traffic = State()


class ProductEditForm(StatesGroup):
    entering_value = State()
