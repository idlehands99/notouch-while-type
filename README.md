# notouch-while-type: disable touchpad while you type
#### (Linux, python3, libevdev, libudev; tested on Ubutnu 20.04)

Got a cheap china laptop and annoyed by erratic touchpad behavior? This program is for you.

It does what its name says. Monitor your key types and disable touchpad when they are. It's not perfect. It doesn't prevent first palm touch came before key strokes. But should make your life a bit easier, hopefully, bearable.

## Background

Typing on laptops inevitably interfares with its touchpad. Your linux distro may provide 'syndaemon' command, which exactry is designed to deal with this, but that only works with synaptics devices. Mine didn't come with synaptics touchpad, because... it's cheap.

Then I found [this discussion](https://forum.pine64.org/showthread.php?tid=4916). The script on that thread worked great. I had only a few dissatisfaction with it.
- it uses 'xset' command to disable/enable touchpad device; launching an external command each time I type (or didn't type) seemed a little excessive.
- it listens on /dev/input/eventX device for keyboard events, and uses X11 layer ('xset' command) to deal with touchpad. seemed unbalanced.
- I haven't tested it yet, but obviously it would not work with Wayland, as it uses X11 layer for touchpad control.

Especially third point seemed a deal breaker. This script is my take on the issue. The concept is exactly same as the script mentioned above, just with fancy device selection and daemon capability. It uses event device interface (EVIOCGRAB) to control touchpad, which should work with any program uses kernel event devices.

## Install this script
If you felt lucky enough, run `sudo ./install-notouch-while-type.bash`, but if it failed, you have to clean them up by yourself. You have to edit /etc/notouch-while-type/default.conf to choose right devices for you anyway.

#### prerequisites
notouch-while-type is a python3 script. You need `python3` command obviously. Beside standard python libraries, it uses two external modules:
- [python-libevdev](https://gitlab.freedesktop.org/libevdev/python-libevdev)
- [pyudev](https://pyudev.readthedocs.io/en/latest/)

On ubuntu, you can install those by running:
```
sudo apt install python3-libevdev python3-pyudev
```
Or you should be able to install them by pip3 (I haven't tried it):
```
sudo pip3 install libevdev pyudev
```
This script also uses `column` external command to print device tables, which your linux system should have already.

#### line-by-line procedure
1. copy notouch-while-type.py to /usr/local/sbin/notouch-while-type.
   ```
   sudo install -v -o root -g root -m 755 notouch-while-type.py /usr/local/sbin/notouch-while-type
   ```
2. copy and edit default.conf as /etc/notouch-while-type/default.conf. see [configuration section](#configration) below.
   ```
   sudo mkdir /etc/notouch-while-type
   sudo install -v -o root -g root -m 644 default.conf /etc/notouch-while-type/default.conf
   sudo vi /etc/notouch-while-type/default.conf
   ```
3. if you run it under systemd, copy unit and default files. enable it to autostart.
   ```
   sudo install -v -o root -g root -m 644 systemd/notouch-while-type.service /etc/systemd/system/notouch-while-type.service
   sudo install -v -o root -g root -m 644 systemd/notouch-while-type.default /etc/default/notouch-while-type
   sudo systemctl daemon-reload
   sudo systemctl enable notouch-while-type.service
   ```
4. this is usually not necessary, but if your devices are hot-pluggable, install udev rule too.
   ```
   sudo install -v -o root -g root -m 0644 systemd/85-notouch-while-type.rules /etc/udev/rules.d/85-notouch-while-type.rules
   sudo udevadm control --reload
   ```

## Running the script
If you have chosen systemd, run:
```
sudo systemctl start notouch-while-type.service
```
Otherwise (You have to be in `input` group to use this script, or use `sudo`):
```
notouch-while-type daemon start
```
To stop it, do:
```
sudo systemctl stop notouch-while-type.service
```
or:
```
notouch-while-type daemon stop
```

## Configration
### Find your devices
#### list-devices subcommand
First, you should run `notouch-while-type list-devices` to see what event devices are on your system. You have to be in `input` group to use this command, or use `sudo`.
```
$ notouch-while-type list-devices
### KEY DEVICES ###
NAME                              ID(BUS:VND:PRD:VER)  UNIQ  USB      DEVICE-NODE
"USB 2.0 Camera: USB 2.0 Camera"  0003:058f:5608:0003  -     001/005  /dev/input/event7
"Intel HID events"                0019:0000:0000:0000  -     -        /dev/input/event6
"HAILUCK CO.,LTD Usb Touch"       0003:258a:1205:0110  -     001/002  /dev/input/event4
"Video Bus"                       0019:0000:0006:0000  -     -        /dev/input/event3
"AT Translated Set 2 keyboard"    0011:0001:0001:ab83  -     -        /dev/input/event2
"Power Button"                    0019:0000:0001:0000  -     -        /dev/input/event1

### MOUSE DEVICES ###
NAME                               ID(BUS:VND:PRD:VER)  UNIQ  USB      DEVICE-NODE
"HAILUCK CO.,LTD Usb Touch Mouse"  0003:258a:1205:0110  -     001/002  /dev/input/event5
```
So, in my case above, keyboard is "AT Translated Set 2 keyboard" and touchpad is "HAILUCK CO.,LTD Usb Touch Mouse". 

#### monitor subcommand
If you are not sure which one is yours, run `notouch-while-type monitor` command. There are several ways to mointor your devices.

You can choose them by their name:
```
notouch-while-type monitor --name "AT Translated Set 2 keyboard"
```
You can choose them by their ID (`*` below means _I don't care about versions_):
```
notouch-while-type monitor --id "0011:0001:0001:*"
```
You can choose by their unique ID (addresses on Bluetooth devices):
```
notouch-while-type monitor --uniq "01:02:03:04:05:06"
```
You can choose by their USB bus#/device#:
```
notouch-while-type monitor --usb "001/002"
```
Or you can choose by their device node:
```
notouch-while-type monitor --dev "/dev/input/event5"
```

After monitoring started, use your device (type keys, move your fingers on touchpad) to see if it prints a bunch of events it recieved from the device.
```
$ notouch-while-type monitor --name "AT Translated Set 2 keyboard"
<name:AT Translated Set 2 keyboard> matched [/dev/input/event2]
using event device(s): [/dev/input/event2]
Type Ctrl-C to quit.
EV_MSC MSC_SCAN 57
EV_KEY KEY_SPACE 1
EV_SYN SYN_REPORT 0
EV_MSC MSC_SCAN 57
EV_KEY KEY_SPACE 0
EV_SYN SYN_REPORT 0
^C$
```

### Edit configuration file
Config file (usally /etc/notouch-while-type/default.conf) is an json file. Simple example may look like this:
```
{
    "format": "notouch-while-type configuration",
    "formatversion": 1,
    "keyboard": {
        "devices": {
            "name": [
                "AT Translated Set 2 keyboard"
            ]
        },
        "ignore": [
            "KEY_LEFTCTRL",
            "KEY_RIGHTCTRL",
            "KEY_LEFTSHIFT",
            "KEY_RIGHTSHIFT",
            "KEY_LEFTALT",
            "KEY_RIGHTALT",
            "KEY_LEFTMETA",
            "KEY_RIGHTMETA"
        ],
        "timeout": 0.3
    },
    "pointer": {
        "devices": {
            "name": [
                "HAILUCK CO.,LTD Usb Touch Mouse"
            ]
        }
    }
}
```
- **format** and **formarversion**: keep these lines as they are. do not edit.
- **keyboard**: this section sets options for keyboard device(s).
   - **devices**: choose keyboard devices. device selection is just like the ones with `monitor` subcommand, except no leading double hyphen.
   - **ignore**: this section lists keys that should NOT triggar disabling/enabling of touchpads. usally set to Ctrl, Shift Alt and Meta keys (left and right), so you can you Ctrl-DRAG, etc.
   - **timeout**: this sets how long touchpads should be kept disabled after last keystroke. around 0.3 seconds seems reasonable.
- **pointer**: this section choose touchpad device(s).
  - **devices**: same as above keyboard/devices section.

#### Which device selection method should I use?
Beware, event device node and usb bus#, device# may not be reliable. They can be changed between system boots.

Name and ID are pretty consistent as long as you don't have multiple identical devices on your system (pretty unlikely).

Unique ID is good, your devices probably are not Bluetooth devices though.

## License
[GPL2](LICENSE)

## Version history
- Version 1.0.0: initial release (2022-06-26)

