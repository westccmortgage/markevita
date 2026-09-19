"""Оценка стоимости, резервирование и бюджетный стоп (spec §6). Цены из pricing.json, override через PRICE_<KEY>."""
import json
import os
from pathlib import Path


def load_prices() -> dict:
    p = json.loads((Path(__file__).parent / "pricing.json").read_text())
    for k in list(p):
        v = os.getenv(f"PRICE_{k.upper()}")
        if v:
            p[k] = float(v)
    return p


PRICE = load_prices()


# Published per-million-token rates, read 2026-09-16. A model with no entry is
# refused rather than priced at another model's tariff: mispricing a run is
# worse than not starting it, because the budget stop then guards nothing.
ANTHROPIC_RATES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}


class UnknownLanguageModel(ValueError):
    pass


def anthropic_rates(model: str) -> tuple[float, float]:
    try:
        return ANTHROPIC_RATES[model]
    except KeyError:
        raise UnknownLanguageModel(
            f"No published price for {model!r}. Known: "
            f"{', '.join(sorted(ANTHROPIC_RATES))}.") from None


class BudgetExceeded(RuntimeError):
    pass


# Каждая модель видео тарифицируется отдельно. Ключ обязателен: обычная Veo 3.1
# стоит вдвое дороже Fast, и молчаливый откат на тариф Fast означал бы запуск,
# который вдвое превышает согласованный бюджет.
VIDEO_PRICE_FAMILY = {
    "fal-ai/veo3.1/fast/image-to-video": "veo31_fast",
    "fal-ai/veo3.1/image-to-video": "veo31",
}


class UnknownVideoModel(ValueError):
    pass


def video_cost(seconds: int, audio: bool, resolution: str, endpoint: str) -> float:
    family = VIDEO_PRICE_FAMILY.get(endpoint)
    if not family:
        raise UnknownVideoModel(
            f"No published price for video model {endpoint!r}. "
            f"Known: {', '.join(sorted(VIDEO_PRICE_FAMILY))}.")
    k4 = "_4k" if resolution == "4k" else ""
    return seconds * PRICE[f"{family}_per_sec{k4}_{'audio' if audio else 'silent'}"]


def image_cost(pro: bool, resolution: str) -> float:
    r = resolution.lower()
    if pro:
        return PRICE["nano_banana_pro_image_2k"] if r == "2k" else PRICE["nano_banana_pro_image_1k"]
    return PRICE.get(f"nano_banana_2_image_{r}", PRICE["nano_banana_2_image_1k"])


def music_cost(seconds: float) -> float:
    """Billed by the minute of output, rounded up the way the provider bills."""
    import math as _math
    return _math.ceil(max(1.0, seconds) / 60.0) * PRICE["cassette_music_per_min"]


def lipsync_cost(seconds: float, variant: str) -> float:
    return seconds * (PRICE["sync_lipsync_2_pro_per_sec"] if variant == "lipsync-2-pro" else PRICE["sync_lipsync_2_per_sec"])


class Budget:
    """reserve() ПЕРЕД платным вызовом, settle() после. Неудачный, но оплаченный запрос тоже списывается."""

    def __init__(self, cap_usd: float, state):
        self.cap = cap_usd
        self.state = state

    @property
    def spent(self) -> float:
        return float(self.state.data.get("spent_usd", 0.0))

    @property
    def reserved(self) -> float:
        return float(self.state.data.get("reserved_usd", 0.0))

    def reserve(self, amount: float, what: str):
        if self.spent + self.reserved + amount > self.cap:
            self.state.data["status"] = "needs_budget_override"
            self.state.save()
            raise BudgetExceeded(
                f"budget: spent ${self.spent:.2f} + reserved ${self.reserved:.2f} + next '{what}' ${amount:.2f} > cap ${self.cap:.2f}. "
                f"Status set to needs_budget_override. Raise MAX_EPISODE_BUDGET_USD explicitly (logged) to continue."
            )
        self.state.data["reserved_usd"] = round(self.reserved + amount, 4)
        self.state.save()

    def settle(self, reserved: float, actual: float, what: str, take_id: str | None = None):
        self.state.data["reserved_usd"] = round(max(0.0, self.reserved - reserved), 4)
        self.state.data["spent_usd"] = round(self.spent + actual, 4)
        self.state.data.setdefault("cost_log", []).append({"what": what, "take_id": take_id, "estimated": round(reserved, 4), "actual": round(actual, 4)})
        self.state.save()
