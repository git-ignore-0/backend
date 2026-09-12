from copy import deepcopy

NFV_PROVIDER_V1 = "NFV_PROVIDER_V1"
CURRENT_NFV_PROVIDER_VERSION = NFV_PROVIDER_V1

_PROVIDERS = {
    NFV_PROVIDER_V1: {
        "name": "Natural Farming Vietnam",
        "representative": "Lê Thị An Khánh",
        "address": ("800/2 Ấp Phú Ngãi, Xã Phú An Hòa, Huyện Châu Thành, Tỉnh Bến Tre"),
        "email": "khanh@nongnghiepthiennhien.com",
        "phone_zalo": "(+84) 988.158.285",
        "websites": {
            "vi": "https://nongnghiepthiennhien.com",
            "en": "https://nongnghiepthiennhien.com/en",
        },
    }
}


class InvalidCSAContractProvider(ValueError):
    pass


def get_contract_provider(version: str, locale: str) -> dict:
    try:
        provider = deepcopy(_PROVIDERS[version])
        provider["website"] = provider.pop("websites")[locale]
    except KeyError as error:
        raise InvalidCSAContractProvider(
            "Unsupported CSA contract provider version or locale"
        ) from error
    provider["version"] = version
    return provider
