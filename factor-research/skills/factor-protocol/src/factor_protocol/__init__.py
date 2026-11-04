"""Stable factor package contracts and DuckDB-backed Hive Parquet access."""

from .artifacts import (
    dataset_manifest,
    factor_column_output,
    manifests_match,
    read_config,
    write_config,
)
from .config import (
    resolve_artifacts_root,
    resolve_data_root,
    resolve_factor_data_root,
    resolve_factor_schema_path,
    resolve_factor_zoo_root,
)
from .fingerprint import (
    factor_definition_fingerprint,
)
from .identity import (
    read_identity,
    require_identity,
    stored_identity_matches,
    write_identity,
)
from .loader import load_module_from_path, resolve_factor_package, select_factor_columns
from .paths import FactorPath, parse_factor_paths, validate_identifier
from .protocol import (
    CalculateFactor,
    FactorCalculateContext,
    FactorPackage,
    FactorProtocolError,
    FactorValidateContext,
    FactorValidationDefinition,
    ValidateFactor,
    call_with_supported_kwargs,
    callable_accepts,
    define_factor_validator,
    resolve_factor_columns,
)
from .registry import (
    active_factor_columns_by_name,
    list_active_factor_names,
    list_active_factor_paths,
    list_factor_names,
)
from .storage import FactorStorage, FactorStoragePaths

__all__ = [name for name in globals() if not name.startswith("_")]
