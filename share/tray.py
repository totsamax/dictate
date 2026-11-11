#!/usr/bin/env python3
"""Значок диктовки в трее.

Показывает состояние и даёт выбрать микрофон. Выбор пишется в файл,
который служба перечитывает перед каждой записью, поэтому перезапускать
ничего не нужно.

Рисуется через AyatanaAppIndicator3 — в GNOME на Wayland классического
трея нет, значки показывает расширение ubuntu-appindicators.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "dictate"
SOCKET_PATH = RUNTIME / "daemon.sock"
STATE_PATH = RUNTIME / "state"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dictate"
SOURCE_PATH = CONFIG_DIR / "source"
SOUNDS_PATH = CONFIG_DIR / "sounds"
PREROLL_PATH = CONFIG_DIR / "preroll"

DEFAULT_LABEL = "Системный по умолчанию"

# Свои значки повторяют глиф с клавиши Logitech: голова, плечи и три
# волны. Нарисованы как symbolic, поэтому GNOME перекрашивает их под тему.
ICON_DIR = str(Path.home() / ".local/share/icons")
ICONS = {
    "idle": "dictate-symbolic",
    "recording": "dictate-recording-symbolic",
    "busy": "dictate-busy-symbolic",
    "offline": "dictate-offline-symbolic",
}
TITLES = {
    "idle": "Диктовка: готова",
    "recording": "Диктовка: идёт запись",
    "busy": "Диктовка: распознаю",
    "offline": "Диктовка: служба остановлена",
}

# раз в столько тактов перечитываем список микрофонов
SOURCES_EVERY = 7


def list_sources():
    """Список микрофонов из PipeWire."""
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, timeout=5).stdout
        data = json.loads(out)
    except Exception:
        return []
    found = []
    for obj in data:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") == "Audio/Source":
            name = props.get("node.name")
            if name:
                found.append((name, props.get("node.description") or name))
    found.sort(key=lambda x: x[1].lower())
    return found


def system_default():
    """Описание того микрофона, который выбран в системе."""
    try:
        out = subprocess.run(["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if "node.description" in line:
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "неизвестно"


def chosen_source():
    try:
        return SOURCE_PATH.read_text().strip()
    except Exception:
        return ""


def choose_source(name):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_PATH.write_text(name)


def sounds_on():
    try:
        return SOUNDS_PATH.read_text().strip() != "0"
    except Exception:
        return True


def set_sounds(value):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SOUNDS_PATH.write_text("1" if value else "0")


def preroll_on():
    try:
        return PREROLL_PATH.read_text().strip() != "0"
    except Exception:
        return False


def set_preroll(value):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PREROLL_PATH.write_text("1" if value else "0")


def autostart_on():
    """Автозапуск — это штатный флаг systemd, свой файл настроек не нужен.
    Значок включён отдельной службой, поэтому он появится в любом случае."""
    r = subprocess.run(["systemctl", "--user", "is-enabled", "dictate.service"],
                       capture_output=True, text=True)
    return r.stdout.strip() == "enabled"


def set_autostart(value):
    verb = "enable" if value else "disable"
    # без --now: галочка отвечает только за вход в систему,
    # текущее состояние службы она не трогает
    subprocess.run(["systemctl", "--user", verb, "dictate.service"],
                   capture_output=True)


def send(cmd):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(str(SOCKET_PATH))
        s.sendall(cmd.encode())
        s.close()
        return True
    except Exception:
        return False


def service_alive():
    """Жива ли служба. Файл состояния для этого не годится: он остаётся
    лежать с последним значением и после остановки, так что значок
    показывал бы «готова» у выключенной службы."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.4)
        s.connect(str(SOCKET_PATH))
        s.close()
        return True
    except Exception:
        return False


def read_state():
    if not service_alive():
        return "offline"
    try:
        return STATE_PATH.read_text().strip() or "idle"
    except Exception:
        return "idle"


class Tray:
    def __init__(self):
        self.indicator = AppIndicator.Indicator.new(
            "dictate", ICONS["idle"],
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_icon_theme_path(ICON_DIR)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.state = None
        self.sources = None
        self.chosen = None
        self.autostart = autostart_on()
        self.sounds = sounds_on()
        self.preroll = preroll_on()
        self.ticks = 0
        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self.build_menu()
        GLib.timeout_add(700, self.tick)

    # --- меню -------------------------------------------------------
    def build_menu(self):
        for child in self.menu.get_children():
            self.menu.remove(child)

        state = read_state()
        head = Gtk.MenuItem(label=TITLES.get(state, "Диктовка"))
        head.set_sensitive(False)
        self.menu.append(head)
        self.menu.append(Gtk.SeparatorMenuItem())

        label = Gtk.MenuItem(label="Микрофон")
        label.set_sensitive(False)
        self.menu.append(label)

        current = chosen_source()
        group = []

        item = Gtk.RadioMenuItem(label=f"{DEFAULT_LABEL} ({system_default()})")
        item.join_group(None)
        group.append(item)
        item.set_active(current == "")
        item.connect("toggled", self.on_pick, "")
        self.menu.append(item)

        for name, description in (self.sources or list_sources()):
            it = Gtk.RadioMenuItem(label=f"   {description}")
            it.join_group(group[0])
            it.set_active(current == name)
            it.connect("toggled", self.on_pick, name)
            self.menu.append(it)

        self.menu.append(Gtk.SeparatorMenuItem())

        offline = state == "offline"

        toggle = Gtk.MenuItem(label="Начать / остановить запись")
        toggle.connect("activate", lambda _w: send("toggle"))
        toggle.set_sensitive(not offline)
        self.menu.append(toggle)

        if offline:
            start = Gtk.MenuItem(label="Запустить службу")
            start.connect("activate", self.on_service, "start")
            self.menu.append(start)
        else:
            restart = Gtk.MenuItem(label="Перезапустить службу")
            restart.connect("activate", self.on_service, "restart")
            self.menu.append(restart)

            stop = Gtk.MenuItem(label="Остановить службу")
            stop.connect("activate", self.on_service, "stop")
            self.menu.append(stop)

        self.menu.append(Gtk.SeparatorMenuItem())

        pre = Gtk.CheckMenuItem(label="Не терять начало фразы")
        pre.set_active(self.preroll)
        pre.set_tooltip_text(
            "Микрофон остаётся открытым, и в запись попадает то, что"
            " прозвучало до нажатия. Пока включено, в панели горит"
            " индикатор «микрофон используется».")
        pre.connect("toggled", self.on_preroll)
        self.menu.append(pre)

        snd = Gtk.CheckMenuItem(label="Звуковые сигналы")
        snd.set_active(self.sounds)
        snd.connect("toggled", self.on_sounds)
        self.menu.append(snd)

        auto = Gtk.CheckMenuItem(label="Запускать при входе в систему")
        auto.set_active(self.autostart)
        # обработчик вешаем после set_active, иначе он сработает
        # на программной установке галочки при каждой перестройке меню
        auto.connect("toggled", self.on_autostart)
        self.menu.append(auto)

        self.menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Убрать значок")
        quit_item.connect("activate", lambda _w: Gtk.main_quit())
        self.menu.append(quit_item)

        self.menu.show_all()

    # --- действия ---------------------------------------------------
    def on_pick(self, widget, name):
        if not widget.get_active():
            return
        if chosen_source() == name:
            return
        choose_source(name)
        shown = DEFAULT_LABEL if name == "" else widget.get_label().strip()
        subprocess.run(
            ["notify-send", "-a", "Диктовка",
             "-h", "string:x-canonical-private-synchronous:dictate",
             "Диктовка", f"Микрофон: {shown}"], check=False)

    def on_preroll(self, widget):
        value = widget.get_active()
        if value == self.preroll:
            return
        set_preroll(value)
        self.preroll = value
        subprocess.run(
            ["notify-send", "-a", "Диктовка",
             "-h", "string:x-canonical-private-synchronous:dictate", "Диктовка",
             "Начало фразы сохраняется. Микрофон остаётся открытым."
             if value else
             "Микрофон открывается только на время записи."], check=False)

    def on_sounds(self, widget):
        value = widget.get_active()
        if value == self.sounds:
            return
        set_sounds(value)
        self.sounds = value

    def on_autostart(self, widget):
        value = widget.get_active()
        if value == self.autostart:
            return
        set_autostart(value)
        self.autostart = autostart_on()
        subprocess.run(
            ["notify-send", "-a", "Диктовка",
             "-h", "string:x-canonical-private-synchronous:dictate", "Диктовка",
             "Будет запускаться при входе в систему" if self.autostart
             else "При входе появится только значок, служба — вручную"],
            check=False)

    def on_service(self, _widget, verb):
        """Остановка освобождает видеопамять: модель уходит вместе с
        процессом. Обратный запуск занимает около пяти секунд."""
        subprocess.run(["systemctl", "--user", verb, "dictate.service"],
                       check=False)
        words = {"start": "Служба запущена",
                 "stop": "Служба остановлена, видеопамять освобождена",
                 "restart": "Служба перезапущена"}
        subprocess.run(
            ["notify-send", "-a", "Диктовка",
             "-h", "string:x-canonical-private-synchronous:dictate",
             "Диктовка", words.get(verb, verb)], check=False)

    # --- обновление ---------------------------------------------------
    def tick(self):
        """Состояние опрашиваем часто — это дёшево. Список микрофонов реже:
        он требует запуска pw-dump, и каждые 0,7 с это расточительно."""
        self.ticks += 1
        state = read_state()
        chosen = chosen_source()

        changed = state != self.state or chosen != self.chosen
        if self.ticks % SOURCES_EVERY == 0:
            auto = autostart_on()
            if auto != self.autostart:
                self.autostart = auto
                changed = True
            snd = sounds_on()
            if snd != self.sounds:
                self.sounds = snd
                changed = True
            pre = preroll_on()
            if pre != self.preroll:
                self.preroll = pre
                changed = True
        if self.ticks % SOURCES_EVERY == 0 or self.sources is None:
            sources = list_sources()
            if sources != self.sources:
                self.sources = sources
                changed = True

        if state != self.state:
            self.indicator.set_icon_full(ICONS.get(state, ICONS["idle"]),
                                         TITLES.get(state, "Диктовка"))
        self.state, self.chosen = state, chosen
        if changed:
            self.build_menu()
        return True


def main():
    Tray()
    Gtk.main()


if __name__ == "__main__":
    sys.exit(main())
