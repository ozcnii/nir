from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]

    operations = [
        # Django adds a text_pattern_ops index for LIKE searches on text primary keys.
        migrations.RunSQL(
            sql="DROP INDEX sources_code_354b3317_like",
            reverse_sql=(
                "CREATE INDEX sources_code_354b3317_like ON sources (code text_pattern_ops)"
            ),
        ),
    ]
