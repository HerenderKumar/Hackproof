"""Source text to tokens for the prior-work comparison (D-052, the M9 plan's A8).

Comments and whitespace are dropped. A name becomes the symbol `N`, a string `S`, a number `0`;
a keyword or a punctuation character is its own symbol. Each token keeps its text as written and
its line, so a match can be shown as the lines it covers. Two families: Python, and C-like for
everything else (an extension with no keyword table is C-like with no keywords).

One compiled pattern per family, read with `finditer`. No alternative repeats a repetition, so a
long line costs time in proportion to its length.
"""

import keyword
import re
from typing import Literal, NamedTuple

LEXER_VERSION = "1"
NAME, STRING, NUMBER = "N", "S", "0"

Family = Literal["c", "python"]


class Token(NamedTuple):
    sym: str
    text: str
    line: int


_JS = [
    "break",
    "case",
    "catch",
    "class",
    "const",
    "continue",
    "debugger",
    "default",
    "delete",
    "do",
    "else",
    "export",
    "extends",
    "false",
    "finally",
    "for",
    "function",
    "if",
    "import",
    "in",
    "instanceof",
    "let",
    "new",
    "null",
    "return",
    "super",
    "switch",
    "this",
    "throw",
    "true",
    "try",
    "typeof",
    "var",
    "void",
    "while",
    "with",
    "yield",
    "async",
    "await",
    "of",
    "static",
    "get",
    "set",
    "enum",
    "implements",
    "interface",
    "package",
    "private",
    "protected",
    "public",
    "type",
    "declare",
    "namespace",
    "abstract",
    "readonly",
    "keyof",
    "as",
]
_C = [
    "auto",
    "break",
    "case",
    "char",
    "const",
    "continue",
    "default",
    "do",
    "double",
    "else",
    "enum",
    "extern",
    "float",
    "for",
    "goto",
    "if",
    "inline",
    "int",
    "long",
    "register",
    "restrict",
    "return",
    "short",
    "signed",
    "sizeof",
    "static",
    "struct",
    "switch",
    "typedef",
    "union",
    "unsigned",
    "void",
    "volatile",
    "while",
    "bool",
    "true",
    "false",
    "NULL",
]
_CPP = [
    *_C,
    "class",
    "namespace",
    "template",
    "typename",
    "public",
    "private",
    "protected",
    "virtual",
    "override",
    "new",
    "delete",
    "this",
    "try",
    "catch",
    "throw",
    "using",
    "operator",
    "friend",
    "nullptr",
    "constexpr",
    "noexcept",
    "explicit",
    "mutable",
    "static_cast",
    "dynamic_cast",
    "reinterpret_cast",
    "const_cast",
    "decltype",
]
_JAVA = [
    "abstract",
    "assert",
    "boolean",
    "break",
    "byte",
    "case",
    "catch",
    "char",
    "class",
    "const",
    "continue",
    "default",
    "do",
    "double",
    "else",
    "enum",
    "extends",
    "final",
    "finally",
    "float",
    "for",
    "goto",
    "if",
    "implements",
    "import",
    "instanceof",
    "int",
    "interface",
    "long",
    "native",
    "new",
    "package",
    "private",
    "protected",
    "public",
    "return",
    "short",
    "static",
    "strictfp",
    "super",
    "switch",
    "synchronized",
    "this",
    "throw",
    "throws",
    "transient",
    "try",
    "void",
    "volatile",
    "while",
    "true",
    "false",
    "null",
    "var",
    "record",
    "yield",
]
_GO = [
    "break",
    "case",
    "chan",
    "const",
    "continue",
    "default",
    "defer",
    "else",
    "fallthrough",
    "for",
    "func",
    "go",
    "goto",
    "if",
    "import",
    "interface",
    "map",
    "package",
    "range",
    "return",
    "select",
    "struct",
    "switch",
    "type",
    "var",
    "true",
    "false",
    "nil",
]

KEYWORDS: dict[str, frozenset[str]] = {
    ".py": frozenset(keyword.kwlist),
    **dict.fromkeys((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"), frozenset(_JS)),
    **dict.fromkeys((".c", ".h"), frozenset(_C)),
    **dict.fromkeys((".cpp", ".cc", ".hpp", ".cxx", ".hh"), frozenset(_CPP)),
    ".java": frozenset(_JAVA),
    ".go": frozenset(_GO),
}

_C_LIKE = re.compile(
    r"""
    (?P<skip>\s+|//[^\n]*|/\*[\s\S]*?(?:\*/|\Z))
    |(?P<string>"(?:[^"\\\n]|\\.?)*(?:"|$)|'(?:[^'\\\n]|\\.?)*(?:'|$)|`(?:[^`\\]|\\[\s\S]?)*(?:`|\Z))
    |(?P<number>\d[\w.]*)
    |(?P<word>(?:[^\W\d]|\$)[\w$]*)
    |(?P<other>\S)
    """,
    re.VERBOSE | re.MULTILINE,
)
_PYTHON = re.compile(
    r"""
    (?P<skip>\s+|\#[^\n]*)
    |(?P<string>(?:[rbfuRBFU]{1,2})?
        (?:'''[\s\S]*?(?:'''|\Z)|\"\"\"[\s\S]*?(?:\"\"\"|\Z)
          |'(?:[^'\\\n]|\\.?)*(?:'|$)|"(?:[^"\\\n]|\\.?)*(?:"|$)))
    |(?P<number>\d[\w.]*)
    |(?P<word>[^\W\d]\w*)
    |(?P<other>\S)
    """,
    re.VERBOSE | re.MULTILINE,
)


def family(ext: str) -> Family:
    return "python" if ext.lower() == ".py" else "c"


def tokenize(source: str, ext: str) -> list[Token]:
    ext = ext.lower()
    pattern = _PYTHON if family(ext) == "python" else _C_LIKE
    words = KEYWORDS.get(ext, frozenset())
    tokens: list[Token] = []
    line, seen = 1, 0
    append = tokens.append
    for match in pattern.finditer(source):
        kind = match.lastgroup
        if kind == "skip":
            continue
        start = match.start()
        line += source.count("\n", seen, start)
        seen = start
        text = match.group()
        if kind == "string":
            sym = STRING
        elif kind == "number":
            sym = NUMBER
        elif kind == "word":
            sym = text if text in words else NAME
        else:
            sym = text
        append(Token(sym, text, line))
    return tokens
