"""UI steps in plain words (M11 design, part 1): the one place a step becomes a sentence.

The runner puts these sentences in the video's captions and the trace; the API adds one to every
UI step it serves, so the web app prints them and holds no wording of its own. A step's strings
are used as they are: with placeholders when a spec is shown, filled when a case runs.

Copied into the runner image: imports only `core.specs`.
"""

from core.specs import Locator, UiStep

_ROLE_WORDS = {
    "textbox": "field",
    "searchbox": "search field",
    "spinbutton": "number field",
    "combobox": "drop-down",
    "listbox": "list box",
    "listitem": "list item",
    "img": "image",
    "columnheader": "column header",
    "menuitem": "menu item",
    "progressbar": "progress bar",
    "tablist": "tab list",
    "tabpanel": "tab panel",
    "contentinfo": "footer",
    "banner": "header",
    "complementary": "side panel",
    "navigation": "navigation",
    "radio": "radio button",
}


def quoted(text: str) -> str:
    return f"“{text}”"


def locator_words(locator: Locator) -> str:
    """'the button named “Add”', 'the field labelled “New item”'."""
    if locator.role is not None:
        role = _ROLE_WORDS.get(locator.role, locator.role)
        if locator.name is None:
            return f"a {role}"
        return f"the {role} named {quoted(locator.name)}"
    if locator.label is not None:
        return f"the field labelled {quoted(locator.label)}"
    if locator.placeholder is not None:
        return f"the field with the hint {quoted(locator.placeholder)}"
    if locator.text is not None:
        return f"the text {quoted(locator.text)}"
    return f"the element with test ID {quoted(locator.test_id or '')}"


def step_words(step: UiStep) -> str:
    """One sentence for one step."""
    where = locator_words(step.locator) if step.locator is not None else ""
    match step.action:
        case "goto":
            return f"Open the page at {step.path}"
        case "fill":
            return f"Type {quoted(step.value or '')} into {where}"
        case "click":
            return f"Click {where}"
        case "select":
            return f"Choose {quoted(step.value or '')} in {where}"
        case "press":
            return f"Press {step.key} in {where}" if where else f"Press {step.key}"
        case "wait_for":
            return f"Wait for {where} to appear"
        case "expect_visible":
            return f"Check that {where} is visible"
        case "expect_text":
            return f"Check that {where} shows {quoted(step.text or '')}"
        case "expect_value":
            if not step.value:
                return f"Check that {where} is empty"
            return f"Check that {where} holds {quoted(step.value)}"
        case "expect_url":
            return f"Check that the page's address is {step.path}"
    raise AssertionError(step.action)  # pragma: no cover
