import asyncio

from app.browser import (
    BrowserBridge,
    BrowserInputUnavailableError,
    BrowserResponseTimeout,
)


class FakeElement:
    def __init__(self, text, editable=True):
        self.text = text
        self.editable = editable

    async def inner_text(self):
        return self.text

    async def is_editable(self):
        return self.editable

    async def fill(self, value, **kwargs):
        self.text = value

    async def press(self, key, **kwargs):
        return None


class FakePage:
    def __init__(self):
        self.input = FakeElement("")
        self.old_response = FakeElement("old response")

    def is_closed(self):
        return False

    async def wait_for_selector(self, selector, **kwargs):
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
