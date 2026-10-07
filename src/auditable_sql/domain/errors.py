"""Domain-level exceptions. Adapters translate library errors into these."""


class AuditableSqlError(Exception):
    """Base class."""


class GenerationError(AuditableSqlError):
    """The language model call failed or returned something unusable."""


class QueryExecutionError(AuditableSqlError):
    """The database rejected or aborted a validated query (timeout, permission, syntax)."""


class AuditWriteError(AuditableSqlError):
    """The audit record could not be persisted. Callers must not release the answer."""


class RecordNotFoundError(AuditableSqlError):
    pass
