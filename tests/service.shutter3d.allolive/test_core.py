from shutter3d import core


def test_fa_sync_lines():
    assert core.parse_fa_sync("123 4567890123 L\n") == (123, 4567890123, "L")
    assert core.parse_fa_sync("456 4567890123 -\n") == (456, 4567890123, "-")
    assert core.parse_fa_sync("off\n") is None
    assert core.parse_fa_sync("") is None
    assert core.parse_fa_sync("1 x L") is None
