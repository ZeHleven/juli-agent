"""Canonical extracted text and positions, independent of parser libraries."""
from pydantic import Field, model_validator

from .common import Contract, Reference

MAX_TEXT_CHARS = 250000


class TextSection(Contract):
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    page: int | None = Field(default=None, ge=1)
    paragraph: int | None = Field(default=None, ge=1)


class ParsedDocument(Contract):
    parser_version: Reference
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    sections: tuple[TextSection, ...] = Field(min_length=1, max_length=10000)
    warnings: tuple[str, ...] = Field(default=(), max_length=200)

    @model_validator(mode="after")
    def exact_partition(self):
        offset = 0
        for section in self.sections:
            if section.start != offset or not section.start < section.end <= len(self.text):
                raise ValueError("sections must exactly partition extracted text")
            offset = section.end
        if offset != len(self.text) or not self.text.strip() or '\x00' in self.text:
            raise ValueError("nonempty text and complete positions are required")
        self.text.encode('utf-8')
        return self
