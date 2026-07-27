from members.administrative_units import province_choices, ward_choices


def test_administrative_dataset_has_current_two_level_units():
    provinces = province_choices("vi")
    wards = [
        ward
        for province_code, _province_name in provinces
        for ward in ward_choices(province_code, "vi")
    ]

    assert len(provinces) == 34
    assert len(wards) == 3321
    assert len({code for code, _name in provinces}) == len(provinces)
    assert len({code for code, _name in wards}) == len(wards)


def test_bac_son_ward_name_is_normalized():
    lang_son_wards = dict(ward_choices("20", "vi"))

    assert lang_son_wards["06325"] == "Xã Bắc Sơn"
