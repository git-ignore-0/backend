"""Immutable CSA contract PDF presentation helpers."""

from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

_LABELS = {
    "vi": {
        "title": "Natural Farming Vietnam — Phiếu Đăng Ký & Thỏa Thuận CSA",
        "subtitle": "Nông nghiệp do cộng đồng hỗ trợ · sự cộng tác giữa nông trại và bạn",
        "reference": "Mã hợp đồng",
        "start_date": "Ngày bắt đầu",
        "end_date": "Ngày kết thúc",
        "status": "Trạng thái",
        "active": "Đang hiệu lực",
        "revoked": "Đã thu hồi",
        "revoked_notice": "Hợp đồng này đã bị thu hồi.",
        "revocation_reason": "Lý do thu hồi",
        "revoked_at": "Thời gian thu hồi",
        "revoked_by": "Người thực hiện",
        "parties": "1. Hai bên",
        "farm": "Nông trại / Bên cung cấp",
        "member": "Thành viên",
        "representative": "Đại diện",
        "phone": "Điện thoại",
        "email": "Email",
        "zalo": "Zalo/WhatsApp",
        "address": "Địa chỉ giao hàng",
        "package": "2. Chọn gói của bạn",
        "selected_plan": "Gói đã chọn",
        "duration": "Thời hạn",
        "total_value": "Tổng giá trị",
        "quota": "Định mức sản phẩm",
        "quota_item": "Nhóm sản phẩm",
        "quota_amount": "Hạn mức",
        "quota_unit": "Đơn vị",
        "quota_rollover": "Hạn mức chưa sử dụng được cộng dồn theo chính sách gói.",
        "quota_expire": "Hạn mức không sử dụng trong kỳ sẽ hết hiệu lực.",
        "quota_unknown": "Định mức sản phẩm được áp dụng theo snapshot của gói đã chọn.",
        "months": "tháng",
        "payment": "3. Thanh toán",
        "payment_method": "Phương thức",
        "payment_full": "Thanh toán toàn bộ khi đăng ký",
        "payment_installment": "Trả góp {count} lần",
        "paid": "Đã thanh toán",
        "remaining": "Còn phải thanh toán",
        "opened_months": "Số tháng đã mở",
        "payment_schedule": "Lịch thanh toán",
        "payment_no": "Lần",
        "amount_due": "Số tiền phải trả",
        "amount_paid": "Số tiền đã trả",
        "months_covered": "Số tháng áp dụng",
        "due_date": "Hạn thanh toán",
        "payment_status": "Trạng thái",
        "payment_pending": "Chờ thanh toán",
        "payment_reported": "Đã báo chuyển khoản",
        "payment_confirmed": "Đã xác nhận",
        "payment_rejected": "Đã từ chối",
        "payment_overdue": "Quá hạn",
        "bank": "Ngân hàng",
        "account_number": "Số tài khoản",
        "account_name": "Tên tài khoản",
        "transfer_content": "Nội dung chuyển khoản",
        "terms": "4. Chương trình hoạt động thế nào",
        "accepted": (
            "Khách hàng xác nhận đã đọc, hiểu và đồng ý với các điều khoản áp dụng cho gói CSA này."
        ),
        "confirmation": "5. Xác nhận & chữ ký",
        "confirmation_text": (
            "Bằng việc xác nhận điện tử, hai bên đồng ý với các thông tin và điều khoản "
            "được ghi trong hợp đồng này."
        ),
        "member_confirmation": "Xác nhận của thành viên",
        "farm_confirmation": "Xác nhận của Natural Farming Vietnam",
        "electronic_confirmation": "Đã xác nhận điện tử",
        "confirmation_method": "Hình thức xác nhận",
        "online": "Trực tuyến",
        "in_store": "Tại cửa hàng",
        "accepted_at": "Thời gian xác nhận",
        "terms_version": "Phiên bản điều khoản",
        "verification": "Quét để xác minh trạng thái hợp đồng",
        "page": "Trang",
        "pdf_version": "Phiên bản PDF",
    },
    "en": {
        "title": "Natural Farming Vietnam — CSA Sign-Up & Agreement",
        "subtitle": "Community-Supported Agriculture · a partnership between the farm and you",
        "reference": "Contract reference",
        "start_date": "Start date",
        "end_date": "End date",
        "status": "Status",
        "active": "Active",
        "revoked": "Revoked",
        "revoked_notice": "This contract has been revoked.",
        "revocation_reason": "Revocation reason",
        "revoked_at": "Revoked at",
        "revoked_by": "Processed by",
        "parties": "1. The two parties",
        "farm": "Farm / Provider",
        "member": "Member",
        "representative": "Representative",
        "phone": "Phone",
        "email": "Email",
        "zalo": "Zalo/WhatsApp",
        "address": "Delivery address",
        "package": "2. Choose your plan",
        "selected_plan": "Selected plan",
        "duration": "Duration",
        "total_value": "Total value",
        "quota": "Product quota",
        "quota_item": "Product group",
        "quota_amount": "Quota",
        "quota_unit": "Unit",
        "quota_rollover": "Quota may roll over according to the selected plan policy.",
        "quota_expire": "Unused quota expires at the end of each period.",
        "quota_unknown": "Product quota follows the snapshot of the selected plan.",
        "months": "months",
        "payment": "3. Payment",
        "payment_method": "Method",
        "payment_full": "Pay the full amount at sign-up",
        "payment_installment": "Pay in {count} installments",
        "paid": "Paid",
        "remaining": "Remaining",
        "opened_months": "Months opened",
        "payment_schedule": "Payment schedule",
        "payment_no": "No.",
        "amount_due": "Amount due",
        "amount_paid": "Amount paid",
        "months_covered": "Months covered",
        "due_date": "Due date",
        "payment_status": "Status",
        "payment_pending": "Pending",
        "payment_reported": "Transfer reported",
        "payment_confirmed": "Confirmed",
        "payment_rejected": "Rejected",
        "payment_overdue": "Overdue",
        "bank": "Bank",
        "account_number": "Account number",
        "account_name": "Account name",
        "transfer_content": "Transfer content",
        "terms": "4. How the program works",
        "accepted": (
            "The member confirms that they have read, understood, and agreed to the terms "
            "applicable to this CSA package."
        ),
        "confirmation": "5. Confirmation & signatures",
        "confirmation_text": (
            "By confirming electronically, both parties agree to the information and terms "
            "recorded in this agreement."
        ),
        "member_confirmation": "Member confirmation",
        "farm_confirmation": "Natural Farming Vietnam confirmation",
        "electronic_confirmation": "Electronically confirmed",
        "confirmation_method": "Confirmation method",
        "online": "Online",
        "in_store": "In store",
        "accepted_at": "Confirmed at",
        "terms_version": "Terms version",
        "verification": "Scan to verify contract status",
        "page": "Page",
        "pdf_version": "PDF version",
    },
}


def _date(value) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


def _datetime(value) -> str:
    return timezone.localtime(value).strftime("%d/%m/%Y %H:%M") if value else "—"


def _date_from_snapshot(value) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return _date(timezone.localtime(value).date())
    parsed = parse_datetime(str(value))
    return _date(timezone.localtime(parsed).date()) if parsed else "—"


def _value(value) -> str:
    return escape(str(value)) if value not in (None, "") else "—"


def _money(value: int | str | None, currency: str) -> str:
    try:
        return f"{int(value):,.0f} {currency}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def _section(title, styles):
    return [Spacer(1, 3.5 * mm), Paragraph(escape(title), styles["section"])]


def _plain_rows(rows, styles):
    return [
        Paragraph(f"<b>{escape(label)}:</b> {_value(value)}", styles["body"])
        for label, value in rows
        if value not in (None, "")
    ]


def _table(rows, widths, styles, *, compact=False):
    content = [
        [
            Paragraph(_value(value), styles["table_header" if row_index == 0 else "table_body"])
            for value in row
        ]
        for row_index, row in enumerate(rows)
    ]
    table = Table(content, colWidths=widths, hAlign="LEFT", repeatRows=1)
    padding = 3 if compact else 4
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E7D32")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#7A7A7A")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), padding),
                ("RIGHTPADDING", (0, 0), (-1, -1), padding),
                ("TOPPADDING", (0, 0), (-1, -1), padding),
                ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
            ]
        )
    )
    return table


def _selected_plan_table(contract, labels, styles):
    selected_name = contract.package_name_snapshot
    if contract.price_option_name_snapshot:
        selected_name = f"{selected_name} · {contract.price_option_name_snapshot}"
    return _table(
        [
            [
                labels["selected_plan"],
                labels["duration"],
                labels["start_date"],
                labels["end_date"],
                labels["total_value"],
            ],
            [
                selected_name,
                f"{contract.duration_months_snapshot} {labels['months']}",
                _date(contract.start_date),
                _date(contract.end_date - timedelta(days=1)),
                _money(contract.amount_snapshot, contract.currency_snapshot),
            ],
        ],
        [58 * mm, 24 * mm, 27 * mm, 27 * mm, 31 * mm],
        styles,
    )


def _quota_story(quota, labels, locale, styles):
    items = quota.get("items") if isinstance(quota, dict) else None
    rows = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = (
                item.get(f"product_name_{locale}")
                or item.get("product_name")
                or item.get("product")
                or item.get("name")
            )
            amount = item.get("quota_units_per_cycle", item.get("units", item.get("quantity")))
            unit = item.get(f"unit_label_{locale}") or item.get("unit") or item.get("unit_size")
            if name and amount not in (None, ""):
                rows.append([name, str(amount), str(unit) if unit not in (None, "") else "—"])
    if rows:
        return [
            Paragraph(escape(labels["quota"]), styles["subsection"]),
            _table(
                [[labels["quota_item"], labels["quota_amount"], labels["quota_unit"]], *rows],
                [78 * mm, 40 * mm, 49 * mm],
                styles,
            ),
        ]
    policy = quota.get("policy") if isinstance(quota, dict) else None
    return [
        Paragraph(escape(labels["quota"]), styles["subsection"]),
        Paragraph(escape(labels.get(f"quota_{policy}", labels["quota_unknown"])), styles["body"]),
    ]


def _payment_schedule(version, labels, styles):
    if version is None or version.payment_type != "installment":
        return []
    rows = [
        [
            labels["payment_no"],
            labels["amount_due"],
            labels["amount_paid"],
            labels["months_covered"],
            labels["due_date"],
            labels["payment_status"],
        ]
    ]
    for line in sorted(version.payment_schedule_snapshot, key=lambda item: item["sequence"]):
        rows.append(
            [
                str(line.get("sequence", "—")),
                _money(line.get("amount_due"), version.currency_snapshot),
                _money(line.get("amount_paid"), version.currency_snapshot),
                str(line.get("cycle_count", "—")),
                _date_from_snapshot(line.get("due_at")),
                labels.get(f"payment_{line.get('status')}", "—"),
            ]
        )
    return [
        Paragraph(escape(labels["payment_schedule"]), styles["subsection"]),
        _table(rows, [17 * mm, 31 * mm, 31 * mm, 24 * mm, 33 * mm, 31 * mm], styles, compact=True),
    ]


def _payment_story(contract, payment, version, labels, styles):
    if version is None:
        method, total, paid, remaining = (
            labels["payment_full"],
            contract.amount_snapshot,
            contract.amount_snapshot,
            0,
        )
        opened_months = contract.duration_months_snapshot
    else:
        method = (
            labels["payment_full"]
            if version.payment_type == "full"
            else labels["payment_installment"].format(count=version.installment_count)
        )
        total, paid, remaining = (
            version.total_payment_amount,
            version.paid_amount,
            version.remaining_amount,
        )
        opened_months = f"{version.paid_cycles}/{version.total_cycles}"
    story = _section(labels["payment"], styles)
    story.extend(
        _plain_rows(
            [
                (labels["payment_method"], method),
                (labels["total_value"], _money(total, contract.currency_snapshot)),
                (labels["paid"], _money(paid, contract.currency_snapshot)),
                (labels["remaining"], _money(remaining, contract.currency_snapshot)),
                (labels["opened_months"], opened_months),
            ],
            styles,
        )
    )
    story.extend(_payment_schedule(version, labels, styles))
    story.extend(
        [
            Spacer(1, 1.5 * mm),
            Paragraph(escape(labels["bank"]), styles["subsection"]),
            *_plain_rows(
                [
                    (labels["bank"], payment.get("bank_name")),
                    (labels["account_number"], payment.get("account_number")),
                    (labels["account_name"], payment.get("account_name")),
                    (labels["transfer_content"], payment.get("transfer_content")),
                ],
                styles,
            ),
        ]
    )
    return story


def _terms_story(terms, styles):
    story = []
    for section in terms["sections"]:
        story.append(Paragraph(escape(str(section["heading"])), styles["terms_heading"]))
        story.extend(
            Paragraph(escape(str(paragraph)), styles["terms_body"])
            for paragraph in section["paragraphs"]
        )
    return story


def contract_verification_url(reference_code: str) -> str:
    origin = settings.PUBLIC_SITE_ORIGINS[0].rstrip("/")
    return f"{origin}/csa/verify?{urlencode({'reference': reference_code})}"


def _verification_story(*, reference_code: str, label: str, styles):
    url = contract_verification_url(reference_code)
    qr = QrCodeWidget(url)
    x1, y1, x2, y2 = qr.getBounds()
    # The verification feature remains available without forcing a separate
    # trailing page after the DOCX-style confirmation table.
    size = 11 * mm
    scale = size / max(x2 - x1, y2 - y1)
    drawing = Drawing(size, size, transform=[scale, 0, 0, scale, -x1 * scale, -y1 * scale])
    drawing.hAlign = "CENTER"
    drawing.add(qr)
    return KeepTogether(
        [
            Paragraph(escape(label), styles["verification"]),
            drawing,
            Paragraph(escape(url), styles["verification_url"]),
        ]
    )


def _confirmation_table(contract, provider, labels, styles):
    method = (
        labels["online"] if contract.terms_acceptance_method == "online" else labels["in_store"]
    )
    member_rows = [
        Paragraph(escape(labels["member_confirmation"]), styles["confirmation_heading"]),
        Paragraph(_value(contract.member_name_snapshot), styles["table_body"]),
        Paragraph(escape(labels["electronic_confirmation"]), styles["table_body"]),
        Paragraph(
            f"<b>{escape(labels['confirmation_method'])}:</b> {escape(method)}",
            styles["table_body"],
        ),
        Paragraph(
            f"<b>{escape(labels['accepted_at'])}:</b> {_datetime(contract.terms_accepted_at)}",
            styles["table_body"],
        ),
    ]
    farm_rows = [
        Paragraph(escape(labels["farm_confirmation"]), styles["confirmation_heading"]),
        Paragraph(_value(provider["representative"]), styles["table_body"]),
        Paragraph(escape(labels["electronic_confirmation"]), styles["table_body"]),
        Paragraph(
            f"<b>{escape(labels['terms_version'])}:</b> {_value(contract.terms_version)}",
            styles["table_body"],
        ),
    ]
    table = Table([[member_rows, farm_rows]], colWidths=[83.5 * mm, 83.5 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#7A7A7A")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def build_contract_story(*, contract, locale, provider, payment, terms, styles, version=None, **_):
    labels = _LABELS[locale]
    status_label = labels["revoked"] if contract.status == "revoked" else labels["active"]
    story = [
        Paragraph(escape(labels["title"]), styles["title"]),
        Paragraph(escape(labels["subtitle"]), styles["subtitle"]),
        Spacer(1, 2.5 * mm),
        Paragraph(
            " · ".join(
                (
                    f"<b>{escape(labels['reference'])}:</b> {_value(contract.reference_code)}",
                    f"<b>{escape(labels['start_date'])}:</b> {_date(contract.start_date)}",
                    f"<b>{escape(labels['end_date'])}:</b> "
                    f"{_date(contract.end_date - timedelta(days=1))}",
                )
            ),
            styles["metadata"],
        ),
    ]
    if contract.status == "revoked":
        story.extend(
            [
                Spacer(1, 2.5 * mm),
                Paragraph(escape(labels["revoked_notice"]), styles["revoked_notice"]),
                *_plain_rows(
                    [
                        (labels["status"], status_label),
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
            Paragraph(escape(labels["farm"]), styles["subsection"]),
            *_plain_rows(
                [
                    (labels["farm"], provider["name"]),
                    (labels["representative"], provider["representative"]),
                    (labels["address"], provider["address"]),
                    (labels["email"], provider["email"]),
                    (labels["phone"], provider["phone_zalo"]),
                    ("Website", provider["website"]),
                ],
                styles,
            ),
            Spacer(1, 1.5 * mm),
            Paragraph(escape(labels["member"]), styles["subsection"]),
            *_plain_rows(
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
    story.append(_selected_plan_table(contract, labels, styles))
    story.extend(_quota_story(contract.quota_snapshot, labels, locale, styles))
    story.extend(_payment_story(contract, payment, version, labels, styles))
    story.extend(_section(labels["terms"], styles))
    story.append(Paragraph(escape(labels["accepted"]), styles["note"]))
    story.extend(_terms_story(terms, styles))
    story.extend(_section(labels["confirmation"], styles))
    story.append(Paragraph(escape(labels["confirmation_text"]), styles["body"]))
    story.append(Spacer(1, 2 * mm))
    story.append(_confirmation_table(contract, provider, labels, styles))
    # Preserved from the contract-verification phase; QR encodes only public URL/reference.
    story.append(
        _verification_story(
            reference_code=contract.reference_code, label=labels["verification"], styles=styles
        )
    )
    return story


def build_pdf_styles(*, regular_font: str, bold_font: str):
    return {
        "title": ParagraphStyle(
            "ContractTitle",
            fontName=bold_font,
            # Noto Sans is intentionally used from the configured Unicode font
            # bundle. At 16pt it matches the single-line DOCX heading within
            # the A4 template's 15mm side margins without losing Vietnamese glyphs.
            fontSize=16,
            leading=20,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#1B5E20"),
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "ContractSubtitle",
            fontName=regular_font,
            fontSize=10.5,
            leading=14,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#555555"),
            spaceAfter=4,
        ),
        "metadata": ParagraphStyle(
            "ContractMetadata",
            fontName=regular_font,
            fontSize=10.5,
            leading=14,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#222222"),
        ),
        "section": ParagraphStyle(
            "ContractSection",
            fontName=bold_font,
            fontSize=13,
            leading=17,
            textColor=colors.HexColor("#1B5E20"),
            spaceAfter=2,
        ),
        "subsection": ParagraphStyle(
            "ContractSubsection",
            fontName=bold_font,
            fontSize=10.5,
            leading=14,
            textColor=colors.HexColor("#222222"),
            spaceBefore=2,
            spaceAfter=1,
        ),
        "body": ParagraphStyle("ContractBody", fontName=regular_font, fontSize=10.5, leading=14),
        "terms_heading": ParagraphStyle(
            "ContractTermsHeading",
            fontName=bold_font,
            fontSize=10.5,
            leading=14,
            spaceBefore=2,
            spaceAfter=1,
        ),
        "terms_body": ParagraphStyle(
            "ContractTermsBody", fontName=regular_font, fontSize=10.5, leading=14, spaceAfter=2
        ),
        "note": ParagraphStyle(
            "ContractNote",
            fontName=regular_font,
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#555555"),
            spaceAfter=2,
        ),
        "table_header": ParagraphStyle(
            "ContractTableHeader",
            fontName=bold_font,
            fontSize=8.5,
            leading=10.5,
            textColor=colors.white,
        ),
        "table_body": ParagraphStyle(
            "ContractTableBody", fontName=regular_font, fontSize=9, leading=11.5
        ),
        "confirmation_heading": ParagraphStyle(
            "ContractConfirmationHeading",
            fontName=bold_font,
            fontSize=9.5,
            leading=12,
            textColor=colors.HexColor("#1B5E20"),
            spaceAfter=1,
        ),
        "revoked_notice": ParagraphStyle(
            "ContractRevokedNotice",
            fontName=bold_font,
            fontSize=10.5,
            leading=14,
            textColor=colors.HexColor("#B42318"),
        ),
        "verification": ParagraphStyle(
            "ContractVerification",
            fontName=bold_font,
            fontSize=8.5,
            leading=11,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#1B5E20"),
        ),
        "verification_url": ParagraphStyle(
            "ContractVerificationUrl",
            fontName=regular_font,
            fontSize=7,
            leading=9,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#555555"),
        ),
    }


def footer_labels(locale):
    return _LABELS[locale]
