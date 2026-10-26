"""The compat service's own Redis state, parsed once where it's read (docs/typing.md)."""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict


class CompatRequest(BaseModel):
    """A legacy request's ``compat:<request_id>`` hash: the engine it was opened for,
    and the filenames it will upload (stored as a JSON list)."""

    model_config = ConfigDict(frozen=True)

    engine: int
    filenames: list[str]

    @classmethod
    def from_hash(cls, raw: Mapping[Any, Any]) -> "CompatRequest":
        fields = {_text(k): _text(v) for k, v in raw.items()}
        return cls(engine=int(fields["engine"]), filenames=json.loads(fields["filenames"]))

    def to_hash(self) -> dict[str, str]:
        return {"engine": str(self.engine), "filenames": json.dumps(self.filenames)}


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value
