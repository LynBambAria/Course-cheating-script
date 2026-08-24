"""
刷课脚本 - 带 UI 界面
自动检测「当前任务已达到完成条件」弹窗 + 自动检测「播放」按钮
支持抗背景干扰的边缘多尺度模板匹配与几何拓扑结构智能识别
高精度全局最佳匹配算法，彻底杜绝误点「上一个」、文字笔画误判与未完成时误跳课
"""

import time
import logging
import os
import sys
import math
import threading
import ctypes
from pathlib import Path

# 启用 Windows 高 DPI 感知，确保截图像素与鼠标点击坐标 1:1 匹配
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import pyautogui
import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from PIL import ImageGrab

# ============ 默认配置 ============
DEFAULT_CHECK_INTERVAL = 5       # 默认主检测循环间隔（秒）
DEFAULT_POPUP_CONFIDENCE = 0.83  # 弹窗模板匹配置信度（高精度，防误触发）
DEFAULT_NEXT_CONFIDENCE = 0.85   # 「下一个」按钮匹配置信度（防误点「上一个」）
MIN_POPUP_CLICK_INTERVAL = 5     # 弹窗点击最小防抖间隔（秒）
DEFAULT_PLAY_COOLDOWN = 15       # 播放按钮点击冷却时间（秒）
EDGE_MATCH_THRESHOLD = 0.60      # 边缘模板匹配置信度阈值
GEO_CONF_THRESHOLD = 0.85        # 几何识别置信度阈值（严格防文字误判）

# 播放按钮几何识别参数（设置合理尺寸，彻底过滤字号只有 10~14px 的中文字体笔画）
PLAY_TRI_MIN_AREA = 120          # 播放三角图标最小面积（像素）
PLAY_TRI_MAX_AREA = 25000        # 播放三角图标最大面积
PLAY_TRI_MIN_SIZE = 16           # 播放三角最小宽高（像素）
PLAY_TRI_ASPECT_MIN = 0.58       # 三角形高宽比下限
PLAY_TRI_ASPECT_MAX = 1.65       # 三角形高宽比上限


def get_resource_path(relative_path):
    """
    获取资源绝对路径：
    1. 优先读取 exe 同级目录中的文件（便于用户外部放置或覆盖模板）
    2. 其次读取 PyInstaller 打包内置的 _MEIPASS 目录（内置默认模板）
    3. 最后回退到脚本源码所在目录
    """
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).resolve().parent
        external_path = exe_dir / relative_path
        if external_path.exists():
            return external_path
        bundle_dir = getattr(sys, '_MEIPASS', None)
        if bundle_dir:
            bundle_path = Path(bundle_dir) / relative_path
            if bundle_path.exists():
                return bundle_path
        return external_path
    else:
        return Path(__file__).resolve().parent / relative_path


def get_template_save_dir():
    """获取保存新截图模板的目标目录（始终保存到 exe 同级目录的 templates/ 文件夹中）"""
    if getattr(sys, 'frozen', False):
        save_dir = Path(sys.executable).resolve().parent / "templates"
    else:
        save_dir = Path(__file__).resolve().parent / "templates"
    save_dir.mkdir(parents=True, exist_ok=True)
    return save_dir


def get_template_path(filename):
    """获取模板文件的可用路径"""
    return str(get_resource_path(f"templates/{filename}"))


class RedirectText:
    """重定向日志输出至 UI 文本框"""
    def __init__(self, text_widget):
        self.text_widget = text_widget

    def write(self, text):
        if text:
            self.text_widget.after(0, self._append, text)

    def flush(self):
        pass

    def _append(self, text):
        try:
            self.text_widget.insert(tk.END, text)
            self.text_widget.see(tk.END)
        except Exception:
            pass


def find_best_template_match(screen_bgr, tpl_bgr, min_confidence=0.82, roi=None):
    """
    在屏幕（或 ROI 区域）中执行全局最佳模板匹配，返回最高相似度坐标
    彻底解决 PyAutoGUI locateOnScreen 从左至右扫描时误选左侧「上一个」的问题
    返回: (center_x, center_y, width, height, max_val) 或 None
    """
    if screen_bgr is None or tpl_bgr is None:
        return None

    try:
        screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
        tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
        th, tw = tpl_gray.shape[:2]

        offset_x, offset_y = 0, 0
        if roi is not None:
            rx, ry, rw, rh = roi
            rx = max(0, min(screen_gray.shape[1] - 1, rx))
            ry = max(0, min(screen_gray.shape[0] - 1, ry))
            rw = min(rw, screen_gray.shape[1] - rx)
            rh = min(rh, screen_gray.shape[0] - ry)
            if rw >= tw and rh >= th:
                screen_gray = screen_gray[ry:ry + rh, rx:rx + rw]
                offset_x, offset_y = rx, ry

        if screen_gray.shape[0] < th or screen_gray.shape[1] < tw:
            return None

        res = cv2.matchTemplate(screen_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

        if max_val >= min_confidence:
            gx = max_loc[0] + offset_x
            gy = max_loc[1] + offset_y
            cx = gx + tw // 2
            cy = gy + th // 2
            return (cx, cy, tw, th, max_val)

    except Exception:
        pass

    return None


def is_right_pointing_triangle(approx, contour_area):
    """
    严格校验多边形是否为播放按钮的右向三角形（▶）
    设置合理尺寸下限，彻底过滤文字（如「上」「个」等中文字符笔画）
    返回: (is_valid, score, (center_x, center_y, width, height))
    """
    if len(approx) != 3:
        return False, 0.0, None

    pts = approx.reshape(3, 2)
    # 按 X 坐标排序：左边两个点，右边一个尖端顶点
    pts_sorted_x = pts[np.argsort(pts[:, 0])]
    left_pt1 = pts_sorted_x[0]
    left_pt2 = pts_sorted_x[1]
    right_tip = pts_sorted_x[2]

    # 左侧两点形成的底边高度
    dy_left = abs(left_pt1[1] - left_pt2[1])
    if dy_left < 14:  # 过滤普通文字
        return False, 0.0, None

    # 左侧两点的水平偏差（底边应当接近垂直）
    dx_left = abs(left_pt1[0] - left_pt2[0])

    # 右侧尖端必须明显在左侧两个顶点的右方
    dx_tip = right_tip[0] - max(left_pt1[0], left_pt2[0])
    if dx_tip <= max(4, dx_left * 0.8):
        return False, 0.0, None

    # 三角形整体尺寸
    tri_w = right_tip[0] - min(left_pt1[0], left_pt2[0])
    tri_h = max(left_pt1[1], left_pt2[1], right_tip[1]) - min(left_pt1[1], left_pt2[1], right_tip[1])

    if tri_w < PLAY_TRI_MIN_SIZE or tri_h < PLAY_TRI_MIN_SIZE:
        return False, 0.0, None

    # 宽高比校验（标准播放箭头宽高比约为 0.65 ~ 1.55）
    aspect = tri_w / float(max(tri_h, 1))
    if not (PLAY_TRI_ASPECT_MIN <= aspect <= PLAY_TRI_ASPECT_MAX):
        return False, 0.0, None

    # 右侧尖端的 Y 坐标应大致居中于左侧底边两点之间
    left_mid_y = (left_pt1[1] + left_pt2[1]) / 2.0
    tip_y_diff = abs(right_tip[1] - left_mid_y)
    if tip_y_diff > dy_left * 0.35:
        return False, 0.0, None

    # 面积与外接矩形比例校验
    box_area = tri_w * tri_h
    if contour_area < PLAY_TRI_MIN_AREA or box_area <= 0:
        return False, 0.0, None

    area_ratio = contour_area / float(box_area)
    if not (0.30 <= area_ratio <= 0.70):
        return False, 0.0, None

    score = 0.80
    if dx_left <= max(2, tri_w * 0.12):
        score += 0.10
    if tip_y_diff <= dy_left * 0.15:
        score += 0.10

    center_x = int(np.mean(pts[:, 0]))
    center_y = int(np.mean(pts[:, 1]))

    return True, score, (center_x, center_y, tri_w, tri_h)


def detect_play_button_geometry(screen_bgr):
    """
    智能几何拓扑检测：严格寻找视频播放器中心的同心圆盘+播放箭头 ▶
    强制要求外层同心圆盘底座或大尺寸播放标志，彻底过滤「上一个」等汉字按钮
    返回: (center_x, center_y, confidence, method_desc) 或 None
    """
    if screen_bgr is None:
        return None

    gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # 排除顶部导航栏和底部任务栏干扰区域
    roi_top = int(h * 0.06)
    roi_bottom = int(h * 0.94)
    roi_gray = gray[roi_top:roi_bottom, :]

    blurred = cv2.GaussianBlur(roi_gray, (5, 5), 0)

    # 边缘与阈值二值化
    binary_maps = []
    edges = cv2.Canny(blurred, 40, 130)
    binary_maps.append(("canny", edges))

    _, thresh_otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    binary_maps.append(("otsu", thresh_otsu))
    binary_maps.append(("otsu_inv", cv2.bitwise_not(thresh_otsu)))

    detected_triangles = []
    detected_circles = []

    for name, bin_img in binary_maps:
        contours, _ = cv2.findContours(bin_img, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < PLAY_TRI_MIN_AREA:
                continue

            peri = cv2.arcLength(cnt, True)
            if peri <= 0:
                continue

            approx = cv2.approxPolyDP(cnt, 0.048 * peri, True)
            if len(approx) == 3 and cv2.isContourConvex(approx):
                is_tri, score, box = is_right_pointing_triangle(approx, area)
                if is_tri:
                    cx, cy, tw, th = box
                    screen_cy = cy + roi_top
                    detected_triangles.append({
                        "center": (cx, screen_cy),
                        "box": (cx - tw // 2, screen_cy - th // 2, tw, th),
                        "score": score,
                        "area": area
                    })

            # 检测外围包围圆/圆角按钮底座（半径需 ≥ 22px，面积 ≥ 1500px）
            if area >= 1500:
                circularity = 4 * np.pi * area / (peri * peri)
                if circularity > 0.65:
                    (ccx, ccy), radius = cv2.minEnclosingCircle(cnt)
                    if radius >= 22:
                        detected_circles.append({
                            "center": (int(ccx), int(ccy) + roi_top),
                            "radius": int(radius),
                            "area": area,
                            "circularity": circularity
                        })

    if not detected_triangles:
        return None

    # 聚类去重
    unique_candidates = []
    for tri in detected_triangles:
        tcx, tcy = tri["center"]
        merged = False
        for uc in unique_candidates:
            ucx, ucy = uc["center"]
            if math.hypot(tcx - ucx, tcy - ucy) < 25:
                if tri["score"] > uc["score"]:
                    uc["score"] = tri["score"]
                    uc["center"] = tri["center"]
                    uc["box"] = tri["box"]
                merged = True
                break
        if not merged:
            unique_candidates.append(tri)

    best_candidate = None
    best_final_score = 0.0
    best_desc = ""

    for cand in unique_candidates:
        cx, cy = cand["center"]
        tw, th = cand["box"][2], cand["box"][3]
        has_enclosing_circle = False

        # 检查外围是否有同心圆盘结构
        for circ in detected_circles:
            ccx, ccy = circ["center"]
            r = circ["radius"]
            dist = math.hypot(cx - ccx, cy - ccy)
            # 同心度对齐且尺寸比例符合播放器特征（外圈半径为三角宽度的 0.65 ~ 3.5 倍）
            if dist <= max(18, r * 0.35) and (r >= tw * 0.65 and r <= tw * 3.5):
                has_enclosing_circle = True
                break

        # 无同心圆底座的独立三角，必须具有大尺寸（≥26x26）且位于屏幕中心视频播放区域
        if not has_enclosing_circle:
            if tw < 26 or th < 26:
                continue

        final_score = cand["score"]
        if has_enclosing_circle:
            final_score += 0.20

        rel_x = cx / float(w)
        rel_y = cy / float(h)
        if 0.20 < rel_x < 0.80 and 0.20 < rel_y < 0.80:
            final_score += 0.05

        desc = "视频中心圆盘播放键" if has_enclosing_circle else "大尺寸播放三角"
        if final_score > best_final_score:
            best_final_score = final_score
            best_candidate = cand
            best_desc = f"智能几何识别: {desc}"

    if best_candidate and best_final_score >= GEO_CONF_THRESHOLD:
        bx, by = best_candidate["center"]
        return bx, by, min(best_final_score, 0.99), best_desc

    return None


def match_template_edge_multiscale(screen_bgr, tpl_bgr, threshold=EDGE_MATCH_THRESHOLD,
                                    scales=(0.85, 0.92, 1.0, 1.08, 1.18)):
    """
    边缘多尺度模板匹配：通过提取轮廓边缘抵消半透明及背景视频帧的干扰
    返回: (center_x, center_y, confidence, method_desc) 或 None
    """
    if tpl_bgr is None or screen_bgr is None:
        return None

    try:
        screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
        tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)

        screen_edge = cv2.Canny(screen_gray, 35, 120)
        kernel = np.ones((2, 2), np.uint8)
        screen_edge = cv2.dilate(screen_edge, kernel, iterations=1)

        tpl_edge = cv2.Canny(tpl_gray, 35, 120)
        th, tw = tpl_edge.shape[:2]

        if tw < 8 or th < 8:
            return None

        best_match = None
        max_val_found = -1.0

        for scale in scales:
            sw = int(tw * scale)
            sh = int(th * scale)
            if sw >= screen_gray.shape[1] or sh >= screen_gray.shape[0] or sw < 10 or sh < 10:
                continue

            scaled_tpl = cv2.resize(tpl_edge, (sw, sh),
                                    interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
            res = cv2.matchTemplate(screen_edge, scaled_tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            if max_val > max_val_found:
                max_val_found = max_val
                best_match = (max_loc[0] + sw // 2, max_loc[1] + sh // 2, max_val, scale)

        if best_match and max_val_found >= threshold:
            cx, cy, conf, scale_used = best_match
            return cx, cy, conf, f"边缘多尺度模板匹配(置信度:{conf:.2f}, 缩放:{scale_used:.2f})"

    except Exception:
        pass

    return None


class CourseAutoApp:
    def __init__(self, root):
        self.root = root
        self.root.title("📚 刷课自动化助手 v1.3")
        # 更加宽敞舒适的默认窗口尺寸：宽 880px，高 720px，支持自由缩放调整
        self.root.geometry("880x720")
        self.root.minsize(780, 580)
        self.root.resizable(True, True)
        self.root.configure(bg="#f4f6f9")

        # 窗口居中
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - 880) // 2
        y = (self.root.winfo_screenheight() - 720) // 2
        self.root.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.running = False
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.popup_click_count = 0
        self.play_click_count = 0
        self.last_popup_click_time = 0
        self.last_play_click_time = 0
        self.play_click_attempts = 0
        self.last_play_coord = None

        self._setup_ui()
        self._setup_logging()

    def _setup_ui(self):
        # 顶部标题栏
        title_frame = tk.Frame(self.root, bg="#20232a", pady=14)
        title_frame.pack(fill=tk.X)

        tk.Label(
            title_frame, text="📚 刷课助手 · 智能播放与弹窗监控",
            font=("Microsoft YaHei", 16, "bold"),
            bg="#20232a", fg="#61dafb"
        ).pack()

        tk.Label(
            title_frame, text="高精度全局最佳匹配 · 彻底杜绝文字笔画误判与未完成时误跳课",
            font=("Microsoft YaHei", 9),
            bg="#20232a", fg="#abb2bf"
        ).pack(pady=(3, 0))

        main_container = tk.Frame(self.root, bg="#f4f6f9", padx=20, pady=10)
        main_container.pack(fill=tk.BOTH, expand=True)

        # 参数配置区域
        config_frame = tk.LabelFrame(
            main_container, text=" ⚙️ 监控与检测参数设置 ",
            font=("Microsoft YaHei", 10, "bold"),
            bg="white", fg="#333", padx=16, pady=10, relief="solid", borderwidth=1
        )
        config_frame.pack(fill=tk.X, pady=(0, 8))

        # 第 1 行：检测间隔、弹窗置信度与播放冷却（横向宽敞排布）
        row1 = tk.Frame(config_frame, bg="white")
        row1.pack(fill=tk.X, pady=3)

        tk.Label(row1, text="检测间隔:", font=("Microsoft YaHei", 9), bg="white", fg="#444").pack(side=tk.LEFT)
        self.interval_var = tk.StringVar(value=str(DEFAULT_CHECK_INTERVAL))
        self.interval_entry = tk.Entry(
            row1, textvariable=self.interval_var,
            font=("Microsoft YaHei", 9, "bold"), width=5, justify="center",
            relief="solid", borderwidth=1
        )
        self.interval_entry.pack(side=tk.LEFT, padx=(4, 2))
        tk.Label(row1, text="秒", font=("Microsoft YaHei", 9), bg="white", fg="#666").pack(side=tk.LEFT, padx=(0, 24))

        tk.Label(row1, text="弹窗匹配阈值:", font=("Microsoft YaHei", 9), bg="white", fg="#444").pack(side=tk.LEFT)
        self.popup_conf_var = tk.StringVar(value=str(DEFAULT_POPUP_CONFIDENCE))
        self.popup_conf_entry = tk.Entry(
            row1, textvariable=self.popup_conf_var,
            font=("Microsoft YaHei", 9, "bold"), width=5, justify="center",
            relief="solid", borderwidth=1
        )
        self.popup_conf_entry.pack(side=tk.LEFT, padx=(4, 2))
        tk.Label(row1, text="(防误触建议≥0.82)", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(side=tk.LEFT, padx=(0, 24))

        tk.Label(row1, text="播放检测冷却:", font=("Microsoft YaHei", 9), bg="white", fg="#444").pack(side=tk.LEFT)
        self.play_cooldown_var = tk.StringVar(value=str(DEFAULT_PLAY_COOLDOWN))
        self.play_cooldown_entry = tk.Entry(
            row1, textvariable=self.play_cooldown_var,
            font=("Microsoft YaHei", 9, "bold"), width=5, justify="center",
            relief="solid", borderwidth=1
        )
        self.play_cooldown_entry.pack(side=tk.LEFT, padx=(4, 2))
        tk.Label(row1, text="秒", font=("Microsoft YaHei", 9), bg="white", fg="#666").pack(side=tk.LEFT)

        # 第 2 行：自动播放开关与识别策略
        row2 = tk.Frame(config_frame, bg="white")
        row2.pack(fill=tk.X, pady=(8, 2))

        self.auto_play_var = tk.BooleanVar(value=True)
        self.auto_play_cb = tk.Checkbutton(
            row2, text="启用自动播放", variable=self.auto_play_var,
            font=("Microsoft YaHei", 9, "bold"), bg="white", fg="#1a73e8",
            activebackground="white", selectcolor="white"
        )
        self.auto_play_cb.pack(side=tk.LEFT, padx=(0, 25))

        tk.Label(row2, text="播放识别模式:", font=("Microsoft YaHei", 9), bg="white", fg="#444").pack(side=tk.LEFT)
        self.play_mode_var = tk.StringVar(value="dual")
        self.play_mode_cb = ttk.Combobox(
            row2, textvariable=self.play_mode_var,
            values=["dual", "geo", "template"], state="readonly", width=22,
            font=("Microsoft YaHei", 9)
        )
        self.play_mode_cb.pack(side=tk.LEFT, padx=6)
        self.play_mode_cb['values'] = ("智能综合识别 (推荐)", "纯几何识别 (免模板)", "模板匹配优先")
        self.play_mode_cb.current(0)

        # 状态概览区域（网格平均拉伸，布局自适应）
        status_frame = tk.Frame(main_container, bg="white", padx=16, pady=10, relief="solid", borderwidth=1)
        status_frame.pack(fill=tk.X, pady=(0, 8))

        status_frame.columnconfigure(0, weight=1)
        status_frame.columnconfigure(1, weight=1)
        status_frame.columnconfigure(2, weight=1)
        status_frame.columnconfigure(3, weight=1)

        # 状态卡片 1
        card1 = tk.Frame(status_frame, bg="white")
        card1.grid(row=0, column=0, sticky="ew")
        tk.Label(card1, text="运行状态", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(anchor="w")
        self.status_label = tk.Label(
            card1, text="⏸ 已停止",
            font=("Microsoft YaHei", 11, "bold"), bg="white", fg="#888"
        )
        self.status_label.pack(anchor="w", pady=(2, 0))

        # 状态卡片 2
        card2 = tk.Frame(status_frame, bg="white")
        card2.grid(row=0, column=1, sticky="ew")
        tk.Label(card2, text="切课点击", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(anchor="w")
        self.popup_count_label = tk.Label(
            card2, text="0 次",
            font=("Microsoft YaHei", 11, "bold"), bg="white", fg="#2e7d32"
        )
        self.popup_count_label.pack(anchor="w", pady=(2, 0))

        # 状态卡片 3
        card3 = tk.Frame(status_frame, bg="white")
        card3.grid(row=0, column=2, sticky="ew")
        tk.Label(card3, text="播放点击", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(anchor="w")
        self.play_count_label = tk.Label(
            card3, text="0 次",
            font=("Microsoft YaHei", 11, "bold"), bg="white", fg="#1976d2"
        )
        self.play_count_label.pack(anchor="w", pady=(2, 0))

        # 状态卡片 4
        card4 = tk.Frame(status_frame, bg="white")
        card4.grid(row=0, column=3, sticky="ew")
        tk.Label(card4, text="播放模板状态", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(anchor="w")
        self.play_tpl_label = tk.Label(
            card4, text="检查中...",
            font=("Microsoft YaHei", 10, "bold"), bg="white", fg="#666"
        )
        self.play_tpl_label.pack(anchor="w", pady=(2, 0))

        # 控制与工具按钮区域
        btn_frame = tk.Frame(main_container, bg="#f4f6f9")
        btn_frame.pack(fill=tk.X, pady=(2, 8))

        self.start_btn = tk.Button(
            btn_frame, text="▶ 开始监控", command=self.start_monitor,
            font=("Microsoft YaHei", 10, "bold"),
            bg="#2e7d32", fg="white", relief="flat", padx=20, pady=8,
            cursor="hand2", activebackground="#1b5e20"
        )
        self.start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self.stop_btn = tk.Button(
            btn_frame, text="⏹ 停止监控", command=self.stop_monitor,
            font=("Microsoft YaHei", 10, "bold"),
            bg="#d32f2f", fg="white", relief="flat", padx=20, pady=8,
            cursor="hand2", state=tk.DISABLED, activebackground="#b71c1c"
        )
        self.stop_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        self.test_btn = tk.Button(
            btn_frame, text="🔍 测试识别当前屏幕", command=self.test_recognition,
            font=("Microsoft YaHei", 10),
            bg="#0288d1", fg="white", relief="flat", padx=16, pady=8,
            cursor="hand2", activebackground="#01579b"
        )
        self.test_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        self.capture_btn = tk.Button(
            btn_frame, text="📷 截取播放键", command=self.capture_play_button,
            font=("Microsoft YaHei", 10),
            bg="#5c6bc0", fg="white", relief="flat", padx=14, pady=8,
            cursor="hand2", activebackground="#3949ab"
        )
        self.capture_btn.pack(side=tk.LEFT, padx=(6, 0))

        # 日志输出区域
        log_frame = tk.Frame(main_container, bg="#f4f6f9")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        log_hdr = tk.Frame(log_frame, bg="#f4f6f9")
        log_hdr.pack(fill=tk.X)
        tk.Label(log_hdr, text="📋 实时运行日志", font=("Microsoft YaHei", 9, "bold"), bg="#f4f6f9", fg="#555").pack(side=tk.LEFT)
        tk.Button(
            log_hdr, text="清空日志", command=self.clear_log,
            font=("Microsoft YaHei", 8), bg="#e0e0e0", fg="#444", relief="flat", padx=8, pady=2
        ).pack(side=tk.RIGHT)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=14, font=("Consolas", 10),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white", relief="flat", borderwidth=1
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        self._update_play_tpl_status()

    def _update_play_tpl_status(self):
        play_tpl_path = get_template_path("play_button.png")
        if os.path.exists(play_tpl_path):
            self.play_tpl_label.config(text="✅ 已配置模板", fg="#2e7d32")
        else:
            self.play_tpl_label.config(text="ℹ️ 智能免模板", fg="#0288d1")

    def _setup_logging(self):
        log_handler = logging.StreamHandler(RedirectText(self.log_text))
        log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        logging.basicConfig(level=logging.INFO, handlers=[log_handler], force=True)
        self.log = logging.getLogger(__name__)

    def _update_status(self, text, color):
        self.status_label.config(text=text, fg=color)

    def _update_counts(self):
        self.popup_count_label.config(text=f"{self.popup_click_count} 次")
        self.play_count_label.config(text=f"{self.play_click_count} 次")

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def grab_screen(self):
        """截取全屏并转为 OpenCV BGR 格式"""
        try:
            pil_img = ImageGrab.grab()
            return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        except Exception as e:
            self.log.error(f"全屏截图失败: {e}")
            return None

    def find_play_button(self, screen_bgr=None):
        """
        综合查找播放按钮：
        1. 边缘多尺度模板匹配（消除半透明与背景色干扰）
        2. 智能几何拓扑识别（寻找标准播放三角形与同心圆盘）
        3. 绝不返回模糊默认坐标或屏幕中央
        返回: (center_x, center_y, confidence, method_desc) 或 None
        """
        if screen_bgr is None:
            screen_bgr = self.grab_screen()
        if screen_bgr is None:
            return None

        mode_idx = self.play_mode_cb.current()
        play_tpl_path = get_template_path("play_button.png")
        has_tpl = os.path.exists(play_tpl_path)
        tpl_bgr = cv2.imread(play_tpl_path) if has_tpl else None

        # 模式 1: 纯几何识别
        if mode_idx == 1:
            return detect_play_button_geometry(screen_bgr)

        # 模式 2: 模板匹配优先
        if mode_idx == 2 and tpl_bgr is not None:
            res = match_template_edge_multiscale(screen_bgr, tpl_bgr)
            if res:
                return res
            return detect_play_button_geometry(screen_bgr)

        # 模式 0 (默认): 智能综合识别（边缘模板 + 几何识别互补）
        if tpl_bgr is not None:
            edge_res = match_template_edge_multiscale(screen_bgr, tpl_bgr)
            if edge_res and edge_res[2] >= 0.65:
                return edge_res

        # 执行几何拓扑识别
        geo_res = detect_play_button_geometry(screen_bgr)
        if geo_res:
            return geo_res

        # 若几何未直接命中且存在模板，降级尝试普通边缘匹配
        if tpl_bgr is not None:
            return match_template_edge_multiscale(screen_bgr, tpl_bgr, threshold=0.55)

        return None

    def find_popup_and_next_button(self, screen_bgr, min_popup_conf=DEFAULT_POPUP_CONFIDENCE):
        """
        高精度检测完成弹窗并精准定位「下一个」按钮：
        1. 检查是否存在真正的完成弹窗（严格高置信度匹配 + 内容方差校验）
        2. 优先在弹窗区域内部寻找「下一个」按钮（全局最高相似度匹配，杜绝误点左侧「上一个」）
        返回: (click_x, click_y, popup_conf, btn_conf) 或 None
        """
        complete_tpl_path = get_template_path("task_complete.png")
        if not os.path.exists(complete_tpl_path):
            return None

        tc_tpl = cv2.imread(complete_tpl_path)
        if tc_tpl is None:
            return None

        # 1. 检测弹窗
        popup_match = find_best_template_match(screen_bgr, tc_tpl, min_confidence=min_popup_conf)
        if not popup_match:
            return None

        pcx, pcy, pw, ph, pconf = popup_match
        popup_roi = (pcx - pw // 2, pcy - ph // 2, pw, ph)

        # 2. 匹配「下一个」按钮
        next_tpl_path = get_template_path("next_button.png")
        next_wide_path = get_template_path("next_button_wide.png")

        nb_tpl = cv2.imread(next_tpl_path) if os.path.exists(next_tpl_path) else None
        nb_wide = cv2.imread(next_wide_path) if os.path.exists(next_wide_path) else None

        btn_match = None
        # 优先在弹窗区域内（限定 ROI）寻找标准按钮
        if nb_tpl is not None:
            btn_match = find_best_template_match(screen_bgr, nb_tpl, min_confidence=DEFAULT_NEXT_CONFIDENCE, roi=popup_roi)

        # 次选在弹窗区域内寻找宽按钮
        if not btn_match and nb_wide is not None:
            btn_match = find_best_template_match(screen_bgr, nb_wide, min_confidence=0.82, roi=popup_roi)

        # 若限定 ROI 内未找到，在全屏以严格阈值寻找全局最佳匹配
        if not btn_match and nb_tpl is not None:
            btn_match = find_best_template_match(screen_bgr, nb_tpl, min_confidence=0.88)

        if btn_match:
            bx, by, bw, bh, bconf = btn_match
            return (bx, by, pconf, bconf)
        else:
            # 兜底：根据弹窗严格几何相对偏移计算「下一个」按钮位置（弹窗右下角区域）
            top_left_x = pcx - pw // 2
            top_left_y = pcy - ph // 2
            fallback_x = top_left_x + int(pw * 0.84)
            fallback_y = top_left_y + int(ph * 0.82)
            return (fallback_x, fallback_y, pconf, 0.80)

    def test_recognition(self):
        """单次测试识别当前屏幕（用于用户调优与诊断）"""
        self.log.info("=" * 45)
        self.log.info("🔍 开始单次屏幕识别诊断...")
        screen = self.grab_screen()
        if screen is None:
            self.log.error("无法捕获屏幕图像")
            return

        h, w = screen.shape[:2]
        self.log.info(f"当前屏幕分辨率: {w}x{h}")

        try:
            p_conf = float(self.popup_conf_var.get().strip())
        except ValueError:
            p_conf = DEFAULT_POPUP_CONFIDENCE

        # 1. 弹窗与「下一个」按钮诊断
        complete_tpl_path = get_template_path("task_complete.png")
        if os.path.exists(complete_tpl_path):
            tc_tpl = cv2.imread(complete_tpl_path)
            raw_popup = find_best_template_match(screen, tc_tpl, min_confidence=0.50)
            if raw_popup:
                r_pcx, r_pcy, r_pw, r_ph, r_pconf = raw_popup
                if r_pconf >= p_conf:
                    self.log.info(f"🎯 检测到「完成任务」弹窗: 坐标 ({r_pcx}, {r_pcy}) | 相似度: {r_pconf:.3f} (高于阈值 {p_conf})")
                    target = self.find_popup_and_next_button(screen, min_popup_conf=p_conf)
                    if target:
                        tx, ty, _, bconf = target
                        self.log.info(f"✅ 成功锁定「下一个」按钮: 点击坐标 ({tx}, {ty}) | 按钮置信度: {bconf:.3f}")
                else:
                    self.log.info(f"ℹ️ 屏幕存在类似弹窗区域，但相似度仅 {r_pconf:.3f} (低于安全阈值 {p_conf}，已防误触过滤)")
            else:
                self.log.info("ℹ️ 未检测到任何完成弹窗（当前视频未完成，属于正常播放状态）")
        else:
            self.log.warning("⚠️ 未找到 task_complete.png 弹窗模板")

        # 2. 检测播放按钮
        play_res = self.find_play_button(screen)
        if play_res:
            px, py, conf, method = play_res
            self.log.info(f"▶ 成功定位播放按钮: 坐标 ({px}, {py}) | 置信度: {conf:.2f} | 策略: {method}")
        else:
            self.log.info("ℹ️ 未检测到播放按钮（画面可能正在播放中，或文字已正确防误判过滤）")

        self.log.info("🔍 诊断完成")
        self.log.info("=" * 45)

    def capture_play_button(self):
        """截取播放按钮模板"""
        self.log.info("=" * 45)
        self.log.info("📷 准备截取播放按钮模板...")
        self.log.info("👉 请在 5 秒内将鼠标移到「播放」按钮的【左上角】...")

        def do_capture():
            time.sleep(5)
            x1, y1 = pyautogui.position()
            self.log.info(f"📍 左上角坐标记录: ({x1}, {y1})")
            self.log.info("👉 请在 5 秒内将鼠标移到「播放」按钮的【右下角】...")
            time.sleep(5)
            x2, y2 = pyautogui.position()
            self.log.info(f"📍 右下角坐标记录: ({x2}, {y2})")

            left, top = min(x1, x2), min(y1, y2)
            w, h = abs(x2 - x1), abs(y2 - y1)

            if w < 10 or h < 10:
                self.log.error("截取区域过小，请重新截取！")
                return

            pad = 4
            left = max(0, left - pad)
            top = max(0, top - pad)
            w += pad * 2
            h += pad * 2

            save_dir = get_template_save_dir()
            img = ImageGrab.grab(bbox=(left, top, left + w, top + h))
            save_path = save_dir / "play_button.png"
            img.save(str(save_path))
            self.log.info(f"✅ 「播放」按钮模板已保存: {save_path} (尺寸 {w}x{h})")
            self.root.after(0, self._update_play_tpl_status)

        threading.Thread(target=do_capture, daemon=True).start()

    def click_play_button(self, x, y, method_desc):
        """安全点击播放按钮并进行防多点与状态确认"""
        pyautogui.click(x, y)
        self.play_click_count += 1
        self.last_play_click_time = time.time()
        self.root.after(0, self._update_counts)
        self.log.info(f"▶ [{method_desc}] 点击播放按钮 ({x}, {y}) (播放第 {self.play_click_count} 次)")

        # 点击后延迟 1 秒验证按钮是否消失
        time.sleep(1.0)
        verify_screen = self.grab_screen()
        still_exists = self.find_play_button(verify_screen)
        if not still_exists:
            self.log.info("✨ 播放按钮已消失，视频开始播放")
            self.play_click_attempts = 0
            self.last_play_coord = None
        else:
            self.play_click_attempts += 1
            self.last_play_coord = (x, y)
            if self.play_click_attempts >= 3:
                self.log.warning(f"⚠️ 同一位置已连续点击 {self.play_click_attempts} 次未消除，进入防刷保护冷却")

    def click_next(self, x, y):
        """点击「下一个」按钮"""
        pyautogui.click(x, y)
        self.popup_click_count += 1
        self.last_popup_click_time = time.time()
        self.root.after(0, self._update_counts)
        self.log.info(f"✅ 点击「下一个」 ({x}, {y}) (切课第 {self.popup_click_count} 次)")

    def monitor_loop(self, check_interval, play_cooldown, popup_confidence):
        complete_tpl = get_template_path("task_complete.png")
        if not os.path.exists(complete_tpl):
            self.log.error(f"未找到弹窗模板 (task_complete.png)，请确保 templates 文件夹中存在该模板 (路径: {complete_tpl})")
            self.root.after(0, self.stop_monitor)
            return

        self.log.info("=" * 45)
        self.log.info("🚀 刷课监控已启动")
        self.log.info(f"主检测间隔: {check_interval}s | 弹窗安全阈值: {popup_confidence} | 播放冷却: {play_cooldown}s")
        self.log.info(f"自动播放: {'已开启' if self.auto_play_var.get() else '已禁用'}")
        self.log.info("=" * 45)

        while not self.stop_event.is_set():
            try:
                now = time.time()

                # ========== 1. 播放按钮检测与触发 ==========
                if self.auto_play_var.get():
                    cooldown_needed = play_cooldown if self.play_click_attempts < 3 else (play_cooldown * 2.5)
                    if now - self.last_play_click_time > cooldown_needed:
                        screen = self.grab_screen()
                        play_pos = self.find_play_button(screen)
                        if play_pos:
                            px, py, conf, method = play_pos
                            self.click_play_button(px, py, method)

                # ========== 2. 检测任务完成弹窗（严格高精度匹配 + 左右防误选） ==========
                screen = self.grab_screen()
                target = self.find_popup_and_next_button(screen, min_popup_conf=popup_confidence)

                if target:
                    tx, ty, pconf, bconf = target
                    self.log.info(f"🎯 检测到完成弹窗 (置信度: {pconf:.2f})，定位「下一个」按钮 ({tx}, {ty})")

                    if now - self.last_popup_click_time < MIN_POPUP_CLICK_INTERVAL:
                        self.log.info("刚刚已点击过切课，处于安全防抖冷却中...")
                    else:
                        # 弹窗稳定性二次确认（延时 0.4s 再次确认弹窗仍在，杜绝偶发误触发）
                        time.sleep(0.4)
                        verify_screen = self.grab_screen()
                        re_target = self.find_popup_and_next_button(verify_screen, min_popup_conf=popup_confidence)

                        if re_target:
                            rtx, rty, _, _ = re_target
                            self.click_next(rtx, rty)

                            # 点击切换后，等待页面加载并主动触发一次新视频播放检测
                            time.sleep(3.5)
                            if self.auto_play_var.get():
                                screen_after_next = self.grab_screen()
                                next_play_pos = self.find_play_button(screen_after_next)
                                if next_play_pos:
                                    npx, npy, nconf, nmethod = next_play_pos
                                    self.click_play_button(npx, npy, f"切课后-{nmethod}")
                        else:
                            self.log.info("ℹ️ 弹窗二次验证未通过（已自动过滤瞬态干扰）")

                # 间隔休眠等待（支持快速响应停止事件）
                sleep_steps = max(1, int(check_interval * 2))
                for _ in range(sleep_steps):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.5)

            except Exception as e:
                self.log.error(f"监控循环异常: {e}")
                time.sleep(2)

        self.log.info(f"脚本已停止。本次运行: 切课点击 {self.popup_click_count} 次，播放点击 {self.play_click_count} 次")

    def start_monitor(self):
        if self.running:
            return

        # 解析检测间隔
        try:
            interval = float(self.interval_var.get().strip())
            interval = max(0.5, min(60.0, interval))
        except ValueError:
            interval = DEFAULT_CHECK_INTERVAL
        self.interval_var.set(str(int(interval) if interval.is_integer() else interval))

        # 解析弹窗置信度
        try:
            popup_conf = float(self.popup_conf_var.get().strip())
            popup_conf = max(0.60, min(0.98, popup_conf))
        except ValueError:
            popup_conf = DEFAULT_POPUP_CONFIDENCE
        self.popup_conf_var.set(f"{popup_conf:.2f}")

        # 解析播放冷却
        try:
            cooldown = float(self.play_cooldown_var.get().strip())
            cooldown = max(2.0, min(120.0, cooldown))
        except ValueError:
            cooldown = DEFAULT_PLAY_COOLDOWN
        self.play_cooldown_var.set(str(int(cooldown) if cooldown.is_integer() else cooldown))

        self.running = True
        self.stop_event.clear()
        self.popup_click_count = 0
        self.play_click_count = 0
        self.play_click_attempts = 0
        self.last_popup_click_time = 0
        self.last_play_click_time = 0
        self._update_counts()

        self.start_btn.config(state=tk.DISABLED, bg="#999")
        self.stop_btn.config(state=tk.NORMAL, bg="#d32f2f")
        self.test_btn.config(state=tk.DISABLED, bg="#999")
        self.capture_btn.config(state=tk.DISABLED, bg="#999")
        self.interval_entry.config(state=tk.DISABLED, bg="#eee")
        self.popup_conf_entry.config(state=tk.DISABLED, bg="#eee")
        self.play_cooldown_entry.config(state=tk.DISABLED, bg="#eee")
        self.play_mode_cb.config(state=tk.DISABLED)
        self.auto_play_cb.config(state=tk.DISABLED)
        self._update_status("▶ 运行中...", "#2e7d32")

        self.monitor_thread = threading.Thread(
            target=self.monitor_loop,
            args=(interval, cooldown, popup_conf),
            daemon=True
        )
        self.monitor_thread.start()

    def stop_monitor(self):
        self.running = False
        self.stop_event.set()

        self.start_btn.config(state=tk.NORMAL, bg="#2e7d32")
        self.stop_btn.config(state=tk.DISABLED, bg="#999")
        self.test_btn.config(state=tk.NORMAL, bg="#0288d1")
        self.capture_btn.config(state=tk.NORMAL, bg="#5c6bc0")
        self.interval_entry.config(state=tk.NORMAL, bg="white")
        self.popup_conf_entry.config(state=tk.NORMAL, bg="white")
        self.play_cooldown_entry.config(state=tk.NORMAL, bg="white")
        self.play_mode_cb.config(state="readonly")
        self.auto_play_cb.config(state=tk.NORMAL)
        self._update_status("⏸ 已停止", "#888")


if __name__ == "__main__":
    root = tk.Tk()
    app = CourseAutoApp(root)
    root.mainloop()