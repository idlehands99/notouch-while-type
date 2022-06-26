#!/bin/bash
#
# $Id$

set -e
progname=`basename "$0"`

if [[ "$(/usr/bin/id -u)" != "0" ]]; then
	echo "${progname}: this program must be run with root privileges" >&2
	exit 1
fi

usage() {
	cat <<-EOF
	usage: ${progname} [--setup-udev-rule]
	EOF
}

if ! optargs="$(getopt -n "${progname}" -o '' --long setup-udev-rule -- "$@")"; then
	usage >&2
	exit 1
fi
eval set -- "${optargs}"

setup_udev_rule=
while [[ "$1" != '--' ]]; do
	case "$1" in
	--setup-udev-rule)
		setup_udev_rule=1
		;;
	*)
		usage >&2
		exit 1
		;;
	esac
	shift
done
shift
if [[ $# -ne 0 ]]; then
	usage >&2
	exit 2
fi



for c in python3 column; do 
	if ! command -v $c >/dev/null 2>&1; then
		echo "${progname}: $c: command not found. insatll it and try again." >&2
		exit 1
	fi
done

if ! python3 -c 'import libevdev' >/dev/null 2>&1; then
	cat <<-EOF >&2
	python module 'libevdev' not found. insatll it and try again.
	go see https://gitlab.freedesktop.org/libevdev/python-libevdev
	may I suggest:
	    sudo apt install python3-libevdev
	  or
	    sudo pip3 install libevdev
	EOF
	exit 1
fi

if ! python3 -c 'import pyudev' >/dev/null 2>&1; then
	cat <<-EOF >&2
	python module 'pyudev' not found. insatll it and try again.
	go see https://github.com/pyudev/pyudev
	may I suggest:
	    sudo apt install python3-pyudev
	  or
	    sudo pip3 install pyudev
	EOF
	exit 1
fi



set -x

install -v -d -o root -g root -m 755 /usr/local/sbin /etc/notouch-while-type
install -v -o root -g root -m 755 notouch-while-type.py /usr/local/sbin/notouch-while-type
install -v -o root -g root -m 644 default.conf /etc/notouch-while-type/default.conf
install -v -o root -g root -m 644 systemd/notouch-while-type.service /etc/systemd/system/notouch-while-type.service
install -v -o root -g root -m 644 systemd/notouch-while-type.default /etc/default/notouch-while-type
systemctl daemon-reload
#systemctl enable notouch-while-type.service

if [[ -n "${setup_udev_rule}" ]]; then
	install -v -o 0 -g 0 -m 0644 systemd/85-notouch-while-type.rules /etc/udev/rules.d/85-notouch-while-type.rules
	udevadm control --reload
fi

set +x
cat <<EOF

notouch-while-type installed.
systemd unit is also istalled, but not enabled nor started.

edit /etc/notouch-while-type/default.conf to choose your keybord & touchpad.

to see keyboard and mouse/touchpad devices on your system, run:
  notouch-while-type list-devices
  (you have to be in 'input' group to use notouch-while-type, or use 'sudo')

use 'notouch-while-type monitor --dev /dev/input/eventN' (or other device
options) to see if those are right devices.

then 'sudo systemd start notouch-while-type.service' to start it.
to start notouch-while-type on each reboot, run:
  sudo systemd enable notouch-while-type.service

enjoy.

EOF
