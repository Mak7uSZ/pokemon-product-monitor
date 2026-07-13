from __future__ import annotations

from abc import ABC, abstractmethod

from pokemon_parser.config import AppConfig
from pokemon_parser.models import ActionTarget
from pokemon_parser.workers.trace import WorkerTraceLogger


class BaseWorkerCase(ABC):
    @staticmethod
    @abstractmethod
    def add_to_cart(
        driver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def checkout(
        driver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def add_to_cart_and_checkout(
        driver,
        target: ActionTarget,
        cfg: AppConfig,
        trace: WorkerTraceLogger | None = None,
    ) -> None:
        BaseWorkerCase.add_to_cart(driver, target, cfg, trace)
        BaseWorkerCase.checkout(driver, target, cfg, trace)
