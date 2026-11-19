"""ACL acceptance on a dedicated PostgreSQL database with committed fixtures."""

import os

import psycopg
from fixture_database import require_fixture_database
from psycopg.conninfo import make_conninfo

SOURCE = "fixture"
ORIGINAL_HISTORY = [(0, "listed")]
INSERT_AD = """
    INSERT INTO ads
        (property_id, source_code, external_id, url, deal_type, is_active,
         observed_at, photos, photos_observed_at)
    VALUES (1, 'fixture', 'ref', 'https://example.test/one', 'sale', true, now(),
            '["https://example.test/photo.jpg"]', now())
"""


def check_postgres() -> None:
    # Arrange
    credentials = make_conninfo(
        dbname=os.environ["DB_NAME"],
        host=os.getenv("DB_HOST", "db"),
        port=os.getenv("DB_PORT", "5432"),
        require_auth="scram-sha-256",
    )

    with psycopg.connect(
        credentials,
        user="app",
        password=os.environ["DB_APP_PASSWORD"],
        autocommit=True,
    ) as app:
        with app.cursor() as cursor:
            require_fixture_database(cursor)

        # Arrange
        description = "committed"
        # Act
        with app.transaction():
            app.execute("UPDATE sources SET enabled = false WHERE code = %s", (SOURCE,))
            app.execute("UPDATE regions SET name = 'Renamed region' WHERE id = 1")
            app.execute("INSERT INTO regions (name) VALUES ('Unused region')")
            app.execute("DELETE FROM regions WHERE name = 'Unused region'")
            test_type = app.execute(
                "INSERT INTO property_types (name) VALUES ('Type') RETURNING id"
            ).fetchone()
            assert test_type is not None
            app.execute(
                """
                INSERT INTO properties (region_id, property_type_id, rooms, floor)
                VALUES (1, %s, 0, -1)
                """,
                test_type,
            )
            app.execute("UPDATE properties SET floor = 0 WHERE id = 1")
            app.execute(INSERT_AD)
            app.execute("UPDATE ads SET description = %s WHERE id = 1", (description,))
            app.execute("""
                INSERT INTO price_history (ad_id, observed_at, price_rub, price_kind)
                VALUES (1, now(), 0, 'listed')
            """)

        # Assert
        assert app.execute(
            "SELECT floor, rooms FROM properties WHERE id = 1"
        ).fetchone() == (0, 0)

        # Arrange
        denied = (
            ("UPDATE price_history SET price_rub = 100 WHERE id = 1", "42501"),
            ("DELETE FROM price_history WHERE id = 1", "42501"),
            ("TRUNCATE price_history", "42501"),
            ("UPDATE ads SET external_id = 'changed' WHERE id = 1", "42501"),
            ("UPDATE sources SET code = 'changed' WHERE code = 'fixture'", "42501"),
            ("DELETE FROM sources WHERE code = 'fixture'", "42501"),
            ("DELETE FROM properties WHERE id = 1", "42501"),
            ("DELETE FROM ads WHERE id = 1", "42501"),
            ("SET ROLE migrator", "42501"),
            ("SELECT setval('price_history_id_seq', 99)", "42501"),
            ("CREATE TABLE public.no_access (id integer)", "42501"),
            ("CREATE TEMP TABLE no_access (id integer)", "42501"),
            ("ALTER TABLE price_history OWNER TO app", "42501"),
            ("DROP TABLE price_history", "42501"),
            ("DELETE FROM regions WHERE id = 1", "23503"),
            ("UPDATE properties SET id = 999 WHERE id = 1", "23503"),
            ("UPDATE properties SET region_id = 999 WHERE id = 1", "23503"),
            ("UPDATE properties SET latitude = 40 WHERE id = 1", "23514"),
            (
                "UPDATE properties SET latitude = 91, longitude = 0 WHERE id = 1",
                "23514",
            ),
            ("UPDATE properties SET area_m2 = 0 WHERE id = 1", "23514"),
            ("UPDATE properties SET floors_total = 0 WHERE id = 1", "23514"),
            ("UPDATE ads SET url = ' ' WHERE id = 1", "23514"),
            (
                """INSERT INTO ads (property_id, source_code, external_id, url, deal_type,
                         is_active, observed_at, photos)
                VALUES (1, 'fixture', ' ', 'https://example.test/two', 'sale',
                        true, now(), '[]')""",
                "23514",
            ),
            ("UPDATE ads SET photos = '[0]' WHERE id = 1", "23514"),
        )
        for statement, sqlstate in denied:
            # Arrange
            expected_description = ("committed",)
            # Act
            try:
                with app.transaction():
                    app.execute(
                        "UPDATE ads SET description = 'rolled back' WHERE id = 1"
                    )
                    app.execute(statement)
            except psycopg.Error as error:
                # Assert
                assert error.sqlstate == sqlstate, (statement, error.sqlstate, sqlstate)
            else:
                raise AssertionError(f"Unexpected successful statement: {statement}")
            assert (
                app.execute("SELECT description FROM ads WHERE id = 1").fetchone()
                == expected_description
            )
            assert app.execute(
                "SELECT price_rub, price_kind FROM price_history"
            ).fetchall() == (ORIGINAL_HISTORY)

        # Arrange
        target_region = 1
        # Act
        region = app.execute(
            "SELECT name FROM regions WHERE id = %s", (target_region,)
        ).fetchone()
        source = app.execute(
            "SELECT enabled FROM sources WHERE code = %s", (SOURCE,)
        ).fetchone()
        # Assert
        assert region == ("Renamed region",)
        assert source == (False,)
    print(f"F25: signed-in app verified committed data and {len(denied)} denied writes")


if __name__ == "__main__":
    check_postgres()
