"""The Notifier interface. Swap the channel without touching the engine."""
from __future__ import annotations

from abc import ABC, abstractmethod

from reminders.models import Reminder, SendResult


class Notifier(ABC):
    channel: str = "abstract"

    @abstractmethod
    def send(self, reminder: Reminder) -> SendResult:
        """Deliver a fully-rendered reminder and report the outcome."""
        raise NotImplementedError
