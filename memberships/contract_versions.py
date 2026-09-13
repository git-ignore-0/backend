"""Immutable CSA agreement revisions and private, per-version PDF publication."""

import hashlib

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from memberships.contract_pdfs import (
    ContractPDFDocuments,
    CSAContractPDFError,
    CSAContractPDFSnapshotIncomplete,
    CSAContractPDFStorageError,
    _delete_quietly,
    _storage_key,
    get_contract_pdf_storage,
    render_contract_pdf,
)
from memberships.models import (
    CSAContract,
    CSAContractStatus,
    CSAContractVersion,
    CSAContractVersionReason,
    CSAPurchasePayment,
    CSAPurchasePaymentStatus,
    MembershipPaymentType,
)


def build_contract_version_snapshot(*, contract, reason, actor) -> dict:
    """Read locked payment records and immutable Contract facts, never catalog/plan live data."""

    if reason not in CSAContractVersionReason.values:
        raise CSAContractPDFSnapshotIncomplete("Unknown contract version reason")
    payments = list(
        CSAPurchasePayment.objects.select_for_update()
        .filter(purchase_request_id=contract.purchase_request_id)
        .order_by("installment_sequence", "id")
        if contract.purchase_request_id
        else CSAPurchasePayment.objects.select_for_update()
        .filter(membership_id=contract.membership_id)
        .order_by("installment_sequence", "id")
    )
    if not payments or [payment.installment_sequence for payment in payments] != list(
        range(1, len(payments) + 1)
    ):
        raise CSAContractPDFSnapshotIncomplete("Contract payment schedule is incomplete")
    if contract.purchase_request_id and contract.purchase_request.payment_plan_id:
        expected = contract.purchase_request.payment_installments_snapshot
        actual = [
            {
                "sequence": p.installment_sequence,
                "amount": format(p.amount_due, "f"),
                "cycle_count": p.cycle_count,
            }
            for p in payments
        ]
        if actual != expected:
            raise CSAContractPDFSnapshotIncomplete(
                "Contract payment schedule differs from purchase snapshot"
            )
    total = sum((p.amount_due for p in payments), 0)
    paid = sum(
        (p.amount_paid for p in payments if p.status == CSAPurchasePaymentStatus.CONFIRMED),
        0,
    )
    cycles = sum(p.cycle_count for p in payments)
    paid_cycles = sum(
        p.cycle_count for p in payments if p.status == CSAPurchasePaymentStatus.CONFIRMED
    )
    if total != contract.amount_snapshot or cycles != contract.duration_months_snapshot:
        raise CSAContractPDFSnapshotIncomplete("Contract payment totals do not match snapshot")
    if any(
        p.status == CSAPurchasePaymentStatus.CONFIRMED and p.amount_paid != p.amount_due
        for p in payments
    ):
        raise CSAContractPDFSnapshotIncomplete("Contract confirmed amount is invalid")
    payment_type = (
        contract.purchase_request.payment_type_snapshot
        if contract.purchase_request_id and contract.purchase_request.payment_type_snapshot
        else MembershipPaymentType.FULL
    )
    if contract.status == CSAContractStatus.REVOKED and (
        not contract.revoked_at
        or not contract.revocation_reason.strip()
        or not contract.revoked_by_name_snapshot
    ):
        raise CSAContractPDFSnapshotIncomplete("Contract revocation snapshot is incomplete")
    return {
        "contract": contract,
        "version_reason": reason,
        "created_by": actor,
        "created_by_name_snapshot": (actor.name or actor.email) if actor else "",
        "contract_status": contract.status,
        "membership_status": contract.membership.status,
        "issued_at": contract.issued_at,
        "start_date": contract.start_date,
        "end_date": contract.end_date,
        "revoked_at": contract.revoked_at,
        "revocation_reason": contract.revocation_reason,
        "revoked_by_name_snapshot": contract.revoked_by_name_snapshot or "",
        "package_name_snapshot": contract.package_name_snapshot,
        "price_option_name_snapshot": contract.price_option_name_snapshot,
        "duration_months_snapshot": contract.duration_months_snapshot,
        "amount_snapshot": contract.amount_snapshot,
        "currency_snapshot": contract.currency_snapshot,
        "payment_type": payment_type,
        "total_payment_amount": total,
        "paid_amount": paid,
        "remaining_amount": total - paid,
        "confirmed_installment_count": sum(
            p.status == CSAPurchasePaymentStatus.CONFIRMED for p in payments
        ),
        "installment_count": len(payments),
        "paid_cycles": paid_cycles,
        "total_cycles": cycles,
        "payment_schedule_snapshot": [
            {
                "sequence": p.installment_sequence,
                "amount_due": format(p.amount_due, "f"),
                "amount_paid": format(p.amount_paid, "f") if p.amount_paid is not None else None,
                "cycle_count": p.cycle_count,
                "status": p.status,
                "due_at": p.due_at.isoformat() if p.due_at else None,
                "confirmed_at": p.confirmed_at.isoformat() if p.confirmed_at else None,
            }
            for p in payments
        ],
    }


@transaction.atomic
def issue_contract_version(*, contract_id, reason, actor, storage=None):
    """Publish both locales atomically in DB and return keys for outer rollback compensation."""

    contract = (
        CSAContract.objects.select_for_update()
        .select_related("purchase_request")
        .get(pk=contract_id)
    )
    if contract.current_version_id:
        number = contract.current_version.version_number + 1
    elif reason == CSAContractVersionReason.ISSUED and contract.pdf_generated_at is None:
        number = 1
    elif reason != CSAContractVersionReason.ISSUED and contract.pdf_generated_at is not None:
        # Existing contracts keep their unversioned original/current PDFs, without fake backfill.
        number = contract.pdf_version + 1
    else:
        raise CSAContractPDFSnapshotIncomplete("Contract version state is invalid")
    if reason == CSAContractVersionReason.ISSUED and number != 1:
        raise CSAContractPDFSnapshotIncomplete("Contract has already been issued")
    if (
        reason == CSAContractVersionReason.MEMBERSHIP_REVOKED
        and contract.status != CSAContractStatus.REVOKED
    ):
        raise CSAContractPDFSnapshotIncomplete("Contract is not revoked")
    if (
        reason != CSAContractVersionReason.MEMBERSHIP_REVOKED
        and contract.status != CSAContractStatus.ACTIVE
    ):
        raise CSAContractPDFSnapshotIncomplete("Revoked contract cannot receive active versions")
    snapshot = build_contract_version_snapshot(contract=contract, reason=reason, actor=actor)
    version = CSAContractVersion(version_number=number, **snapshot)
    storage = storage or get_contract_pdf_storage()
    saved = {}
    attempted = []
    try:
        contents = {
            locale: render_contract_pdf(contract, locale, pdf_version=number, version=version)
            for locale in ("vi", "en")
        }
        for locale in ("vi", "en"):
            key = _storage_key(contract, locale, number)
            attempted.append(key)
            saved[locale] = storage.save(key, ContentFile(contents[locale]))
            if saved[locale] != key:
                raise CSAContractPDFStorageError("Contract PDF storage key changed unexpectedly")
        generated_at = timezone.now()
        version.pdf_vi_storage_key = saved["vi"]
        version.pdf_en_storage_key = saved["en"]
        version.pdf_vi_sha256 = hashlib.sha256(contents["vi"]).hexdigest()
        version.pdf_en_sha256 = hashlib.sha256(contents["en"]).hexdigest()
        version.pdf_generated_at = generated_at
        version.save()
        values = {
            "current_version": version,
            "pdf_version": number,
            "pdf_vi_storage_key": version.pdf_vi_storage_key,
            "pdf_en_storage_key": version.pdf_en_storage_key,
            "pdf_vi_sha256": version.pdf_vi_sha256,
            "pdf_en_sha256": version.pdf_en_sha256,
            "pdf_generated_at": generated_at,
            "updated_at": generated_at,
        }
        if number == 1:
            values.update(
                original_pdf_vi_storage_key=version.pdf_vi_storage_key,
                original_pdf_en_storage_key=version.pdf_en_storage_key,
                original_pdf_vi_sha256=version.pdf_vi_sha256,
                original_pdf_en_sha256=version.pdf_en_sha256,
            )
        CSAContract.objects.filter(pk=contract.pk).update(**values)
        documents = ContractPDFDocuments(
            version.pdf_vi_storage_key,
            version.pdf_en_storage_key,
            version.pdf_vi_sha256,
            version.pdf_en_sha256,
            generated_at,
            number,
        )
        return version, documents
    except CSAContractPDFError:
        _delete_quietly(storage, {*attempted, *saved.values()})
        raise
    except Exception:
        _delete_quietly(storage, {*attempted, *saved.values()})
        raise CSAContractPDFStorageError("Contract version PDF publication failed") from None
