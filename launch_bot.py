"""
NFL Bot Launcher
=================
Run this script (via Windows Startup or manually) to start the bot.
A dialog asks which environment to run (Testing / Production) and
whether to open a visible console or run silently in the background.

On each launch, any previously running bot instance is killed first,
so switching modes won't result in two bots running simultaneously.

PID tracking: a bot.pid file is written next to this script after each
launch. The next launch reads it and calls taskkill /F /T before starting
the new process.

To add to Windows startup:
  1. Press Win+R, type: shell:startup, press Enter
  2. Create a shortcut in that folder pointing to this file,
     but make the shortcut run with: pythonw launch_bot.py
     (so the launcher itself has no console window)
"""

import os
import sys
import subprocess
import tkinter as tk
from tkinter import ttk
from pathlib import Path

BOT_SCRIPT = Path(__file__).parent / "bot.py"
PID_FILE   = Path(__file__).parent / "bot.pid"

PYTHON  = sys.executable
PYTHONW = sys.executable.replace("python.exe", "pythonw.exe")

CREATE_NEW_CONSOLE = subprocess.CREATE_NEW_CONSOLE
CREATE_NO_WINDOW   = 0x08000000


def kill_existing() -> None:
    """
    Kill any previously launched bot process using the stored PID file.
    Uses taskkill /F /T so the entire process tree is terminated.
    Missing or stale PID files are silently ignored.
    """
    if not PID_FILE.exists():
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
        )
    except Exception:
        pass
    finally:
        PID_FILE.unlink(missing_ok=True)


def ask_options() -> tuple[str, bool] | None:
    """
    Show a dialog with environment and console options.

    Returns (env_arg, use_console) where env_arg is "" (testing) or "prod",
    or None if the user cancelled.
    """
    result: dict = {"value": None}

    root = tk.Tk()
    root.title("NFL Bot Launcher")
    root.resizable(True, False)
    root.eval("tk::PlaceWindow . center")

    # -- Environment ---------------------------------------------------
    env_var = tk.StringVar(value="testing")

    env_frame = ttk.LabelFrame(root, text="Which Bot?", padding=10)
    env_frame.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")

    ttk.Radiobutton(
        env_frame, text="Testing", variable=env_var, value="testing"
    ).grid(row=0, column=0, sticky="w")
    ttk.Radiobutton(
        env_frame, text="Production", variable=env_var, value="prod"
    ).grid(row=1, column=0, sticky="w")

    # -- Console mode --------------------------------------------------
    console_var = tk.StringVar(value="silent")

    console_frame = ttk.LabelFrame(root, text="How to run?", padding=10)
    console_frame.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="ew")

    ttk.Radiobutton(
        console_frame,
        text="Silent (background)",
        variable=console_var,
        value="silent",
    ).grid(row=0, column=0, sticky="w")
    ttk.Radiobutton(
        console_frame,
        text="Console (visible window)",
        variable=console_var,
        value="console",
    ).grid(row=1, column=0, sticky="w")

    # -- Buttons -------------------------------------------------------
    btn_frame = ttk.Frame(root, padding=(16, 0, 16, 16))
    btn_frame.grid(row=2, column=0, sticky="ew")
    btn_frame.columnconfigure(0, weight=1)
    btn_frame.columnconfigure(1, weight=1)

    def on_launch():
        result["value"] = (env_var.get(), console_var.get() == "console")
        root.destroy()

    def on_cancel():
        root.destroy()

    ttk.Button(btn_frame, text="Launch", command=on_launch).grid(
        row=0, column=0, padx=(0, 4), sticky="ew"
    )
    ttk.Button(btn_frame, text="Cancel", command=on_cancel).grid(
        row=0, column=1, padx=(4, 0), sticky="ew"
    )

    root.bind("<Return>", lambda _: on_launch())
    root.bind("<Escape>", lambda _: on_cancel())
    root.mainloop()

    return result["value"]


def main() -> None:
    choice = ask_options()
    if choice is None:
        return  # user cancelled — do nothing, leave existing bot running

    env_arg, use_console = choice

    kill_existing()

    cmd_args = [str(BOT_SCRIPT)]
    if env_arg == "prod":
        cmd_args.append("prod")

    if use_console:
        proc = subprocess.Popen(
            ["cmd", "/k", PYTHON] + cmd_args,
            creationflags=CREATE_NEW_CONSOLE,
        )
    else:
        proc = subprocess.Popen(
            [PYTHONW] + cmd_args,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )

    PID_FILE.write_text(str(proc.pid))


if __name__ == "__main__":
    main()
