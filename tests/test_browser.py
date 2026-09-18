import asyncio

from app.browser import (
    BrowserBridge,
    BrowserInputUnavailableError,
    BrowserResponseTimeout,
    PlaywrightError,
)


class FakeElement:
    def __init__(self, text, editable=True):
        self.text = text
        self.editable = editable
        self.fill_error = None
        self.native_fill_used = False
        self.present = True

    @property
    def first(self):
        return self

    async def wait_for(self, **kwargs):
        return None

    async def inner_text(self):
        return self.text

    async def is_editable(self):
        return self.editable

    async def fill(self, value, **kwargs):
        if self.fill_error:
            raise self.fill_error
        self.text = value

    async def evaluate(self, expression, value):
        self.native_fill_used = True
        self.text = value

    async def input_value(self):
        return self.text

    async def count(self):
        return int(self.present)

    async def press(self, key, **kwargs):
        return None


class FakePage:
    def __init__(self):
        self.input = FakeElement("")
        self.old_response = FakeElement("old response")

    def is_closed(self):
        return False

    def locator(self, selector):
        return self.input

    async def query_selector_all(self, selector):
        return [self.old_response]


def test_old_response_is_never_returned_as_new():
    async def run():
        bridge = BrowserBridge("./unused-test-profile")
        bridge.page = FakePage()
        bridge.ready = True
        bridge.RESPONSE_START_TIMEOUT = 0.01
        async with bridge.conversation(reset=False):
            try:
                await bridge.send_message_to_deepseek("new question")
            except BrowserResponseTimeout:
                return
        raise AssertionError("Expected BrowserResponseTimeout")

    asyncio.run(run())


def test_conversations_are_serialized_for_multiple_clients():
    async def run():
        bridge = BrowserBridge("./unused-test-profile")
        order = []

        async def client(name):
            async with bridge.conversation(reset=False):
                order.append(f"{name}:start")
                await asyncio.sleep(0.01)
                order.append(f"{name}:end")

        await asyncio.gather(client("first"), client("second"))
        assert order == [
            "first:start",
            "first:end",
            "second:start",
            "second:end",
        ]

    asyncio.run(run())


def test_non_editable_input_starts_cooldown():
    async def run():
        bridge = BrowserBridge("./unused-test-profile")
        bridge.page = FakePage()
        bridge.page.input.editable = False
        bridge.FAILURE_COOLDOWN_SECONDS = 5

        try:
            await bridge._wait_for_chat_input(timeout_ms=1)
        except BrowserInputUnavailableError:
            assert bridge.cooldown_error()
            return
        raise AssertionError("Expected BrowserInputUnavailableError")

    asyncio.run(run())


def test_native_setter_fallback_after_fill_failure():
    async def run():
        bridge = BrowserBridge("./unused-test-profile")
        chat_input = FakeElement("")
        chat_input.fill_error = PlaywrightError("textarea was replaced")

        await bridge._fill_chat_input(chat_input, "hello from fallback")

        assert chat_input.text == "hello from fallback"
        assert chat_input.native_fill_used is True

    asyncio.run(run())


def test_missing_input_skips_slow_native_fallback():
    async def run():
        bridge = BrowserBridge("./unused-test-profile")
        chat_input = FakeElement("")
        chat_input.fill_error = PlaywrightError("textarea disappeared")
        chat_input.present = False

        try:
            await bridge._fill_chat_input(chat_input, "hello")
        except BrowserInputUnavailableError:
            assert chat_input.native_fill_used is False
            return
        raise AssertionError("Expected BrowserInputUnavailableError")

    asyncio.run(run())


def test_send_reloads_once_when_input_disappears_before_fill():
    async def run():
        bridge = BrowserBridge("./unused-test-profile")
        page = FakePage()
        page.input.fill_error = PlaywrightError("textarea disappeared")
        page.input.present = False
        bridge.page = page
        bridge.ready = True
        bridge.RESPONSE_START_TIMEOUT = 0.01
        reloads = 0

        async def reload_chat():
            nonlocal reloads
            reloads += 1
            page.input = FakeElement("")

        bridge.start_new_conversation = reload_chat

        async with bridge.conversation(reset=False):
            try:
                await bridge.send_message_to_deepseek("hello")
            except BrowserResponseTimeout:
                pass

        assert reloads == 1
        assert page.input.text == "hello"

    asyncio.run(run())


def test_close_suppresses_already_closed_driver_errors():
    class BrokenContext:
        async def close(self):
            raise RuntimeError("Connection closed")

    class BrokenPlaywright:
        async def stop(self):
            raise RuntimeError("EPIPE")

    async def run():
        bridge = BrowserBridge("./unused-test-profile")
        bridge.browser_context = BrokenContext()
        bridge.playwright = BrokenPlaywright()
        await bridge.close()
        assert bridge.browser_context is None
        assert bridge.playwright is None

    asyncio.run(run())
