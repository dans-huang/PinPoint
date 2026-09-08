from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class IntelligenceStoreError(RuntimeError):
    pass


class TemplateUnavailable(IntelligenceStoreError):
    pass


class TemplateImmutable(IntelligenceStoreError):
    pass


class TemplateLimitReached(IntelligenceStoreError):
    pass


class InvalidVocabularyTerm(IntelligenceStoreError):
    pass


class VocabularyTermExists(IntelligenceStoreError):
    pass


class VocabularyLimitReached(IntelligenceStoreError):
    pass


class SummaryJobUnavailable(IntelligenceStoreError):
    pass


class SummaryJobConflict(IntelligenceStoreError):
    pass


class SummaryJobInProgress(IntelligenceStoreError):
    pass


class SummaryConcurrencyLimitReached(IntelligenceStoreError):
    pass


class SummaryDailyLimitReached(IntelligenceStoreError):
    pass


class StaleSummaryApproval(IntelligenceStoreError):
    pass


MAX_USER_TEMPLATES = 50
MAX_TEMPLATE_NAME_CHARS = 80
MAX_TEMPLATE_INSTRUCTION_CHARS = 4_000
MAX_VOCABULARY_TERMS = 500
MAX_VOCABULARY_TERM_CHARS = 64
MAX_PROPOSED_TERMS = 20
MAX_SUMMARY_CHARS = 30_000
DEFAULT_USER_ACTIVE_GENERATION_LIMIT = 2
DEFAULT_USER_DAILY_GENERATION_LIMIT = 40
# A crashed worker may have already started the external model call. After the
# lease, the row becomes 'unknown' (never silently retryable) rather than free.
SUMMARY_GENERATION_LEASE_SECONDS = 15 * 60


@dataclass(frozen=True)
class TemplateRecord:
    template_id: str
    name: str
    instructions: str
    builtin: bool
    state: str
    created_at: float | None = None
    updated_at: float | None = None


# The official Plaud Partner API exposes transcripts only; summary templates are
# PinPoint-owned. Built-ins are seeded in code so they are identical for every
# user, cannot be edited or deleted, and never occupy per-user template quota.
BUILTIN_TEMPLATES: tuple[TemplateRecord, ...] = (
    TemplateRecord(
        template_id="tpl_builtin_adaptive",
        name="Adaptive Summary",
        instructions=(
            "Write a summary adapted to the meeting's own structure: a short "
            "overview, the key discussion points, decisions made, and open "
            "questions. Match the meeting's language."
        ),
        builtin=True,
        state="active",
    ),
    TemplateRecord(
        template_id="tpl_builtin_action_items",
        name="Action Items",
        instructions=(
            "List every concrete action item as '- owner: task (due date if "
            "stated)'. Include only commitments actually made in the meeting; "
            "do not invent owners or dates."
        ),
        builtin=True,
        state="active",
    ),
    TemplateRecord(
        template_id="tpl_builtin_one_on_one",
        name="1:1",
        instructions=(
            "Summarize this one-on-one conversation: topics raised by each "
            "participant, feedback exchanged, agreements reached, and "
            "follow-ups for the next one-on-one."
        ),
        builtin=True,
        state="active",
    ),
)

BUILTIN_TEMPLATE_IDS = frozenset(record.template_id for record in BUILTIN_TEMPLATES)
DEFAULT_TEMPLATE_ID = BUILTIN_TEMPLATES[0].template_id


def builtin_template(template_id: str) -> TemplateRecord | None:
    for record in BUILTIN_TEMPLATES:
        if record.template_id == template_id:
            return record
    return None


@dataclass(frozen=True)
class IntelligenceSettings:
    auto_summary_enabled: bool
    default_template_id: str


@dataclass(frozen=True)
class VocabularyTerm:
    term_id: str
    term: str
    created_at: float


@dataclass(frozen=True)
class SummaryJob:
    job_id: str
    user_id: str
    transcription_id: str
    template_id: str
    request_id: str
    request_fingerprint: str
    mode: str
    state: str
    base_summary_version: int | None
    proposed_summary: str | None
    proposal_hash: str | None
    proposed_terms: tuple[str, ...]
    failure_reason: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class CanonicalSummary:
    transcription_id: str
    template_id: str
    summary_text: str
    version: int
    summary_hash: str
    updated_at: float


def normalize_vocabulary_term(term: str) -> tuple[str, str]:
    """Return (display term, case-insensitive uniqueness key) or raise."""
    cleaned = " ".join(term.split())
    if not cleaned or len(cleaned) > MAX_VOCABULARY_TERM_CHARS:
        raise InvalidVocabularyTerm(
            f"Vocabulary terms must be 1 to {MAX_VOCABULARY_TERM_CHARS} characters"
        )
    if not cleaned.isprintable():
        raise InvalidVocabularyTerm("Vocabulary terms must be printable text")
    normalized = unicodedata.normalize("NFKC", cleaned).casefold()
    return cleaned, normalized


def summary_text_hash(summary_text: str) -> str:
    return hashlib.sha256(summary_text.encode("utf-8")).hexdigest()


def _validated_template_fields(name: str, instructions: str) -> tuple[str, str]:
    cleaned_name = " ".join(name.split())
    if not cleaned_name or len(cleaned_name) > MAX_TEMPLATE_NAME_CHARS:
        raise IntelligenceStoreError(
            f"Template names must be 1 to {MAX_TEMPLATE_NAME_CHARS} characters"
        )
    if not cleaned_name.isprintable():
        raise IntelligenceStoreError("Template names must be printable text")
    cleaned_instructions = instructions.strip()
    if not cleaned_instructions or len(cleaned_instructions) > MAX_TEMPLATE_INSTRUCTION_CHARS:
        raise IntelligenceStoreError(
            f"Template instructions must be 1 to {MAX_TEMPLATE_INSTRUCTION_CHARS} characters"
        )
    if "\x00" in cleaned_instructions:
        raise IntelligenceStoreError("Template instructions must be printable text")
    return cleaned_name, cleaned_instructions


class IntelligenceStore:
    """PinPoint-owned intelligence ledger sharing the durable SQLite file.

    Every row is keyed by the composite (user_id, entity id); the legacy tables
    do not enable foreign keys, so ownership is always enforced in the WHERE
    clause of each statement, never assumed from a bare identifier.
    """

    def __init__(self, path: str, *, now=time.time) -> None:
        self._path = Path(path).expanduser().resolve()
        self._now = now
        self._initialize()

    # ------------------------------------------------------------- templates

    def list_templates(self, user_id: str) -> list[TemplateRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT template_id, name, instructions, state, created_at, updated_at
                FROM intelligence_templates
                WHERE user_id = ?
                ORDER BY created_at, template_id
                """,
                (user_id,),
            ).fetchall()
        return list(BUILTIN_TEMPLATES) + [
            TemplateRecord(
                template_id=str(row[0]),
                name=str(row[1]),
                instructions=str(row[2]),
                builtin=False,
                state=str(row[3]),
                created_at=float(row[4]),
                updated_at=float(row[5]),
            )
            for row in rows
        ]

    def usable_template(self, user_id: str, template_id: str) -> TemplateRecord | None:
        builtin = builtin_template(template_id)
        if builtin is not None:
            return builtin
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT template_id, name, instructions, created_at, updated_at
                FROM intelligence_templates
                WHERE user_id = ? AND template_id = ? AND state = 'active'
                """,
                (user_id, template_id),
            ).fetchone()
        if row is None:
            return None
        return TemplateRecord(
            template_id=str(row[0]),
            name=str(row[1]),
            instructions=str(row[2]),
            builtin=False,
            state="active",
            created_at=float(row[3]),
            updated_at=float(row[4]),
        )

    def create_template(
        self,
        *,
        user_id: str,
        template_id: str,
        name: str,
        instructions: str,
        max_templates: int = MAX_USER_TEMPLATES,
    ) -> TemplateRecord:
        cleaned_name, cleaned_instructions = _validated_template_fields(name, instructions)
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) FROM intelligence_templates WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            if count >= max_templates:
                connection.rollback()
                raise TemplateLimitReached(
                    f"At most {max_templates} custom templates are allowed"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO intelligence_templates
                        (user_id, template_id, name, instructions, state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (user_id, template_id, cleaned_name, cleaned_instructions, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise IntelligenceStoreError("Template identifier already exists") from exc
            connection.commit()
        return TemplateRecord(
            template_id=template_id,
            name=cleaned_name,
            instructions=cleaned_instructions,
            builtin=False,
            state="active",
            created_at=now,
            updated_at=now,
        )

    def update_template(
        self,
        *,
        user_id: str,
        template_id: str,
        name: str | None = None,
        instructions: str | None = None,
    ) -> TemplateRecord:
        if template_id in BUILTIN_TEMPLATE_IDS:
            raise TemplateImmutable("Built-in templates cannot be modified")
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT name, instructions, state, created_at
                FROM intelligence_templates
                WHERE user_id = ? AND template_id = ?
                """,
                (user_id, template_id),
            ).fetchone()
            if row is None or row[2] != "active":
                connection.rollback()
                raise TemplateUnavailable("Template not found")
            new_name, new_instructions = _validated_template_fields(
                row[0] if name is None else name,
                row[1] if instructions is None else instructions,
            )
            connection.execute(
                """
                UPDATE intelligence_templates
                SET name = ?, instructions = ?, updated_at = ?
                WHERE user_id = ? AND template_id = ? AND state = 'active'
                """,
                (new_name, new_instructions, now, user_id, template_id),
            )
            connection.commit()
        return TemplateRecord(
            template_id=template_id,
            name=new_name,
            instructions=new_instructions,
            builtin=False,
            state="active",
            created_at=float(row[3]),
            updated_at=now,
        )

    def delete_template(self, *, user_id: str, template_id: str) -> str:
        """Delete an unreferenced user template; archive a referenced one."""
        if template_id in BUILTIN_TEMPLATE_IDS:
            raise TemplateImmutable("Built-in templates cannot be deleted")
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state FROM intelligence_templates
                WHERE user_id = ? AND template_id = ?
                """,
                (user_id, template_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise TemplateUnavailable("Template not found")
            referenced = any(
                connection.execute(query, (user_id, template_id)).fetchone() is not None
                for query in (
                    """
                    SELECT 1 FROM intelligence_settings
                    WHERE user_id = ? AND default_template_id = ? LIMIT 1
                    """,
                    """
                    SELECT 1 FROM summary_jobs
                    WHERE user_id = ? AND template_id = ? LIMIT 1
                    """,
                    """
                    SELECT 1 FROM transcription_summaries
                    WHERE user_id = ? AND template_id = ? LIMIT 1
                    """,
                )
            )
            if referenced:
                # A default template must always remain usable. Resetting it in
                # the same transaction prevents automatic summary generation
                # from observing an archived default between two writes.
                connection.execute(
                    """
                    UPDATE intelligence_settings
                    SET default_template_id = ?, updated_at = ?
                    WHERE user_id = ? AND default_template_id = ?
                    """,
                    (DEFAULT_TEMPLATE_ID, now, user_id, template_id),
                )
                connection.execute(
                    """
                    UPDATE intelligence_templates
                    SET state = 'archived', updated_at = ?
                    WHERE user_id = ? AND template_id = ?
                    """,
                    (now, user_id, template_id),
                )
                connection.commit()
                return "archived"
            connection.execute(
                "DELETE FROM intelligence_templates WHERE user_id = ? AND template_id = ?",
                (user_id, template_id),
            )
            connection.commit()
            return "deleted"

    # -------------------------------------------------------------- settings

    def get_settings(self, user_id: str) -> IntelligenceSettings:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT auto_summary_enabled, default_template_id
                FROM intelligence_settings WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return IntelligenceSettings(
                auto_summary_enabled=True,
                default_template_id=DEFAULT_TEMPLATE_ID,
            )
        return IntelligenceSettings(
            auto_summary_enabled=bool(row[0]),
            default_template_id=str(row[1]),
        )

    def put_settings(
        self,
        *,
        user_id: str,
        auto_summary_enabled: bool,
        default_template_id: str,
    ) -> IntelligenceSettings:
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if default_template_id not in BUILTIN_TEMPLATE_IDS:
                usable = connection.execute(
                    """
                    SELECT 1 FROM intelligence_templates
                    WHERE user_id = ? AND template_id = ? AND state = 'active'
                    """,
                    (user_id, default_template_id),
                ).fetchone()
                if usable is None:
                    connection.rollback()
                    raise TemplateUnavailable("Template not found")
            connection.execute(
                """
                INSERT INTO intelligence_settings
                    (user_id, auto_summary_enabled, default_template_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    auto_summary_enabled = excluded.auto_summary_enabled,
                    default_template_id = excluded.default_template_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, 1 if auto_summary_enabled else 0, default_template_id, now),
            )
            connection.commit()
        return IntelligenceSettings(
            auto_summary_enabled=auto_summary_enabled,
            default_template_id=default_template_id,
        )

    # ------------------------------------------------------------ vocabulary

    def list_vocabulary(self, user_id: str) -> list[VocabularyTerm]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT term_id, term, created_at FROM intelligence_vocabulary
                WHERE user_id = ? ORDER BY created_at, term_id
                """,
                (user_id,),
            ).fetchall()
        return [
            VocabularyTerm(term_id=str(row[0]), term=str(row[1]), created_at=float(row[2]))
            for row in rows
        ]

    def add_vocabulary_term(
        self,
        *,
        user_id: str,
        term: str,
        max_terms: int = MAX_VOCABULARY_TERMS,
    ) -> VocabularyTerm:
        cleaned, normalized = normalize_vocabulary_term(term)
        term_id = "voc_" + secrets.token_urlsafe(9)
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) FROM intelligence_vocabulary WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            if count >= max_terms:
                connection.rollback()
                raise VocabularyLimitReached(
                    f"At most {max_terms} custom vocabulary terms are allowed"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO intelligence_vocabulary
                        (user_id, term_id, normalized_term, term, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, term_id, normalized, cleaned, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise VocabularyTermExists("An equivalent vocabulary term already exists") from exc
            connection.commit()
        return VocabularyTerm(term_id=term_id, term=cleaned, created_at=now)

    def delete_vocabulary_term(self, *, user_id: str, term_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM intelligence_vocabulary WHERE user_id = ? AND term_id = ?",
                (user_id, term_id),
            )
        return cursor.rowcount == 1

    # ---------------------------------------------------------- summary jobs

    def reserve_summary_job(
        self,
        *,
        job_id: str,
        user_id: str,
        transcription_id: str,
        template_id: str,
        request_id: str,
        request_fingerprint: str,
        mode: str = "generate",
        user_active_limit: int = DEFAULT_USER_ACTIVE_GENERATION_LIMIT,
        user_daily_limit: int = DEFAULT_USER_DAILY_GENERATION_LIMIT,
    ) -> tuple[str, SummaryJob | None]:
        """Idempotently claim one generation attempt per (user, request key).

        Returns the canonical job id plus the stored job when the key was
        already settled. ``None`` means the caller now owns a fresh
        ``generating`` reservation (a safely failed attempt may be revived and
        consumes another daily attempt, but an ``unknown`` outcome is never
        silently retried).
        """
        now = float(self._now())
        day_start = now - (now % (24 * 60 * 60))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_summary_jobs(connection, now)
            row = connection.execute(
                self._SUMMARY_JOB_SELECT + " WHERE user_id = ? AND request_id = ?",
                (user_id, request_id),
            ).fetchone()
            if row is not None:
                job = self._summary_job_from_row(row)
                if job.request_fingerprint != request_fingerprint:
                    connection.rollback()
                    raise SummaryJobConflict(
                        "This idempotency key was already used with a different request"
                    )
                if job.state == "failed":
                    daily_count = connection.execute(
                        """
                        SELECT COUNT(*) FROM summary_job_attempts
                        WHERE user_id = ? AND attempted_at >= ?
                        """,
                        (user_id, day_start),
                    ).fetchone()[0]
                    if daily_count >= user_daily_limit:
                        connection.rollback()
                        raise SummaryDailyLimitReached(
                            "Daily summary generation limit reached"
                        )
                    active_count = connection.execute(
                        """
                        SELECT COUNT(*) FROM summary_jobs
                        WHERE user_id = ? AND state = 'generating' AND job_id != ?
                        """,
                        (user_id, job.job_id),
                    ).fetchone()[0]
                    if active_count >= user_active_limit:
                        connection.rollback()
                        raise SummaryConcurrencyLimitReached(
                            "Too many summary generations are already running"
                        )
                    connection.execute(
                        """
                        UPDATE summary_jobs
                        SET state = 'generating', failure_reason = NULL, updated_at = ?
                        WHERE job_id = ? AND user_id = ? AND state = 'failed'
                        """,
                        (now, job.job_id, user_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO summary_job_attempts (job_id, user_id, attempted_at)
                        VALUES (?, ?, ?)
                        """,
                        (job.job_id, user_id, now),
                    )
                    connection.commit()
                    return job.job_id, None
                connection.commit()
                return job.job_id, job
            daily_count = connection.execute(
                """
                SELECT COUNT(*) FROM summary_job_attempts
                WHERE user_id = ? AND attempted_at >= ?
                """,
                (user_id, day_start),
            ).fetchone()[0]
            if daily_count >= user_daily_limit:
                connection.rollback()
                raise SummaryDailyLimitReached(
                    "Daily summary generation limit reached"
                )
            active_count = connection.execute(
                """
                SELECT COUNT(*) FROM summary_jobs
                WHERE user_id = ? AND state = 'generating'
                """,
                (user_id,),
            ).fetchone()[0]
            if active_count >= user_active_limit:
                connection.rollback()
                raise SummaryConcurrencyLimitReached(
                    "Too many summary generations are already running"
                )
            connection.execute(
                """
                INSERT INTO summary_jobs (
                    job_id, user_id, transcription_id, template_id, request_id,
                    request_fingerprint, mode, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'generating', ?, ?)
                """,
                (
                    job_id, user_id, transcription_id, template_id, request_id,
                    request_fingerprint, mode, now, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO summary_job_attempts (job_id, user_id, attempted_at)
                VALUES (?, ?, ?)
                """,
                (job_id, user_id, now),
            )
            connection.commit()
        return job_id, None

    def fail_summary_job(self, *, job_id: str, user_id: str, reason: str) -> None:
        """Retire a reservation that verifiably never reached the model."""
        self._settle_generating_job(job_id, user_id, state="failed", reason=reason)

    def mark_summary_job_unknown(self, *, job_id: str, user_id: str) -> None:
        """Freeze an attempt whose external model outcome is unprovable."""
        self._settle_generating_job(job_id, user_id, state="unknown", reason=None)

    def discard_generating_job(self, *, job_id: str, user_id: str) -> None:
        """Drop a generation result that may no longer be persisted safely."""
        self._settle_generating_job(job_id, user_id, state="discarded", reason=None)

    def save_summary_proposal(
        self,
        *,
        job_id: str,
        user_id: str,
        summary_text: str,
        proposed_terms: tuple[str, ...],
        expected_base_version: int | None = None,
    ) -> SummaryJob:
        if not summary_text or len(summary_text) > MAX_SUMMARY_CHARS:
            raise IntelligenceStoreError("Proposed summary length is invalid")
        if len(proposed_terms) > MAX_PROPOSED_TERMS:
            raise IntelligenceStoreError("Too many proposed vocabulary terms")
        proposal_hash = summary_text_hash(summary_text)
        terms_json = json.dumps(list(proposed_terms), separators=(",", ":"))
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                self._SUMMARY_JOB_SELECT + " WHERE job_id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if row is None or row[7] != "generating":
                connection.rollback()
                raise SummaryJobUnavailable("Summary job is not generating")
            base_version_row = connection.execute(
                """
                SELECT version FROM transcription_summaries
                WHERE user_id = ? AND transcription_id = ?
                """,
                (user_id, row[2]),
            ).fetchone()
            base_version = 0 if base_version_row is None else int(base_version_row[0])
            if expected_base_version is not None and base_version != expected_base_version:
                connection.rollback()
                raise StaleSummaryApproval(
                    "The canonical summary changed while this proposal was generated"
                )
            connection.execute(
                """
                UPDATE summary_jobs
                SET state = 'proposed', base_summary_version = ?, proposed_summary = ?,
                    proposal_hash = ?, proposed_terms_json = ?, failure_reason = NULL,
                    updated_at = ?
                WHERE job_id = ? AND user_id = ? AND state = 'generating'
                """,
                (base_version, summary_text, proposal_hash, terms_json, now, job_id, user_id),
            )
            connection.commit()
        job = self.summary_job(job_id, user_id)
        if job is None:
            raise SummaryJobUnavailable("Summary job is not generating")
        return job

    def summary_job(self, job_id: str, user_id: str) -> SummaryJob | None:
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_summary_jobs(connection, now)
            row = connection.execute(
                self._SUMMARY_JOB_SELECT + " WHERE job_id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            connection.commit()
        return None if row is None else self._summary_job_from_row(row)

    def summary_job_for_request(self, request_id: str, user_id: str) -> SummaryJob | None:
        """Recover a job after the client lost its create response.

        The lookup is always scoped by the authenticated owner and never
        reveals whether another user has used the same request identifier.
        """
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_summary_jobs(connection, now)
            row = connection.execute(
                self._SUMMARY_JOB_SELECT + " WHERE request_id = ? AND user_id = ?",
                (request_id, user_id),
            ).fetchone()
            connection.commit()
        return None if row is None else self._summary_job_from_row(row)

    def canonical_summary(self, transcription_id: str, user_id: str) -> CanonicalSummary | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT transcription_id, template_id, summary_text, version,
                       summary_hash, updated_at
                FROM transcription_summaries
                WHERE user_id = ? AND transcription_id = ?
                """,
                (user_id, transcription_id),
            ).fetchone()
        if row is None:
            return None
        return CanonicalSummary(
            transcription_id=str(row[0]),
            template_id=str(row[1]),
            summary_text=str(row[2]),
            version=int(row[3]),
            summary_hash=str(row[4]),
            updated_at=float(row[5]),
        )

    def approve_summary_job(
        self,
        *,
        job_id: str,
        user_id: str,
        proposal_hash: str,
        base_version: int,
        accepted_terms: tuple[str, ...],
        max_terms: int = MAX_VOCABULARY_TERMS,
    ) -> tuple[CanonicalSummary, tuple[str, ...]]:
        """Atomically promote a proposal and add only the accepted terms."""
        accepted: list[tuple[str, str]] = []
        seen: set[str] = set()
        for term in accepted_terms:
            cleaned, normalized = normalize_vocabulary_term(term)
            if normalized not in seen:
                seen.add(normalized)
                accepted.append((cleaned, normalized))
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_summary_jobs(connection, now)
            row = connection.execute(
                self._SUMMARY_JOB_SELECT + " WHERE job_id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise SummaryJobUnavailable("Summary job not found")
            job = self._summary_job_from_row(row)
            if job.state == "approved" and job.proposal_hash == proposal_hash:
                connection.commit()
                canonical = self.canonical_summary(job.transcription_id, user_id)
                if canonical is None:
                    raise SummaryJobUnavailable("Summary job not found")
                return canonical, ()
            if job.state != "proposed" or job.proposed_summary is None:
                connection.rollback()
                raise SummaryJobConflict("Summary job is not awaiting approval")
            if job.proposal_hash != proposal_hash:
                connection.rollback()
                raise StaleSummaryApproval("Approval does not match the stored proposal")
            proposed_normals = {
                normalize_vocabulary_term(term)[1] for term in job.proposed_terms
            }
            if any(normalized not in proposed_normals for _, normalized in accepted):
                connection.rollback()
                raise InvalidVocabularyTerm(
                    "Accepted terms must come from this job's proposal"
                )
            current_row = connection.execute(
                """
                SELECT version FROM transcription_summaries
                WHERE user_id = ? AND transcription_id = ?
                """,
                (user_id, job.transcription_id),
            ).fetchone()
            current_version = 0 if current_row is None else int(current_row[0])
            if current_version != job.base_summary_version or base_version != current_version:
                connection.rollback()
                raise StaleSummaryApproval(
                    "The canonical summary changed after this proposal was generated"
                )
            existing_normals = {
                str(vocabulary_row[0])
                for vocabulary_row in connection.execute(
                    "SELECT normalized_term FROM intelligence_vocabulary WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
            }
            new_terms = [
                (cleaned, normalized)
                for cleaned, normalized in accepted
                if normalized not in existing_normals
            ]
            if len(existing_normals) + len(new_terms) > max_terms:
                connection.rollback()
                raise VocabularyLimitReached(
                    f"At most {max_terms} custom vocabulary terms are allowed"
                )
            new_version = current_version + 1
            connection.execute(
                """
                INSERT INTO transcription_summaries
                    (user_id, transcription_id, template_id, summary_text,
                     version, summary_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, transcription_id) DO UPDATE SET
                    template_id = excluded.template_id,
                    summary_text = excluded.summary_text,
                    version = excluded.version,
                    summary_hash = excluded.summary_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id, job.transcription_id, job.template_id,
                    job.proposed_summary, new_version, job.proposal_hash, now,
                ),
            )
            for cleaned, normalized in new_terms:
                connection.execute(
                    """
                    INSERT INTO intelligence_vocabulary
                        (user_id, term_id, normalized_term, term, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, "voc_" + secrets.token_urlsafe(9), normalized, cleaned, now),
                )
            cursor = connection.execute(
                """
                UPDATE summary_jobs SET state = 'approved', updated_at = ?
                WHERE job_id = ? AND user_id = ? AND state = 'proposed'
                """,
                (now, job_id, user_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise SummaryJobConflict("Summary job changed during approval")
            connection.commit()
        canonical = CanonicalSummary(
            transcription_id=job.transcription_id,
            template_id=job.template_id,
            summary_text=job.proposed_summary,
            version=new_version,
            summary_hash=job.proposal_hash or "",
            updated_at=now,
        )
        return canonical, tuple(cleaned for cleaned, _ in new_terms)

    def discard_summary_job(self, *, job_id: str, user_id: str) -> None:
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_summary_jobs(connection, now)
            row = connection.execute(
                "SELECT state FROM summary_jobs WHERE job_id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise SummaryJobUnavailable("Summary job not found")
            if row[0] not in {"proposed", "failed", "unknown", "discarded"}:
                connection.rollback()
                raise SummaryJobConflict("Summary job can no longer be discarded")
            connection.execute(
                """
                UPDATE summary_jobs
                SET state = 'discarded', proposed_summary = NULL,
                    proposal_hash = NULL, proposed_terms_json = NULL,
                    failure_reason = NULL, updated_at = ?
                WHERE job_id = ? AND user_id = ?
                  AND state IN ('proposed', 'failed', 'unknown', 'discarded')
                """,
                (now, job_id, user_id),
            )
            connection.commit()

    # -------------------------------------------------------------- internal

    _SUMMARY_JOB_SELECT = """
        SELECT job_id, user_id, transcription_id, template_id, request_id,
               request_fingerprint, mode, state, base_summary_version, proposed_summary,
               proposal_hash, proposed_terms_json, failure_reason,
               created_at, updated_at
        FROM summary_jobs
    """

    def _settle_generating_job(
        self,
        job_id: str,
        user_id: str,
        *,
        state: str,
        reason: str | None,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE summary_jobs
                SET state = ?, failure_reason = ?, updated_at = ?
                WHERE job_id = ? AND user_id = ? AND state = 'generating'
                """,
                (state, reason, float(self._now()), job_id, user_id),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    """
                    SELECT 1 FROM summary_jobs
                    WHERE job_id = ? AND user_id = ? AND state = ?
                    """,
                    (job_id, user_id, state),
                ).fetchone()
                if existing is None:
                    raise SummaryJobUnavailable("Summary job outcome could not be recorded")

    @staticmethod
    def _summary_job_from_row(row) -> SummaryJob:
        raw_terms = row[11]
        try:
            parsed = json.loads(raw_terms) if raw_terms else []
        except json.JSONDecodeError:
            parsed = []
        terms = tuple(term for term in parsed if isinstance(term, str))
        return SummaryJob(
            job_id=str(row[0]),
            user_id=str(row[1]),
            transcription_id=str(row[2]),
            template_id=str(row[3]),
            request_id=str(row[4]),
            request_fingerprint=str(row[5]),
            mode=str(row[6]),
            state=str(row[7]),
            base_summary_version=None if row[8] is None else int(row[8]),
            proposed_summary=None if row[9] is None else str(row[9]),
            proposal_hash=None if row[10] is None else str(row[10]),
            proposed_terms=terms,
            failure_reason=None if row[12] is None else str(row[12]),
            created_at=float(row[13]),
            updated_at=float(row[14]),
        )

    @staticmethod
    def _expire_summary_jobs(connection: sqlite3.Connection, now: float) -> None:
        # A worker that died between reserving the attempt and recording its
        # outcome may have already reached the external model. Freeze the row
        # as 'unknown' so the same idempotency key is never silently repeated.
        connection.execute(
            """
            UPDATE summary_jobs
            SET state = 'unknown', failure_reason = NULL, updated_at = ?
            WHERE state = 'generating' AND updated_at <= ?
            """,
            (now, now - SUMMARY_GENERATION_LEASE_SECONDS),
        )

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS intelligence_templates (
                    user_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('active', 'archived')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, template_id)
                );

                CREATE TABLE IF NOT EXISTS intelligence_settings (
                    user_id TEXT PRIMARY KEY,
                    auto_summary_enabled INTEGER NOT NULL
                        CHECK (auto_summary_enabled IN (0, 1)),
                    default_template_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intelligence_vocabulary (
                    user_id TEXT NOT NULL,
                    term_id TEXT NOT NULL,
                    normalized_term TEXT NOT NULL,
                    term TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (user_id, term_id),
                    UNIQUE (user_id, normalized_term)
                );

                CREATE TABLE IF NOT EXISTS summary_jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    transcription_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'generate'
                        CHECK (mode IN ('generate', 'improve')),
                    state TEXT NOT NULL CHECK (
                        state IN ('generating', 'proposed', 'approved',
                                  'discarded', 'failed', 'unknown')
                    ),
                    base_summary_version INTEGER,
                    proposed_summary TEXT,
                    proposal_hash TEXT,
                    proposed_terms_json TEXT,
                    failure_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS summary_jobs_owner_request
                    ON summary_jobs (user_id, request_id);
                CREATE INDEX IF NOT EXISTS summary_jobs_owner_transcription
                    ON summary_jobs (user_id, transcription_id);

                CREATE TABLE IF NOT EXISTS summary_job_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    attempted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS summary_job_attempts_owner_time
                    ON summary_job_attempts (user_id, attempted_at);

                CREATE TABLE IF NOT EXISTS transcription_summaries (
                    user_id TEXT NOT NULL,
                    transcription_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    summary_text TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version > 0),
                    summary_hash TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, transcription_id)
                );
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(summary_jobs)").fetchall()
            }
            if "mode" not in columns:
                connection.execute(
                    """
                    ALTER TABLE summary_jobs ADD COLUMN mode TEXT NOT NULL
                        DEFAULT 'generate' CHECK (mode IN ('generate', 'improve'))
                    """
                )
            # Existing ledgers predate attempt-level accounting. Count each
            # existing job once without inventing retries that cannot be
            # reconstructed from the old schema.
            connection.execute(
                """
                INSERT INTO summary_job_attempts (job_id, user_id, attempted_at)
                SELECT jobs.job_id, jobs.user_id, jobs.created_at
                FROM summary_jobs AS jobs
                WHERE NOT EXISTS (
                    SELECT 1 FROM summary_job_attempts AS attempts
                    WHERE attempts.job_id = jobs.job_id
                      AND attempts.user_id = jobs.user_id
                )
                """
            )
            # A process that disappeared during a provider call cannot resume
            # that external request safely. Expired leases are frozen during
            # startup as well as reads/reservations, so recovery never depends
            # on a particular client polling first.
            connection.execute("BEGIN IMMEDIATE")
            self._expire_summary_jobs(connection, float(self._now()))
            connection.commit()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        finally:
            connection.close()
