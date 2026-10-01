"""Immutable CSA contract PDF presentation helpers."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, Spacer, Table, TableStyle

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
        "original_total": "Giá gốc trước mã giảm giá",
        "coupon_code": "Mã giảm giá",
        "coupon_discount": "Loại và giá trị giảm",
        "discount_percent": "Phần trăm",
        "discount_fixed": "Số tiền cố định",
        "discount_amount": "Số tiền giảm",
        "payable_total": "Tổng phải thanh toán sau giảm",
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
        "terms": (
            "4. Chương trình hoạt động thế nào — tôi hiểu và đồng ý (vui lòng đánh dấu từng ô)"
        ),
        "legacy_terms": "4. Chương trình hoạt động thế nào",
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
        "original_total": "Original total before coupon",
        "coupon_code": "Coupon code",
        "coupon_discount": "Discount type and value",
        "discount_percent": "Percent",
        "discount_fixed": "Fixed amount",
        "discount_amount": "Discount amount",
        "payable_total": "Customer payable total",
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
        "terms": "4. How the program works — I understand and agree (please tick each box)",
        "legacy_terms": "4. How the program works",
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


def _decimal_text(value) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    if not parsed.is_finite():
        return "—"
    return format(parsed.normalize(), "f")


def _section(title, styles):
    return [Paragraph(escape(title), styles["section"])]


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
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
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
    pricing = getattr(version, "pricing_snapshot", None) if version is not None else None
    coupon = pricing.get("coupon") if isinstance(pricing, dict) else None
    pricing_rows = []
    if isinstance(coupon, dict):
        discount_type = coupon.get("discount_type")
        discount_value = coupon.get("discount_value")
        discount_description = (
            f"{labels['discount_percent']} · {_decimal_text(discount_value)}%"
            if discount_type == "percent"
            else (
                f"{labels['discount_fixed']} · {_money(discount_value, contract.currency_snapshot)}"
            )
        )
        pricing_rows = [
            (
                labels["original_total"],
                _money(pricing.get("original_total"), contract.currency_snapshot),
            ),
            (labels["coupon_code"], coupon.get("code")),
            (labels["coupon_discount"], discount_description),
            (
                labels["discount_amount"],
                _money(pricing.get("discount_amount"), contract.currency_snapshot),
            ),
            (
                labels["payable_total"],
                _money(pricing.get("customer_payable_total"), contract.currency_snapshot),
            ),
        ]
    summary_rows = [(labels["payment_method"], method), *pricing_rows]
    if not pricing_rows:
        summary_rows.append((labels["total_value"], _money(total, contract.currency_snapshot)))
    summary_rows.extend(
        [
            (labels["paid"], _money(paid, contract.currency_snapshot)),
            (labels["remaining"], _money(remaining, contract.currency_snapshot)),
            (labels["opened_months"], opened_months),
        ]
    )
    story.extend(_plain_rows(summary_rows, styles))
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


class _CheckedBox(Flowable):
    """A font-independent checked checkbox used for accepted V2 clauses."""

    def __init__(self):
        super().__init__()
        self.width = 4.4 * mm
        self.height = 4.4 * mm

    def draw(self):
        canvas = self.canv
        canvas.setStrokeColor(colors.HexColor("#1B5E20"))
        canvas.setLineWidth(0.75)
        canvas.rect(0.3 * mm, 0.3 * mm, 3.7 * mm, 3.7 * mm, stroke=1, fill=0)
        canvas.setLineWidth(1.1)
        path = canvas.beginPath()
        path.moveTo(0.9 * mm, 2.05 * mm)
        path.lineTo(1.7 * mm, 1.15 * mm)
        path.lineTo(3.4 * mm, 3.25 * mm)
        canvas.drawPath(path, stroke=1, fill=0)


def _is_document_terms(terms):
    return len(terms["sections"]) == 11 and all(
        not section.get("heading") and len(section["paragraphs"]) == 1
        for section in terms["sections"]
    )


def _terms_story(terms, styles, *, checked_items=False):
    if checked_items:
        rows = [
            [_CheckedBox(), Paragraph(escape(section["paragraphs"][0]), styles["terms_body"])]
            for section in terms["sections"]
        ]
        table = Table(rows, colWidths=[6 * mm, 174 * mm], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        return [table]

    story = []
    for section in terms["sections"]:
        if section["heading"]:
            story.append(Paragraph(escape(str(section["heading"])), styles["terms_heading"]))
        story.extend(
            Paragraph(escape(str(paragraph)), styles["terms_body"])
            for paragraph in section["paragraphs"]
        )
    return story


def _confirmation_table(contract, provider, labels, styles):
    method = (
        labels["online"] if contract.terms_acceptance_method == "online" else labels["in_store"]
    )
    member_rows = [
        Paragraph(escape(labels["member_confirmation"]), styles["confirmation_heading"]),
        Paragraph(_value(contract.member_name_snapshot), styles["confirmation_body"]),
        Paragraph(escape(labels["electronic_confirmation"]), styles["confirmation_body"]),
        Paragraph(
            f"<b>{escape(labels['confirmation_method'])}:</b> {escape(method)}",
            styles["confirmation_body"],
        ),
        Paragraph(
            f"<b>{escape(labels['accepted_at'])}:</b> {_datetime(contract.terms_accepted_at)}",
            styles["confirmation_body"],
        ),
    ]
    farm_rows = [
        Paragraph(escape(labels["farm_confirmation"]), styles["confirmation_heading"]),
        Paragraph(_value(provider["representative"]), styles["confirmation_body"]),
        Paragraph(escape(labels["electronic_confirmation"]), styles["confirmation_body"]),
        Paragraph(
            f"<b>{escape(labels['terms_version'])}:</b> {_value(contract.terms_version)}",
            styles["confirmation_body"],
        ),
    ]
    table = Table([[member_rows, farm_rows]], colWidths=[83.5 * mm, 83.5 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
                ("RIGHTPADDING", (0, 0), (0, -1), 8),
                ("LEFTPADDING", (1, 0), (1, -1), 8),
                ("RIGHTPADDING", (1, 0), (1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
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
                Spacer(1, 8),
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
    document_terms = _is_document_terms(terms)
    story.extend(_section(labels["terms" if document_terms else "legacy_terms"], styles))
    if not document_terms:
        story.append(Paragraph(escape(labels["accepted"]), styles["note"]))
    story.extend(_terms_story(terms, styles, checked_items=document_terms))
    story.extend(_section(labels["confirmation"], styles))
    story.append(Paragraph(escape(labels["confirmation_text"]), styles["body"]))
    story.append(Spacer(1, 2 * mm))
    story.append(_confirmation_table(contract, provider, labels, styles))
    return story


def build_pdf_styles(*, regular_font: str, bold_font: str, italic_font: str, bold_italic_font: str):
    return {
        "title": ParagraphStyle(
            "ContractTitle",
            fontName=bold_font,
            fontSize=19,
            leading=23,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#1B5E20"),
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "ContractSubtitle",
            fontName=italic_font,
            fontSize=10.5,
            leading=13.8,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#555555"),
            spaceAfter=8,
        ),
        "metadata": ParagraphStyle(
            "ContractMetadata",
            fontName=regular_font,
            fontSize=10.5,
            leading=13.8,
            textColor=colors.HexColor("#222222"),
            spaceAfter=8,
        ),
        "section": ParagraphStyle(
            "ContractSection",
            fontName=bold_font,
            fontSize=13,
            leading=16.5,
            textColor=colors.HexColor("#1B5E20"),
            spaceBefore=8,
            spaceAfter=4,
            keepWithNext=True,
        ),
        "subsection": ParagraphStyle(
            "ContractSubsection",
            fontName=bold_font,
            fontSize=10.5,
            leading=13.8,
            textColor=colors.HexColor("#222222"),
            spaceBefore=0,
            spaceAfter=1,
        ),
        "body": ParagraphStyle(
            "ContractBody",
            fontName=regular_font,
            fontSize=10.5,
            leading=13.8,
            textColor=colors.HexColor("#222222"),
            spaceAfter=1,
        ),
        "terms_heading": ParagraphStyle(
            "ContractTermsHeading",
            fontName=bold_font,
            fontSize=10.5,
            leading=13.8,
            spaceBefore=2,
            spaceAfter=1,
        ),
        "terms_body": ParagraphStyle(
            "ContractTermsBody",
            fontName=regular_font,
            fontSize=10.5,
            leading=13.8,
            textColor=colors.HexColor("#222222"),
        ),
        "note": ParagraphStyle(
            "ContractNote",
            fontName=italic_font,
            fontSize=8.5,
            leading=10.5,
            textColor=colors.HexColor("#555555"),
            spaceAfter=8,
        ),
        "table_header": ParagraphStyle(
            "ContractTableHeader",
            fontName=bold_font,
            fontSize=8.5,
            leading=10.5,
            alignment=TA_CENTER,
            textColor=colors.white,
        ),
        "table_body": ParagraphStyle(
            "ContractTableBody", fontName=regular_font, fontSize=9.5, leading=11.5
        ),
        "confirmation_heading": ParagraphStyle(
            "ContractConfirmationHeading",
            fontName=bold_font,
            fontSize=10.5,
            leading=13.8,
            textColor=colors.HexColor("#1B5E20"),
            spaceAfter=2,
        ),
        "confirmation_body": ParagraphStyle(
            "ContractConfirmationBody",
            fontName=regular_font,
            fontSize=10.5,
            leading=13.8,
            textColor=colors.HexColor("#222222"),
        ),
        "revoked_notice": ParagraphStyle(
            "ContractRevokedNotice",
            fontName=bold_font,
            fontSize=10.5,
            leading=14,
            textColor=colors.HexColor("#B42318"),
        ),
    }


def footer_labels(locale):
    return _LABELS[locale]
