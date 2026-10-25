"""TuzkaM's own HTTP responses (private to its adapter)."""

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator

from app.engines.schemas import JobStatusBody, Metric


def _scalar_or_none(value: Any) -> Any:
    if isinstance(value, int | float | str) and not isinstance(value, bool):
        return value
    return None


def _object_or_none(value: Any) -> Any:
    return value if isinstance(value, dict) else None


# A georef summary value: a number, or its status word.
_GeorefValue = Annotated[int | float | str | None, BeforeValidator(_scalar_or_none)]


class GeorefSummary(BaseModel):
    """The georeferencing outcome; ``residual_m`` is the RMS in meters."""

    status: _GeorefValue = None
    control_points: _GeorefValue = None
    inliers: _GeorefValue = None
    residual_m: _GeorefValue = None


class TuzkaMStatus(JobStatusBody):
    n_words: Metric = None
    mean_conf: Metric = None
    progress: Metric = None
    eta_s: Metric = None
    georef: Annotated[GeorefSummary | None, BeforeValidator(_object_or_none)] = None

    def metrics(self) -> dict[str, int | float | str]:
        """Its numbers, and the georef summary flattened to dotted keys (D7/D8)."""
        own = self.model_dump(include={"n_words", "mean_conf", "progress", "eta_s"})
        georef = self.georef.model_dump() if self.georef else {}
        return {key: value for key, value in own.items() if value is not None} | {
            f"georef.{key}": value for key, value in georef.items() if value is not None
        }
