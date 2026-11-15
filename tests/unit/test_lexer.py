"""The prior-work lexer (M9 scanner, A8): source text to symbols that survive reformatting."""

import time

from core.priorwork.lexer import NAME, NUMBER, STRING, family, tokenize


def syms(source: str, ext: str = ".js") -> list[str]:
    return [t.sym for t in tokenize(source, ext)]


def test_comments_and_whitespace_are_dropped() -> None:
    c = "a = 1; // note\n/* block\n comment */  b = 2;"
    assert syms(c) == ["N", "=", "0", ";", "N", "=", "0", ";"]
    py = "a = 1  # note\n\n\tb = 2\n"
    assert syms(py, ".py") == ["N", "=", "0", "N", "=", "0"]


def test_names_strings_and_numbers_become_one_symbol_each() -> None:
    assert syms("total = price * 3") == syms("sum = cost * 42") == ["N", "=", "N", "*", "0"]
    assert syms("x = \"hello\" + 'w' + `t`") == ["N", "=", STRING, "+", STRING, "+", STRING]
    assert (NAME, STRING, NUMBER) == ("N", "S", "0")
    assert syms("n = 3.14e5 + 0x1F + 1_000") == ["N", "=", "0", "+", "0", "+", "0"]


def test_keywords_and_punctuation_stay() -> None:
    assert syms("if (a) { b }") != syms("for (a) { b }")
    assert syms("if (a) { b }")[0] == "if"
    assert syms("(") != syms("{")
    assert syms("def f(): return x", ".py") == ["def", "N", "(", ")", ":", "return", "N"]


def test_a_token_keeps_its_text_and_its_line() -> None:
    tokens = tokenize("let a = 1;\n\nconst bee = 'x';\n", ".js")
    bee = next(t for t in tokens if t.text == "bee")
    assert bee.sym == "N" and bee.line == 3
    assert next(t for t in tokens if t.sym == "S").text == "'x'"


def test_python_docstrings_and_prefixed_strings_are_one_string() -> None:
    source = 'def f():\n    """Doc\n    string."""\n    return rb"x" + f\'\'\'a\nb\'\'\' + u"y"\n'
    assert syms(source, ".py") == [
        "def", "N", "(", ")", ":", "S", "return", "S", "+", "S", "+", "S",
    ]  # fmt: skip
    assert tokenize(source, ".py")[6].line == 4


def test_template_strings_and_block_comments_span_lines() -> None:
    tokens = tokenize("a = `one\ntwo\nthree`; /* x\ny */ b", ".ts")
    assert [t.sym for t in tokens] == ["N", "=", "S", ";", "N"]
    assert tokens[-1].line == 4


def test_an_unterminated_string_or_comment_never_raises() -> None:
    assert syms('a = "open\nb = 2') == ["N", "=", "S", "N", "=", "0"]
    assert syms("a /* never closed\n b c") == ["N"]
    assert syms("a = `never closed\n b") == ["N", "=", "S"]
    assert syms("x = '''open\n y", ".py") == ["N", "=", "S"]
    assert syms('x = "\\', ".py") == ["N", "=", "S"]


def test_an_unknown_extension_is_read_as_c_like_with_no_keywords() -> None:
    assert family(".rs") == "c" and family(".py") == "python"
    assert syms("if (a) { b } // c", ".zz") == ["N", "(", "N", ")", "{", "N", "}"]


def test_the_letter_case_of_an_extension_is_ignored() -> None:
    source = "def f(x):\n    return x  # c\n"
    assert syms(source, ".PY") == syms(source, ".py")
    assert family(".Py") == "python"


def test_a_one_megabyte_single_line_is_tokenized_within_seconds() -> None:
    line = "var a=b+c*(d-1),e='s';" * (1024 * 1024 // 22)
    started = time.monotonic()
    tokens = tokenize(line, ".js")
    assert time.monotonic() - started < 5
    assert len(tokens) > 400_000 and tokens[-1].line == 1
