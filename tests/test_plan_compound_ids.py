from pitwall.engine import build_engine
from pitwall.model.deg import DegFit
from pitwall.state.session import Snapshot, TyreSet
from pitwall.strategy.plans import _is_softer_next


def test_is_softer_next_on_visual_ids() -> None:
    assert _is_softer_next((17, 16)) is True
    assert _is_softer_next((18, 17)) is True
    assert _is_softer_next((16, 17)) is False
    assert _is_softer_next((16, 16)) is False
    assert _is_softer_next((16,)) is False
    assert _is_softer_next((7, 16)) is False


def test_compound_models_use_visual_ids_when_actual_ids_overlap() -> None:
    engine = build_engine(sinks=[])
    snapshot = Snapshot(
        now=0.0,
        tyre_visual=16,
        tyre_sets=(
            TyreSet(actual=22, visual=16, available=1),
            TyreSet(actual=16, visual=17, available=1),
            TyreSet(actual=17, visual=18, available=1),
        ),
    )
    fit = DegFit(90_000.0, 100.0, 30.0, 10, 100.0, 0.9, "fit")

    models = engine._compound_models(snapshot, fit)

    assert tuple(model.visual for model in models) == (16, 17, 18)
