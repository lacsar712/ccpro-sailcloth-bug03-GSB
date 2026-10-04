from django.db import transaction
from django.db.models import Count
from rest_framework import serializers, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ClothRoll, DipRun, Loft
from .rules import can_mark_roll_cured
from .serializers import ClothRollSerializer, DipRunSerializer, LoftSerializer


class LoftViewSet(viewsets.ModelViewSet):
    queryset = Loft.objects.annotate(roll_count=Count("rolls")).all()
    serializer_class = LoftSerializer


class ClothRollViewSet(viewsets.ModelViewSet):
    serializer_class = ClothRollSerializer

    def perform_update(self, serializer):
        # 锁住本卷行，与浸渍记录写入串行化，避免标固化和改时长交叉
        with transaction.atomic():
            locked = ClothRoll.objects.select_for_update().get(pk=serializer.instance.pk)
            new_status = serializer.validated_data.get("status")
            if (
                locked.status == ClothRoll.STATUS_CURED
                and new_status is not None
                and new_status != ClothRoll.STATUS_CURED
            ):
                raise serializers.ValidationError(
                    {"status": "布卷已固化，卷态锁定，不能退回浸渍中或原布"}
                )
            if (
                new_status == ClothRoll.STATUS_CURED
                and locked.status != ClothRoll.STATUS_CURED
            ):
                # 锁内用最新时长复核：防止校验通过后时长刚被改小
                ok, msg = can_mark_roll_cured(locked)
                if not ok:
                    raise serializers.ValidationError({"status": msg})
            serializer.save()

    def get_queryset(self):
        qs = ClothRoll.objects.select_related("loft").all()
        loft_id = self.request.query_params.get("loftId")
        status = self.request.query_params.get("status")
        if loft_id:
            qs = qs.filter(loft_id=loft_id)
        if status:
            qs = qs.filter(status=status)
        return qs


class DipRunViewSet(viewsets.ModelViewSet):
    serializer_class = DipRunSerializer
    http_method_names = ["get", "post", "patch", "put", "head", "options"]

    def _locked_roll(self, roll):
        return ClothRoll.objects.select_for_update().get(pk=roll.pk)

    def perform_create(self, serializer):
        # 行锁内复核：堵住「校验时浸渍中、提交时刚被标固化」的交叉窗口
        with transaction.atomic():
            roll = self._locked_roll(serializer.validated_data["roll"])
            if roll.status == ClothRoll.STATUS_CURED:
                raise serializers.ValidationError(
                    {"rollId": "该布卷已固化，不能再补登浸渍记录或固化时长"}
                )
            serializer.save()

    def perform_update(self, serializer):
        with transaction.atomic():
            target_roll = serializer.validated_data.get("roll", serializer.instance.roll)
            roll = self._locked_roll(target_roll)
            if roll.status == ClothRoll.STATUS_CURED:
                raise serializers.ValidationError(
                    {"cureHours": "该布卷已固化，固化时长已锁定，不能再修改"}
                )
            serializer.save()

    def get_queryset(self):
        qs = DipRun.objects.select_related("roll", "roll__loft").all()
        roll_id = self.request.query_params.get("rollId")
        if roll_id:
            qs = qs.filter(roll_id=roll_id)
        return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dashboard_stats(request):
    data = {
        "loftCount": Loft.objects.count(),
        "rawRollCount": ClothRoll.objects.filter(status=ClothRoll.STATUS_RAW).count(),
        "dippingRollCount": ClothRoll.objects.filter(
            status=ClothRoll.STATUS_DIPPING
        ).count(),
        "curedRollCount": ClothRoll.objects.filter(status=ClothRoll.STATUS_CURED).count(),
        "dipRunCount": DipRun.objects.count(),
    }
    return Response(data)
