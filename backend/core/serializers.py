from rest_framework import serializers

from .models import ClothRoll, DipRun, Loft
from .rules import can_mark_roll_cured


class LoftSerializer(serializers.ModelSerializer):
    rollCount = serializers.SerializerMethodField()

    class Meta:
        model = Loft
        fields = ("id", "name", "location", "notes", "rollCount", "created_at")
        read_only_fields = ("id", "rollCount", "created_at")

    def get_rollCount(self, obj):
        if hasattr(obj, "roll_count"):
            return obj.roll_count
        return obj.rolls.count()


class ClothRollSerializer(serializers.ModelSerializer):
    loftId = serializers.PrimaryKeyRelatedField(source="loft", queryset=Loft.objects.all())
    rollCode = serializers.CharField(source="roll_code")
    fabricWeightGsm = serializers.IntegerField(source="fabric_weight_gsm", required=False)
    loftName = serializers.CharField(source="loft.name", read_only=True)

    class Meta:
        model = ClothRoll
        fields = (
            "id",
            "loftId",
            "loftName",
            "rollCode",
            "status",
            "fabricWeightGsm",
            "notes",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "loftName", "created_at", "updated_at")

    def validate(self, attrs):
        loft = attrs.get("loft") or getattr(self.instance, "loft", None)
        roll_code = attrs.get("roll_code") or getattr(self.instance, "roll_code", None)
        if loft and roll_code:
            qs = ClothRoll.objects.filter(loft=loft, roll_code=roll_code)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"rollCode": "同一帆布间卷号必须唯一"})

        new_status = attrs.get("status")
        if new_status == ClothRoll.STATUS_CURED and self.instance is None:
            raise serializers.ValidationError({"status": "新建布卷不能直接设为已固化"})
        if new_status is not None and self.instance is not None:
            # 已固化是终态：固化后只能保持已固化，不能退回原布/浸渍中
            if (
                self.instance.status == ClothRoll.STATUS_CURED
                and new_status != ClothRoll.STATUS_CURED
            ):
                raise serializers.ValidationError(
                    {"status": "布卷已固化，卷态锁定，不能退回浸渍中或原布"}
                )
        if (
            new_status == ClothRoll.STATUS_CURED
            and self.instance is not None
            and self.instance.status != ClothRoll.STATUS_CURED
        ):
            roll = self.instance
            # 转入已固化：最近浸渍固化时长必须 ≥ 12 小时
            ok, msg = can_mark_roll_cured(roll)
            if not ok:
                raise serializers.ValidationError({"status": msg})
        return attrs


class DipRunSerializer(serializers.ModelSerializer):
    rollId = serializers.PrimaryKeyRelatedField(
        source="roll", queryset=ClothRoll.objects.all()
    )
    startedAt = serializers.DateTimeField(source="started_at")
    resinPct = serializers.DecimalField(source="resin_pct", max_digits=5, decimal_places=2)
    cureHours = serializers.DecimalField(
        source="cure_hours",
        max_digits=6,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    rollCode = serializers.CharField(source="roll.roll_code", read_only=True)
    loftName = serializers.CharField(source="roll.loft.name", read_only=True)

    class Meta:
        model = DipRun
        fields = (
            "id",
            "rollId",
            "rollCode",
            "loftName",
            "startedAt",
            "resinPct",
            "cureHours",
            "notes",
            "created_at",
        )
        read_only_fields = ("id", "rollCode", "loftName", "created_at")

    def validate(self, attrs):
        if self.instance is not None:
            # 更新时取目标卷（PUT 允许换卷）：所属或目标卷已固化即冻结
            target_roll = attrs.get("roll") or self.instance.roll
            if target_roll.status == ClothRoll.STATUS_CURED:
                raise serializers.ValidationError(
                    {"cureHours": "该布卷已固化，固化时长已锁定，不能再修改"}
                )
        else:
            # 已固化卷不能补登新浸渍（否则会顶掉最近一条、动摇固化依据）
            roll = attrs.get("roll")
            if roll is not None and roll.status == ClothRoll.STATUS_CURED:
                raise serializers.ValidationError(
                    {"rollId": "该布卷已固化，不能再补登浸渍记录"}
                )
        return attrs
