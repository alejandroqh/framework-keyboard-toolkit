# framework-keyboard-toolkit

A lightweight service for **Framework laptop** input modules (keyboard,
numpad, macropad). It keeps their backlights in sync, sets the level from the
ambient light sensor, and turns NumLock on at start:

- **Backlight sync.** The keyboard and the numpad each keep their own
  backlight level, and each has its own key for it (on the numpad you even
  have to switch NumLock off first). With this service, changing one changes
  the others.
- **Ambient light.** The backlight level follows the laptop's ambient light
  sensor: low in a dark room, brighter in dim light, off in bright light.
- **NumLock on at start.** NumLock is turned on at boot and at login, so the
  numpad types numbers right away.
- **NumLock indicator** (optional, off by default). The numpad has no NumLock
  light, so a change is shown on the power-button LED: two short blinks when
  NumLock turns on, one long blink when it turns off.

Each of them can be switched on or off on its own, see
[Configuration](#configuration).

## How it works

One unprivileged, sandboxed process polls every 2 seconds:

- It asks each module for its backlight level over the module's VIA raw-HID
  interface (the same protocol [keyboard.frame.work](https://keyboard.frame.work)
  uses). If a level changed since the last look, somebody pressed a backlight
  key: that level is copied to the other modules, and polling speeds up to
  0.4 s for ten seconds so stepping through the levels feels immediate.
- Every 5 seconds it reads the ambient light sensor from sysfs and maps the
  lux value to a level through a small table. A change has to pass the limit
  by 20 % and show up on two readings in a row, so a passing shadow or light
  hovering at a limit does not make the keys flicker.
- A level chosen by hand wins over the sensor until the light moves to
  another band of the table; then the sensor takes over again.
- When the service starts, and whenever a local session appears (login
  screen, desktop), it watches the numpad's NumLock LED for 30 seconds. If
  the LED is off, it presses NumLock once on a throwaway virtual keyboard
  that only has that one key. After those 30 seconds NumLock is left alone,
  so switching it off by hand sticks. Without a numpad nothing is pressed.
- With the NumLock indicator on, it also reads the numpad's NumLock state
  five times a second (one small sysfs read). On a change it takes over the
  power-button LED for less than a second, blinks the pattern and hands the
  LED back to the embedded controller. If something else is using the LED at
  that moment (for example
  [framework-fingerprint-led-blink](https://github.com/alejandroqh/framework-fingerprint-led-blink)),
  the blink is skipped.

Levels are set in the modules' memory only; nothing is written to their
EEPROM and no firmware is changed. The white keyboards and the numpad have
four backlight steps (off, 33 %, 67 %, 100 %).

One pass costs two USB requests of 32 bytes and, every 5 s, one sysfs read.
Measured on the reference system: 0.02 s of CPU time per minute when idle
(0.03 % of one core), 13 MB of memory.

## Install

```bash
git clone https://github.com/alejandroqh/framework-keyboard-toolkit.git
cd framework-keyboard-toolkit
./fkt.sh install
```

`fkt.sh` asks for sudo, asks which features you want (first
install only), copies the files, creates the group `fw-kbd-toolkit`, loads
the udev rules and starts the service. The only dependency is Python
3.8 or newer (standard library only) and systemd.

Everything else goes through the same script:

| Command | What it does |
|---|---|
| `./fkt.sh install` | install or update, and start the service |
| `./fkt.sh configure` | ask what to switch on, then apply it |
| `./fkt.sh set SETTING=VALUE...` | change settings and apply them |
| `./fkt.sh status` | show settings, modules, light and NumLock (changes nothing) |
| `./fkt.sh logs` / `logs -f` | show / follow the service log |
| `./fkt.sh restart` | restart the service |
| `./fkt.sh uninstall` | stop, disable and remove everything |

`status` and `logs` run as your user; the others ask for sudo.

### Installed files

| File | Purpose |
|---|---|
| `/usr/local/sbin/framework-keyboard-toolkit` | the service (Python, stdlib only) |
| `/etc/systemd/system/framework-keyboard-toolkit.service` | systemd unit (unprivileged, sandboxed) |
| `/etc/framework-keyboard-toolkit.conf` | configuration |
| `/etc/udev/rules.d/90-framework-keyboard-toolkit.rules` | gives group `fw-kbd-toolkit` access to the modules' VIA interface |
| `/etc/udev/rules.d/90-framework-keyboard-toolkit-numlock.rules` | gives that group access to `/dev/uinput`; only installed with `numlock = yes` |
| `/etc/sysusers.d/framework-keyboard-toolkit.conf` | creates that group |

Only with `indicator = yes`:

| File | Purpose |
|---|---|
| `/etc/udev/rules.d/90-framework-keyboard-toolkit-led.rules` | gives group `fprint-led` write access to the power-button LED's `trigger` and `brightness` |
| `/etc/sysusers.d/framework-keyboard-toolkit-led.conf` | creates that group if it is not there |
| `/etc/systemd/system/framework-keyboard-toolkit.service.d/indicator.conf` | adds the service to that group and lets it write to `/sys` |

## Configuration

Four switches; the first three are on by default, the indicator is off:

```bash
./fkt.sh configure                       # asks about each one
./fkt.sh set ambient=no                  # or set them directly
./fkt.sh set sync=no numlock=no
```

Both forms update `/etc/framework-keyboard-toolkit.conf` and restart the
service. Settings you do not mention keep their value.

| Setting | `yes` (default) | `no` |
|---|---|---|
| `sync` | keyboard, numpad and macropad share one level | only the keyboard is managed; the numpad keeps the level set with its own key |
| `ambient` | the level follows the light sensor | the level only changes when you press a backlight key |
| `numlock` | NumLock is turned on at boot and login | NumLock is never touched, and the service gets no access to `/dev/uinput` |
| `indicator` | a NumLock change blinks the power-button LED (2 short = on, 1 long = off) | (default) the power-button LED is never touched |

Some combinations:

| You want | Use |
|---|---|
| Keyboard and numpad in step, no light sensor | `./fkt.sh set ambient=no` |
| Light sensor, no numpad attached | nothing to change: without a numpad there is nothing to sync and NumLock is not pressed |
| Light sensor on the keyboard, numpad left alone | `./fkt.sh set sync=no` |
| Keep NumLock as the system sets it | `./fkt.sh set numlock=no` |
| See whether NumLock is on or off | `./fkt.sh set indicator=yes` |

You can also edit the file by hand and run
`sudo systemctl restart framework-keyboard-toolkit`. The exceptions are
switching `numlock` or `indicator` **on**: they need extra permissions (the
uinput rule, the power-button LED rule), so use `./fkt.sh set numlock=yes` or
`./fkt.sh set indicator=yes`.

### Light table

`bands` in the same file maps ambient light to a backlight level (0-255), as
`below-this-many-lux:level` entries ending with `*:level`:

```bash
./fkt.sh set bands="8:85 60:170 250:255 *:0"     # the default
```

| Ambient light | Backlight |
|---|---|
| below 8 lux (dark room) | 85 (33 %) |
| 8 to 60 lux (dim) | 170 (67 %) |
| 60 to 250 lux (indoor light) | 255 (100 %) |
| 250 lux and more (bright) | off |

For example `./fkt.sh set bands="8:85 60:170 *:255"` keeps the backlight on
in bright light. A line the service cannot read is reported in the journal and the
default is used instead.

### Timing

The poll intervals, the hysteresis and the NumLock window are constants at
the top of `framework-keyboard-toolkit` (`TICK_S`, `FAST_TICK_S`,
`LUX_EVERY_S`, `HYSTERESIS`, `DWELL`, `NUMLOCK_WINDOW_S`), and so are the
indicator's blink patterns (`BLINK_ON`, `BLINK_OFF`). Edit them and
run `./fkt.sh install` again.

## Security

- **No root.** The service runs as a systemd `DynamicUser`. A udev rule gives
  its group access to two things only:
  - USB interface 01 of the input modules, the VIA raw-HID interface. The
    interfaces that carry key presses are not opened up, so the service
    cannot read what you type.
  - `/dev/uinput`, needed to press NumLock. This is the one broad
    permission: a process with it can create virtual input devices. If you
    do not want that, install with `./fkt.sh install numlock=no`: the uinput rule is then
    not installed at all.
  - With `indicator = yes` only: the `trigger` and `brightness` files of the
    power-button LED, through group `fprint-led`. That is the group
    framework-fingerprint-led-blink uses for the same LED, so both services
    can be installed together. The service is then also allowed to write to
    `/sys` (`ProtectKernelTunables=no`), where that LED lives.
- **Sandboxed.** Read-only filesystem, no network and no sockets at all,
  only hidraw and uinput device nodes, syscall filter, `NoNewPrivileges`,
  `MemoryDenyWriteExecute` and more.
  `systemd-analyze security framework-keyboard-toolkit` rates it
  **0.6 SAFE**.
- **No input from other users.** It only reads sysfs, the logind session
  list and the modules' own answers.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests run the real logic against fake modules and a fake sysfs: sync in
both directions, hot-plug, modules with different step counts, the light
table with hysteresis, manual override, the NumLock window, each switch
turned off, the NumLock indicator and its blink patterns, and the
configuration file parser.

## Tested on

| Laptop | Modules | Distro | Install method | Reported by |
|---|---|---|---|---|
| Framework Laptop 16 (AMD Ryzen AI 300 Series) | ANSI keyboard, numpad | Fedora 45, KDE Plasma (Wayland) | `fkt.sh install` | maintainer (reference system below) |

So far all features, including the NumLock indicator, work on that one setup,
also after a reboot.
Not tested yet:

- the ISO (`32AC:0018`) and JIS (`32AC:0019`) keyboards and the RGB macropad
  (`32AC:0013`): they are recognised, but nobody has tried them;
- other desktops (GNOME, X11, ...): sync and ambient light do not depend on
  the desktop, turning NumLock on does;
- other distros: `fkt.sh` knows `dnf` and `pacman`, only Fedora has been run.

Tested it on another module, desktop or distro? Open an issue or PR to add
it here.

### Reference system

Developed and tested on 2026-11-11.

| Component | Version |
|---|---|
| Keyboard | Keyboard Module - ANSI (`32AC:0012`), firmware 0.3.1 |
| Numpad | Numpad Module (`32AC:0014`), firmware 0.3.1 |
| Light sensor | built-in, `hid-sensor-als` kernel driver |
| Fedora | 45, kernel 7.3.3 |
| systemd | 262.1 |
| Python | 3.15.2 |
| Desktop | KDE Plasma 6.8.2 (Wayland) |

## Troubleshooting

```bash
./fkt.sh status
./fkt.sh logs
systemctl status framework-keyboard-toolkit
```

- **"cannot be opened or does not answer"** in the journal: the udev rule did
  not apply to that module. Re-seat the module or reboot, then check that
  `ls -l /dev/hidraw*` shows group `fw-kbd-toolkit` on two of the nodes.
- **NumLock is not turned on**: check that `./fkt.sh status` shows `NumLock on` in
  its `config:` line and that `/dev/uinput` belongs to group
  `fw-kbd-toolkit`. If your desktop has its own "NumLock at startup: off"
  setting, set it to "leave unchanged" or "on".
- **The backlight does not follow the light**: `./fkt.sh status` shows the lux value
  the sensor reports. Without a sensor the service only syncs.
- If the service fails to start on an unusual Python build,
  `MemoryDenyWriteExecute` is the most likely cause; disable it with
  `sudo systemctl edit framework-keyboard-toolkit` and add:

  ```ini
  [Service]
  MemoryDenyWriteExecute=no
  ```

## License

Apache License 2.0, see [LICENSE](LICENSE).
