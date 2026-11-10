from api.services.authorization import can_access_product, can_administer_runtime


def test_active_user_can_access_product_but_not_runtime_admin():
    assert can_access_product(is_authenticated=True, is_active=True) is True
    assert can_administer_runtime(is_authenticated=True, is_active=True) is False
