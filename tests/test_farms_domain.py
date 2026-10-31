from datetime import date

import pytest
from django.db import IntegrityError, transaction

from commerce.models import Category, Product
from farms.models import Farm, FarmProduct, FarmProductStockBatch


@pytest.mark.django_db
def test_farm_product_is_unique_and_stock_batch_quantity_uses_sale_units():
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
    batch = FarmProductStockBatch.objects.create(
        farm_product=farm_product,
        start_date=date(2026, 10, 19),
        end_date=date(2026, 10, 25),
        quantity_units=10,
    )

    assert batch.quantity_units == 10
    assert farm_product.farm_id == farm.pk
    assert farm_product.product_id == product.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        FarmProduct.objects.create(farm=farm, product=product)
    with pytest.raises(IntegrityError), transaction.atomic():
        FarmProductStockBatch.objects.filter(pk=batch.pk).update(quantity_units=-1)
