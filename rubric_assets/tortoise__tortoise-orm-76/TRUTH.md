# Canonical Solve Notes

## 1. Problem
This combined PR addresses three related deficiencies in tortoise-orm's schema/query layer. First, `Tortoise.generate_schemas()` had no way to safely (idempotently) create tables — it always emitted plain `CREATE TABLE`, so calling it against an already-provisioned database raised an error, forcing users to catch-and-ignore exceptions (which breaks after the first failing statement, leaving subsequent tables uncreated). Second, the `in`/`not_in` filter machinery used a `list_encoder` that simply cast the passed iterable to a `list` without running each element through the field's `to_db_value` conversion, so custom fields (e.g. an `EnumField` wrapping a Python `Enum`) leaked their raw Python values (or enum members) straight into the SQL parameters instead of their DB-safe representation, causing incorrect or failing queries when filtering with `__in`/`__not_in` on custom-typed fields. Third, the field set was missing a 64-bit integer field (`BigIntField`, usable as PK) and a `timedelta` field (`TimeDeltaField`), and existing int fields lacked boundary-value coverage.

## 2. Load-bearing changes
- `tortoise/__init__.py` (`Tortoise.generate_schemas`): add `safe=True` parameter, forward it to `generate_schema_for_client`. This is the public API surface for the safe-schema feature; version bumped to 0.11.2.
- `tortoise/utils.py` (`get_schema_sql`, `generate_schema_for_client`): thread a `safe` argument (default `True`) through to `get_create_schema_sql`.
- `tortoise/backends/base/schema_generator.py`:
  - `TABLE_CREATE_TEMPLATE` and `M2M_TABLE_TEMPLATE` reworked to use named placeholders including an `{exists}` slot for the `IF NOT EXISTS ` clause — required so the generated SQL can conditionally include the clause.
  - `_get_table_sql` and `get_create_schema_sql` gain a `safe` parameter, formatting `exists="IF NOT EXISTS " if safe else ""` into both table and m2m-table templates. This is the actual mechanism producing safe DDL.
  - `FIELD_TYPE_MAP` extended with `fields.BigIntField: 'BIGINT'` and `fields.TimeDeltaField: 'BIGINT'`.
  - PK detection updated to `isinstance(field_object, (fields.IntField, fields.BigIntField))` so `BigIntField(pk=True)` is recognized as a primary key.
- `tortoise/backends/mysql/schema_generator.py`: `TABLE_CREATE_TEMPLATE` updated to the new named-placeholder format with backticks, keeping MySQL-specific quoting consistent with the base class's safe-mode change.
- `tortoise/fields.py`:
  - New `BigIntField` class (64-bit signed int, usable as `pk`), and new `TimeDeltaField` class with `to_db_value`/`to_python_value` converting `datetime.timedelta` to/from an integer microsecond count. Both required to satisfy the new field types requested.
  - `JSONField.to_db_value` changed to explicitly `return None` instead of `return value` when value is `None` — minor correctness/refactor fix.
- `tortoise/filters.py` (`list_encoder`): signature changed to `(values, instance, field)`, and implementation changed from `list(value)` to `[field.to_db_value(element, instance) for element in values]`. This is the core fix for the `__in`/`__not_in` filter bug — each list element is now passed through the field's own conversion logic. `bool_encoder`/`string_encoder` signatures loosened to `(value, *args)` for call-signature compatibility.
- `tortoise/query_utils.py` (`_process_filter_kwarg`): updated call site to pass `field_object` into `value_encoder(value, model, field_object)`, matching the new `list_encoder` signature — required or `list_encoder` cannot access `to_db_value`.

## 3. Correct verification behavior
- Generate schema SQL both with `safe=True` (default) and `safe=False` and confirm the `IF NOT EXISTS` clause is present only in the safe case, for both regular tables and many-to-many join tables, on the SQLite/base generator and on MySQL's overridden template.
- Confirm `Tortoise.generate_schemas()` can be invoked twice in a row without raising when `safe=True` (idempotent), and that omitting `safe` still creates tables (default now `True`).
- Exercise filtering with `__in` and `__not_in` on a model field backed by a custom `Field` subclass (e.g. an enum-backed field) using Python-level values (enum members) rather than pre-converted DB values, and confirm the query executes correctly and returns matching rows — validating that `to_db_value` is applied per-element.
- Also check plain `equal`, `not`, `isnull`/`not_isnull` filters continue to work on such custom fields, since the encoder dispatch was touched.
- Create records with `BigIntField` (as a normal field and as `pk=True`) using boundary values (`9223372036854775807` / `-9223372036854775808`) and confirm round-trip storage/retrieval, including via `.values()`/`.values_list()`.
- Create records with `TimeDeltaField` using a `timedelta` with days/seconds/microseconds and confirm round-trip equality after save/reload, including via `.values()`/`.values_list()`.
- Re-run existing `IntField`/`SmallIntField` tests with min/max 32-bit and 16-bit boundary values to ensure no regression in storage precision.

## 4. Traps and near-misses
- Adding the `safe` parameter to `generate_schemas`/`generate_schema_for_client` but not threading it into `_get_table_sql`/`get_create_schema_sql`, or hardcoding the `IF NOT EXISTS` string only into the table template but forgetting the m2m join-table template — m2m tables would still fail to be created safely.
- Changing `TABLE_CREATE_TEMPLATE` to use `{exists}` but leaving positional `.format(table, fields)` calls unchanged (or vice versa) — mismatched template placeholders (named vs positional) cause `IndexError`/`KeyError` at generation time; the MySQL subclass template must also be updated in lockstep since it overrides the base template string.
- Fixing `list_encoder` to convert values but not updating `_process_filter_kwarg` to pass `field_object` — causes a `TypeError` (missing argument) or silently reverts to raw values if the encoder signature mismatch is masked by `*args`.
- Converting `__in` values with `field.to_db_value` but forgetting `bool_encoder`/`string_encoder` still work when called with the extra `field` argument — must use `*args` or add an unused third parameter, otherwise those filters break with `TypeError`.
- Adding `BigIntField` without updating the PK-detection `isinstance` check in `_get_table_sql` — `BigIntField(pk=True)` would not receive the primary-key create string and would instead be treated as a normal nullable column, breaking schema generation for models using it as PK.
- Implementing `TimeDeltaField.to_db_value` using only `.total_seconds()` or seconds-based conversion (losing microsecond precision) instead of the full days/seconds/microseconds-to-microseconds calculation — fails round-trip tests with fractional-second timedeltas.
- Leaving `JSONField.to_db_value` returning `value` for `None` inputs — inconsistent with other fields' `None`-handling convention, though not itself security-critical, it's part of the same cleanup pass and its omission is a partial/incomplete fix.
- Defaulting `safe` to `False` instead of `True` in `generate_schemas`/`get_create_schema_sql`/`generate_schema_for_client` — contradicts the documented behavior (README/docs updated to state safe mode is the default) and breaks the "call schema generation as part of app init" use case described in the motivation.
