#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AlexDev07
# Instalador de wii-gamepad (Wiimote + Nunchuk -> mando Xbox 360) para Linux.
#
#   ./install.sh               instala dependencias, permisos y el comando wii-gamepad
#   ./install.sh --service     además lo arranca al conectar el Wiimote y lo cierra al desconectarlo
#   ./install.sh --hide-raw    oculta el Wiimote original a Steam/SDL (evita mandos duplicados)
#   ./install.sh --uninstall   lo quita todo
set -euo pipefail

APP=wii-gamepad
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UDEV_RULE=/etc/udev/rules.d/65-wii-gamepad.rules
MODULES_CONF=/etc/modules-load.d/wii-gamepad.conf
DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/$APP.desktop"
ICON_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/$APP.svg"

WANT_SERVICE=0 HIDE_RAW=0 UNINSTALL=0
ME="$(id -un)"
for arg in "$@"; do
    case "$arg" in
        --service) WANT_SERVICE=1 ;;
        --hide-raw) HIDE_RAW=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '4,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Opción desconocida: $arg" >&2; exit 1 ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
    [ -n "${SUDO_USER:-}" ] && die "Ejecútalo como tu usuario normal (sin sudo); pedirá la contraseña cuando haga falta."
elif command -v sudo >/dev/null; then
    SUDO=sudo
elif command -v doas >/dev/null; then
    SUDO=doas
else
    die "Se necesita sudo o doas para instalar paquetes y reglas udev."
fi

has_systemd() { [ -d /run/systemd/system ]; }

reload_udev() {
    if command -v udevadm >/dev/null; then
        $SUDO udevadm control --reload-rules 2>/dev/null || true
        $SUDO udevadm trigger --subsystem-match=input --subsystem-match=misc 2>/dev/null || true
    fi
}

if [ "$UNINSTALL" -eq 1 ]; then
    info "Desinstalando $APP"
    if has_systemd && command -v systemctl >/dev/null; then
        systemctl --user disable --now "$APP.service" 2>/dev/null || true
    fi
    rm -f "$SERVICE_DIR/$APP.service" "$BIN_DIR/$APP" "$BIN_DIR/$APP-config" "$DESKTOP_FILE" "$ICON_FILE"
    rm -rf "$DATA_DIR"
    $SUDO rm -f "$UDEV_RULE" "$MODULES_CONF"
    reload_udev
    info "Listo. (Los paquetes del sistema como python3-evdev se dejaron instalados.)"
    exit 0
fi

# --- 1. Paquetes ---------------------------------------------------------------
. /etc/os-release 2>/dev/null || true
info "Distribución detectada: ${PRETTY_NAME:-desconocida}"

# Cada gestor: comando de instalación, paquetes base, paquete evdev,
# y paquetes para compilar evdev con pip si el paquete del sistema no existe.
if command -v dnf >/dev/null; then
    PM_INSTALL="dnf install -y"; BASE="python3 python3-tkinter bluez"; KB="python3-gobject gobject-introspection at-spi2-core"; EVDEV="python3-evdev"; NOTIFY="libnotify"
    BUILD="gcc python3-devel kernel-headers"
elif command -v apt-get >/dev/null; then
    PM_INSTALL="apt-get install -y"; BASE="python3 python3-tk bluez"; KB="python3-gi gir1.2-atspi-2.0 libxkbcommon0"; EVDEV="python3-evdev"; NOTIFY="libnotify-bin"
    BUILD="gcc python3-dev python3-venv linux-libc-dev"
    $SUDO apt-get update
elif command -v pacman >/dev/null; then
    PM_INSTALL="pacman -Syu --needed --noconfirm"; BASE="python tk bluez bluez-utils"; KB="python-gobject at-spi2-core libxkbcommon"; EVDEV="python-evdev"; NOTIFY="libnotify"
    BUILD="gcc linux-api-headers"
elif command -v zypper >/dev/null; then
    PM_INSTALL="zypper --non-interactive install"; BASE="python3 python3-tk bluez"; KB="python3-gobject typelib-1_0-Atspi-2_0 libxkbcommon0"; EVDEV="python3-evdev"; NOTIFY="libnotify-tools"
    BUILD="gcc python3-devel linux-glibc-devel"
elif command -v xbps-install >/dev/null; then
    PM_INSTALL="xbps-install -Sy"; BASE="python3 python3-tkinter bluez"; KB="python3-gobject at-spi2-core libxkbcommon"; EVDEV="python3-evdev"; NOTIFY="libnotify"
    BUILD="gcc python3-devel kernel-libc-headers"
elif command -v apk >/dev/null; then
    PM_INSTALL="apk add"; BASE="python3 python3-tkinter bluez"; KB="py3-gobject3 at-spi2-core libxkbcommon"; EVDEV="py3-evdev"; NOTIFY="libnotify"
    BUILD="gcc musl-dev python3-dev linux-headers"
elif command -v emerge >/dev/null; then
    PM_INSTALL="emerge --noreplace"; BASE="dev-lang/python net-wireless/bluez"; KB="dev-python/pygobject app-accessibility/at-spi2-core x11-libs/libxkbcommon"; EVDEV="dev-python/evdev"; NOTIFY="x11-libs/libnotify"
    BUILD="sys-kernel/linux-headers"
elif command -v eopkg >/dev/null; then
    PM_INSTALL="eopkg install -y"; BASE="python3 python3-tkinter bluez"; KB="python3-gobject at-spi2-core libxkbcommon"; EVDEV="python-evdev"; NOTIFY="libnotify"
    BUILD="gcc python3-devel linux-headers"
else
    PM_INSTALL=""
    warn "Gestor de paquetes no reconocido: instala tú python3 (>=3.9) con tkinter, bluez y python3-evdev"
    warn "(y, para el teclado en pantalla, python3-gobject con AT-SPI y libxkbcommon)."
fi

pm() { [ -n "$PM_INSTALL" ] && $SUDO $PM_INSTALL "$@"; }

if [ -n "$PM_INSTALL" ]; then
    info "Instalando paquetes base: $BASE"
    # shellcheck disable=SC2086
    pm $BASE || warn "No se pudieron instalar algunos paquetes base"
    info "Instalando paquetes del teclado en pantalla: $KB"
    # shellcheck disable=SC2086
    pm $KB || warn "Sin estos paquetes el teclado en pantalla no se abre solo en los campos de texto"
    info "Instalando notify-send para las notificaciones: $NOTIFY"
    # shellcheck disable=SC2086
    pm $NOTIFY || warn "Sin notify-send no habrá notificaciones de escritorio (los sonidos sí funcionan)"
fi

command -v python3 >/dev/null || die "python3 no está disponible."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || die "Se necesita Python 3.9 o superior (tienes $(python3 -V))."

PYTHON=python3
mkdir -p "$DATA_DIR" "$BIN_DIR"
if python3 -c 'import evdev' 2>/dev/null; then
    info "python-evdev ya está instalado"
else
    info "Instalando python-evdev"
    # shellcheck disable=SC2086
    pm $EVDEV || true
    if ! python3 -c 'import evdev' 2>/dev/null; then
        warn "No hay paquete evdev en el sistema; se compila con pip en un entorno aislado"
        # shellcheck disable=SC2086
        pm $BUILD || true
        python3 -m venv --system-site-packages "$DATA_DIR/venv" || die "No se pudo crear el venv (instala python3-venv)."
        "$DATA_DIR/venv/bin/pip" install --quiet --upgrade evdev || die "Falló pip install evdev."
        PYTHON="$DATA_DIR/venv/bin/python"
    fi
fi

# --- 2. Bluetooth ---------------------------------------------------------------
# Arch, Void, Alpine y Gentoo instalan bluez sin arrancarlo; Fedora, Debian y Ubuntu ya lo traen activo.
if has_systemd && command -v systemctl >/dev/null; then
    if ! systemctl is-active --quiet bluetooth.service; then
        info "Activando el servicio de Bluetooth"
        $SUDO systemctl enable --now bluetooth.service || warn "No se pudo activar bluetooth.service"
    fi
elif command -v rc-update >/dev/null; then  # OpenRC (Alpine, Gentoo)
    info "Activando el servicio de Bluetooth (OpenRC)"
    for svc in dbus bluetooth; do
        $SUDO rc-update add "$svc" default >/dev/null 2>&1 || true
        $SUDO rc-service "$svc" start >/dev/null 2>&1 || warn "No se pudo arrancar $svc"
    done
elif [ -d /etc/sv/bluetoothd ] && [ -d /var/service ]; then  # runit (Void)
    info "Activando el servicio de Bluetooth (runit)"
    for svc in dbus bluetoothd; do
        [ -e "/var/service/$svc" ] || $SUDO ln -s "/etc/sv/$svc" /var/service/ || warn "No se pudo activar $svc"
    done
else
    warn "No se pudo activar el Bluetooth automáticamente: arranca el servicio bluetooth de tu sistema."
fi
command -v rfkill >/dev/null && $SUDO rfkill unblock bluetooth 2>/dev/null || true

# --- 3. Módulos del kernel ------------------------------------------------------
info "Configurando módulos del kernel (hid-wiimote, uinput)"
$SUDO mkdir -p "$(dirname "$MODULES_CONF")" "$(dirname "$UDEV_RULE")"
printf 'hid-wiimote\nuinput\n' | $SUDO tee "$MODULES_CONF" >/dev/null
for mod in hid-wiimote uinput; do
    $SUDO modprobe "$mod" 2>/dev/null || warn "No se pudo cargar $mod ahora (puede estar integrado en el kernel)"
done

# --- 4. Permisos (udev) ---------------------------------------------------------
info "Instalando regla udev en $UDEV_RULE"
{
    echo '# wii-gamepad: acceso del usuario al Wiimote y a /dev/uinput'
    echo 'KERNEL=="uinput", SUBSYSTEM=="misc", MODE="0660", GROUP="input", TAG+="uaccess", OPTIONS+="static_node=uinput"'
    echo 'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="Nintendo Wii Remote*", MODE="0660", GROUP="input", TAG+="uaccess"'
    if [ "$WANT_SERVICE" -eq 1 ] && has_systemd; then
        echo '# Arranca el puente (servicio de usuario) cuando se conecta el Wiimote'
        echo "SUBSYSTEM==\"input\", KERNEL==\"event*\", ATTRS{name}==\"Nintendo Wii Remote\", TAG+=\"systemd\", ENV{SYSTEMD_USER_WANTS}+=\"$APP.service\""
    fi
    if [ "$HIDE_RAW" -eq 1 ]; then
        echo '# Oculta el Wiimote original a Steam/SDL; los juegos solo ven el mando virtual'
        echo 'SUBSYSTEM=="input", ATTRS{name}=="Nintendo Wii Remote*", ENV{ID_INPUT_JOYSTICK}="0"'
    fi
} | $SUDO tee "$UDEV_RULE" >/dev/null
reload_udev

# Sin systemd-logind la etiqueta uaccess no hace nada: se usa el grupo input.
NEED_RELOGIN=0
if ! has_systemd && ! id -nG | tr ' ' '\n' | grep -qx input; then
    getent group input >/dev/null || $SUDO groupadd -r input
    $SUDO usermod -aG input "$ME" 2>/dev/null || $SUDO addgroup "$ME" input
    NEED_RELOGIN=1
fi

# --- 5. Programa ----------------------------------------------------------------
info "Instalando el programa en $DATA_DIR"
install -m 755 "$SRC_DIR/wii_gamepad.py" "$SRC_DIR/wii_gamepad_gui.py" "$SRC_DIR/wii_keyboard.py" "$DATA_DIR/"
install -D -m 644 "$SRC_DIR/$APP.svg" "$ICON_FILE"
install -m 644 "$SRC_DIR/$APP.svg" "$DATA_DIR/"  # icono de la ventana
rm -rf "$DATA_DIR/sonidos"  # los incluidos se reemplazan; los del usuario están en ~/.config
mkdir -p "$DATA_DIR/sonidos"
[ -d "$SRC_DIR/sonidos" ] && cp -f "$SRC_DIR"/sonidos/* "$DATA_DIR/sonidos/" 2>/dev/null
cat > "$BIN_DIR/$APP" <<EOF
#!/bin/sh
exec "$PYTHON" "$DATA_DIR/wii_gamepad.py" "\$@"
EOF
cat > "$BIN_DIR/$APP-config" <<EOF
#!/bin/sh
exec "$PYTHON" "$DATA_DIR/wii_gamepad_gui.py" "\$@"
EOF
chmod 755 "$BIN_DIR/$APP" "$BIN_DIR/$APP-config"
"$PYTHON" -c 'import tkinter' 2>/dev/null || warn "Falta tkinter: la ventana de configuración no abrirá (instala python3-tk)."

mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Wii Gamepad
GenericName=Configuración del mando
Comment=Configura el Wiimote y el Nunchuk como mando de Xbox
Keywords=wii;wiimote;nunchuk;mando;control;controles;gamepad;joystick;botones;nintendo;
Exec=$BIN_DIR/$APP-config
Icon=$APP
StartupWMClass=Wii-gamepad
Terminal=false
Categories=Game;
EOF
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$(dirname "$(dirname "$(dirname "$ICON_FILE")")")" 2>/dev/null || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$(dirname "$DESKTOP_FILE")" 2>/dev/null || true

if [ "$WANT_SERVICE" -eq 1 ]; then
    if has_systemd && command -v systemctl >/dev/null; then
        info "Instalando servicio de usuario $APP.service (arranca al conectar el Wiimote)"
        mkdir -p "$SERVICE_DIR"
        # Sin [Install]: lo arranca la regla udev al aparecer el Wiimote y
        # --once lo cierra cuando se desconecta (o se apaga por inactividad).
        cat > "$SERVICE_DIR/$APP.service" <<EOF
[Unit]
Description=Wiimote + Nunchuk como mando Xbox 360

[Service]
ExecStart=$BIN_DIR/$APP --once
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=3
EOF
        systemctl --user disable "$APP.service" 2>/dev/null || true  # instalaciones antiguas: arrancaba al iniciar sesión
        systemctl --user daemon-reload
        if grep -qx "Nintendo Wii Remote" /sys/class/input/event*/device/name 2>/dev/null; then
            systemctl --user start "$APP.service"
        fi
    else
        warn "Sin systemd: añade '$BIN_DIR/$APP' a los programas de inicio de tu escritorio."
    fi
fi

# --- 6. Flatpak (Roblox con Sober) ----------------------------------------------
if command -v flatpak >/dev/null && flatpak info org.vinegarhq.Sober >/dev/null 2>&1; then
    info "Dando acceso a mandos a Sober (Roblox)"
    flatpak override --user --device=input org.vinegarhq.Sober || true
fi

echo
if [ "$WANT_SERVICE" -eq 1 ] && has_systemd; then
    info "¡Instalado! Enciende el Wiimote (botones 1+2) y el puente arrancará solo"
else
    info "¡Instalado! Conecta el Wiimote por Bluetooth (botones 1+2) y ejecuta: $APP"
fi
info "Para cambiar botones y perfiles abre «Wii Gamepad» en el menú o ejecuta: $APP-config"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR no está en tu PATH; usa $BIN_DIR/$APP o añádelo." ;; esac
[ "$NEED_RELOGIN" -eq 1 ] && warn "Cierra sesión y vuelve a entrar para que se aplique el grupo 'input'."
exit 0
