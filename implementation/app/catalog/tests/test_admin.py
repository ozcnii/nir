from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import ClassVar

from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth.models import Permission, User
from django.db import transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from app.catalog.locks import CollectionBusy, run_session
from app.catalog.models import Property, PropertyType, Region, Source
from app.catalog.reference_defaults import APARTMENT_TYPE_ID, SAMARA_REGION_ID
from app.collection.browser import CollectionError
from app.collection.search import collect_search


class AdminFormsTests(TestCase):
    admin: ClassVar[User]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.admin = User.objects.create_superuser("operator")

    def test_admin_session_permissions_and_csrf(self) -> None:
        # Arrange
        region = Region.objects.create(name="Запись для проверки прав")
        staff = User.objects.create_user("reader", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_region"))
        listing = reverse("admin:catalog_region_changelist")
        delete = reverse("admin:catalog_region_delete", args=[region.pk])
        source = reverse("admin:catalog_source_change", args=["cian"])
        bulk = {
            "action": "delete_selected",
            "_selected_action": region.pk,
            "post": "yes",
        }

        # Act
        guest_get = self.client.get(listing)
        guest_post = self.client.post(delete, {"post": "yes"})
        self.client.force_login(staff)
        staff_get = self.client.get(listing)
        staff_delete = self.client.post(delete, {"post": "yes"})
        staff_bulk = self.client.post(listing, bulk)
        staff_change = self.client.post(source, {})
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        csrf_client.get(source)
        invalid_csrf = csrf_client.post(source, {"csrfmiddlewaretoken": "A" * 64})

        # Assert
        self.assertRedirects(
            guest_get,
            f"{reverse('admin:login')}?next={listing}",
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            guest_post,
            f"{reverse('admin:login')}?next={delete}",
            fetch_redirect_response=False,
        )
        self.assertEqual(staff_get.status_code, 200)
        for response in (staff_delete, staff_bulk, staff_change, invalid_csrf):
            self.assertEqual(response.status_code, 403)
        self.assertTrue(Region.objects.filter(pk=region.pk).exists())
        self.assertTrue(Source.objects.get(pk="cian").enabled)

    def test_source_form_changes_only_enabled_for_supported_code(self) -> None:
        # Arrange
        self.client.force_login(self.admin)
        Source.objects.create(code="fixture", enabled=True)
        listing = reverse("admin:catalog_source_changelist")
        url = reverse("admin:catalog_source_change", args=["cian"])

        # Act
        overview = self.client.get(listing)
        changed = self.client.post(url, {"code": "other"})
        forbidden = (
            self.client.post(reverse("admin:catalog_source_add"), {"code": "other"}),
            self.client.post(
                reverse("admin:catalog_source_delete", args=["cian"]), {"post": "yes"}
            ),
        )

        # Assert
        self.assertContains(overview, "Циан")
        self.assertContains(overview, "Домклик")
        self.assertContains(overview, "Авито")
        self.assertNotContains(overview, "fixture")
        self.assertRedirects(changed, listing)
        for response in forbidden:
            self.assertEqual(response.status_code, 403)
        self.assertFalse(Source.objects.get(pk="cian").enabled)
        self.assertFalse(Source.objects.filter(pk="other").exists())

    def test_directory_forms_preserve_ids_and_reject_invalid_names(self) -> None:
        # Arrange
        self.client.force_login(self.admin)
        for model, slug in ((Region, "region"), (PropertyType, "propertytype")):
            with self.subTest(directory=slug):
                add = reverse(f"admin:catalog_{slug}_add")
                listing = reverse(f"admin:catalog_{slug}_changelist")
                name = "Новая запись"

                # Act
                created = self.client.post(add, {"name": name, "id": "54321"})
                entry = model.objects.get(name=name)
                change = reverse(f"admin:catalog_{slug}_change", args=[entry.pk])
                duplicate = self.client.post(add, {"name": name})
                blank = self.client.post(change, {"name": "   "})
                renamed = self.client.post(
                    change, {"name": "Новое название", "id": "54321"}
                )

                # Assert
                self.assertRedirects(created, listing)
                self.assertEqual(duplicate.status_code, 200)
                self.assertTrue(duplicate.context["adminform"].form.errors)
                self.assertEqual(blank.status_code, 200)
                self.assertTrue(blank.context["adminform"].form.errors)
                self.assertRedirects(renamed, listing)
                self.assertNotEqual(entry.pk, 54321)
                self.assertEqual(model.objects.get(pk=entry.pk).name, "Новое название")

    def test_used_records_are_protected_in_single_and_bulk_confirmation(self) -> None:
        # Arrange
        self.client.force_login(self.admin)
        region = Region.objects.create(name="Используемый регион")
        kind = PropertyType.objects.create(name="Используемый вид")
        spare = Region.objects.create(name="Свободный регион")
        linked_property = Property.objects.create(region=region, property_type=kind)

        # Act
        attempted = (
            self.client.post(
                reverse("admin:catalog_propertytype_delete", args=[kind.pk]),
                {"post": "yes"},
            ),
            self.client.post(
                reverse("admin:catalog_region_changelist"),
                {
                    "action": "delete_selected",
                    "_selected_action": [region.pk, spare.pk],
                    "post": "yes",
                },
            ),
        )

        # Assert
        for response in attempted:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["protected"])
        self.assertTrue(Property.objects.filter(pk=linked_property.pk).exists())
        self.assertTrue(Region.objects.filter(pk=spare.pk).exists())
        self.assertFalse(LogEntry.objects.filter(action_flag=DELETION).exists())


class ReferenceDeletionTests(TransactionTestCase):
    def test_collector_lock_blocks_single_and_bulk_without_deletion_logs(self) -> None:
        # Arrange: a collection session owns the key on a separate connection.
        self.client.force_login(User.objects.create_superuser("operator"))
        region = Region.objects.create(name="Регион для удаления")
        kind = PropertyType.objects.create(name="Вид для удаления")
        single = reverse("admin:catalog_region_delete", args=[region.pk])
        bulk = reverse("admin:catalog_propertytype_changelist")
        action = {
            "action": "delete_selected",
            "_selected_action": kind.pk,
            "post": "yes",
        }
        acquired = Event()
        released = Event()

        def hold_collection() -> bool:
            with run_session():
                acquired.set()
                return released.wait(timeout=10)

        with ThreadPoolExecutor(max_workers=1) as worker:
            holder = worker.submit(hold_collection)
            try:
                self.assertTrue(acquired.wait(timeout=5))

                # Act
                single_blocked = self.client.post(single, {"post": "yes"}, follow=True)
                bulk_blocked = self.client.post(bulk, action, follow=True)

                # Assert
                for response in (single_blocked, bulk_blocked):
                    self.assertContains(
                        response, "Удаление недоступно, пока выполняется сбор."
                    )
                self.assertTrue(Region.objects.filter(pk=region.pk).exists())
                self.assertTrue(PropertyType.objects.filter(pk=kind.pk).exists())
                self.assertFalse(LogEntry.objects.filter(action_flag=DELETION).exists())
            finally:
                released.set()
            self.assertTrue(holder.result(timeout=5))

        # Act
        single_allowed = self.client.post(single, {"post": "yes"})
        bulk_allowed = self.client.post(bulk, action)

        # Assert
        self.assertEqual(single_allowed.status_code, 302)
        self.assertEqual(bulk_allowed.status_code, 302)
        self.assertFalse(Region.objects.filter(pk=region.pk).exists())
        self.assertFalse(PropertyType.objects.filter(pk=kind.pk).exists())

    def test_collection_checks_scope_after_committed_admin_deletion(self) -> None:
        # Arrange
        self.client.force_login(User.objects.create_superuser("operator"))
        Region.objects.get_or_create(
            pk=SAMARA_REGION_ID, defaults={"name": "Самарская область"}
        )
        PropertyType.objects.get_or_create(
            pk=APARTMENT_TYPE_ID, defaults={"name": "Квартира"}
        )
        single = reverse("admin:catalog_region_delete", args=[SAMARA_REGION_ID])

        def start_collection() -> dict[str, object]:
            return collect_search(
                ["cian"], SAMARA_REGION_ID, APARTMENT_TYPE_ID, "sale", lambda: False
            )

        with transaction.atomic(), ThreadPoolExecutor(max_workers=1) as worker:
            # Act
            deleted = self.client.post(single, {"post": "yes"})
            competing_error = worker.submit(start_collection).exception(timeout=5)

            # Assert: the admin deletion owns the key until commit.
            self.assertEqual(deleted.status_code, 302)
            self.assertIsInstance(competing_error, CollectionBusy)

        # Act
        with self.assertRaisesRegex(CollectionError, "^unknown_collection_scope$"):
            start_collection()

        # Assert: the next collection saw the committed deletion.
        self.assertFalse(Region.objects.filter(pk=SAMARA_REGION_ID).exists())
