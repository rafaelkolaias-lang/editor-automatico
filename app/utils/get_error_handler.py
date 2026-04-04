import logging
import os
import time
import traceback
import tkinter as tk
from tkinter import messagebox
from .handle_thread_error import _write_crash_log, _show_fatal_error_message


def get_error_handler(app: tk.Tk):
    def handle_error(self, *args):
        # args costuma vir como (exc_type, exc_value, exc_tb)
        exc_info = args if len(args) == 3 else None

        _write_crash_log("Unhandled exception (Tk callback)",
                         exc_info=exc_info)
        _show_fatal_error_message()

        app.destroy()

    return handle_error
