"""The Telegram menu, against a fake Telegram."""
from datetime import datetime

import pytest

from tradebot.bot.telegram_bot import (MENU, TradeAssistant, fno_selling_check, options_buying_check)

ME = 4242


class FakeTelegram:
    def __init__(self):
        self.calls, self.queue = [], []

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "getUpdates":
            out, self.queue = [u for u in self.queue if u["update_id"] >= payload["offset"]], []
            return out
        return {}

    def texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


class FakeSwing:
    def __init__(self):
        self.runs = 0

    def run(self):
        self.runs += 1
        return "Swing scan: buy 5 SBIN"

    def skip(self, symbol):
        return f"{symbol.upper()} removed."

    def holdings(self):
        return "Swing: nothing held."


def message(text, chat=ME, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def tap(key, chat=ME, uid=1):
    return {"update_id": uid, "callback_query": {"id": "cb1", "data": key, "message": {"chat": {"id": chat}}}}


@pytest.fixture
def bot(tmp_path):
    clock = {"now": datetime(2026, 11, 12, 10, 0)}        # a Thursday
    api, swing, started = FakeTelegram(), FakeSwing(), []
    b = TradeAssistant(api, ME, swing, 20_000, tmp_path / "assistant.json", background=False,
                       start_intraday=lambda: (started.append(1) or True, "Intraday adviser is starting."),
                       stop_intraday=lambda: started.append("stop"), now=lambda: clock["now"])
    b.clock, b.started = clock, started
    return b


def test_any_message_shows_the_four_options(bot):
    bot.handle(message("hi"))
    method, payload = bot.api.calls[-1]
    assert method == "sendMessage" and payload["chat_id"] == str(ME)
    buttons = [row[0] for row in payload["reply_markup"]["inline_keyboard"]]
    assert [b["callback_data"] for b in buttons][:4] == ["swing", "intraday", "options", "fno_sell"]
    assert [b["text"] for b in buttons] == [label for _, label in MENU]
    assert "you place every order yourself" in payload["text"]


def test_strangers_get_no_answer_and_cannot_press_buttons(bot):
    bot.handle(message("hi", chat=999))
    assert bot.api.texts() == []
    bot.handle(tap("swing", chat=999))
    assert bot.api.texts() == [] and bot.swing.runs == 0 and not bot.state["swing_on"]


def test_swing_button_scans_now_and_then_once_each_weekday_evening(bot):
    bot.handle(tap("swing"))
    assert bot.api.calls[0][0] == "answerCallbackQuery"
    assert bot.api.texts()[-1] == "Swing scan: buy 5 SBIN" and bot.swing.runs == 1
    assert bot.state["swing_on"] and bot.state["last_scan"] == "2026-11-12"

    bot.clock["now"] = datetime(2026, 11, 12, 16, 30)
    bot.tick()
    assert bot.swing.runs == 1                            # already scanned today
    bot.clock["now"] = datetime(2026, 11, 13, 16, 14)
    bot.tick()
    assert bot.swing.runs == 1                            # before 16:15
    bot.clock["now"] = datetime(2026, 11, 13, 16, 15)
    bot.tick()
    bot.tick()
    assert bot.swing.runs == 2                            # once, not twice
    bot.clock["now"] = datetime(2026, 11, 14, 17, 0)      # Saturday
    bot.tick()
    assert bot.swing.runs == 2


def test_swing_setting_survives_a_restart(bot, tmp_path):
    bot.handle(tap("swing"))
    again = TradeAssistant(FakeTelegram(), ME, FakeSwing(), 20_000, tmp_path / "assistant.json")
    assert again.state["swing_on"] and again.state["last_scan"] == "2026-11-12"


def test_scan_failure_is_reported_not_fatal(bot):
    def boom():
        raise OSError("no internet")
    bot.swing.run = boom
    bot.handle(tap("swing"))
    assert "swing scan failed" in bot.api.texts()[-1]


def test_intraday_button_starts_the_adviser(bot):
    bot.handle(tap("intraday"))
    assert bot.started == [1] and bot.api.texts()[-1] == "Intraday adviser is starting."


def test_fno_options_are_blocked_at_small_capital_with_the_numbers(bot):
    bot.handle(tap("options"))
    text = bot.api.texts()[-1]
    assert "blocked" in text and "Rs 9,750" in text and "Rs 2,925" in text and "Rs 200" in text
    bot.handle(tap("fno_sell"))
    text = bot.api.texts()[-1]
    assert "not possible" in text and "Rs 175,000" in text and "Rs 20,000" in text


def test_fno_gates_open_only_with_enough_capital():
    assert options_buying_check(20_000)[0] is False
    assert options_buying_check(100_000)[0] is False       # still 2.9% of capital at risk
    ok, text = options_buying_check(300_000)
    assert ok and "not been built" in text                 # allowed, but no untested signals
    assert fno_selling_check(200_000)[0] is False
    assert fno_selling_check(400_000)[0] is True


def test_status_stop_and_skip(bot):
    bot.handle(tap("swing"))
    bot.handle(message("/status"))
    assert "Swing alerts: on (last scan 2026-11-12)" in bot.api.texts()[-1]
    bot.handle(message("/skip sbin"))
    assert bot.api.texts()[-1] == "SBIN removed."
    bot.handle(tap("stop"))
    assert not bot.state["swing_on"] and bot.started == ["stop"] and "Stopped" in bot.api.texts()[-1]


def test_polling_advances_the_offset_and_survives_a_bad_update(bot):
    bot.api.queue = [{"update_id": 7, "message": None}, message("hi", uid=8)]
    bot.poll_once(timeout=0)
    assert bot.offset == 9 and len(bot.api.texts()) == 1
    bot.poll_once(timeout=0)
    assert bot.api.calls[-1] == ("getUpdates", {"offset": 9, "timeout": 0,
                                                "allowed_updates": ["message", "callback_query"]})
