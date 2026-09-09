"""Bounded memory mutation metadata; never includes the stored memory body."""

from typing import Literal

from pydantic import Field, model_validator

from ksadk.plugins.contracts import PluginContractModel


class MemoryMutationReceipt(PluginContractModel):
    operation_id: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    memory_id: str = Field(strict=True, min_length=1, max_length=256)
    new_memory_id: str = Field(default="", strict=True, max_length=256)
    status: Literal["succeeded", "failed", "unknown"]
    error_code: (
        Literal["MEMORY_MUTATION_UNKNOWN", "MEMORY_RECORD_NOT_OBSERVED", "MEMORY_RECORD_NOT_FOUND"]
        | None
    )

    @model_validator(mode="after")
    def consistent_result(self):
        if self.status == "succeeded":
            if self.error_code is not None:
                raise ValueError("Successful mutation cannot carry an error")
        elif self.new_memory_id:
            raise ValueError("Unconfirmed mutation cannot grant a new record ID")
        elif self.status == "unknown" and self.error_code != "MEMORY_MUTATION_UNKNOWN":
            raise ValueError("Unknown mutation requires an unknown result code")
        elif self.status == "failed" and self.error_code not in {
            "MEMORY_RECORD_NOT_OBSERVED",
            "MEMORY_RECORD_NOT_FOUND",
        }:
            raise ValueError("Failed mutation requires a confirmed rejection")
        return self
