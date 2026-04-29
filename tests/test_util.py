from zelda.util import slugify


def test_slugify_lowercases_and_dashes_spaces():
    assert slugify("New Delhi") == "new-delhi"


def test_slugify_strips_leading_trailing_whitespace():
    assert slugify("  Ludhiana  ") == "ludhiana"


def test_slugify_collapses_runs_of_unsafe_chars():
    assert slugify("Foo / Bar  --  Baz") == "foo-bar-baz"


def test_slugify_falls_back_to_placeholder_on_empty():
    assert slugify("///") == "unknown"
    assert slugify("") == "unknown"
    assert slugify("   ") == "unknown"


def test_slugify_keeps_alphanumerics_and_underscores():
    assert slugify("city_42") == "city_42"
    assert slugify("Sector_3 Block_B") == "sector_3-block_b"


def test_slugify_strips_leading_trailing_dashes():
    assert slugify("---foo---") == "foo"


def test_slugify_handles_apostrophes_as_separators():
    """Indian cities sometimes have apostrophes (D'Souza, Mary's, etc.).
    They become separators rather than disappearing."""
    assert slugify("Mary's Place") == "mary-s-place"
