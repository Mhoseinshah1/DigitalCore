"""FSM states for the admin 3X-UI server editor."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ServerAddForm(StatesGroup):
    picking_version = State()
    entering_name = State()
    entering_base_url = State()
    entering_path = State()
    entering_username = State()
    entering_password = State()
