#!/bin/bash
# Обёртка: подкладывает CUDA-библиотеки из venv и запускает службу.
# ctranslate2 ищет libcudnn и libcublas через LD_LIBRARY_PATH, а pip
# кладёт их внутрь venv, поэтому путь собираем на лету.
VENV="$HOME/.local/share/dictate/venv"
NVLIBS=$("$VENV/bin/python" - <<'PY'
import os, glob
try:
    import nvidia
    # nvidia — пакет-пространство имён, у него __file__ равен None,
    # поэтому корни берём из __path__
    paths = []
    for base in list(nvidia.__path__):
        paths += glob.glob(os.path.join(base, "*", "lib"))
    print(":".join(sorted(set(paths))))
except Exception:
    print("")
PY
)
export LD_LIBRARY_PATH="${NVLIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$VENV/bin/python" "$HOME/.local/share/dictate/daemon.py"
