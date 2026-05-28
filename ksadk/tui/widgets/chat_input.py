"""Chat input widget with history support."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, List

from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static, TextArea

from ksadk.tui.widgets.history import HistoryManager

if TYPE_CHECKING:
    from textual.app import ComposeResult


class ChatTextArea(TextArea):
    """TextArea subclass with custom key handling for chat input."""

    BINDINGS: ClassVar[List[Binding]] = [
        Binding(
            "shift+enter,ctrl+j,alt+enter,ctrl+enter",
            "insert_newline",
            "New Line",
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+a",
            "select_all_text",
            "Select All",
            show=False,
            priority=True,
        ),
    ]

    class Submitted(Message):
        """Message sent when text is submitted."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class HistoryPrevious(Message):
        """Request previous history entry."""

        def __init__(self, current_text: str) -> None:
            self.current_text = current_text
            super().__init__()

    class HistoryNext(Message):
        """Request next history entry."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.pop("placeholder", None)
        super().__init__(**kwargs)
        self._navigating_history = False

    def action_insert_newline(self) -> None:
        """Insert a newline character."""
        self.insert("\n")

    def action_select_all_text(self) -> None:
        """Select all text in the text area."""
        if not self.text:
            return
        lines = self.text.split("\n")
        end_row = len(lines) - 1
        end_col = len(lines[end_row])
        self.selection = ((0, 0), (end_row, end_col))

    async def _on_key(self, event: events.Key) -> None:
        """Handle key events."""
        if event.key in ("shift+enter", "ctrl+j", "alt+enter", "ctrl+enter"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return

        if event.key == "enter":
            event.prevent_default()
            event.stop()
            value = self.text.strip()
            if value:
                self.post_message(self.Submitted(value))
            return

        if event.key == "up":
            row, _ = self.cursor_location
            if row == 0:
                event.prevent_default()
                event.stop()
                self._navigating_history = True
                self.post_message(self.HistoryPrevious(self.text))
                return

        if event.key == "down":
            row, _ = self.cursor_location
            total_lines = self.text.count("\n") + 1
            if row == total_lines - 1:
                event.prevent_default()
                event.stop()
                self._navigating_history = True
                self.post_message(self.HistoryNext())
                return

        await super()._on_key(event)

    def set_text_from_history(self, text: str) -> None:
        """Set text from history navigation."""
        self._navigating_history = True
        self.text = text
        lines = text.split("\n")
        last_row = len(lines) - 1
        last_col = len(lines[last_row])
        self.move_cursor((last_row, last_col))
        self._navigating_history = False

    def clear_text(self) -> None:
        """Clear the text area."""
        self.text = ""
        self.move_cursor((0, 0))


class ChatInput(Vertical):
    """Chat input widget with prompt indicator, multi-line text, and history."""

    DEFAULT_CSS = """
    ChatInput {
        height: auto;
        min-height: 3;
        max-height: 12;
        padding: 0;
        background: $surface;
        border: solid $primary;
    }

    ChatInput .input-row {
        height: auto;
        width: 100%;
    }

    ChatInput .input-prompt {
        width: 3;
        height: 1;
        padding: 0 1;
        color: $primary;
        text-style: bold;
    }

    ChatInput ChatTextArea {
        width: 1fr;
        height: auto;
        min-height: 1;
        max-height: 8;
        border: none;
        background: transparent;
        padding: 0;
    }

    ChatInput ChatTextArea:focus {
        border: none;
    }
    """

    class Submitted(Message):
        """Message sent when input is submitted."""

        def __init__(self, value: str, mode: str = "normal") -> None:
            super().__init__()
            self.value = value
            self.mode = mode

    mode: reactive[str] = reactive("normal")

    def __init__(
        self,
        cwd: Optional[str | Path] = None,
        history_file: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._text_area: Optional[ChatTextArea] = None

        if history_file is None:
            history_file = Path.home() / ".ksadk" / "history.jsonl"
        self._history = HistoryManager(history_file)

    def compose(self) -> ComposeResult:
        """Compose the chat input layout."""
        with Horizontal(classes="input-row"):
            yield Static(">", classes="input-prompt", id="prompt")
            yield ChatTextArea(id="chat-input")

    def on_mount(self) -> None:
        """Initialize components after mount."""
        self._text_area = self.query_one("#chat-input", ChatTextArea)
        self._text_area.focus()

    def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        """Handle text submission."""
        value = event.value
        if value:
            self._history.add(value)
            self.post_message(self.Submitted(value, self.mode))
            if self._text_area:
                self._text_area.clear_text()
            self.mode = "normal"

    def on_chat_text_area_history_previous(self, event: ChatTextArea.HistoryPrevious) -> None:
        """Handle history previous request."""
        entry = self._history.get_previous(event.current_text)
        if entry is not None and self._text_area:
            self._text_area.set_text_from_history(entry)

    def on_chat_text_area_history_next(self, event: ChatTextArea.HistoryNext) -> None:
        """Handle history next request."""
        entry = self._history.get_next()
        if entry is not None and self._text_area:
            self._text_area.set_text_from_history(entry)

    def focus_input(self) -> None:
        if self._text_area:
            self._text_area.focus()

    @property
    def value(self) -> str:
        if self._text_area:
            return self._text_area.text
        return ""

    @value.setter
    def value(self, val: str) -> None:
        if self._text_area:
            self._text_area.text = val

    def set_disabled(self, *, disabled: bool) -> None:
        if self._text_area:
            self._text_area.disabled = disabled
            if disabled:
                self._text_area.blur()
