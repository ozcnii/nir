from argparse import ArgumentParser
from typing import cast

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from psycopg import Cursor

from catalog.models import PropertyType, Region, Source
from catalog.reference_data_v1 import (
    PROPERTY_TYPES,
    REGIONS,
    SEQUENCE_FLOORS,
    SOURCE_CODES,
)
from config.db_roles import RoleError, validate_owners


class Command(BaseCommand):
    help = "Restore explicitly selected standard reference rows as migrator."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--source", dest="sources", action="append", default=[])
        parser.add_argument(
            "--property-type",
            dest="property_types",
            action="append",
            type=int,
            default=[],
        )
        parser.add_argument(
            "--region", dest="regions", action="append", type=int, default=[]
        )

    def handle(self, *args: object, **options: object) -> None:
        sources = dict.fromkeys(cast(list[str], options["sources"]))
        property_types = dict.fromkeys(cast(list[int], options["property_types"]))
        regions = dict.fromkeys(cast(list[int], options["regions"]))
        type_names = {pk: name for pk, _, name in PROPERTY_TYPES}
        region_names = dict(REGIONS)

        if not (sources or property_types or regions):
            raise CommandError(
                "Select at least one --source, --property-type or --region."
            )
        for code in sources:
            if code not in SOURCE_CODES:
                raise CommandError(f"Unknown standard source {code!r}.")
        for pk in property_types:
            if pk not in type_names:
                raise CommandError(f"Unknown standard property type ID {pk}.")
        for pk in regions:
            if pk not in region_names:
                raise CommandError(f"Unknown standard region ID {pk}.")

        results = []
        with transaction.atomic():
            try:
                with connection.cursor() as cursor:
                    validate_owners(
                        cast(Cursor[tuple[object, ...]], cursor.cursor), "migrator"
                    )
                    if ("catalog", "0003_seed_reference_data") not in (
                        MigrationRecorder(connection).applied_migrations()
                    ):
                        raise CommandError(
                            "The initial reference data migration is not applied."
                        )
                    for sequence, minimum in SEQUENCE_FLOORS:
                        quoted_name = connection.ops.quote_name(sequence)
                        cursor.execute(
                            f"SELECT last_value, is_called FROM public.{quoted_name}"
                        )
                        last_value, is_called = cursor.fetchone()
                        if last_value + is_called < minimum:
                            raise CommandError(
                                f"Sequence {sequence} must have a next ID of at least {minimum}."
                            )
            except RoleError as error:
                raise CommandError(str(error)) from error

            for code in sources:
                _, created = Source.objects.get_or_create(
                    pk=code, defaults={"enabled": True}
                )
                results.append((f"sources {code}", created))
            for model, ids, names in (
                (PropertyType, property_types, type_names),
                (Region, regions, region_names),
            ):
                for pk in ids:
                    created = restore_named_row(model, pk, names[pk])
                    results.append((f"{model._meta.db_table} {pk}", created))

        for row, created in results:
            action = "Restored" if created else "Already exists"
            self.stdout.write(f"{action} {row}")


def restore_named_row(
    model: type[PropertyType] | type[Region], pk: int, name: str
) -> bool:
    try:
        _, created = model.objects.get_or_create(pk=pk, defaults={"name": name})
    except IntegrityError as error:
        other_id = model.objects.filter(name=name).values_list("pk", flat=True).first()
        if other_id is None or other_id == pk:
            raise
        raise CommandError(
            f"{model._meta.db_table} name {name!r} for ID {pk} belongs to ID {other_id}."
        ) from error
    return created
