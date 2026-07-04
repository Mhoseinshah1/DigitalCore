"""FSM states for the admin settings editor."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SettingsForm(StatesGroup):
    entering_value = State()
