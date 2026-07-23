## Task 1: Shared response-envelope + log-tail helpers

**Files:**
- Modify: `src/foundry_mcp/utils.py` (append two functions after `remove_none`)
- Test: `test/test_utils.py`

**Interfaces:**
- Produces:
  - `envelope(summary: str, data=None, next_steps: list=None) -> dict` — returns a dict whose keys are ordered `summary`, `next_steps` (only if truthy), `data` (defaults to `{}`).
  - `tail_text(text, max_lines: int = 200, max_chars: int = 12000) -> str` — returns the last `max_lines` lines of `text`, then truncates to the last `max_chars` characters; returns `""` for falsy input.

- [ ] **Step 1: Write the failing tests**

Append to `test/test_utils.py`:

```python
from src.foundry_mcp.utils import tail_text, envelope


def test_tail_text_returns_last_n_lines():
    text = "\n".join(str(i) for i in range(1, 501))
    assert tail_text(text, max_lines=3) == "498\n499\n500"


def test_tail_text_caps_chars_keeping_tail():
    text = "x" * 100 + "TAIL"
    assert tail_text(text, max_lines=10, max_chars=4) == "TAIL"


def test_tail_text_handles_empty():
    assert tail_text("") == ""
    assert tail_text(None) == ""


def test_envelope_orders_summary_next_steps_data():
    result = envelope("did a thing", data={"x": 1}, next_steps=["do next"])
    assert list(result.keys()) == ["summary", "next_steps", "data"]
    assert result["summary"] == "did a thing"
    assert result["data"] == {"x": 1}
    assert result["next_steps"] == ["do next"]


def test_envelope_omits_next_steps_when_empty():
    result = envelope("ok")
    assert "next_steps" not in result
    assert result["data"] == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test/test_utils.py -v`
Expected: FAIL with `ImportError: cannot import name 'tail_text'` (and `envelope`).

- [ ] **Step 3: Implement the helpers**

Append to `src/foundry_mcp/utils.py`:

```python
def tail_text(text, max_lines: int = 200, max_chars: int = 12000) -> str:
    """Return the last `max_lines` lines of `text`, capped at `max_chars`
    characters (keeping the tail — the end of a log is where errors live)."""
    if not text:
        return ""
    tail = "\n".join(text.splitlines()[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def envelope(summary: str, data=None, next_steps=None) -> dict:
    """Wrap a tool result in the standard bench-scientist response shape:
    a plain-language `summary`, optional `next_steps` suggestions, and the
    structured `data` payload. Key order is summary, next_steps, data."""
    result = {"summary": summary}
    if next_steps:
        result["next_steps"] = next_steps
    result["data"] = data if data is not None else {}
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test/test_utils.py -v`
Expected: PASS (all envelope/tail tests green, existing tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/foundry_mcp/utils.py test/test_utils.py
git commit -m "feat(mcp): add envelope + tail_text response helpers"
```

---

