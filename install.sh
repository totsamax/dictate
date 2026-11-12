#!/bin/bash
# Установка голосового ввода. Ничего системного не трогает: всё кладётся
# в домашний каталог, права root не нужны.
set -e
cd "$(dirname "$(readlink -f "$0")")"

SHARE="$HOME/.local/share/dictate"
ICONS="$HOME/.local/share/icons/hicolor/scalable/status"
UNITS="$HOME/.config/systemd/user"

echo "==> проверяю, чем записывать и вставлять"
for c in pw-record pw-play wl-copy wl-paste notify-send python3; do
  command -v "$c" >/dev/null || { echo "нет $c — поставьте wl-clipboard и pipewire"; exit 1; }
done
python3 -c "import evdev" 2>/dev/null || { echo "нет python3-evdev"; exit 1; }
[ -w /dev/uinput ] || echo "ВНИМАНИЕ: /dev/uinput недоступен на запись — вставка не сработает"
if ! command -v ddcutil >/dev/null; then
  echo "    ddcutil не установлен — клавиши яркости работать не будут (остальное будет)"
elif ! ddcutil detect --brief 2>/dev/null | grep -q "I2C bus:"; then
  echo "    ddcutil не видит монитора с DDC/CI — клавиши яркости работать не будут."
  echo "    Проверьте права на /dev/i2c-* и включите DDC/CI в меню монитора."
fi

echo "==> копирую файлы"
mkdir -p "$SHARE/sounds" "$ICONS" "$UNITS" "$HOME/.local/bin" "$HOME/.config/dictate"
install -m 755 bin/dictate            "$HOME/.local/bin/dictate"
install -m 755 bin/monitor-brightness "$HOME/.local/bin/monitor-brightness"
install -m 644 share/daemon.py        "$SHARE/daemon.py"
install -m 644 share/tray.py          "$SHARE/tray.py"
install -m 755 share/run-daemon.sh    "$SHARE/run-daemon.sh"
install -m 644 share/sounds/*.wav     "$SHARE/sounds/"
install -m 644 icons/*.svg            "$ICONS/"
install -m 644 systemd/*.service      "$UNITS/"
install -m 644 DOCS.md                "$SHARE/README.md"

echo "==> собираю окружение с faster-whisper (это надолго, ~2.6 ГБ)"
if [ ! -x "$SHARE/venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$SHARE/venv" 2>/dev/null \
    || python3 -m venv --without-pip --system-site-packages "$SHARE/venv"
  if ! "$SHARE/venv/bin/python" -m pip --version >/dev/null 2>&1; then
    # python3-venv без ensurepip — поднимаем pip прямо из колеса
    TMP=$(mktemp -d)
    URL=$(python3 -c "import json,urllib.request;d=json.load(urllib.request.urlopen('https://pypi.org/pypi/pip/json'));v=d['info']['version'];print(next(f['url'] for f in d['releases'][v] if f['filename'].endswith('-py3-none-any.whl')))")
    curl -sSL -o "$TMP/${URL##*/}" "$URL"
    "$SHARE/venv/bin/python" "$TMP/${URL##*/}/pip" install -q "$TMP/${URL##*/}"
    rm -rf "$TMP"
  fi
  "$SHARE/venv/bin/python" -m pip install -q faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12
fi

echo "==> правило Solaar"
mkdir -p "$HOME/.config/solaar"
if [ -f "$HOME/.config/solaar/rules.yaml" ]; then
  echo "    rules.yaml уже есть — не трогаю, образец в solaar/rules.yaml.template"
else
  sed "s#\$HOME#$HOME#g" solaar/rules.yaml.template > "$HOME/.config/solaar/rules.yaml"
  echo "    создан ~/.config/solaar/rules.yaml"
fi
echo "    не забудьте включить перехват: в Solaar у клавиатуры,"
echo "    «Поведение клавиш/кнопок», переключите в «Перенаправлено»:"
echo "      Dictation        — голосовой ввод"
echo "      Mute Microphone  — выключение микрофона (необязательно)"
echo "      Screen Capture   — снимок экрана (необязательно)"
echo "      Brightness Down/Up — яркость монитора (необязательно)"

# Клавиша снимка шлёт Print, а оверлей GNOME открывается по Print только
# если эта привязка на месте. По умолчанию она там есть, но её легко потерять.
if command -v gsettings >/dev/null &&
   ! gsettings get org.gnome.shell.keybindings show-screenshot-ui 2>/dev/null | grep -q "'Print'"; then
  echo
  echo "    ВНИМАНИЕ: оверлей снимка экрана не привязан к Print, клавиша не сработает."
  echo "    Добавить, ничего не потеряв:"
  echo "      gsettings set org.gnome.shell.keybindings show-screenshot-ui \\"
  echo "        \"\$(gsettings get org.gnome.shell.keybindings show-screenshot-ui | sed \"s/]$/, 'Print']/\")\""
fi

echo "==> службы"
systemctl --user daemon-reload
systemctl --user enable --now dictate.service dictate-tray.service
echo
echo "Готово. Первый запуск скачает модель (~1.5 ГБ), это минуты."
echo "Проверка:  systemctl --user status dictate"
