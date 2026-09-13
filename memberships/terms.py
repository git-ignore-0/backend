import copy
import hashlib
import json
import secrets

from memberships.models import MembershipContentLocale

CSA_TERMS_V1 = "CSA_TERMS_V1"
CSA_TERMS_V2 = "CSA_TERMS_V2"
CURRENT_CSA_TERMS_VERSION = CSA_TERMS_V2

_CSA_TERMS_CONTENT = {
    CSA_TERMS_V1: {
        MembershipContentLocale.VI: {
            "title": "Điều khoản tham gia chương trình CSA",
            "sections": [
                {
                    "key": "parties",
                    "heading": "Thông tin các bên",
                    "paragraphs": [
                        (
                            "Chương trình được cung cấp bởi Natural Farming Vietnam cho thành viên "
                            "có thông tin được ghi trong yêu cầu mua và hợp đồng CSA."
                        ),
                    ],
                },
                {
                    "key": "payment",
                    "heading": "Thanh toán một lần",
                    "paragraphs": [
                        (
                            "Thành viên thanh toán một lần toàn bộ số tiền của gói đã chọn theo "
                            "thông tin chuyển khoản trong yêu cầu mua. Chương trình không cung cấp "
                            "lựa chọn trả góp."
                        ),
                    ],
                },
                {
                    "key": "program_operation",
                    "heading": "Cách chương trình hoạt động",
                    "paragraphs": [
                        (
                            "CSA kết nối thành viên với hoạt động sản xuất theo mùa vụ trong thời "
                            "hạn của gói. Quyền nhận sản phẩm phát sinh theo từng kỳ và lịch của "
                            "chương trình."
                        ),
                    ],
                },
                {
                    "key": "product_quota",
                    "heading": "Hạn mức sản phẩm",
                    "paragraphs": [
                        (
                            "Số lượng được nhận của từng sản phẩm tuân theo hạn mức đã snapshot "
                            "trong gói. Phần chưa sử dụng được xử lý theo chính sách expire hoặc "
                            "rollover của gói tại thời điểm mua."
                        ),
                    ],
                },
                {
                    "key": "seasonal_changes",
                    "heading": "Thay đổi theo mùa vụ",
                    "paragraphs": [
                        (
                            "Chủng loại, kích thước và thời điểm có sản phẩm có thể thay đổi do "
                            "mùa vụ, thời tiết, dịch bệnh và điều kiện canh tác thực tế."
                        ),
                    ],
                },
                {
                    "key": "substitution",
                    "heading": "Thay thế sản phẩm",
                    "paragraphs": [
                        (
                            "Khi sản phẩm dự kiến không có sẵn, chương trình có thể đề xuất sản "
                            "phẩm thay thế có giá trị hoặc công dụng tương đương và thông báo qua "
                            "kênh liên lạc phù hợp."
                        ),
                    ],
                },
                {
                    "key": "delivery",
                    "heading": "Giao và nhận sản phẩm",
                    "paragraphs": [
                        (
                            "Lịch, địa điểm, phạm vi và chi phí giao nhận được thông báo theo từng "
                            "khu vực. Thành viên có trách nhiệm cung cấp địa chỉ và thông tin liên "
                            "lạc chính xác."
                        ),
                    ],
                },
                {
                    "key": "pause_skip",
                    "heading": "Tạm dừng hoặc bỏ qua kỳ nhận",
                    "paragraphs": [
                        (
                            "Yêu cầu tạm dừng hoặc bỏ qua một kỳ phải được gửi trước thời hạn "
                            "chương trình thông báo. Khả năng bảo lưu hạn mức phụ thuộc chính sách "
                            "của gói đã snapshot."
                        ),
                    ],
                },
                {
                    "key": "communication",
                    "heading": "Kênh liên lạc",
                    "paragraphs": [
                        (
                            "Thông báo có thể được gửi qua email, điện thoại, Zalo hoặc WhatsApp "
                            "theo thông tin thành viên cung cấp. Thành viên cần thông báo khi "
                            "thông tin liên lạc thay đổi."
                        ),
                    ],
                },
                {
                    "key": "program_adjustments",
                    "heading": "Điều chỉnh chương trình",
                    "paragraphs": [
                        (
                            "Chương trình có thể điều chỉnh lịch, điểm nhận hoặc cách vận hành khi "
                            "cần thiết và sẽ thông báo trong thời gian hợp lý, nhưng không làm "
                            "thay đổi snapshot giá và thời hạn của hợp đồng đã phát hành."
                        ),
                    ],
                },
                {
                    "key": "liability",
                    "heading": "Giới hạn trách nhiệm",
                    "paragraphs": [
                        (
                            "Trong phạm vi pháp luật cho phép, trách nhiệm của chương trình được "
                            "giới hạn ở nghĩa vụ cung cấp hoặc xử lý quyền lợi còn lại của gói; "
                            "chương trình không chịu trách nhiệm cho thiệt hại gián tiếp do sự "
                            "kiện ngoài khả năng kiểm soát hợp lý."
                        ),
                    ],
                },
            ],
        },
        MembershipContentLocale.EN: {
            "title": "CSA Program Participation Terms",
            "sections": [
                {
                    "key": "parties",
                    "heading": "Party information",
                    "paragraphs": [
                        (
                            "The program is provided by Natural Farming Vietnam to the member "
                            "identified in the CSA purchase request and contract."
                        ),
                    ],
                },
                {
                    "key": "payment",
                    "heading": "One-time payment",
                    "paragraphs": [
                        (
                            "The member pays the full selected package amount once using the "
                            "transfer details in the purchase request. The program does not offer "
                            "installment payments."
                        ),
                    ],
                },
                {
                    "key": "program_operation",
                    "heading": "How the program works",
                    "paragraphs": [
                        (
                            "CSA connects members with seasonal production during the package "
                            "term. Product collection rights arise by cycle under the program "
                            "schedule."
                        ),
                    ],
                },
                {
                    "key": "product_quota",
                    "heading": "Product quotas",
                    "paragraphs": [
                        (
                            "Each product follows the quota snapshotted with the package. Unused "
                            "quantities follow the package's expire or rollover policy at purchase "
                            "time."
                        ),
                    ],
                },
                {
                    "key": "seasonal_changes",
                    "heading": "Seasonal changes",
                    "paragraphs": [
                        (
                            "Product varieties, sizes, and availability may change because of "
                            "seasons, weather, disease, and actual farming conditions."
                        ),
                    ],
                },
                {
                    "key": "substitution",
                    "heading": "Product substitution",
                    "paragraphs": [
                        (
                            "When an expected product is unavailable, the program may propose a "
                            "substitute of comparable value or use and communicate it through an "
                            "appropriate channel."
                        ),
                    ],
                },
                {
                    "key": "delivery",
                    "heading": "Delivery and collection",
                    "paragraphs": [
                        (
                            "Schedules, collection locations, service areas, and delivery charges "
                            "are communicated by area. Members must provide accurate address and "
                            "contact information."
                        ),
                    ],
                },
                {
                    "key": "pause_skip",
                    "heading": "Pausing or skipping a collection",
                    "paragraphs": [
                        (
                            "A pause or skip request must be submitted before the announced "
                            "deadline. Whether quota is preserved depends on the snapshotted "
                            "package policy."
                        ),
                    ],
                },
                {
                    "key": "communication",
                    "heading": "Communication channels",
                    "paragraphs": [
                        (
                            "Notices may be sent by email, telephone, Zalo, or WhatsApp using the "
                            "member's supplied contact details. Members should report contact "
                            "changes."
                        ),
                    ],
                },
                {
                    "key": "program_adjustments",
                    "heading": "Program adjustments",
                    "paragraphs": [
                        (
                            "The program may reasonably adjust schedules, collection points, or "
                            "operations with notice, without changing the price and term snapshots "
                            "of an issued contract."
                        ),
                    ],
                },
                {
                    "key": "liability",
                    "heading": "Limitation of liability",
                    "paragraphs": [
                        (
                            "To the extent permitted by law, program liability is limited to "
                            "providing or resolving the package's remaining benefits and excludes "
                            "indirect loss caused by events beyond reasonable control."
                        ),
                    ],
                },
            ],
        },
    }
}

# Keep V1 byte-for-byte stable: existing requests and Contracts verify their
# accepted content against its original canonical hash.
_CSA_TERMS_CONTENT[CSA_TERMS_V2] = copy.deepcopy(_CSA_TERMS_CONTENT[CSA_TERMS_V1])
_CSA_TERMS_CONTENT[CSA_TERMS_V2][MembershipContentLocale.VI]["sections"][1] = {
    "key": "payment",
    "heading": "Thanh toán",
    "paragraphs": [
        (
            "Tùy cấu hình từng gói, thành viên có thể trả thẳng hoặc trả góp. Tổng tiền, "
            "số lần thanh toán, số tiền từng lần và số kỳ tương ứng được hiển thị trước khi "
            "thành viên xác nhận yêu cầu."
        ),
        (
            "Khoản thanh toán đầu tiên cần được chuyển để yêu cầu được tiếp nhận và xử lý "
            "theo quy trình hiện hành. Membership chỉ được tạo sau khi yêu cầu được duyệt. "
            "Các khoản tiếp theo phải thanh toán đúng hạn."
        ),
        (
            "Admin kiểm tra và có quyền xác nhận hoặc từ chối thủ công từng giao dịch. "
            "Khoản tiếp theo quá hạn có thể chuyển trạng thái quá hạn và Membership có thể "
            "bị thu hồi theo quy định."
        ),
    ],
}
_CSA_TERMS_CONTENT[CSA_TERMS_V2][MembershipContentLocale.EN]["sections"][1] = {
    "key": "payment",
    "heading": "Payment",
    "paragraphs": [
        (
            "Depending on the package configuration, a member may pay in full or in "
            "installments. The total, number of payments, amount of each payment, and "
            "corresponding cycles are shown before the member accepts the request."
        ),
        (
            "The first payment must be transferred for the request to be received and "
            "processed under the current workflow. Membership is created only after the "
            "request is approved. Later installments must be paid by their due dates."
        ),
        (
            "An admin reviews and may manually confirm or reject each transaction. "
            "A later payment that misses its deadline may become overdue, and the "
            "Membership may be revoked under the applicable rules."
        ),
    ],
}


class InvalidCSATerms(ValueError):
    pass


def canonical_csa_terms_hash(content) -> str:
    serialized = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def get_csa_terms(*, version: str, locale: str) -> dict:
    try:
        content = _CSA_TERMS_CONTENT[version][locale]
    except KeyError as error:
        raise InvalidCSATerms("Unsupported CSA terms version or locale") from error
    content = copy.deepcopy(content)
    return {
        "version": version,
        "locale": locale,
        "content": content,
        "hash": canonical_csa_terms_hash(content),
    }


def get_current_csa_terms(locale: str) -> dict:
    return get_csa_terms(version=CURRENT_CSA_TERMS_VERSION, locale=locale)


def validate_csa_terms_snapshot(*, version: str, locale: str, content, content_hash: str):
    expected = get_csa_terms(version=version, locale=locale)
    if content != expected["content"] or not secrets.compare_digest(
        str(content_hash), expected["hash"]
    ):
        raise InvalidCSATerms("CSA terms snapshot or hash is invalid")
