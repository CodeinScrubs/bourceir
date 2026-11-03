"""Manual synthetic GUI smoke; closes itself and never accesses the broker."""
import sys
import tkinter as tk
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import farabi_morning_check as check  # noqa: E402

original = tk.Tk
observed = []


class TemporaryRoot(original):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.after(17_000, self.inspect)
        self.after(19_000, self.destroy)

    def inspect(self):
        texts = [widget.cget("text") for widget in self.winfo_children() if isinstance(widget, tk.Label)]
        contents = "\n".join(texts)
        observed.append(all(word in contents for word in ("as of", "Recorder:", "NO automatic click")))


tk.Tk = TemporaryRoot
try:
    target = (check.now_tehran() + timedelta(seconds=22)).replace(microsecond=0)
    root = check.WORKSPACE / "tests/.timing-output"
    check.run_window(target, "IRO3FSTZ0001", root, root / "morning-check", True)
finally:
    tk.Tk = original

if observed != [True]:
    raise SystemExit("Synthetic window did not display the expected diagnostic state")
