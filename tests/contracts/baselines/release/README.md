# Phase 2 v1 schema baseline

The JSON Schema files below are the immutable review baseline for the sixteen
Phase 2 v1 contracts: eleven plugin contracts, three conversation contracts,
and two scheduler contracts. Current schemas are compared recursively against
these copies so a manifest refresh cannot hide a breaking change.

Only optional properties or new definitions may be added in the same v1.
Changing required fields, removing an existing property, changing an enum or
adding a constraint to an existing field requires a new contract version.
