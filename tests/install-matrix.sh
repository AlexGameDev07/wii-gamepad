#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
# Prueba install.sh en varias distros con contenedores (podman o docker).
# No hay Bluetooth ni kernel propio dentro, así que comprueba que se instalen
# los paquetes, los comandos, el .desktop y que el programa arranque.
#
#   tests/install-matrix.sh              todas las distros
#   tests/install-matrix.sh arch alpine  solo esas
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="$(command -v podman || command -v docker)" || { echo "Necesitas podman o docker" >&2; exit 1; }

declare -A IMAGE=(
    [fedora]=registry.fedoraproject.org/fedora:43
    [debian]=docker.io/library/debian:stable
    [ubuntu]=docker.io/library/ubuntu:24.04
    [arch]=docker.io/library/archlinux:latest
    [opensuse]=registry.opensuse.org/opensuse/tumbleweed:latest
    [alpine]=docker.io/library/alpine:latest
    [void]=ghcr.io/void-linux/void-glibc-full:latest
)
# Lo mínimo para tener un usuario con sudo (el instalador se niega a correr como root).
declare -A PREP=(
    [fedora]="dnf install -y sudo shadow-utils util-linux"
    [debian]="apt-get update && apt-get install -y sudo"
    [ubuntu]="apt-get update && apt-get install -y sudo"
    [arch]="pacman -Sy --noconfirm sudo"
    [opensuse]="zypper --non-interactive install sudo shadow"
    [alpine]="apk add bash sudo shadow openrc"
    [void]="xbps-install -Syu xbps && xbps-install -Sy sudo bash && mkdir -p /run/runit/runsvdir/current"  # en el contenedor runit no corre
)

CHECK='set -e
export PATH="$HOME/.local/bin:$PATH"
wii-gamepad --help >/dev/null && echo "  ok  wii-gamepad arranca"
py="$(sed -n "s/^exec \"\([^\"]*\)\".*/\1/p" "$HOME/.local/bin/wii-gamepad")"
"$py" -c "import evdev" && echo "  ok  python-evdev"
"$py" -c "import tkinter" && echo "  ok  tkinter (ventana de configuración)"
"$py" -c "import gi; gi.require_version(\"Atspi\", \"2.0\"); from gi.repository import Atspi" \
    && echo "  ok  AT-SPI (teclado en pantalla)" || echo "  --  sin AT-SPI (el teclado solo se abre con el botón 2)"
test -f "$HOME/.local/share/applications/wii-gamepad.desktop" && echo "  ok  acceso en el menú"
ls "$HOME/.local/share/wii-gamepad/sonidos/"*.ogg >/dev/null && echo "  ok  sonidos"
grep -q uinput /etc/udev/rules.d/65-wii-gamepad.rules && echo "  ok  regla udev"
if command -v rc-update >/dev/null; then
    rc-update show default | grep -q bluetooth && echo "  ok  Bluetooth activado al arrancar (OpenRC)"
elif [ -d /var/service ]; then
    [ -L /var/service/bluetoothd ] && [ -L /var/service/dbus ] && echo "  ok  Bluetooth activado al arrancar (runit)"
fi'

distros=("$@")
[ ${#distros[@]} -eq 0 ] && distros=(fedora debian ubuntu arch opensuse alpine void)
failed=()
for d in "${distros[@]}"; do
    [ -n "${IMAGE[$d]:-}" ] || { echo "Distro desconocida: $d" >&2; exit 1; }
    echo "=== $d"
    log="$(mktemp)"
    if "$ENGINE" run --rm --security-opt label=disable -v "$ROOT:/src:ro" "${IMAGE[$d]}" sh -c "
        { ${PREP[$d]}; } >/dev/null 2>&1 || exit 10
        useradd -m tester && echo 'tester ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/tester
        cp -r /src /home/tester/app && chown -R tester /home/tester/app
        su tester -c 'cd ~/app && ./install.sh --service' >/tmp/install.log 2>&1 || { tail -25 /tmp/install.log; exit 11; }
        grep -E '^(.\\[1;33m)?!!' /tmp/install.log | sed 's/^/  aviso: /'
        su tester -c '$CHECK'
    " >"$log" 2>&1; then
        sed 's/\x1b\[[0-9;]*m//g' "$log"
    else
        sed 's/\x1b\[[0-9;]*m//g' "$log"; echo "  FALLÓ ($d)"; failed+=("$d")
    fi
    rm -f "$log"
done
echo
[ ${#failed[@]} -eq 0 ] && echo "Todas las distros OK" || { echo "Fallaron: ${failed[*]}"; exit 1; }
