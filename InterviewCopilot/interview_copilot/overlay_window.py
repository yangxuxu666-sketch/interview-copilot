"""Movable, resizable, translucent desktop answer window; no microphone access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont

import httpx

if __package__:
    from .window_capture import CapturePrivacyError, set_capture_excluded
else:
    from window_capture import CapturePrivacyError, set_capture_excluded

BG, PANEL, FG, MUTED, ACCENT = "#ffffff", "#f5f7f6", "#26332e", "#75827b", "#4b7865"
FONT_FAMILY = "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei UI"


class AnswerWindow:
    def __init__(self, config):
        self.config = config
        self.data_dir = Path(config["data_dir"])
        self.path = self.data_dir / "overlay-window.json"
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if saved.get("layout_version", 0) < 2:
            saved.update(width=640, height=420, font_size=12)
        self.root = root = tk.Tk()
        root.withdraw()
        root.title("面试伴航 · 回答悬浮窗")
        self.icon = tk.PhotoImage(width=32, height=32)
        self.icon.put("#6a8d7c", to=(0, 0, 32, 32))
        self.icon.put("#ffffff", to=(8, 7, 12, 25))
        self.icon.put("#ffffff", to=(17, 7, 24, 11))
        self.icon.put("#ffffff", to=(17, 15, 24, 19))
        root.iconphoto(True, self.icon)
        root.configure(bg=BG)
        # Keep a normal desktop window so it can be recovered from the taskbar,
        # moved across monitors, resized, and reached by accessibility tools.
        root.overrideredirect(False)
        root.minsize(380, 280)
        width = min(max(int(saved.get("width", 640)), 380), root.winfo_screenwidth())
        height = min(max(int(saved.get("height", 420)), 280), root.winfo_screenheight())
        x = min(max(int(saved.get("x", root.winfo_screenwidth() - width - 40)), 0), root.winfo_screenwidth() - width)
        y = min(max(int(saved.get("y", 100)), 0), root.winfo_screenheight() - height)
        root.geometry(f"{width}x{height}+{x}+{y}")
        self.opacity = tk.DoubleVar(value=min(100, max(50, float(saved.get("opacity", 85)))))
        self.pinned = tk.BooleanVar(value=bool(saved.get("pinned", True)))
        self.capture_supported = sys.platform == "win32"
        self.capture_excluded = tk.BooleanVar(value=self.capture_supported and bool(saved.get("exclude_from_capture", True)))
        self.capture_message = tk.StringVar(value="正在设置…")
        self.capture_affinity = None
        self.capture_error = ""
        self.capture_job = None
        self.ready_path = self.data_dir / "overlay-ready.json"
        self.size = min(22, max(9, int(saved.get("font_size", 12))))
        self.font = tkfont.Font(family=FONT_FAMILY, size=self.size)
        self.stop = threading.Event()
        self.inbox = queue.Queue(maxsize=2)
        self.answer_id = None
        self.last_text = None
        self.show_revision = -1
        self.build()
        root.attributes("-alpha", self.opacity.get() / 100)
        root.attributes("-topmost", self.pinned.get())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Configure>", self.resized)
        root.bind("<Map>", self.capture_mapped, add="+")
        root.update_idletasks()
        self.apply_capture_privacy()
        root.deiconify()
        root.update_idletasks()
        root.lift()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.write_ready()
        root.after(40, self.drain)
        threading.Thread(target=self.receive, name="overlay-feed", daemon=True).start()
        threading.Thread(target=self.watch_parent, name="overlay-lifecycle", daemon=True).start()

    def button(self, parent, text, action):
        return tk.Button(parent, text=text, command=action, relief="flat", bd=0,
                         bg=PANEL, fg=FG, activebackground="#e6eeea", activeforeground=FG,
                         font=(FONT_FAMILY, 9), cursor="hand2", padx=8, pady=4)

    def build(self):
        root = self.root
        frame = tk.Frame(root, bg=BG, highlightthickness=1, highlightbackground="#e1e7e3")
        frame.pack(fill="both", expand=True)
        title = tk.Frame(frame, bg=PANEL, cursor="fleur")
        title.pack(fill="x")
        label = tk.Label(title, text="回答思路   ·   拖动移动", bg=PANEL, fg=ACCENT,
                         font=(FONT_FAMILY, 10, "bold"), padx=14, pady=7, anchor="w")
        label.pack(side="left", fill="x", expand=True)
        for widget in (title, label):
            widget.bind("<ButtonPress-1>", self.drag_start)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", lambda _: self.save())
        self.button(title, "关闭", self.close).pack(side="right", padx=5)
        self.pin_button = self.button(title, "已置顶" if self.pinned.get() else "置顶", self.toggle_pin)
        self.pin_button.pack(side="right")
        self.status = tk.StringVar(value="正在连接本机工具…")
        tk.Label(frame, textvariable=self.status, bg=BG, fg=ACCENT,
                 font=(FONT_FAMILY, 9), anchor="w", padx=16, pady=5).pack(fill="x")
        self.question = tk.StringVar(value="等待问题")
        self.question_label = tk.Label(frame, textvariable=self.question, bg=BG, fg=MUTED,
                 font=(FONT_FAMILY, 10), justify="left", anchor="nw", height=2, padx=16)
        self.question_label.pack(fill="x")
        body = tk.Frame(frame, bg=BG)
        self.text = tk.Text(body, bg=BG, fg=FG, insertbackground=FG, selectbackground="#deebe3",
                            selectforeground=FG,
                            relief="flat", borderwidth=0, wrap="word", font=self.font,
                            spacing1=0, spacing3=4, padx=2, state="disabled", cursor="arrow")
        bar = tk.Scrollbar(body, command=self.text.yview, bg=PANEL, width=10)
        self.text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.set_text("在主窗口开始监听或输入问题。\n\n生成的回答会同步显示在这里。", True)
        controls = tk.Frame(frame, bg=PANEL, padx=10, pady=3)
        controls.pack(side="bottom", fill="x")
        self.button(controls, "A−", lambda: self.font_size(-1)).pack(side="left")
        self.button(controls, "A+", lambda: self.font_size(1)).pack(side="left")
        tk.Label(controls, text="不透明度", fg=MUTED, bg=PANEL, font=(FONT_FAMILY, 9)).pack(side="left", padx=(12, 0))
        slider = tk.Scale(controls, from_=50, to=100, orient="horizontal", variable=self.opacity,
                          command=self.change_opacity, length=130, showvalue=True, resolution=1,
                          bg=PANEL, fg=FG, highlightthickness=0, troughcolor=BG, bd=0,
                          font=(FONT_FAMILY, 8))
        slider.pack(side="left", padx=(6, 0))
        slider.bind("<ButtonRelease-1>", lambda _: self.save())
        grip = tk.Label(controls, text="◢", fg=MUTED, bg=PANEL,
                        cursor="bottom_right_corner" if sys.platform == "darwin" else "size_nw_se", padx=8)
        grip.pack(side="right", anchor="se")
        grip.bind("<ButtonPress-1>", self.resize_start)
        grip.bind("<B1-Motion>", self.resize)
        grip.bind("<ButtonRelease-1>", lambda _: self.save())
        privacy = tk.Frame(frame, bg=BG, padx=12, pady=2)
        privacy.pack(side="bottom", fill="x")
        tk.Checkbutton(privacy, text="共享时隐藏", variable=self.capture_excluded,
                       command=self.toggle_capture_privacy, bg=BG, fg=FG,
                       activebackground=BG, selectcolor=BG, bd=0, highlightthickness=0,
                       font=(FONT_FAMILY, 9), cursor="hand2",
                       state="normal" if self.capture_supported else "disabled").pack(side="left")
        self.capture_label = tk.Label(privacy, textvariable=self.capture_message,
                                     bg=BG, fg=MUTED, font=(FONT_FAMILY, 8),
                                     anchor="w", justify="left", wraplength=220)
        self.capture_label.pack(side="left", fill="x", expand=True, padx=(8, 0))
        # Pack the expanding body last so opacity/font controls always retain
        # space, including at the minimum window size and high Windows DPI.
        body.pack(fill="both", expand=True, padx=14, pady=(5, 4))

    def drag_start(self, event):
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def drag(self, event):
        px, py, x, y = self.drag_origin
        x = max(0, min(self.root.winfo_screenwidth()-80, x+event.x_root-px))
        y = max(0, min(self.root.winfo_screenheight()-50, y+event.y_root-py))
        self.root.geometry(f"+{x}+{y}")

    def resize_start(self, event):
        self.resize_origin = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def resize(self, event):
        px, py, width, height = self.resize_origin
        width = min(self.root.winfo_screenwidth(), max(380, width+event.x_root-px))
        height = min(self.root.winfo_screenheight(), max(280, height+event.y_root-py))
        self.root.geometry(f"{width}x{height}")

    def resized(self, event):
        if event.widget is self.root:
            self.question_label.configure(wraplength=max(250, event.width-40))
            self.capture_label.configure(wraplength=max(180, event.width-155))

    def write_ready(self):
        value = {"pid": os.getpid(), "mapped": bool(self.root.winfo_ismapped()),
                 "launch_id": self.config["launch_id"],
                 "capture_requested": self.capture_excluded.get(),
                 "capture_supported": self.capture_supported,
                 "capture_affinity": self.capture_affinity, "capture_error": self.capture_error}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp = self.ready_path.with_suffix(".tmp")
            temp.write_text(json.dumps(value), encoding="utf-8")
            os.replace(temp, self.ready_path)
        except OSError:
            pass

    def apply_capture_privacy(self):
        self.capture_job = None
        if not self.capture_supported:
            self.capture_excluded.set(False)
            self.capture_affinity = None
            self.capture_error = "macOS 版不支持共享隐藏"
            self.capture_message.set(self.capture_error)
            self.capture_label.configure(fg=MUTED)
            self.write_ready()
            return
        try:
            self.capture_affinity = set_capture_excluded(self.root.winfo_id(), self.capture_excluded.get())
            self.capture_error = ""
            self.capture_message.set("系统已开启 · 请在会议中验证" if self.capture_excluded.get() else "未开启")
            self.capture_label.configure(fg=MUTED)
        except (CapturePrivacyError, OSError, AttributeError) as exc:
            self.capture_affinity = None
            self.capture_error = str(exc)
            self.capture_message.set("未确认：" + self.capture_error)
            self.capture_label.configure(fg="#a15444")
        self.write_ready()

    def schedule_capture_privacy(self):
        if self.capture_job is None:
            self.capture_job = self.root.after_idle(self.apply_capture_privacy)

    def capture_mapped(self, event):
        if event.widget is self.root:
            self.schedule_capture_privacy()

    def toggle_capture_privacy(self):
        self.apply_capture_privacy()
        self.save()

    def toggle_pin(self):
        self.pinned.set(not self.pinned.get())
        self.root.attributes("-topmost", self.pinned.get())
        self.pin_button.configure(text="已置顶" if self.pinned.get() else "置顶")
        self.schedule_capture_privacy()
        self.save()

    def change_opacity(self, value):
        self.root.attributes("-alpha", float(value)/100)
        self.schedule_capture_privacy()

    def font_size(self, delta):
        self.size = max(9, min(22, self.size+delta))
        self.font.configure(size=self.size)
        self.save()

    def save(self):
        value = {"x": self.root.winfo_x(), "y": self.root.winfo_y(), "width": self.root.winfo_width(),
                 "height": self.root.winfo_height(), "opacity": self.opacity.get(),
                 "pinned": self.pinned.get(), "font_size": self.size, "layout_version": 2,
                 "exclude_from_capture": self.capture_excluded.get()}
        try:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(value), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            pass

    def receive(self):
        url = f"http://127.0.0.1:{int(self.config['port'])}/api/overlay/state"
        revision = -1
        with httpx.Client(timeout=20, trust_env=False,
                          cookies={"interview_session": self.config["token"]}) as client:
            while not self.stop.is_set():
                try:
                    response = client.get(url, params={"after": revision})
                    response.raise_for_status()
                    value = response.json()
                    revision = value["revision"]
                except (httpx.HTTPError, ValueError, KeyError):
                    value = {"disconnected": True}
                    self.deliver(value)
                    self.stop.wait(1)
                    continue
                self.deliver(value)

    def watch_parent(self):
        try:
            for line in sys.stdin.buffer:
                if json.loads(line).get("op") == "close":
                    break
        except (OSError, ValueError):
            pass
        # An EOF also means the owning app exited. Dispatch Tk work on its thread.
        self.root_quit = True

    def deliver(self, value):
        try:
            self.inbox.put_nowait(value)
        except queue.Full:
            try:
                self.inbox.get_nowait()
            except queue.Empty:
                pass
            self.inbox.put_nowait(value)

    def set_text(self, value, new_answer=False):
        # A compact paragraph rhythm keeps the floating answer easy to scan.
        # This is display-only; stored answers and exports keep their formatting.
        value = re.sub(r"\n[ \t]*\n+", "\n", value.strip())
        if value == self.last_text:
            return
        scroll = self.text.yview()[0]
        self.text.configure(state="normal")
        if not new_answer and self.last_text and value.startswith(self.last_text):
            self.text.insert("end", value[len(self.last_text):])
        else:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", value)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0 if new_answer else scroll)
        self.last_text = value

    def drain(self):
        if getattr(self, "root_quit", False):
            self.close()
            return
        try:
            while True:
                value = self.inbox.get_nowait()
                if value.get("disconnected"):
                    self.status.set("本机连接已断开，请从主窗口重新打开悬浮窗")
                    continue
                if value.get("show_revision", 0) != self.show_revision:
                    self.root.lift()
                    self.show_revision = value.get("show_revision", 0)
                answer = value.get("answer")
                if not answer:
                    self.answer_id = None
                    self.question.set("等待问题")
                    self.status.set(value.get("audio_message") or "等待生成回答")
                    self.set_text("在主窗口开始监听或输入问题。\n\n生成的回答会同步显示在这里。", True)
                    continue
                changed = answer.get("id") != self.answer_id
                self.answer_id = answer.get("id")
                self.question.set(answer.get("question", "")[:600])
                labels = {"streaming": "正在生成 · 实时同步", "done": "回答已完成", "error": "生成未完成", "cancelled": "生成已停止"}
                self.status.set(labels.get(answer.get("status"), "回答思路"))
                self.set_text(answer.get("text") or answer.get("error") or "正在组织回答…", changed)
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(40, self.drain)

    def close(self):
        self.save()
        self.stop.set()
        self.ready_path.unlink(missing_ok=True)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    settings = json.loads(sys.stdin.buffer.readline())
    AnswerWindow(settings).run()
