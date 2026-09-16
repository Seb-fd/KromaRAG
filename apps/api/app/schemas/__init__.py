"""Shared Pydantic schemas (specs/02 §2, specs/06 §2.3)."""
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field, field_validator


class Principal(BaseModel):
    sub: str
    email: EmailStr | None = None
    roles: list[str] = []


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=6, ge=1, le=20)
    stream: bool = True
    conv_id: UUID | None = None


class ChunkResult(BaseModel):
    chunk_id: UUID
    doc_id: UUID
    title: str
    content: str
    score: float
    ord: int


class IngestRequest(BaseModel):
    title: str
    content: str | None = None
    url: str | None = None
    mime: str = "text/plain"
    allowed_roles: list[str] = ["viewer"]


class DocMeta(BaseModel):
    id: UUID
    title: str
    mime: str
    status: str
    chunks: int = 0
    created_at: datetime | None = None
    version: int = 1  # NEW additive field (specs/07 §2.4); old clients ignore


# ── Conversations & history (specs/06 §2.3) ──

class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class ConversationMeta(BaseModel):
    id: UUID
    title: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int = 0


class ConversationList(BaseModel):
    items: list[ConversationMeta]
    total: int


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def _strip_nonblank(cls, v: str) -> str:
        t = v.strip()
        if not t:
            raise ValueError("title must not be blank")
        return t


class Message(BaseModel):
    id: UUID
    role: Literal["user", "assistant", "system"]
    content: str
    trace_id: str | None = None
    created_at: datetime | None = None


class MessageList(BaseModel):
    items: list[Message]  # ascending by (created_at, id)
    next_before_id: UUID | None = None


# ── Document lifecycle (specs/07 §2.4) ──

from typing import Any

DOC_STATUSES = ("pending", "indexing", "indexed", "failed", "archived")


class DocumentDetail(BaseModel):
    """Full lifecycle view (extends DocMeta)."""

    id: UUID
    title: str
    mime: str
    status: str
    chunks: int = 0
    version: int = 1
    owner_sub: str = ""
    uri: str | None = None
    allowed_roles: list[str] = []
    chunk_config: dict[str, Any] = Field(
        default_factory=lambda: {"chunk_tokens": 600, "chunk_overlap": 90}
    )
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    error: str | None = None  # truncated ≤500 chars on read
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None


class DocumentUpdate(BaseModel):
    """PATCH body; all optional, at least one actionable field required."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    allowed_roles: list[str] | None = None  # ⊆ {admin,editor,viewer}; [] = owner-only
    owner_sub: str | None = None  # admin-only; non-admin supply → 404 (leak-safe)
    uri: str | None = Field(default=None, max_length=2000)


class DocumentList(BaseModel):
    items: list[DocumentDetail]
    total: int


class ReindexRequest(BaseModel):
    chunk_tokens: int | None = Field(default=None, ge=100, le=2000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=500)
    embedding_model: str | None = None  # must equal pinned model Phase 1 else 422


class ReindexResponse(BaseModel):
    doc_id: UUID
    job_id: UUID
    status: str = "indexing"  # Literal["indexing"] in spec; str for fake-compat
    version: int


class DocumentStatus(BaseModel):
    doc_id: UUID
    status: str
    chunks: int = 0
    version: int = 1
    error: str | None = None
    updated_at: datetime | None = None
