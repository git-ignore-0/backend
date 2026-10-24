from datetime import date

import pytest
from django.db import IntegrityError, transaction

from commerce.models import Category, Product
from farms.models import Farm, FarmProduct, FarmWeeklySupply


@pytest.mark.django_db
def test_farm_product_is_unique_and_weekly_quantity_uses_sale_units():
    farm = Farm.objects.create(
        name_en="Green Farm",
        name_vi="Nông trại Xanh",
        location_en="Da Lat",
        location_vi="Đà Lạt",
    )
    category = Category.objects.create(slug="vegetables", is_active=True)
    product = Product.objects.create(
        category=category,
        sku="SPINACH-500G",
        slug="spinach-500g",
        unit_size="500.000",
        unit_label_vi="gói",
        unit_label_en="pack",
        retail_price_vnd=25000,
    )
    farm_product = FarmProduct.objects.create(farm=farm, product=product)
    supply = FarmWeeklySupply.objects.create(
        farm_product=farm_product,
        week_start=date(2026, 10, 19),
        quantity_units=10,
    )

    assert supply.quantity_units == 10
    assert farm_product.farm_id == farm.pk
    assert farm_product.product_id == product.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        FarmProduct.objects.create(farm=farm, product=product)
    with pytest.raises(IntegrityError), transaction.atomic():
        FarmWeeklySupply.objects.create(
            farm_product=farm_product,
            week_start=date(2026, 10, 19),
            quantity_units=11,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        FarmWeeklySupply.objects.filter(pk=supply.pk).update(quantity_units=-1)
