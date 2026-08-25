from validate_cross_dso import NEEDED_RE


def test_needed_parser_extracts_provider_names():
    text = "0x1 (NEEDED) Shared library: [libcore.so]\n0x1 (NEEDED) Shared library: [libc.so.6]"
    assert set(NEEDED_RE.findall(text)) == {"libcore.so", "libc.so.6"}
