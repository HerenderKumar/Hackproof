"""The scanner blanks what looks like a key before anything leaves the device (M9, Task 10)."""

import pytest

from tests.priorwork_fakes import block
from tests.scanner_module import load

scan = load()
MARK = scan.KEY_MARKER

PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AHB7MhgHcTz6sE2I2yPB\n"
    "aFDrBz9vFqU4yQIDAQABAoIBAQC5Rj2N\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAY34FZKBOKMUTVV7A",
        "ghp_" + "a1B2c3D4e5" * 4,
        "gho_" + "Z9y8X7w6V5" * 4,
        "github_pat_" + "11ABCDEFG0" * 7,
        "sk-" + "proj4Hd8Kq2Lm9Xc7Vb3Nw",
        "sk_live_" + "51HqJ2KLm9Xc7Vb3",
        "rk_live_" + "51HqJ2KLm9Xc7Vb3",
        "xoxb-" + "1234567890-abcdefghij",
        "AIza" + "SyD3k9Lm2Xc7Vb3Nw8Qp4Rt6Yh1Uj5Ik0Ol",
        "glpat-" + "x9Yz8Wv7Ut6Sr5Qp4On3",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        PRIVATE_KEY,
    ],
)
def test_a_key_shape_is_blanked(secret: str) -> None:
    text = f'const x = "{secret}";\nnext();\n'
    blanked, count = scan.blank_keys(text)
    assert count >= 1 and MARK in blanked
    assert secret not in blanked
    assert blanked.count("\n") == text.count("\n")


def test_a_secret_assigned_to_a_telling_name_is_blanked() -> None:
    cases = [
        'API_KEY = "hunter2hunter2"',
        "db_password: 'correct-horse'",
        '{"clientSecret": "abcd1234efgh"}',
        "const authToken = `t0k3n-value-x`;",
        'PWD="letmein123"',
        'aws_credentials = "x9x9x9x9x9"',
    ]
    for line in cases:
        blanked, count = scan.blank_keys(line)
        assert count == 1 and MARK in blanked, line
    short, count = scan.blank_keys('password = "short"')
    assert count == 0 and short == 'password = "short"'


def test_a_long_random_string_is_blanked_and_a_long_sentence_is_not() -> None:
    random_value = 'x = "Zx8Qp2Lm9Rt4Vb7Nc1Kd6Fh3Gj5Sw0Ty"'
    assert scan.blank_keys(random_value) == (f'x = "{MARK}"', 1)
    for line in (
        'msg = "this is a long sentence about nothing at all really"',
        'x = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
        'url = "https://example.com/api/v1/users/profile"',
    ):
        assert scan.blank_keys(line) == (line, 0)


def test_blanking_keeps_the_lines_and_the_quotes() -> None:
    text = f"const k = `{PRIVATE_KEY}`;\nconst t = 'ghp_{'a' * 36}';\nend();\n"
    blanked, count = scan.blank_keys(text)
    assert count == 2
    assert blanked.splitlines()[0] == f"const k = `{MARK}"
    assert blanked.count("\n") == text.count("\n")
    assert f"const t = '{MARK}';" in blanked and blanked.endswith(
        "`;\nconst t = '" + MARK + "';\nend();\n"
    )


ORDINARY_PY = '''
import json
from pathlib import Path


def load(path: Path) -> dict:
    """Read a settings file."""
    with open(path) as handle:
        return json.load(handle)


class Basket:
    def __init__(self) -> None:
        self.items = []

    def add(self, name: str, price: float) -> None:
        if price < 0:
            raise ValueError("price must not be negative")
        self.items.append({"name": name, "price": price})

    def total(self) -> float:
        return round(sum(item["price"] for item in self.items), 2)
'''


def test_ordinary_code_is_left_alone() -> None:
    sample = "".join(block(n) for n in range(14)) + ORDINARY_PY * 2
    assert len(sample.splitlines()) >= 200
    assert scan.blank_keys(sample) == (sample, 0)
