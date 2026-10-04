"""固化冻结相关验收测试：

- 已固化后补写时长（PATCH/PUT/POST）一律被拒，时长不变、卷态停留已固化
- 已固化卷不能退回浸渍中/原布
- 保存被拒时不得产生 cured -> dipping 回退
- 两名浸胶工交叉改同一已固化卷的时长，两笔都被挡
- 浸渍中卷改时长正常，且不会改动卷态
"""

import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework import serializers as drf_serializers
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from core.models import ClothRoll, DipRun, Loft
from core.views import DipRunViewSet


class CureFreezeTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="worker1", password="x")
        self.client.force_authenticate(self.user)
        self.loft = Loft.objects.create(name="一车间")
        self.cured_roll = ClothRoll.objects.create(
            loft=self.loft, roll_code="C-1", status=ClothRoll.STATUS_CURED
        )
        now = timezone.now()
        self.cured_dip = DipRun.objects.create(
            roll=self.cured_roll,
            started_at=now - timedelta(days=1),
            resin_pct=Decimal("28.00"),
            cure_hours=Decimal("14.00"),
        )
        self.dipping_roll = ClothRoll.objects.create(
            loft=self.loft, roll_code="D-1", status=ClothRoll.STATUS_DIPPING
        )
        self.dipping_dip = DipRun.objects.create(
            roll=self.dipping_roll,
            started_at=now - timedelta(hours=6),
            resin_pct=Decimal("27.00"),
            cure_hours=None,
        )

    def _reload(self, *objs):
        return [type(o).objects.get(pk=o.pk) for o in objs]

    def test_patch_cure_hours_on_cured_roll_rejected(self):
        resp = self.client.patch(
            f"/api/dips/{self.cured_dip.id}/",
            {"cureHours": "99.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cureHours", resp.data)

        dip, roll = self._reload(self.cured_dip, self.cured_roll)
        self.assertEqual(dip.cure_hours, Decimal("14.00"))
        self.assertEqual(roll.status, ClothRoll.STATUS_CURED)

    def test_patch_other_field_on_cured_roll_also_rejected(self):
        # 不能借改备注/树脂等字段绕开冻结
        resp = self.client.patch(
            f"/api/dips/{self.cured_dip.id}/",
            {"resinPct": "12.34"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        dip, roll = self._reload(self.cured_dip, self.cured_roll)
        self.assertEqual(dip.resin_pct, Decimal("28.00"))
        self.assertEqual(roll.status, ClothRoll.STATUS_CURED)

    def test_put_cure_hours_on_cured_roll_rejected(self):
        resp = self.client.put(
            f"/api/dips/{self.cured_dip.id}/",
            {
                "rollId": self.cured_roll.id,
                "startedAt": self.cured_dip.started_at.isoformat(),
                "resinPct": "28.00",
                "cureHours": "99.00",
                "notes": "",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        dip, roll = self._reload(self.cured_dip, self.cured_roll)
        self.assertEqual(dip.cure_hours, Decimal("14.00"))
        self.assertEqual(roll.status, ClothRoll.STATUS_CURED)

    def test_post_new_dip_for_cured_roll_rejected(self):
        resp = self.client.post(
            "/api/dips/",
            {
                "rollId": self.cured_roll.id,
                "startedAt": timezone.now().isoformat(),
                "resinPct": "28.00",
                "cureHours": "20.00",
                "notes": "",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(DipRun.objects.filter(roll=self.cured_roll).count(), 1)
        self.assertEqual(ClothRoll.objects.get(pk=self.cured_roll.pk).status, "cured")

    def test_cured_roll_cannot_revert_to_dipping(self):
        resp = self.client.patch(
            f"/api/rolls/{self.cured_roll.id}/",
            {"status": "dipping"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("status", resp.data)
        self.assertEqual(ClothRoll.objects.get(pk=self.cured_roll.pk).status, "cured")

    def test_cured_roll_cannot_revert_to_raw(self):
        resp = self.client.patch(
            f"/api/rolls/{self.cured_roll.id}/",
            {"status": "raw"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ClothRoll.objects.get(pk=self.cured_roll.pk).status, "cured")

    def test_patch_hours_on_dipping_roll_succeeds_and_keeps_status(self):
        resp = self.client.patch(
            f"/api/dips/{self.dipping_dip.id}/",
            {"cureHours": "10.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        dip, roll = self._reload(self.dipping_dip, self.dipping_roll)
        self.assertEqual(dip.cure_hours, Decimal("10.00"))
        self.assertEqual(roll.status, ClothRoll.STATUS_DIPPING)

    def test_full_cure_flow_still_works(self):
        # 浸渍中 -> 补够时长 -> 标已固化
        resp = self.client.patch(
            f"/api/dips/{self.dipping_dip.id}/",
            {"cureHours": "12.50"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.patch(
            f"/api/rolls/{self.dipping_roll.id}/",
            {"status": "cured"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(ClothRoll.objects.get(pk=self.dipping_roll.pk).status, "cured")

    def test_cured_roll_keeps_status_when_editing_notes(self):
        # 台账里编辑已固化卷的备注（PATCH 同时回带 status=cured）应放行
        resp = self.client.patch(
            f"/api/rolls/{self.cured_roll.id}/",
            {"notes": "归档核对", "status": "cured"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        roll = ClothRoll.objects.get(pk=self.cured_roll.pk)
        self.assertEqual(roll.status, ClothRoll.STATUS_CURED)
        self.assertEqual(roll.notes, "归档核对")

    def test_new_roll_cannot_be_created_as_cured(self):
        resp = self.client.post(
            "/api/rolls/",
            {
                "loftId": self.loft.id,
                "rollCode": "X-1",
                "status": "cured",
                "fabricWeightGsm": 400,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("status", resp.data)

    def test_perform_update_rechecks_status_inside_lock(self):
        # 模拟竞争窗口：serializer 校验时还是浸渍中，拿锁前另一事务已标固化
        view = DipRunViewSet()
        serializer = mock.Mock()
        serializer.instance = self.dipping_dip
        serializer.validated_data = {}
        ClothRoll.objects.filter(pk=self.dipping_roll.pk).update(
            status=ClothRoll.STATUS_CURED
        )
        with self.assertRaises(drf_serializers.ValidationError):
            view.perform_update(serializer)
        serializer.save.assert_not_called()


class ConcurrentHoursWriteTests(TransactionTestCase):
    """两名浸胶工同时给同一已固化卷改时长：两笔都挡，卷态停留已固化。"""

    def setUp(self):
        self.user = User.objects.create_user(username="worker1", password="x")
        loft = Loft.objects.create(name="一车间")
        self.roll = ClothRoll.objects.create(
            loft=loft, roll_code="C-9", status=ClothRoll.STATUS_CURED
        )
        self.dip = DipRun.objects.create(
            roll=self.roll,
            started_at=timezone.now() - timedelta(days=1),
            resin_pct=Decimal("30.00"),
            cure_hours=Decimal("14.50"),
        )

    def test_two_concurrent_patches_both_blocked(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(hours):
            from rest_framework.test import APIClient

            client = APIClient()
            client.force_authenticate(self.user)
            try:
                barrier.wait(timeout=10)
                resp = client.patch(
                    f"/api/dips/{self.dip.id}/",
                    {"cureHours": hours},
                    format="json",
                )
                outcomes.append(resp.status_code)
            finally:
                connection.close()

        t1 = threading.Thread(target=worker, args=("20.00",))
        t2 = threading.Thread(target=worker, args=("21.00",))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertEqual(sorted(outcomes), [400, 400])
        self.dip.refresh_from_db()
        self.roll.refresh_from_db()
        self.assertEqual(self.dip.cure_hours, Decimal("14.50"))
        self.assertEqual(self.roll.status, ClothRoll.STATUS_CURED)
