"""Provider fallback chain with a per-provider circuit breaker."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from kestrel.datasources.base import DataSourceError, Quote, QuoteProvider

log = logging.getLogger(__name__)


@dataclass
class Breaker:
    """Simple consecutive-failure circuit breaker."""

    threshold: int = 3
    cooldown: float = 300.0
    failures: int = 0
    opened_at: float = 0.0

    @property
    def is_open(self) -> bool:
        if self.failures < self.threshold:
            return False
        if time.monotonic() - self.opened_at > self.cooldown:
            self.failures = 0
            return False
        return True

    def record_ok(self) -> None:
        self.failures = 0

    def record_fail(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


@dataclass
class QuoteChain:
    """Try providers in priority order, filling gaps from the next one."""

    providers: list[QuoteProvider] = field(default_factory=list)
    breakers: dict[str, Breaker] = field(default_factory=dict)

    def _breaker(self, name: str) -> Breaker:
        return self.breakers.setdefault(name, Breaker())

    async def fetch(self, symbols: list[tuple[str, str]]) -> dict[str, Quote]:
        remaining = list(dict.fromkeys(symbols))
        collected: dict[str, Quote] = {}
        errors: list[str] = []

        for provider in self.providers:
            if not remaining:
                break
            breaker = self._breaker(provider.name)
            if breaker.is_open:
                log.warning("provider %s circuit open, skipping", provider.name)
                continue
            try:
                batch = await provider.fetch(remaining)
            except (DataSourceError, Exception) as exc:  # noqa: BLE001
                breaker.record_fail()
                errors.append(f"{provider.name}: {exc}")
                log.warning("provider %s failed: %s", provider.name, exc)
                continue

            breaker.record_ok()
            for key, quote in batch.items():
                collected.setdefault(key, quote)
            remaining = [s for s in remaining if f"{s[0]}:{s[1]}" not in collected]

        if errors and not collected:
            raise DataSourceError("; ".join(errors))
        return collected

    @property
    def health(self) -> dict[str, dict]:
        return {
            name: {"open": self._breaker(name).is_open, "failures": self._breaker(name).failures}
            for name in [p.name for p in self.providers]
        }


def default_chain() -> QuoteChain:
    from kestrel.datasources.eastmoney import EastmoneyProvider
    from kestrel.datasources.sina import SinaProvider
    from kestrel.datasources.tencent import TencentProvider

    return QuoteChain(
        providers=[EastmoneyProvider(), TencentProvider(), SinaProvider()]
    )
