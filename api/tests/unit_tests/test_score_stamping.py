from api.integrations.llm import compute_taste_sha


def test_compute_taste_sha_short_hex():
    sha = compute_taste_sha("I love AI agents")
    assert len(sha) == 12
    assert all(c in "0123456789abcdef" for c in sha)


def test_compute_taste_sha_stable():
    a = compute_taste_sha("profile text")
    b = compute_taste_sha("profile text")
    assert a == b


def test_compute_taste_sha_changes_on_edit():
    a = compute_taste_sha("v1")
    b = compute_taste_sha("v2")
    assert a != b
