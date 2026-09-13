"""Phase 1 payment-plan schema and cross-row domain invariants."""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from memberships.models import (
    MembershipPackage,
    MembershipPackagePriceOption,
    MembershipPaymentPlan,
    MembershipPaymentPlanInstallment,
    MembershipPaymentType,
    QuotaPolicy,
)
from memberships.payment_plans import (
    PaymentPlanValidationError,
    validate_payment_plan_configuration,
)


@pytest.fixture
def price_option():
    package = MembershipPackage.objects.create(name="CSA", quota_policy=QuotaPolicy.EXPIRE)
    return MembershipPackagePriceOption.objects.create(
        package=package, duration_months=3, monthly_price_vnd=1_000_000
    )


def make_plan(price_option, *, payment_type=MembershipPaymentType.FULL, count=1, total=3_000_000):
    return MembershipPaymentPlan(
        price_option=price_option,
        name=f"{payment_type}-{count}",
        payment_type=payment_type,
        installment_count=count,
        total_amount=Decimal(total),
    )


def rows(plan, amounts, cycles, sequences=None):
    return [
        MembershipPaymentPlanInstallment(
            payment_plan=plan,
            sequence=sequence,
            amount=amount,
            cycle_count=cycle,
        )
        for sequence, amount, cycle in zip(
            sequences or range(1, len(amounts) + 1), amounts, cycles, strict=True
        )
    ]


@pytest.mark.django_db
def test_full_plan_is_valid_and_is_inactive_until_configured(price_option):
    plan = make_plan(price_option)
    installments = rows(plan, [Decimal("3000000")], [3])
    validate_payment_plan_configuration(plan, installments)
    plan.save()
    MembershipPaymentPlanInstallment.objects.bulk_create(installments)
    validate_payment_plan_configuration(plan)
    assert plan.is_active is False
    assert plan.installments.count() == 1


@pytest.mark.django_db
def test_installment_plan_and_multiple_plans_per_option(price_option):
    full = make_plan(price_option)
    full.save()
    plan = make_plan(
        price_option, payment_type=MembershipPaymentType.INSTALLMENT, count=2, total=3_100_000
    )
    installments = rows(plan, [Decimal("1550000"), Decimal("1550000")], [1, 2])
    validate_payment_plan_configuration(plan, installments)
    plan.save()
    MembershipPaymentPlanInstallment.objects.bulk_create(installments)
    assert price_option.payment_plans.count() == 2
    assert [item.sequence for item in plan.installments.all()] == [1, 2]


@pytest.mark.django_db
def test_one_month_plan_only_allows_one_full_payment_and_one_cycle(price_option):
    price_option.duration_months = 1
    price_option.save(update_fields=["duration_months"])
    full = make_plan(price_option, total=1_000_000)
    validate_payment_plan_configuration(full, rows(full, [Decimal("1000000")], [1]))

    installment = make_plan(
        price_option,
        payment_type=MembershipPaymentType.INSTALLMENT,
        count=2,
        total=1_000_000,
    )
    with pytest.raises(PaymentPlanValidationError, match="payment_plan_installment_not_allowed"):
        validate_payment_plan_configuration(
            installment,
            rows(installment, [Decimal("500000"), Decimal("500000")], [1, 1]),
        )
    with pytest.raises(PaymentPlanValidationError, match="payment_plan_cycle_total_mismatch"):
        validate_payment_plan_configuration(full, rows(full, [Decimal("1000000")], [2]))
    with pytest.raises(PaymentPlanValidationError, match="payment_plan_amount_total_mismatch"):
        validate_payment_plan_configuration(full, rows(full, [Decimal("900000")], [1]))


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"total_amount": Decimal("0")}, "payment_plan_total_invalid"),
        ({"total_amount": Decimal("-1")}, "payment_plan_total_invalid"),
        (
            {"payment_type": MembershipPaymentType.FULL, "installment_count": 2},
            "payment_plan_installment_count_invalid",
        ),
        (
            {"payment_type": MembershipPaymentType.INSTALLMENT, "installment_count": 1},
            "payment_plan_installment_count_invalid",
        ),
    ],
)
def test_invalid_plan_values_are_rejected(price_option, change, code):
    plan = make_plan(price_option)
    for field, value in change.items():
        setattr(plan, field, value)
    with pytest.raises(PaymentPlanValidationError, match=code):
        validate_payment_plan_configuration(plan, rows(plan, [Decimal("3000000")], [3]))


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("amounts", "cycles", "sequences", "code"),
    [
        ([Decimal("0"), Decimal("3000000")], [1, 2], None, "payment_plan_amount_invalid"),
        ([Decimal("-1"), Decimal("3000001")], [1, 2], None, "payment_plan_amount_invalid"),
        (
            [Decimal("1000000"), Decimal("2000000")],
            [0, 3],
            None,
            "payment_plan_cycle_count_invalid",
        ),
        (
            [Decimal("1000000"), Decimal("2000000")],
            [1, 2],
            [1, 1],
            "payment_plan_sequence_duplicate",
        ),
        ([Decimal("1000000"), Decimal("2000000")], [1, 2], [1, 3], "payment_plan_sequence_gap"),
        (
            [Decimal("1000000"), Decimal("1999999")],
            [1, 2],
            None,
            "payment_plan_amount_total_mismatch",
        ),
        (
            [Decimal("1000000"), Decimal("2000000")],
            [1, 1],
            None,
            "payment_plan_cycle_total_mismatch",
        ),
    ],
)
def test_invalid_installment_rows_are_rejected(price_option, amounts, cycles, sequences, code):
    plan = make_plan(price_option, payment_type=MembershipPaymentType.INSTALLMENT, count=2)
    with pytest.raises(PaymentPlanValidationError, match=code):
        validate_payment_plan_configuration(plan, rows(plan, amounts, cycles, sequences))


@pytest.mark.django_db
def test_full_plan_with_extra_installment_and_installment_count_mismatch(price_option):
    full = make_plan(price_option)
    with pytest.raises(PaymentPlanValidationError, match="payment_plan_installment_count_mismatch"):
        validate_payment_plan_configuration(
            full, rows(full, [Decimal("1500000"), Decimal("1500000")], [1, 2])
        )
    split = make_plan(price_option, payment_type=MembershipPaymentType.INSTALLMENT, count=2)
    with pytest.raises(PaymentPlanValidationError, match="payment_plan_installment_count_mismatch"):
        validate_payment_plan_configuration(split, rows(split, [Decimal("3000000")], [3]))


@pytest.mark.django_db
def test_decimal_vnd_is_exact_and_fractional_amount_is_rejected(price_option):
    plan = make_plan(price_option, total=3_000_001)
    validate_payment_plan_configuration(plan, rows(plan, [Decimal("3000001")], [3]))
    with pytest.raises(PaymentPlanValidationError, match="payment_plan_amount_invalid"):
        validate_payment_plan_configuration(plan, rows(plan, [Decimal("3000001.1")], [3]))


@pytest.mark.django_db
def test_database_constraints_allow_historical_plan_duplicates(price_option):
    full = make_plan(price_option)
    full.save()
    make_plan(price_option).save()  # Same option/name/type/count is historical data.
    duplicate_name = make_plan(
        price_option, payment_type=MembershipPaymentType.INSTALLMENT, count=2
    )
    duplicate_name.name = full.name
    duplicate_name.save()
    duplicate_type = make_plan(price_option)
    duplicate_type.name = "Another full payment"
    duplicate_type.save()
    assert MembershipPaymentPlan.objects.filter(price_option=price_option).count() == 4
    with pytest.raises(IntegrityError), transaction.atomic():
        make_plan(price_option, total=0).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_plan(price_option, count=2).save()
    first = MembershipPaymentPlanInstallment.objects.create(
        payment_plan=full, sequence=1, amount=Decimal("3000000"), cycle_count=3
    )
    assert first.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipPaymentPlanInstallment.objects.create(
            payment_plan=full, sequence=1, amount=Decimal("1"), cycle_count=1
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipPaymentPlanInstallment.objects.create(
            payment_plan=full, sequence=2, amount=Decimal("0"), cycle_count=1
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipPaymentPlanInstallment.objects.create(
            payment_plan=full, sequence=2, amount=Decimal("1"), cycle_count=0
        )


@pytest.mark.django_db
def test_legacy_price_option_needs_no_payment_plan(price_option):
    assert price_option.payment_plans.count() == 0
    assert price_option.total_price_vnd == 3_000_000
