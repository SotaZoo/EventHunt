"""Authorization helpers shared by controllers."""


def can_access_product(*, is_authenticated: bool, is_active: bool) -> bool:
    return is_authenticated and is_active


def can_administer_runtime(*, is_authenticated: bool, is_active: bool) -> bool:
    # Operator/admin work stays CLI/runtime-only — no in-app /admin surface.
    return False
