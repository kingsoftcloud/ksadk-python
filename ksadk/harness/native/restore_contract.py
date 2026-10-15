"""Reader provenance is diagnostic data, never a second execution checkpoint."""

from pydantic import Field

from .state import Contract

SNAPSHOT_READER_VERSION = "ksadk.native-snapshot-reader/v1"


class StoreRestoreAudit(Contract):
    reader_version: str = Field(min_length=1, max_length=160)
    snapshot_schema_version: str = Field(min_length=1, max_length=160)
    configuration_digest: str
    fencing_token: int | None = Field(default=None, strict=True, ge=1)
    executed_migration_ids: tuple[str, ...] = ()
    historical_migration_ids: tuple[str, ...] = ()
