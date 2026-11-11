"""Exercise the sync, ambient-light and NumLock logic with fake modules and a fake sysfs."""
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "framework-keyboard-toolkit"


class FakeModule:
    """Stands in for Module: a backlight with `steps` levels above zero."""

    def __init__(self, name, pid, level, steps=3):
        self.name, self.pid = name, pid
        self.channel = 1
        self.actual = level        # what the hardware shows
        self.level = level         # what the toolkit last read
        self.sent = None
        self.steps = steps
        self.sets = []
        self.dead = False
        self.closed = False

    label = property(lambda self: self.name)

    def get(self):
        return None if self.dead else self.actual

    def set(self, level):
        if self.dead:
            return False
        self.sets.append(level)
        self.actual = (level * self.steps // 255) * 255 // self.steps
        return True

    def close(self):
        self.closed = True


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("toolkit", str(SCRIPT))
        self.tk = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.tk)
        self.tk.log = lambda message: None

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.hidraw = root / "hidraw"
        self.sessions = root / "sessions"
        self.leds = root / "leds"
        self.iio = root / "iio"
        for d in (self.hidraw, self.sessions, self.leds, self.iio):
            d.mkdir()
        self.tk.HIDRAW_SYS = str(self.hidraw)
        self.tk.SESSIONS_DIR = str(self.sessions)
        self.tk.LED_GLOB = str(self.leds / "input*::numlock")
        self.tk.ALS_GLOB = str(self.iio / "iio:device*" / "in_illuminance_raw")

        # Fake hardware: hidraw names map to FakeModules instead of real nodes.
        self.hardware = {}
        self.tk.identify = lambda name: getattr(self.hardware.get(name), "pid", None)
        self.tk.open_module = lambda name, pid: (
            None if self.hardware[name].dead else self.hardware[name])
        self.presses = []
        self.tk.press_numlock = lambda: self.presses.append(self.now)
        self.shown = []
        self.tk.show_numlock = self.shown.append

        self.now = 100.0
        self.kit = self.tk.Toolkit(clock=lambda: self.now)

    # -- helpers -----------------------------------------------------------------

    def plug(self, name, pid, level, **kw):
        self.hardware[name] = FakeModule(name, pid, level, **kw)
        (self.hidraw / name).mkdir()
        return self.hardware[name]

    def unplug(self, name):
        (self.hidraw / name).rmdir()
        return self.hardware.pop(name)

    def keyboard_and_numpad(self, kbd=255, pad=255):
        return self.plug("hidraw7", "0012", kbd), self.plug("hidraw3", "0014", pad)

    def light(self, lux):
        dev = self.iio / "iio:device0"
        dev.mkdir(exist_ok=True)
        (dev / "in_illuminance_raw").write_text(f"{lux}\n")

    def numpad_led(self, on):
        led = self.leds / "input12::numlock"
        (led / "device" / "id").mkdir(parents=True, exist_ok=True)
        (led / "device" / "id" / "vendor").write_text("32ac\n")
        (led / "device" / "id" / "product").write_text("0014\n")
        (led / "brightness").write_text("1\n" if on else "0\n")

    def session(self, name, seat="seat0"):
        (self.sessions / name).write_text(
            "ACTIVE=1\nCLASS=user\n" + (f"SEAT={seat}\n" if seat else ""))

    def tick(self, advance=None):
        """Run one pass, then move the clock by the interval it asked for."""
        interval = self.kit.tick()
        self.now += interval if advance is None else advance
        return interval

    def ticks(self, seconds):
        end = self.now + seconds
        while self.now < end:
            self.tick()

    def no_auto(self):
        self.tk.AMBIENT = False

    def no_numlock(self):
        self.tk.NUMLOCK = False

    # -- sync --------------------------------------------------------------------

    def test_numpad_follows_keyboard_at_start(self):
        self.no_auto()
        kbd, pad = self.keyboard_and_numpad(kbd=85, pad=255)
        self.tick()
        self.assertEqual((kbd.actual, pad.actual), (85, 85))
        self.assertEqual(kbd.sets, [])

    def test_keyboard_key_is_copied_to_numpad(self):
        self.no_auto()
        kbd, pad = self.keyboard_and_numpad()
        self.tick()
        kbd.actual = 0                      # Fn+Space on the keyboard
        self.tick()
        self.assertEqual(pad.actual, 0)
        self.assertEqual(kbd.sets, [])

    def test_numpad_key_is_copied_to_keyboard(self):
        self.no_auto()
        kbd, pad = self.keyboard_and_numpad()
        self.tick()
        pad.actual = 170
        self.tick()
        self.assertEqual(kbd.actual, 170)
        self.assertEqual(pad.sets, [])

    def test_manual_change_polls_faster_for_a_while(self):
        self.no_auto()
        self.no_numlock()
        kbd, _ = self.keyboard_and_numpad()
        self.assertEqual(self.tick(), self.tk.TICK_S)
        kbd.actual = 170
        self.assertEqual(self.tick(), self.tk.FAST_TICK_S)
        self.now += self.tk.FAST_FOR_S
        self.assertEqual(self.tick(), self.tk.TICK_S)

    def test_idle_ticks_send_nothing(self):
        self.no_auto()
        kbd, pad = self.keyboard_and_numpad(kbd=170, pad=170)
        self.ticks(60)
        self.assertEqual((kbd.sets, pad.sets), ([], []))

    def test_module_with_other_steps_is_asked_only_once(self):
        self.no_auto()
        kbd = self.plug("hidraw7", "0012", 255)
        rgb = self.plug("hidraw9", "0013", 255, steps=255)
        self.tick()
        rgb.actual = 100                    # keyboard can only show 85 for this
        self.ticks(20)
        self.assertEqual(kbd.sets, [100])
        self.assertEqual((kbd.actual, rgb.actual), (85, 100))

    def test_hot_plugged_numpad_gets_current_level(self):
        self.no_auto()
        kbd = self.plug("hidraw7", "0012", 85)
        self.tick()
        pad = self.plug("hidraw3", "0014", 255)
        self.tick()
        self.assertEqual(pad.actual, 85)
        self.assertEqual(kbd.sets, [])

    def test_unplugged_module_is_closed_and_forgotten(self):
        self.no_auto()
        kbd, pad = self.keyboard_and_numpad()
        self.tick()
        self.unplug("hidraw3")
        self.tick()
        self.assertTrue(pad.closed)
        self.assertEqual(list(self.kit.modules), ["hidraw7"])
        kbd.actual = 85
        self.tick()                         # must not raise with one module left

    def test_silent_module_is_dropped_then_picked_up_again(self):
        self.no_auto()
        kbd, pad = self.keyboard_and_numpad()
        self.tick()
        pad.dead = True
        self.tick()
        self.assertNotIn("hidraw3", self.kit.modules)
        kbd.actual = 85
        self.tick()
        pad.dead = False
        self.tick()
        self.assertEqual(pad.actual, 85)

    def test_module_that_never_opens_is_given_up_on(self):
        self.no_auto()
        pad = self.plug("hidraw3", "0014", 255)
        pad.dead = True
        self.ticks(60)
        self.assertIn("hidraw3", self.kit.ignored)
        self.unplug("hidraw3")
        self.tick()
        self.assertEqual(self.kit.ignored, set())

    def test_foreign_hidraw_nodes_are_ignored(self):
        self.no_auto()
        (self.hidraw / "hidraw0").mkdir()
        self.tick()
        self.assertEqual(self.kit.modules, {})
        self.assertEqual(self.kit.ignored, {"hidraw0"})

    # -- sync switched off ---------------------------------------------------------

    def test_without_sync_numpad_is_not_even_opened(self):
        self.tk.SYNC = False
        kbd, pad = self.keyboard_and_numpad(kbd=170, pad=85)
        self.light(3)
        self.ticks(30)
        self.assertEqual(list(self.kit.modules), ["hidraw7"])
        self.assertEqual(kbd.actual, 255)           # ambient still drives the keyboard
        self.assertEqual((pad.actual, pad.sets), (85, []))

    def test_without_sync_keyboard_key_still_overrides_light(self):
        self.tk.SYNC = False
        kbd, pad = self.keyboard_and_numpad(pad=0)
        self.light(1000)
        self.tick()
        kbd.actual = 170
        self.ticks(60)
        self.assertEqual((kbd.actual, pad.actual), (170, 0))

    def test_without_sync_and_ambient_modules_are_left_alone(self):
        self.tk.SYNC = False
        self.no_auto()
        kbd, pad = self.keyboard_and_numpad(kbd=255, pad=85)
        self.light(3)
        self.ticks(30)
        self.assertEqual(self.kit.modules, {})
        self.assertEqual((kbd.sets, pad.sets), ([], []))

    def test_ambient_alone_works_without_a_numpad(self):
        kbd = self.plug("hidraw7", "0012", 255)
        self.light(30)
        self.ticks(self.tk.NUMLOCK_WINDOW_S + 10)
        self.assertEqual(kbd.actual, 170)
        self.assertEqual(self.presses, [])

    # -- ambient light -------------------------------------------------------------

    def test_first_reading_sets_both_modules(self):
        kbd, pad = self.keyboard_and_numpad(kbd=85, pad=85)
        self.light(3)
        self.tick()
        self.assertEqual((kbd.actual, pad.actual), (255, 255))

    def test_each_band_maps_to_its_level(self):
        for lux, level in ((0, 255), (19, 255), (20, 170), (249, 170), (250, 0), (40000, 0)):
            self.assertEqual(self.tk.BANDS[self.tk.band_of(lux)][1], level, lux)

    def test_band_change_needs_two_readings(self):
        kbd, _ = self.keyboard_and_numpad()
        self.light(1000)
        self.tick()
        self.assertEqual(kbd.actual, 0)
        self.light(30)
        self.ticks(self.tk.LUX_EVERY_S)     # first dim reading
        self.assertEqual(kbd.actual, 0)
        self.ticks(self.tk.LUX_EVERY_S)     # confirmed
        self.assertEqual(kbd.actual, 170)

    def test_passing_shadow_is_ignored(self):
        kbd, _ = self.keyboard_and_numpad()
        self.light(1000)
        self.tick()
        self.light(2)
        self.ticks(self.tk.LUX_EVERY_S)
        self.light(1000)
        self.ticks(self.tk.LUX_EVERY_S * 4)
        self.assertEqual(kbd.sets, [0])

    def test_light_hovering_at_a_limit_does_not_flap(self):
        kbd, _ = self.keyboard_and_numpad(kbd=170, pad=170)
        self.light(240)
        self.tick()
        for lux in (255, 245, 270, 240, 290, 230) * 3:    # limit 250, +-20 %
            self.light(lux)
            self.ticks(self.tk.LUX_EVERY_S)
        self.assertEqual(kbd.sets, [])
        self.assertEqual(kbd.actual, 170)

    def test_manual_level_holds_until_band_changes(self):
        kbd, pad = self.keyboard_and_numpad()
        self.light(1000)
        self.tick()
        kbd.actual = 255                    # user wants light in a bright room
        self.ticks(60)
        self.assertEqual((kbd.actual, pad.actual), (255, 255))
        self.light(30)                      # evening
        self.ticks(self.tk.LUX_EVERY_S * 3)
        self.assertEqual((kbd.actual, pad.actual), (170, 170))

    def test_without_sensor_it_only_syncs(self):
        kbd, pad = self.keyboard_and_numpad(kbd=170, pad=0)
        self.ticks(30)
        self.assertEqual((kbd.actual, pad.actual), (170, 170))

    def test_unreadable_sensor_value_is_skipped(self):
        kbd, _ = self.keyboard_and_numpad()
        self.light("garbage")
        self.ticks(30)
        self.assertEqual(kbd.sets, [])

    def test_sensor_is_not_read_without_modules(self):
        self.light(3)
        calls = []
        self.tk.read_lux = lambda: calls.append(1)
        self.ticks(30)
        self.assertEqual(calls, [])

    def test_scale_and_offset_are_applied(self):
        self.light(100)
        dev = self.iio / "iio:device0"
        (dev / "in_illuminance_scale").write_text("0.5\n")
        (dev / "in_illuminance_offset").write_text("20\n")
        self.assertEqual(self.tk.read_lux(), 60.0)

    # -- NumLock -------------------------------------------------------------------

    def test_numlock_turned_on_at_start(self):
        self.numpad_led(False)
        self.tick()                         # first "off" reading: wait
        self.assertEqual(self.presses, [])
        self.numpad_led(False)
        self.tick()
        self.assertEqual(len(self.presses), 1)

    def test_numlock_already_on_is_left_alone(self):
        self.numpad_led(True)
        self.ticks(self.tk.NUMLOCK_WINDOW_S + 10)
        self.assertEqual(self.presses, [])

    def test_no_numpad_means_no_key_press(self):
        self.ticks(self.tk.NUMLOCK_WINDOW_S + 10)
        self.assertEqual(self.presses, [])

    def test_numlock_presses_are_capped(self):
        self.numpad_led(False)              # LED never comes on
        self.ticks(self.tk.NUMLOCK_WINDOW_S + 10)
        self.assertEqual(len(self.presses), self.tk.NUMLOCK_MAX_PRESSES)

    def test_numlock_left_alone_after_the_window(self):
        self.numpad_led(True)
        self.ticks(self.tk.NUMLOCK_WINDOW_S + 1)
        self.numpad_led(False)              # user switched it off
        self.ticks(60)
        self.assertEqual(self.presses, [])

    def test_new_local_session_turns_numlock_on_again(self):
        self.numpad_led(True)
        self.ticks(self.tk.NUMLOCK_WINDOW_S + 1)
        self.numpad_led(False)              # new compositor starts with it off
        self.session("2")
        self.tk.press_numlock = lambda: (self.presses.append(self.now), self.numpad_led(True))
        self.ticks(self.tk.NUMLOCK_WINDOW_S)
        self.assertEqual(len(self.presses), 1)

    def test_seatless_session_does_not_touch_numlock(self):
        self.numpad_led(True)
        self.ticks(self.tk.NUMLOCK_WINDOW_S + 1)
        self.numpad_led(False)
        self.session("7", seat=None)        # ssh, user manager
        self.ticks(30)
        self.assertEqual(self.presses, [])

    def test_numlock_window_polls_fast_then_slow(self):
        self.assertEqual(self.tick(), self.tk.FAST_TICK_S)
        self.now += self.tk.NUMLOCK_WINDOW_S
        self.assertEqual(self.tick(), self.tk.TICK_S)

    def test_failing_key_press_does_not_stop_the_loop(self):
        def fail():
            raise PermissionError("uinput")
        self.tk.press_numlock = fail
        self.numpad_led(False)
        self.ticks(self.tk.NUMLOCK_WINDOW_S + 10)

    def test_numlock_can_be_disabled(self):
        self.no_numlock()
        self.numpad_led(False)
        self.ticks(30)
        self.assertEqual(self.presses, [])

    # -- NumLock indicator -----------------------------------------------------------

    def test_indicator_stays_quiet_for_the_state_it_starts_with(self):
        self.numpad_led(True)
        self.kit.indicate()
        self.kit.indicate()
        self.assertEqual(self.shown, [])

    def test_indicator_shows_each_change_once(self):
        self.numpad_led(True)
        self.kit.indicate()
        self.numpad_led(False)
        self.kit.indicate()
        self.kit.indicate()
        self.numpad_led(True)
        self.kit.indicate()
        self.assertEqual(self.shown, [False, True])

    def test_indicator_ignores_numpad_coming_and_going(self):
        self.kit.indicate()                 # no numpad yet
        self.numpad_led(True)
        self.kit.indicate()                 # plugged in: a first state, not a change
        (self.leds / "input12::numlock" / "brightness").unlink()
        self.kit.indicate()                 # unplugged
        self.numpad_led(False)
        self.kit.indicate()                 # back, in the other state
        self.assertEqual(self.shown, [])

    def test_wait_without_indicator_is_one_plain_sleep(self):
        slept = []
        self.numpad_led(True)
        self.kit.wait(2.0, sleep=slept.append)
        self.assertEqual(slept, [2.0])
        self.assertIsNone(self.kit.numlock_shown)

    def test_wait_with_indicator_looks_at_numlock_in_small_steps(self):
        self.tk.INDICATOR = True
        self.numpad_led(True)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            self.now += seconds
            if len(slept) == 3:
                self.numpad_led(False)      # NumLock pressed during the wait
        self.kit.wait(2.0, sleep=sleep)
        self.assertEqual(self.shown, [False])
        self.assertAlmostEqual(sum(slept), 2.0)
        self.assertLessEqual(max(slept), self.tk.INDICATOR_POLL_S + 1e-9)


class PowerLedTests(unittest.TestCase):
    """The blink patterns, against a fake LED directory."""

    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("toolkit", str(SCRIPT))
        self.tk = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.tk)
        self.logged = []
        self.tk.log = self.logged.append
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = Path(self.tmp.name)
        self.tk.POWER_LED = str(self.led)
        (self.led / "trigger").write_text("none kbd-numlock [chromeos-auto] rfkill1\n")
        (self.led / "brightness").write_text("100\n")
        # Record every write and every pause, in order.
        self.steps = []
        write = self.tk.led_write
        self.tk.led_write = lambda name, value: (self.steps.append((name, value)), write(name, value))
        self.tk.time = type("time", (), {"sleep": lambda seconds: self.steps.append(seconds)})

    def test_on_is_two_short_blinks(self):
        self.tk.show_numlock(True)
        self.assertEqual(self.steps, [
            ("trigger", "none"),
            ("brightness", "0"), 0.15, ("brightness", "100"), 0.15,
            ("brightness", "0"), 0.15, ("brightness", "100"), 0.0,
            ("trigger", "chromeos-auto")])

    def test_off_is_one_long_blink(self):
        self.tk.show_numlock(False)
        self.assertEqual(self.steps, [
            ("trigger", "none"),
            ("brightness", "0"), 0.8, ("brightness", "100"), 0.0,
            ("trigger", "chromeos-auto")])

    def test_led_in_use_by_something_else_is_left_alone(self):
        (self.led / "trigger").write_text("[none] kbd-numlock chromeos-auto\n")
        self.tk.show_numlock(True)
        self.assertEqual(self.steps, [])

    def test_led_is_given_back_when_a_write_fails(self):
        write = self.tk.led_write

        def failing(name, value):
            if name == "brightness":
                raise PermissionError("brightness")
            write(name, value)
        self.tk.led_write = failing
        self.tk.show_numlock(True)
        self.assertEqual(len(self.logged), 1)
        self.assertEqual(self.steps[-1], ("trigger", "chromeos-auto"))

    def test_missing_led_is_reported_not_fatal(self):
        self.tk.POWER_LED = str(self.led / "nope")
        self.tk.show_numlock(False)
        self.assertEqual(len(self.logged), 1)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("toolkit", str(SCRIPT))
        self.tk = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.tk)
        self.logged = []
        self.tk.log = self.logged.append
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tk.CONFIG = str(Path(self.tmp.name) / "toolkit.conf")

    def load(self, text):
        Path(self.tk.CONFIG).write_text(text)
        self.tk.load_config()

    def flags(self):
        return self.tk.SYNC, self.tk.AMBIENT, self.tk.NUMLOCK

    def test_indicator_is_off_unless_asked_for(self):
        self.tk.load_config()
        self.assertFalse(self.tk.INDICATOR)
        self.load("indicator = yes\n")
        self.assertTrue(self.tk.INDICATOR)

    def test_missing_file_keeps_defaults_silently(self):
        self.tk.load_config()
        self.assertEqual(self.flags(), (True, True, True))
        self.assertEqual(self.logged, [])

    def test_shipped_config_matches_the_defaults(self):
        bands = self.tk.BANDS
        self.load((SCRIPT.parent / "packaging" / "framework-keyboard-toolkit.conf").read_text())
        self.assertEqual(self.flags(), (True, True, True))
        self.assertFalse(self.tk.INDICATOR)
        self.assertEqual(self.tk.BANDS, bands)
        self.assertEqual(self.logged, [])

    def test_each_switch_can_be_turned_off(self):
        self.load("sync = no\nambient=off   # comment\n  numlock = No\n")
        self.assertEqual(self.flags(), (False, False, False))
        self.assertEqual(self.logged, [])

    def test_bad_lines_are_reported_and_skipped(self):
        self.load("sync = maybe\nnumlock = no\ncolour = red\njust words\n")
        self.assertEqual(self.flags(), (True, True, False))
        self.assertEqual(len(self.logged), 3)
        self.assertIn(":1:", self.logged[0])

    def test_bands_are_parsed(self):
        self.load("bands = 5:85 100:255 *:0\n")
        self.assertEqual(self.tk.BANDS, ((5.0, 85), (100.0, 255), (None, 0)))
        self.assertEqual(self.tk.BANDS[self.tk.band_of(50)][1], 255)

    def test_single_band_means_a_fixed_level(self):
        self.load("bands = *:170\n")
        self.assertEqual(self.tk.BANDS[self.tk.band_of(123456)][1], 170)

    def test_malformed_bands_keep_the_defaults(self):
        default = self.tk.BANDS
        for bad in ("8:85 60:170", "60:85 8:170 *:0", "8:300 *:0", "8:85 *:0 60:170",
                    "dark:85 *:0", "8 *:0", "0:85 *:0", ""):
            self.load(f"bands = {bad}\n")
            self.assertEqual(self.tk.BANDS, default, bad)
        self.assertEqual(len(self.logged), 8)


class ModuleTests(unittest.TestCase):
    """VIA framing against a scripted file descriptor."""

    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("toolkit", str(SCRIPT))
        self.tk = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.tk)
        self.written = []
        self.replies = []
        self.tk.os = type("os", (), {
            "write": lambda fd, data: self.written.append(data) or len(data),
            "read": lambda fd, n: self.replies.pop(0),
            "close": lambda fd: None,
        })
        # Nothing stale to drain, then the reply is ready.
        self.tk.select = type("select", (), {
            "select": lambda r, w, x, timeout: ([], [], []) if timeout == 0 or not self.replies
            else (r, [], [])})
        self.module = self.tk.Module("hidraw7", "0012", 5)

    def reply(self, *data):
        self.replies.append(bytes(data).ljust(32, b"\0"))

    def test_get_sends_a_33_byte_report_and_reads_the_level(self):
        self.reply(0x08, 1, 1, 170)
        self.assertEqual(self.module.get(), 170)
        self.assertEqual(self.written, [bytes([0, 0x08, 1, 1]).ljust(33, b"\0")])
        self.assertEqual(self.module.channel, 1)

    def test_get_falls_back_to_rgb_matrix_channel(self):
        self.reply(0xFF, 1, 1)              # backlight channel not handled
        self.reply(0x08, 3, 1, 200)
        self.assertEqual(self.module.get(), 200)
        self.assertEqual(self.module.channel, 3)

    def test_get_without_answer_is_none(self):
        self.assertIsNone(self.module.get())

    def test_set_sends_level_on_the_detected_channel(self):
        self.module.channel = 3
        self.reply(0x07, 3, 1, 85)
        self.assertTrue(self.module.set(85))
        self.assertEqual(self.written[0][:5], bytes([0, 0x07, 3, 1, 85]))

    def test_set_rejected_by_firmware_is_false(self):
        self.module.channel = 1
        self.reply(0xFF, 1, 1, 85)
        self.assertFalse(self.module.set(85))


if __name__ == "__main__":
    unittest.main()
