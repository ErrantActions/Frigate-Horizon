"""Explicit consent to release permanent evidence protection."""

from typing import Literal

from pydantic import BaseModel


class UnprotectMediaBody(BaseModel):
    confirm: Literal[True]
    acknowledge_retention: Literal[True]
