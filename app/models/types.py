"""Column types that map Python types onto existing columns without changing their DDL."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    """A ``StrEnum`` stored as its value in a text column.

    ``sqlalchemy.Enum`` would emit VARCHAR(n) and its own CHECK; the columns already are
    text with a CHECK listing the values (built with ``check_values``)."""

    impl = Text
    cache_ok = True

    def __init__(self, enum: type[E]) -> None:
        super().__init__()
        self.enum = enum

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else self.enum(value).value

    def process_result_value(self, value: Any, dialect: Dialect) -> E | None:
        return None if value is None else self.enum(value)


def check_values(column: str, enum: type[StrEnum]) -> str:
    """``<column> IN ('a', 'b', …)`` for a CHECK constraint, in the enum's order."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class PydanticJSONBList[M: BaseModel](TypeDecorator[list[M]]):
    """A JSONB array of pydantic models: dumped as JSON on write, validated on read."""

    impl = JSONB
    cache_ok = True

    def __init__(self, model: type[M]) -> None:
        super().__init__()
        self.model = model
        self._adapter = TypeAdapter(list[model])  # type: ignore[valid-type]

    def process_bind_param(self, value: Any, dialect: Dialect) -> list[Any] | None:
        return None if value is None else self._adapter.dump_python(value, mode="json")

    def process_result_value(self, value: Any, dialect: Dialect) -> list[M] | None:
        return None if value is None else self._adapter.validate_python(value)
