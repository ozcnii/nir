from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from catalog.reference_data_v1 import (
    PROPERTY_TYPES,
    REGIONS,
    SEQUENCE_FLOORS,
    SOURCE_CODES,
)


def seed_reference_data(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    db = schema_editor.connection.alias
    sources = apps.get_model("catalog", "Source")
    property_types = apps.get_model("catalog", "PropertyType")
    regions = apps.get_model("catalog", "Region")

    schema_editor.execute(
        "LOCK TABLE public.sources, public.property_types, public.regions "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    for model in (sources, property_types, regions):
        if model.objects.using(db).exists():
            raise RuntimeError(
                f"Initial reference data requires an empty {model._meta.db_table} table."
            )

    sources.objects.using(db).bulk_create(
        [sources(code=code, enabled=True) for code in SOURCE_CODES]
    )
    property_types.objects.using(db).bulk_create(
        [property_types(pk=pk, name=name) for pk, _, name in PROPERTY_TYPES]
    )
    regions.objects.using(db).bulk_create(
        [regions(pk=pk, name=name) for pk, name in REGIONS]
    )

    with schema_editor.connection.cursor() as cursor:
        for sequence, minimum in SEQUENCE_FLOORS:
            qualified_name = f"public.{schema_editor.quote_name(sequence)}"
            cursor.execute(f"SELECT last_value, is_called FROM {qualified_name}")
            last_value, is_called = cursor.fetchone()
            if last_value + is_called < minimum:
                # RESTART is transactional. Leave an already higher counter intact.
                schema_editor.execute(
                    f"ALTER SEQUENCE {qualified_name} RESTART WITH {minimum}"
                )


class Migration(migrations.Migration):
    dependencies = [("catalog", "0002_remove_source_pattern_index")]
    operations = [migrations.RunPython(seed_reference_data)]
