from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ArctosQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    taxon: list[str] | None = None
    """Taxon rows, `rank|name`, `rank|name|relationships`, or that followed by
    `rank|name` pairs for Related taxa. Rows OR together. A literal `|` is
    written `\\|`."""
    attr: list[str] | None = None
    """Attribute rows, `[!]type|value|value…`. A leading `!` negates the row.
    A type with no value asks only whether the record carries it. `null` asks
    for records with no attributes at all, `!null` for those with any."""
    part: list[str] | None = None
    """This field holds part rows, `[!]field|value|value…`, the same grammar as
    `attr`. The field names a part field rather than an attribute type,
    because a part carries its values in named fields, not in type/value
    pairs. `null` and `!null` ask for records with no parts or with any. Rows
    always AND; `attr_op` does not reach them."""
    prefix: list[str] | None = None
    """Collection prefixes, `MSB:Mamm`. Values OR together."""
    country: list[str] | None = None
    """Countries, exact. Values OR together."""
    state: list[str] | None = None
    """States or provinces, exact. Values OR together."""
    from_: date | None = Field(None, alias="from")
    """Earliest event date, `YYYY-MM-DD`. Both ends must hold for one event."""
    to: date | None = None
    """Latest event date, `YYYY-MM-DD`."""
    attr_op: Literal["and", "or"] = "and"
    """How the `attr` rows combine."""
    method: str | None = None
    """Text the determination method of a matching attribute must contain. It
    qualifies the `attr` rows that are not negated, and is never a search on
    its own row."""
    locality: str | None = None
    """A phrase matched against the record's own locality text."""
    collector: str | None = None
    """An exact collector name."""
    tab: str | None = None
    """This field names the table the user is looking at. Absent means the
    Specimens themselves. A value is a relationship, and selects the related
    records reached by it — `/api/<engine>/relations` lists them, and a download
    exports them instead of the Specimens."""
    page: int | None = Field(None, ge=1)
    cols: str | None = None
    """Comma-separated list of columns to return, in order. Absent means the
    default display columns."""
