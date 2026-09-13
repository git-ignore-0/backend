import json
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

_LABELS = {
    "vi": {
        "title": "PHIẾU ĐĂNG KÝ VÀ THỎA THUẬN CSA",
        "reference": "Mã hợp đồng",
        "issued": "Ngày phát hành",
        "status": "Trạng thái",
        "active": "Đang hiệu lực",
        "revoked": "Đã thu hồi",
        "parties": "1. Các bên",
        "provider": "Bên cung cấp",
        "representative": "Đại diện",
        "member": "Thành viên",
        "phone": "Điện thoại",
        "email": "Email",
        "zalo": "Zalo/WhatsApp",
        "address": "Địa chỉ giao hàng",
        "package": "2. Gói đã mua",
        "package_name": "Tên gói",
        "price_option": "Thời hạn / lựa chọn giá",
        "duration": "Thời hạn",
        "months": "tháng",
        "amount": "Số tiền",
        "currency": "Tiền tệ",
        "quota": "Hạn mức snapshot",
        "start_date": "Ngày bắt đầu",
        "end_date": "Ngày kết thúc",
        "payment": "3. Thanh toán",
        "bank": "Ngân hàng",
        "account_number": "Số tài khoản",
        "account_name": "Tên tài khoản",
        "transfer_content": "Nội dung chuyển khoản",
        "one_time": "Hình thức: Thanh toán một lần.",
        "payment_method": "Phương thức thanh toán",
        "payment_full": "Trả thẳng",
        "payment_installment": "Trả góp {count} lần",
        "total_payment": "Tổng giá trị",
        "paid_payment": "Đã thanh toán",
        "remaining_payment": "Còn phải thanh toán",
        "paid_cycles": "Số kỳ đã mở",
        "payment_schedule": "Lịch thanh toán",
        "payment_sequence": (
            "Lần {sequence}: {status} — phải trả {amount_due}; đã trả {amount_paid}; "
            "áp dụng {cycles} kỳ; đến hạn {due}"
        ),
        "payment_pending": "Chờ thanh toán",
        "payment_reported": "Đã báo chuyển khoản",
        "payment_confirmed": "Đã thanh toán",
        "payment_rejected": "Đã từ chối",
        "payment_overdue": "Quá hạn",
        "terms": "4. Điều khoản CSA",
        "accepted_all": "Thành viên đã xác nhận toàn bộ các điều khoản dưới đây.",
        "confirmation": "5. Xác nhận",
        "method": "Phương thức",
        "online": "Xác nhận điện tử",
        "in_store": "Xác nhận tại cửa hàng",
        "confirmed_by": "Người xác nhận",
        "confirmed_at": "Thời gian xác nhận",
        "terms_version": "Phiên bản điều khoản",
        "terms_hash": "Mã hash điều khoản",
        "revoked_banner": "HỢP ĐỒNG ĐÃ BỊ THU HỒI",
        "revocation_reason": "Lý do thu hồi",
        "revoked_at": "Thời gian thu hồi",
        "revoked_by": "Người thực hiện",
        "page": "Trang",
        "pdf_version": "Phiên bản PDF",
        "verification": "Quét để xác minh trạng thái hợp đồng",
    },
    "en": {
        "title": "CSA SIGN-UP AND AGREEMENT",
        "reference": "Contract reference",
        "issued": "Issued date",
        "status": "Status",
        "active": "Active",
        "revoked": "Revoked",
        "parties": "1. Parties",
        "provider": "Provider",
        "representative": "Representative",
        "member": "Member",
        "phone": "Phone",
        "email": "Email",
        "zalo": "Zalo/WhatsApp",
        "address": "Delivery address",
        "package": "2. Purchased package",
        "package_name": "Package",
        "price_option": "Term / price option",
        "duration": "Duration",
        "months": "months",
        "amount": "Amount",
        "currency": "Currency",
        "quota": "Quota snapshot",
        "start_date": "Start date",
        "end_date": "End date",
        "payment": "3. Payment",
        "bank": "Bank",
        "account_number": "Account number",
        "account_name": "Account name",
        "transfer_content": "Transfer content",
        "one_time": "Payment method: One-time payment.",
        "payment_method": "Payment method",
        "payment_full": "Full payment",
        "payment_installment": "Installments ({count})",
        "total_payment": "Total value",
        "paid_payment": "Paid",
        "remaining_payment": "Remaining",
        "paid_cycles": "Cycles opened",
        "payment_schedule": "Payment schedule",
        "payment_sequence": (
            "Installment {sequence}: {status} — due {amount_due}; paid {amount_paid}; "
            "covers {cycles} cycles; deadline {due}"
        ),
        "payment_pending": "Pending",
        "payment_reported": "Transfer reported",
        "payment_confirmed": "Confirmed",
        "payment_rejected": "Rejected",
        "payment_overdue": "Overdue",
        "terms": "4. CSA terms",
        "accepted_all": "The member confirmed all terms shown below.",
        "confirmation": "5. Confirmation",
        "method": "Method",
        "online": "Electronic confirmation",
        "in_store": "In-store confirmation",
        "confirmed_by": "Confirmed by",
        "confirmed_at": "Confirmed at",
        "terms_version": "Terms version",
        "terms_hash": "Terms hash",
        "revoked_banner": "THIS CONTRACT HAS BEEN REVOKED",
        "revocation_reason": "Revocation reason",
        "revoked_at": "Revoked at",
        "revoked_by": "Revoked by",
        "page": "Page",
        "pdf_version": "PDF version",
        "verification": "Scan to verify contract status",
    },
}


def _date(value) -> str:
    return value.strftime("%d/%m/%Y")


def _datetime(value) -> str:
    return timezone.localtime(value).strftime("%d/%m/%Y %H:%M:%S")


def _value(value) -> str:
    return escape(str(value)) if value not in (None, "") else "—"


def _money(value: int, currency: str) -> str:
    return f"{value:,.0f} {currency}".replace(",", ".")


def _section(title, styles):
    return [Spacer(1, 4 * mm), Paragraph(escape(title), styles["section"])]


def _details(rows, styles):
    table = Table(
        [
            [Paragraph(escape(label), styles["label"]), Paragraph(_value(value), styles["body"])]
            for label, value in rows
        ],
        colWidths=[48 * mm, 132 * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ]
        )
    )
    return table


def _terms_story(terms, styles):
    story = []
    title = terms.get("title")
    if title:
        story.append(Paragraph(escape(str(title)), styles["subsection"]))
    for section in terms["sections"]:
        section_story = [Paragraph(escape(str(section["heading"])), styles["subsection"])]
        section_story.extend(
            Paragraph(escape(str(paragraph)), styles["body_spaced"])
            for paragraph in section["paragraphs"]
        )
        story.append(KeepTogether(section_story))
    return story


def contract_verification_url(reference_code: str) -> str:
    origin = settings.PUBLIC_SITE_ORIGINS[0].rstrip("/")
    return f"{origin}/csa/verify?{urlencode({'reference': reference_code})}"


def _verification_story(*, reference_code: str, label: str, styles):
    url = contract_verification_url(reference_code)
    qr = QrCodeWidget(url)
    x1, y1, x2, y2 = qr.getBounds()
    size = 28 * mm
    scale = size / max(x2 - x1, y2 - y1)
    drawing = Drawing(
        size,
        size,
        transform=[scale, 0, 0, scale, -x1 * scale, -y1 * scale],
    )
    drawing.hAlign = "CENTER"
    drawing.add(qr)
    return KeepTogether(
        [
            Spacer(1, 4 * mm),
            Paragraph(escape(label), styles["verification"]),
            Spacer(1, 2 * mm),
            drawing,
            Spacer(1, 1.5 * mm),
            Paragraph(escape(url), styles["verification_url"]),
        ]
    )


def build_contract_story(
    *, contract, locale, provider, payment, terms, terms_hash, styles, version=None
):
    labels = _LABELS[locale]
    status_label = labels["revoked"] if contract.status == "revoked" else labels["active"]
    story = [
        Paragraph(escape(provider["name"]), styles["brand"]),
        Paragraph(labels["title"], styles["title"]),
        Spacer(1, 3 * mm),
        _details(
            [
                (labels["reference"], contract.reference_code),
                (labels["issued"], _date(timezone.localtime(contract.issued_at).date())),
                (labels["status"], status_label),
            ],
            styles,
        ),
    ]

    if contract.status == "revoked":
        story.extend(
            [
                Spacer(1, 4 * mm),
                Paragraph(labels["revoked_banner"], styles["revoked_banner"]),
                _details(
                    [
                        (labels["revocation_reason"], contract.revocation_reason),
                        (labels["revoked_at"], _datetime(contract.revoked_at)),
                        (labels["revoked_by"], contract.revoked_by_name_snapshot),
                    ],
                    styles,
                ),
            ]
        )

    story.extend(_section(labels["parties"], styles))
    story.extend(
        [
            Paragraph(labels["provider"], styles["subsection"]),
            _details(
                [
                    (labels["provider"], provider["name"]),
                    (labels["representative"], provider["representative"]),
                    (labels["address"], provider["address"]),
                    (labels["email"], provider["email"]),
                    (labels["phone"], provider["phone_zalo"]),
                    ("Website", provider["website"]),
                ],
                styles,
            ),
            Spacer(1, 2 * mm),
            Paragraph(labels["member"], styles["subsection"]),
            _details(
                [
                    (labels["member"], contract.member_name_snapshot),
                    (labels["phone"], contract.phone_e164_snapshot),
                    (labels["email"], contract.email_snapshot),
                    (labels["zalo"], contract.zalo_whatsapp_snapshot),
                    (labels["address"], contract.address_snapshot),
                ],
                styles,
            ),
        ]
    )

    story.extend(_section(labels["package"], styles))
    quota = (
        json.dumps(contract.quota_snapshot, ensure_ascii=False, sort_keys=True)
        if contract.quota_snapshot
        else None
    )
    story.append(
        _details(
            [
                (labels["package_name"], contract.package_name_snapshot),
                (labels["price_option"], contract.price_option_name_snapshot),
                (
                    labels["duration"],
                    f"{contract.duration_months_snapshot} {labels['months']}",
                ),
                (labels["amount"], _money(contract.amount_snapshot, contract.currency_snapshot)),
                (labels["currency"], contract.currency_snapshot),
                (labels["quota"], quota),
                (labels["start_date"], _date(contract.start_date)),
                (labels["end_date"], _date(contract.end_date - timedelta(days=1))),
            ],
            styles,
        )
    )

    payment_story = [Spacer(1, 4 * mm), Paragraph(escape(labels["payment"]), styles["section"])]
    if version is None:
        payment_story.append(Paragraph(labels["one_time"], styles["body_spaced"]))
    else:
        method = (
            labels["payment_full"]
            if version.payment_type == "full"
            else labels["payment_installment"].format(count=version.installment_count)
        )
        payment_story.append(
            _details(
                [
                    (labels["payment_method"], method),
                    (
                        labels["total_payment"],
                        _money(version.total_payment_amount, version.currency_snapshot),
                    ),
                    (
                        labels["paid_payment"],
                        _money(version.paid_amount, version.currency_snapshot),
                    ),
                    (
                        labels["remaining_payment"],
                        _money(version.remaining_amount, version.currency_snapshot),
                    ),
                    (labels["paid_cycles"], f"{version.paid_cycles}/{version.total_cycles}"),
                ],
                styles,
            )
        )
        payment_story.append(Paragraph(labels["payment_schedule"], styles["subsection"]))
        for line in version.payment_schedule_snapshot:
            due = line.get("due_at")
            due_text = _date(timezone.localtime(datetime.fromisoformat(due)).date()) if due else "—"
            payment_story.append(
                Paragraph(
                    escape(
                        labels["payment_sequence"].format(
                            sequence=line["sequence"],
                            status=labels[f"payment_{line['status']}"],
                            amount_due=_money(int(line["amount_due"]), version.currency_snapshot),
                            amount_paid=_money(
                                int(line["amount_paid"] or 0), version.currency_snapshot
                            ),
                            cycles=line["cycle_count"],
                            due=due_text,
                        )
                    ),
                    styles["body_spaced"],
                )
            )
    payment_story.append(
        _details(
            [
                (labels["bank"], payment["bank_name"]),
                (labels["account_number"], payment["account_number"]),
                (labels["account_name"], payment["account_name"]),
                (
                    labels["amount"],
                    _money(
                        int(version.payment_schedule_snapshot[0]["amount_due"])
                        if version is not None and version.payment_type == "installment"
                        else contract.amount_snapshot,
                        contract.currency_snapshot,
                    ),
                ),
                (labels["transfer_content"], payment.get("transfer_content")),
            ],
            styles,
        )
    )
    story.extend(payment_story)

    story.extend(_section(labels["terms"], styles))
    story.append(Paragraph(labels["accepted_all"], styles["body_spaced"]))
    story.extend(_terms_story(terms, styles))

    method = (
        labels["online"] if contract.terms_acceptance_method == "online" else labels["in_store"]
    )
    story.append(
        KeepTogether(
            [
                Spacer(1, 4 * mm),
                Paragraph(escape(labels["confirmation"]), styles["section"]),
                _details(
                    [
                        (labels["method"], method),
                        (labels["confirmed_by"], contract.member_name_snapshot),
                        (labels["confirmed_at"], _datetime(contract.terms_accepted_at)),
                        (labels["terms_version"], contract.terms_version),
                        (labels["terms_hash"], terms_hash[:16]),
                    ],
                    styles,
                ),
            ]
        )
    )
    story.append(
        _verification_story(
            reference_code=contract.reference_code,
            label=labels["verification"],
            styles=styles,
        )
    )
    return story


def build_pdf_styles(*, regular_font: str, bold_font: str):
    return {
        "brand": ParagraphStyle(
            "ContractBrand",
            fontName=bold_font,
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#2F6B3C"),
            alignment=TA_CENTER,
        ),
        "title": ParagraphStyle(
            "ContractTitle",
            fontName=bold_font,
            fontSize=17,
            leading=22,
            alignment=TA_CENTER,
            spaceBefore=4,
        ),
        "section": ParagraphStyle(
            "ContractSection",
            fontName=bold_font,
            fontSize=12,
            leading=16,
            textColor=colors.HexColor("#1F5130"),
            spaceAfter=5,
        ),
        "subsection": ParagraphStyle(
            "ContractSubsection",
            fontName=bold_font,
            fontSize=10,
            leading=14,
            spaceBefore=4,
            spaceAfter=3,
        ),
        "label": ParagraphStyle("ContractLabel", fontName=bold_font, fontSize=9, leading=13),
        "body": ParagraphStyle("ContractBody", fontName=regular_font, fontSize=9, leading=13),
        "body_spaced": ParagraphStyle(
            "ContractBodySpaced",
            fontName=regular_font,
            fontSize=9,
            leading=14,
            spaceAfter=5,
        ),
        "revoked_banner": ParagraphStyle(
            "ContractRevokedBanner",
            fontName=bold_font,
            fontSize=13,
            leading=17,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#B42318"),
            backColor=colors.HexColor("#FEE4E2"),
            borderColor=colors.HexColor("#FDA29B"),
            borderWidth=1,
            borderPadding=7,
            spaceAfter=5,
        ),
        "verification": ParagraphStyle(
            "ContractVerification",
            fontName=bold_font,
            fontSize=9,
            leading=13,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#1F5130"),
        ),
        "verification_url": ParagraphStyle(
            "ContractVerificationURL",
            fontName=regular_font,
            fontSize=7,
            leading=10,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#4B5563"),
        ),
    }


def footer_labels(locale):
    return _LABELS[locale]
