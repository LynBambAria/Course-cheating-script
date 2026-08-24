"""
刷课脚本 - 带 UI 界面
自动检测「当前任务已达到完成条件」弹窗 + 自动检测「播放」按钮
"""

import time
import logging
import os
import sys
import threading
from pathlib import Path

import pyautogui
import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, scrolledtext

# ============ 默认配置 ============
DEFAULT_CHECK_INTERVAL = 5
CONFIDENCE = 0.7
MIN_CLICK_INTERVAL = 5
PLAY_BTN_WAIT = 8        # 等待播放按钮出现的最大秒数
PLAY_CLICK_COOLDOWN = 30  # 播放按钮点击冷却（秒），防止反复点击

# 播放按钮自动识别参数
PLAY_BTN_MIN_AREA = 500     # 播放按钮最小面积
PLAY_BTN_MAX_AREA = 15000   # 播放按钮最大面积
PLAY_BTN_ASPECT_MIN = 0.5   # 播放按钮最小宽高比
PLAY_BTN_ASPECT_MAX = 2.0   # 播放按钮最大宽高比

BASE_DIR = Path(__file__).parent
TEMPLATE_DIR = BASE_DIR / "templates"
COMPLETE_TEMPLATE = str(TEMPLATE_DIR / "task_complete.png")
NEXT_BTN_TEMPLATE = str(TEMPLATE_DIR / "next_button.png")
NEXT_BTN_WIDE_TEMPLATE = str(TEMPLATE_DIR / "next_button_wide.png")
PLAY_BTN_TEMPLATE = str(TEMPLATE_DIR / "play_button.png")


class RedirectText:
    def __init__(self, text_widget):
        self.text_widget = text_widget

    def write(self, text):
        if text.strip():
            self.text_widget.after(0, self._append, text)

    def _append(self, text):
        self.text_widget.insert(tk.END, text)
        self.text_widget.see(tk.END)

    def flush(self):
        pass


class CourseAutoApp:
    def __init__(self, root):
        self.root = root
        self.root.title("刷课脚本")
        self.root.geometry("600x620")
        self.root.resizable(False, False)
        self.root.configure(bg="#f0f2f5")

        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - 600) // 2
        y = (self.root.winfo_screenheight() - 620) // 2
        self.root.geometry(f"+{x}+{y}")

        self.running = False
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.click_count = 0
        self.last_click_time = 0
        self.last_play_click_time = 0
        self.consecutive_failures = 0

        self._setup_ui()
        self._setup_logging()

    def _setup_ui(self):
        # 标题
        title_frame = tk.Frame(self.root, bg="#f0f2f5")
        title_frame.pack(fill=tk.X, pady=(20, 8))

        tk.Label(
            title_frame, text="📚 刷课脚本",
            font=("Microsoft YaHei", 18, "bold"),
            bg="#f0f2f5", fg="#1a1a2e"
        ).pack()

        tk.Label(
            title_frame, text="自动检测弹窗 + 自动检测播放按钮",
            font=("Microsoft YaHei", 9),
            bg="#f0f2f5", fg="#666"
        ).pack(pady=(2, 0))

        # 配置区域
        config_frame = tk.Frame(self.root, bg="white", highlightbackground="#ddd", highlightthickness=1, padx=15, pady=10)
        config_frame.pack(fill=tk.X, padx=30, pady=(8, 5))

        tk.Label(config_frame, text="检测间隔（秒）", font=("Microsoft YaHei", 9), bg="white", fg="#555").grid(row=0, column=0, sticky="w", padx=(0, 10))

        self.interval_var = tk.StringVar(value=str(DEFAULT_CHECK_INTERVAL))
        self.interval_entry = tk.Entry(
            config_frame, textvariable=self.interval_var,
            font=("Microsoft YaHei", 11), width=8, justify="center",
            relief="solid", borderwidth=1
        )
        self.interval_entry.grid(row=0, column=1, sticky="w")

        tk.Label(config_frame, text="秒", font=("Microsoft YaHei", 9), bg="white", fg="#555").grid(row=0, column=2, sticky="w", padx=(5, 0))

        tk.Label(config_frame, text="建议 5~30 秒，数值越小检测越快", font=("Microsoft YaHei", 8), bg="white", fg="#aaa").grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

        # 状态区域
        status_frame = tk.Frame(self.root, bg="white", highlightbackground="#ddd", highlightthickness=1, padx=15, pady=10)
        status_frame.pack(fill=tk.X, padx=30, pady=(5, 5))

        tk.Label(status_frame, text="运行状态", font=("Microsoft YaHei", 9), bg="white", fg="#888").grid(row=0, column=0, sticky="w")
        self.status_label = tk.Label(
            status_frame, text="⏸ 已停止",
            font=("Microsoft YaHei", 11, "bold"), bg="white", fg="#999"
        )
        self.status_label.grid(row=1, column=0, sticky="w")

        tk.Label(status_frame, text="点击次数", font=("Microsoft YaHei", 9), bg="white", fg="#888").grid(row=0, column=1, padx=(40, 0), sticky="w")
        self.count_label = tk.Label(
            status_frame, text="0",
            font=("Microsoft YaHei", 11, "bold"), bg="white", fg="#1a1a2e"
        )
        self.count_label.grid(row=1, column=1, padx=(40, 0), sticky="w")

        # 按钮区域
        btn_frame = tk.Frame(self.root, bg="#f0f2f5")
        btn_frame.pack(fill=tk.X, padx=30, pady=(8, 5))

        self.start_btn = tk.Button(
            btn_frame, text="▶ 开始监控", command=self.start_monitor,
            font=("Microsoft YaHei", 11, "bold"),
            bg="#4CAF50", fg="white", relief="flat", padx=20, pady=8,
            cursor="hand2", activebackground="#45a049"
        )
        self.start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        self.stop_btn = tk.Button(
            btn_frame, text="⏹ 停止", command=self.stop_monitor,
            font=("Microsoft YaHei", 11, "bold"),
            bg="#f44336", fg="white", relief="flat", padx=20, pady=8,
            cursor="hand2", state=tk.DISABLED, activebackground="#d32f2f"
        )
        self.stop_btn.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(5, 0))

        # 截图工具按钮
        capture_frame = tk.Frame(self.root, bg="#f0f2f5")
        capture_frame.pack(fill=tk.X, padx=30, pady=(0, 5))

        self.capture_play_btn = tk.Button(
            capture_frame, text="📷 截取「播放」按钮", command=self.capture_play_button,
            font=("Microsoft YaHei", 9),
            bg="#2196F3", fg="white", relief="flat", padx=10, pady=4,
            cursor="hand2", activebackground="#1976D2"
        )
        self.capture_play_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.play_status_label = tk.Label(
            capture_frame, text="未设置",
            font=("Microsoft YaHei", 9), bg="#f0f2f5", fg="#999"
        )
        self.play_status_label.pack(side=tk.LEFT)

        # 日志区域
        log_frame = tk.Frame(self.root, bg="#f0f2f5")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=(5, 20))

        tk.Label(log_frame, text="运行日志", font=("Microsoft YaHei", 9), bg="#f0f2f5", fg="#888").pack(anchor="w")

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=16, font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", relief="flat", borderwidth=1
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(3, 0))

        # 更新播放按钮状态
        self._update_play_status()

    def _update_play_status(self):
        if os.path.exists(PLAY_BTN_TEMPLATE):
            self.play_status_label.config(text="✅ 已设置", fg="#4CAF50")
        else:
            self.play_status_label.config(text="❌ 未设置（点击右侧按钮截图）", fg="#f44336")

    def _setup_logging(self):
        log_handler = logging.StreamHandler(RedirectText(self.log_text))
        log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[log_handler], force=True)
        self.log = logging.getLogger(__name__)

    def _update_status(self, text, color):
        self.status_label.config(text=text, fg=color)

    def _update_count(self):
        self.count_label.config(text=str(self.click_count))

    def find_on_screen(self, template_path, confidence=CONFIDENCE):
        if not os.path.exists(template_path):
            return None
        try:
            return pyautogui.locateOnScreen(template_path, confidence=confidence)
        except Exception:
            return None

    def find_play_button_auto(self):
        """
        自动识别播放按钮：通过图像分析找圆形+三角形图标
        返回 (center_x, center_y) 或 None
        """
        try:
            # 截取全屏
            from PIL import ImageGrab
            pil_img = ImageGrab.grab()
            img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape

            # 排除顶部和底部（导航栏、任务栏）
            roi = gray[int(h*0.08):int(h*0.85), :]

            # 方法1: 霍夫圆检测 — 播放按钮通常是圆形
            blurred = cv2.GaussianBlur(roi, (9, 9), 2)
            circles = cv2.HoughCircles(
                blurred, cv2.HOUGH_GRADIENT,
                dp=1.2, minDist=50,
                param1=50, param2=25,
                minRadius=15, maxRadius=80
            )

            if circles is not None:
                circles = np.round(circles[0, :]).astype(int)
                for (cx, cy, r) in circles:
                    screen_y = cy + int(h*0.08)
                    # 检查圆内是否有三角形特征（播放三角）
                    # 简单方法：检查圆内亮度分布
                    mask = np.zeros_like(gray)
                    cv2.circle(mask, (cx, screen_y), r, 255, -1)
                    circle_region = cv2.bitwise_and(gray, gray, mask=mask)
                    mean_bright = cv2.mean(circle_region, mask=mask)[0]

                    # 圆内平均亮度与周围对比
                    if 30 < mean_bright < 220:
                        self.log.info(f"[自动识别] 霍夫圆检测到播放按钮: ({cx}, {screen_y}) r={r}")
                        return (cx, screen_y)

            # 方法2: 边缘检测 + 轮廓分析 — 找接近正方形的区域
            edges = cv2.Canny(roi, 30, 100)
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            candidates = []
            for cnt in contours:
                x, y, cw, ch = cv2.boundingRect(cnt)
                area = cw * ch
                aspect = cw / max(ch, 1)

                if (PLAY_BTN_MIN_AREA < area < PLAY_BTN_MAX_AREA and
                        PLAY_BTN_ASPECT_MIN < aspect < PLAY_BTN_ASPECT_MAX):
                    # 检查轮廓是否为圆形/椭圆
                    perimeter = cv2.arcLength(cnt, True)
                    circularity = 4 * np.pi * area / max(perimeter * perimeter, 1)
                    # 圆形度 > 0.5 表示接近圆形
                    if circularity > 0.5:
                        screen_y = y + int(h*0.08)
                        cx = x + cw // 2
                        cy = screen_y + ch // 2
                        candidates.append((cx, cy, cw, ch, circularity, area))

            if candidates:
                # 按圆形度排序，取最圆的
                candidates.sort(key=lambda c: c[4], reverse=True)
                cx, cy, cw, ch, circ, area = candidates[0]
                self.log.info(f"[自动识别] 轮廓分析检测到播放按钮: ({cx}, {cy}) 圆形度={circ:.2f}")
                return (cx, cy)

            # 方法3: 屏幕中央区域尝试
            screen_center_x = w // 2
            screen_center_y = h // 2
            self.log.info(f"[自动识别] 未找到圆形按钮，尝试屏幕中央: ({screen_center_x}, {screen_center_y})")
            return (screen_center_x, screen_center_y)

        except Exception as e:
            self.log.warning(f"[自动识别] 图像分析异常: {e}")
            return None

    def capture_play_button(self):
        """截取播放按钮模板"""
        self.log.info("=" * 45)
        self.log.info("请在 5 秒内将鼠标移到「播放」按钮的左上角...")
        self.root.after(0, lambda: self.log_text.insert(tk.END, "请在 5 秒内将鼠标移到「播放」按钮的左上角...\n"))
        self.root.after(0, self.log_text.see, tk.END)

        def do_capture():
            time.sleep(5)
            x1, y1 = pyautogui.position()
            self.log.info(f"左上角: ({x1}, {y1})")
            self.log.info("请在 5 秒内将鼠标移到「播放」按钮的右下角...")
            time.sleep(5)
            x2, y2 = pyautogui.position()
            self.log.info(f"右下角: ({x2}, {y2})")

            left, top = min(x1, x2), min(y1, y2)
            w, h = abs(x2 - x1), abs(y2 - y1)

            # 稍微扩大范围
            pad = 10
            left = max(0, left - pad)
            top = max(0, top - pad)
            w += pad * 2
            h += pad * 2

            TEMPLATE_DIR.mkdir(exist_ok=True)
            from PIL import ImageGrab
            img = ImageGrab.grab(bbox=(left, top, left + w, top + h))
            img.save(PLAY_BTN_TEMPLATE)
            self.log.info(f"「播放」按钮模板已保存: ({left},{top},{w},{h})")
            self.root.after(0, self._update_play_status)

        threading.Thread(target=do_capture, daemon=True).start()

    def click_next(self, x, y):
        """点击"下一个"按钮"""
        pyautogui.click(x, y)
        self.click_count += 1
        self.last_click_time = time.time()
        self.root.after(0, self._update_count)
        self.log.info(f"✅ 点击「下一个」 ({x}, {y}) (第 {self.click_count} 次)")
        time.sleep(3)

    def monitor_loop(self, check_interval):
        TEMPLATE_DIR.mkdir(exist_ok=True)

        if not os.path.exists(COMPLETE_TEMPLATE):
            self.log.error("未找到弹窗模板，请先运行截图模式")
            self.root.after(0, self.stop_monitor)
            return

        self.log.info("=" * 45)
        self.log.info("刷课脚本已启动，开始监控屏幕...")
        self.log.info(f"检测间隔: {check_interval}s | 置信度: {CONFIDENCE}")
        if os.path.exists(PLAY_BTN_TEMPLATE):
            self.log.info("「播放」按钮模板: ✅ 已设置")
        else:
            self.log.info("「播放」按钮模板: ❌ 未设置（将尝试点击屏幕中央）")
        self.log.info("=" * 45)

        while not self.stop_event.is_set():
            try:
                now = time.time()

                # ========== 独立检测播放按钮（与弹窗检测并行） ==========
                if now - self.last_play_click_time > PLAY_CLICK_COOLDOWN:
                    clicked = False

                    # 策略1: 模板匹配（精确，但受背景影响）
                    if os.path.exists(PLAY_BTN_TEMPLATE):
                        play_region = self.find_on_screen(PLAY_BTN_TEMPLATE)
                        if play_region:
                            px, py, pw, ph = play_region
                            pyautogui.click(px + pw // 2, py + ph // 2)
                            self.last_play_click_time = now
                            self.log.info(f"▶ [模板匹配] 点击「播放」按钮 ({px + pw // 2}, {py + ph // 2})")
                            clicked = True

                    # 策略2: 自动图像识别（当模板匹配失败或没有模板时）
                    if not clicked:
                        play_pos = self.find_play_button_auto()
                        if play_pos:
                            px, py = play_pos
                            pyautogui.click(px, py)
                            self.last_play_click_time = now
                            self.log.info(f"▶ [自动识别] 点击播放按钮 ({px}, {py})")
                            clicked = True

                    if not clicked:
                        self.log.info("▶ [自动识别] 未找到播放按钮，跳过本次")

                # ========== 检测完成弹窗 ==========
                complete_region = self.find_on_screen(COMPLETE_TEMPLATE)

                if complete_region:
                    self.log.info(f"检测到完成弹窗: {complete_region}")

                    if now - self.last_click_time < MIN_CLICK_INTERVAL:
                        self.log.info("刚点击过，跳过本次")
                    else:
                        x, y, w, h = complete_region

                        # 策略1: 精确匹配"下一个"按钮
                        btn_region = self.find_on_screen(NEXT_BTN_TEMPLATE)
                        if btn_region:
                            bx, by, bw, bh = btn_region
                            self.click_next(bx + bw // 2, by + bh // 2)
                        else:
                            # 策略2: 宽按钮匹配
                            btn_wide = self.find_on_screen(NEXT_BTN_WIDE_TEMPLATE, confidence=0.6)
                            if btn_wide:
                                bx, by, bw, bh = btn_wide
                                self.click_next(bx + bw // 2, by + bh // 2)
                            else:
                                # 策略3: 精确坐标
                                btn_screen_x = 964 + 510 + 82 // 2
                                btn_screen_y = 688 + 178 + 32 // 2
                                self.click_next(btn_screen_x, btn_screen_y)

                        time.sleep(1.5)
                        if self.find_on_screen(COMPLETE_TEMPLATE):
                            self.log.info("弹窗未消失，尝试其他位置")
                            self.consecutive_failures += 1
                        else:
                            self.log.info("弹窗已消失，继续监控")
                            self.consecutive_failures = 0
                else:
                    self.consecutive_failures = 0

                # 使用自定义检测间隔
                for _ in range(int(check_interval * 2)):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.5)

            except Exception as e:
                self.log.error(f"异常: {e}")
                time.sleep(2)

        self.log.info(f"脚本已停止，共点击 {self.click_count} 次")

    def start_monitor(self):
        if self.running:
            return

        # 读取自定义检测间隔
        try:
            interval = float(self.interval_var.get().strip())
            if interval < 1:
                interval = 1
                self.interval_var.set("1")
            elif interval > 60:
                interval = 60
                self.interval_var.set("60")
        except ValueError:
            interval = DEFAULT_CHECK_INTERVAL
            self.interval_var.set(str(interval))

        self.running = True
        self.stop_event.clear()
        self.click_count = 0
        self.consecutive_failures = 0
        self.last_click_time = 0
        self.last_play_click_time = 0
        self._update_count()

        self.start_btn.config(state=tk.DISABLED, bg="#999")
        self.stop_btn.config(state=tk.NORMAL, bg="#f44336")
        self.interval_entry.config(state=tk.DISABLED, bg="#eee")
        self.capture_play_btn.config(state=tk.DISABLED, bg="#999")
        self._update_status("▶ 运行中...", "#4CAF50")

        self.log_text.delete(1.0, tk.END)

        self.monitor_thread = threading.Thread(target=self.monitor_loop, args=(interval,), daemon=True)
        self.monitor_thread.start()

    def stop_monitor(self):
        self.running = False
        self.stop_event.set()

        self.start_btn.config(state=tk.NORMAL, bg="#4CAF50")
        self.stop_btn.config(state=tk.DISABLED, bg="#999")
        self.interval_entry.config(state=tk.NORMAL, bg="white")
        self.capture_play_btn.config(state=tk.NORMAL, bg="#2196F3")
        self._update_status("⏸ 已停止", "#999")


if __name__ == "__main__":
    root = tk.Tk()
    app = CourseAutoApp(root)
    root.mainloop()