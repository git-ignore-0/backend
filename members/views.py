from django.http import JsonResponse
from django.views.decorators.http import require_GET

from members.administrative_units import province_choices, province_exists, ward_choices


@require_GET
def provinces(request):
    return JsonResponse(
        {
            "data": [
                {"code": code, "name": name}
                for code, name in province_choices(request.LANGUAGE_CODE)
            ]
        }
    )


@require_GET
def wards(request):
    province_code = request.GET.get("province", "")
    if not province_exists(province_code):
        return JsonResponse({"error": "invalid_province"}, status=400)

    return JsonResponse(
        {
            "data": [
                {"code": code, "name": name}
                for code, name in ward_choices(province_code, request.LANGUAGE_CODE)
            ]
        }
    )
