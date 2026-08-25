"""
刷课脚本 - 带 UI 界面
自动检测「当前任务已达到完成条件」弹窗 + 独立「下一个」按钮 + 自动「播放」按钮 + 课程系统音频分节录制
支持多尺度缩放模板匹配 (0.75x~1.30x)，自适应不同浏览器缩放及 Windows DPI 缩放
支持双轨「下一个」智能识别（弹窗内定位 + 页面独立按钮识别），彻底解决播完后无法切课问题
"""

import time
import logging
import os
import sys
import math
import wave
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

# 音频录制库
try:
    import soundcard as sc
    HAS_SOUNDCARD = True
except ImportError:
    HAS_SOUNDCARD = False

# ============ 默认配置 ============
DEFAULT_CHECK_INTERVAL = 4       # 默认主检测循环间隔（秒）
DEFAULT_POPUP_CONFIDENCE = 0.78  # 弹窗模板匹配置信度（支持多尺度缩放）
DEFAULT_NEXT_CONFIDENCE = 0.80   # 「下一个」按钮匹配置信度（防误点「上一个」）
MIN_POPUP_CLICK_INTERVAL = 5     # 切课点击最小防抖间隔（秒）
DEFAULT_PLAY_COOLDOWN = 15       # 播放按钮点击冷却时间（秒）
EDGE_MATCH_THRESHOLD = 0.58      # 边缘模板匹配置信度阈值
GEO_CONF_THRESHOLD = 0.85        # 几何识别置信度阈值（严格防文字误判）
AUDIO_SAMPLE_RATE = 44100        # 音频录制采样率 (Hz)

# 多尺度匹配常用缩放比例序列（覆盖 75% 到 130% 的浏览器与 DPI 缩放）
DEFAULT_MATCH_SCALES = (0.75, 0.85, 0.92, 1.0, 1.08, 1.18, 1.28)

# 播放按钮几何识别参数
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


def get_recordings_dir():
    """获取课程录音文件的存放目录（保存到 exe 同级目录的 recordings/ 文件夹中）"""
    if getattr(sys, 'frozen', False):
        rec_dir = Path(sys.executable).resolve().parent / "recordings"
    else:
        rec_dir = Path(__file__).resolve().parent / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    return rec_dir


def get_template_path(filename):
    """获取模板文件的可用路径"""
    return str(get_resource_path(f"templates/{filename}"))


class SystemAudioRecorder:
    """系统扬声器声音循环录制器（WASAPI Loopback）"""
    def __init__(self, output_dir, samplerate=AUDIO_SAMPLE_RATE):
        self.output_dir = Path(output_dir)
        self.samplerate = samplerate
        self.recording = False
        self.current_thread = None
        self.stop_event = threading.Event()
        self.current_filepath = None
        self.start_time = 0
        self.episode = 1

    def start_recording(self, episode_num=None):
        if not HAS_SOUNDCARD:
            return None
        if self.recording:
            return self.current_filepath

        if episode_num is not None:
            self.episode = episode_num
        else:
            self.episode += 1

        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"课程录音_第{self.episode:02d}节_{timestamp}.wav"
        self.current_filepath = self.output_dir / filename

        self.recording = True
        self.stop_event.clear()
        self.start_time = time.time()

        self.current_thread = threading.Thread(
            target=self._record_worker,
            args=(self.current_filepath,),
            daemon=True
        )
        self.current_thread.start()
        return self.current_filepath

    def _record_worker(self, wav_path):
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)

            with wave.open(str(wav_path), 'wb') as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(self.samplerate)

                # 分块流式写入（每块约 0.25 秒），低内存占用且防止断电丢失
                block_frames = self.samplerate // 4
                with mic.recorder(samplerate=self.samplerate, channels=2, blocksize=block_frames) as recorder:
                    while not self.stop_event.is_set():
                        data = recorder.record(numframes=block_frames)
                        pcm16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
                        wf.writeframes(pcm16.tobytes())

        except Exception as e:
            logging.getLogger(__name__).error(f"录音过程异常: {e}")
        finally:
            self.recording = False

    def stop_recording(self):
        if not self.recording and not self.current_filepath:
            return None

        self.stop_event.set()
        if self.current_thread and self.current_thread.is_alive():
            self.current_thread.join(timeout=3.0)

        duration = max(0.0, time.time() - self.start_time)
        saved_path = self.current_filepath
        self.recording = False
        self.current_filepath = None
        return saved_path, duration


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


def find_best_template_match(screen_bgr, tpl_bgr, min_confidence=0.80, roi=None,
                             scales=DEFAULT_MATCH_SCALES):
    """
    多尺度全局最佳模板匹配：
    1. 自动适配浏览器 75%~130% 缩放与不同 DPI 分辨率
    2. 全局寻找最高相似度位置，彻底避免从左至右扫描时误选左侧「上一个」
    返回: (center_x, center_y, width, height, max_val, scale) 或 None
    """
    if screen_bgr is None or tpl_bgr is None:
        return None

    try:
        screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
        tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
        th_orig, tw_orig = tpl_gray.shape[:2]

        offset_x, offset_y = 0, 0
        if roi is not None:
            rx, ry, rw, rh = roi
            rx = max(0, min(screen_gray.shape[1] - 1, rx))
            ry = max(0, min(screen_gray.shape[0] - 1, ry))
            rw = min(rw, screen_gray.shape[1] - rx)
            rh = min(rh, screen_gray.shape[0] - ry)
            if rw >= 10 and rh >= 10:
                screen_gray = screen_gray[ry:ry + rh, rx:rx + rw]
                offset_x, offset_y = rx, ry

        best_match = None
        global_max_val = -1.0

        for scale in scales:
            sw = int(tw_orig * scale)
            sh = int(th_orig * scale)

            if sw >= screen_gray.shape[1] or sh >= screen_gray.shape[0] or sw < 10 or sh < 10:
                continue

            scaled_tpl = cv2.resize(tpl_gray, (sw, sh),
                                    interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
            res = cv2.matchTemplate(screen_gray, scaled_tpl, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

            if max_val > global_max_val:
                global_max_val = max_val
                gx = max_loc[0] + offset_x
                gy = max_loc[1] + offset_y
                cx = gx + sw // 2
                cy = gy + sh // 2
                best_match = (cx, cy, sw, sh, max_val, scale)

        if best_match and global_max_val >= min_confidence:
            return best_match

    except Exception:
        pass

    return None


def is_right_pointing_triangle(approx_or_cnt, contour_area):
    """
    严格校验轮廓/多边形是否为播放按钮的右向三角形（▶）
    兼容锐角三角形、圆角/抗锯齿多边形逼近（3~8顶点），设置合理尺寸下限，彻底过滤文字（如「上」「个」等中文字符笔画）
    返回: (is_valid, score, (center_x, center_y, width, height))
    """
    if approx_or_cnt is None or len(approx_or_cnt) < 3:
        return False, 0.0, None

    pts = approx_or_cnt.reshape(-1, 2)
    min_x, max_x = int(np.min(pts[:, 0])), int(np.max(pts[:, 0]))
    min_y, max_y = int(np.min(pts[:, 1])), int(np.max(pts[:, 1]))

    tri_w = max_x - min_x
    tri_h = max_y - min_y

    if tri_w < PLAY_TRI_MIN_SIZE or tri_h < PLAY_TRI_MIN_SIZE:
        return False, 0.0, None

    # 宽高比校验（标准播放箭头宽高比约为 0.52 ~ 1.65）
    aspect = tri_w / float(max(tri_h, 1))
    if not (PLAY_TRI_ASPECT_MIN <= aspect <= PLAY_TRI_ASPECT_MAX):
        return False, 0.0, None

    # 面积与外接矩形比例校验（三角形占外接矩形面积通常在 0.25 ~ 0.75）
    box_area = tri_w * tri_h
    if contour_area < PLAY_TRI_MIN_AREA or box_area <= 0:
        return False, 0.0, None

    area_ratio = contour_area / float(box_area)
    if not (0.24 <= area_ratio <= 0.76):
        return False, 0.0, None

    # 右向三角形拓扑特征：
    # 1. 尖端在最右侧（X 接近 max_x 的点）
    # 2. 底边在左侧（X 接近 min_x 的点应具有跨越较大 Y 范围的分布）
    # 3. 质心 X 坐标偏左（因为左侧底边宽，面积重心偏左，通常 < min_x + 0.58 * tri_w）
    right_pts = pts[pts[:, 0] >= max_x - max(2, int(tri_w * 0.22))]
    left_pts = pts[pts[:, 0] <= min_x + max(2, int(tri_w * 0.28))]

    if len(right_pts) == 0 or len(left_pts) == 0:
        return False, 0.0, None

    # 左侧点在 Y 轴上的跨度应接近整体高度的 55% 以上
    dy_left = int(np.max(left_pts[:, 1])) - int(np.min(left_pts[:, 1]))
    if dy_left < max(10, int(tri_h * 0.52)):
        return False, 0.0, None

    # 右侧尖端的平均 Y 坐标应大致居中于 Y 范围中心
    tip_mid_y = float(np.mean(right_pts[:, 1]))
    center_y_expected = (min_y + max_y) / 2.0
    tip_y_diff = abs(tip_mid_y - center_y_expected)
    if tip_y_diff > tri_h * 0.35:
        return False, 0.0, None

    # 质心 X 偏左校验
    cx = int(np.mean(pts[:, 0]))
    cy = int(np.mean(pts[:, 1]))
    if cx > min_x + int(tri_w * 0.60):  # 右向三角形重心必然在前半段偏左
        return False, 0.0, None

    score = 0.80
    if tip_y_diff <= tri_h * 0.15:
        score += 0.08
    if dy_left >= tri_h * 0.72:
        score += 0.08
    if 0.35 <= area_ratio <= 0.60:
        score += 0.04

    return True, score, (cx, cy, tri_w, tri_h)


def detect_play_button_geometry(screen_bgr, center_only=False):
    """
    智能几何拓扑检测：严格寻找视频播放器的同心圆盘/圆角矩形底座 + 播放箭头 ▶
    支持 center_only 模式（限定屏幕中心区域 18%~82% 视频区域），彻底过滤汉字及侧边干扰
    返回: (center_x, center_y, confidence, method_desc) 或 None
    """
    if screen_bgr is None:
        return None

    gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    if center_only:
        roi_top = int(h * 0.18)
        roi_bottom = int(h * 0.82)
        roi_left = int(w * 0.18)
        roi_right = int(w * 0.82)
        roi_gray = gray[roi_top:roi_bottom, roi_left:roi_right]
        offset_x, offset_y = roi_left, roi_top
    else:
        # 排除极顶部导航栏和极底部任务栏
        roi_top = int(h * 0.05)
        roi_bottom = int(h * 0.95)
        roi_gray = gray[roi_top:roi_bottom, :]
        offset_x, offset_y = 0, roi_top

    blurred = cv2.GaussianBlur(roi_gray, (5, 5), 0)

    # 多策略二值化（Canny边缘、Otsu、自适应阈值，全方位适应亮暗背景）
    binary_maps = []
    edges = cv2.Canny(blurred, 35, 120)
    kernel = np.ones((2, 2), np.uint8)
    edges = cv2.dilate(edges, kernel, iterations=1)
    binary_maps.append(("canny", edges))

    _, thresh_otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    binary_maps.append(("otsu", thresh_otsu))
    binary_maps.append(("otsu_inv", cv2.bitwise_not(thresh_otsu)))

    # 自适应二值化（针对半透明播放图标）
    adaptive_th = cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                         cv2.THRESH_BINARY, 15, 2)
    binary_maps.append(("adaptive", adaptive_th))
    binary_maps.append(("adaptive_inv", cv2.bitwise_not(adaptive_th)))

    detected_triangles = []
    detected_bases = []  # 圆盘或圆角矩形底座

    for name, bin_img in binary_maps:
        contours, _ = cv2.findContours(bin_img, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < PLAY_TRI_MIN_AREA:
                continue

            peri = cv2.arcLength(cnt, True)
            if peri <= 0:
                continue

            # 多尺度逼近因子测试（适应抗锯齿圆角或尖锐三角形）
            is_found = False
            for eps_factor in (0.032, 0.048, 0.070):
                approx = cv2.approxPolyDP(cnt, eps_factor * peri, True)
                if 3 <= len(approx) <= 7:
                    hull = cv2.convexHull(approx)
                    is_tri, score, box = is_right_pointing_triangle(hull, area)
                    if is_tri:
                        cx, cy, tw, th = box
                        screen_cx = cx + offset_x
                        screen_cy = cy + offset_y
                        detected_triangles.append({
                            "center": (screen_cx, screen_cy),
                            "box": (screen_cx - tw // 2, screen_cy - th // 2, tw, th),
                            "score": score,
                            "area": area
                        })
                        is_found = True
                        break

            if is_found:
                continue

            # 检测外围包围圆/圆角按钮底座（半径需 ≥ 20px，面积 ≥ 1200px）
            if area >= 1200:
                circularity = 4 * np.pi * area / (peri * peri)
                bx, by, bw, bh = cv2.boundingRect(cnt)
                aspect_base = bw / float(max(bh, 1))
                rect_ratio = area / float(max(bw * bh, 1))

                # 圆盘底座 (circularity > 0.60)
                if circularity > 0.60:
                    (ccx, ccy), radius = cv2.minEnclosingCircle(cnt)
                    if radius >= 20:
                        detected_bases.append({
                            "type": "circle",
                            "center": (int(ccx) + offset_x, int(ccy) + offset_y),
                            "radius": int(radius),
                            "area": area
                        })
                # 圆角矩形底座 (矩形饱满度 >= 0.70 且 宽高比 0.75~1.40)
                elif 0.75 <= aspect_base <= 1.40 and rect_ratio >= 0.70:
                    detected_bases.append({
                        "type": "rect",
                        "center": (bx + bw // 2 + offset_x, by + bh // 2 + offset_y),
                        "radius": int(max(bw, bh) // 2),
                        "area": area
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
        has_enclosing_base = False
        base_desc = ""

        # 检查外围是否有同心圆盘或圆角矩形结构
        for base in detected_bases:
            bcx, bcy = base["center"]
            r = base["radius"]
            dist = math.hypot(cx - bcx, cy - bcy)
            if dist <= max(20, r * 0.40) and (r >= tw * 0.60 and r <= tw * 4.0):
                has_enclosing_base = True
                base_desc = "视频中心圆盘播放键" if base["type"] == "circle" else "视频中心圆角矩形播放键"
                break

        # 无底座的独立三角：如果在全屏模式下，要求尺寸 ≥ 24x24 或位于中心视频区域
        rel_x = cx / float(w)
        rel_y = cy / float(h)
        is_in_center_zone = (0.22 <= rel_x <= 0.78 and 0.20 <= rel_y <= 0.80)

        if not has_enclosing_base:
            if tw < 22 or th < 22:
                # 允许控制栏小播放三角（宽高比合理且位于视频下方区域）
                if not (0.60 <= rel_y <= 0.95 and tw >= 14 and th >= 14):
                    continue

        final_score = cand["score"]
        if has_enclosing_base:
            final_score += 0.20
        if is_in_center_zone:
            final_score += 0.06

        desc = base_desc if has_enclosing_base else ("视频中心播放图标" if is_in_center_zone else "标准播放箭头 ▶")
        if final_score > best_final_score:
            best_final_score = final_score
            best_candidate = cand
            best_desc = f"智能几何识别: {desc}"

    # 中心优先或带底座时放宽阈值至 0.82，普通三角保持 0.85
    min_thresh = 0.82 if (best_candidate and has_enclosing_base) else GEO_CONF_THRESHOLD
    if best_candidate and best_final_score >= min_thresh:
        bx, by = best_candidate["center"]
        return bx, by, min(best_final_score, 0.99), best_desc

    return None


def match_template_edge_multiscale(screen_bgr, tpl_bgr, threshold=EDGE_MATCH_THRESHOLD,
                                    scales=DEFAULT_MATCH_SCALES):
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
            return cx, cy, conf, f"边缘多尺度模板匹配(置信度:{conf:.2f}, 缩放:{scale_used:.2f}x)"

    except Exception:
        pass

    return None


class CourseAutoApp:
    def __init__(self, root):
        self.root = root
        self.root.title("📚 刷课自动化助手 v1.6 (全场景智能切课)")
        win_w, win_h = 1120, 800
        self.root.geometry(f"{win_w}x{win_h}")
        self.root.minsize(960, 650)
        self.root.resizable(True, True)
        self.root.configure(bg="#f4f6f9")

        # 窗口居中
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - win_w) // 2
        y = (self.root.winfo_screenheight() - win_h) // 2
        self.root.geometry(f"{win_w}x{win_h}+{max(0, x)}+{max(0, y)}")

        self.running = False
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.popup_click_count = 0
        self.play_click_count = 0
        self.last_popup_click_time = 0
        self.last_play_click_time = 0
        self.play_click_attempts = 0
        self.last_play_coord = None

        # 音频录制器
        self.audio_recorder = SystemAudioRecorder(get_recordings_dir())

        self._setup_ui()
        self._setup_logging()

    def _setup_ui(self):
        # 顶部标题栏
        title_frame = tk.Frame(self.root, bg="#1e293b", pady=14)
        title_frame.pack(fill=tk.X)

        tk.Label(
            title_frame, text="📚 刷课自动化助手 · 全场景智能切课 · 音频录制",
            font=("Microsoft YaHei", 15, "bold"),
            bg="#1e293b", fg="#38bdf8"
        ).pack()

        tk.Label(
            title_frame, text="多尺度模板自适应 · 双轨弹窗与独立按钮切课 · 智能几何拓扑播放识别 · 系统音频分节录制",
            font=("Microsoft YaHei", 9),
            bg="#1e293b", fg="#94a3b8"
        ).pack(pady=(3, 0))

        main_container = tk.Frame(self.root, bg="#f4f6f9", padx=20, pady=10)
        main_container.pack(fill=tk.BOTH, expand=True)

        # 参数配置区域
        config_frame = tk.LabelFrame(
            main_container, text=" ⚙️ 监控、切课与播放设置 ",
            font=("Microsoft YaHei", 10, "bold"),
            bg="white", fg="#333", padx=18, pady=12, relief="solid", borderwidth=1
        )
        config_frame.pack(fill=tk.X, pady=(0, 8))

        # 第 1 行：检测间隔、弹窗置信度、按钮置信度与播放冷却
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
        tk.Label(row1, text="秒", font=("Microsoft YaHei", 9), bg="white", fg="#666").pack(side=tk.LEFT, padx=(0, 22))

        tk.Label(row1, text="弹窗阈值:", font=("Microsoft YaHei", 9), bg="white", fg="#444").pack(side=tk.LEFT)
        self.popup_conf_var = tk.StringVar(value=str(DEFAULT_POPUP_CONFIDENCE))
        self.popup_conf_entry = tk.Entry(
            row1, textvariable=self.popup_conf_var,
            font=("Microsoft YaHei", 9, "bold"), width=6, justify="center",
            relief="solid", borderwidth=1
        )
        self.popup_conf_entry.pack(side=tk.LEFT, padx=(4, 2))
        tk.Label(row1, text="(默认0.78)", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(side=tk.LEFT, padx=(0, 22))

        tk.Label(row1, text="按钮阈值:", font=("Microsoft YaHei", 9), bg="white", fg="#444").pack(side=tk.LEFT)
        self.btn_conf_var = tk.StringVar(value=str(DEFAULT_NEXT_CONFIDENCE))
        self.btn_conf_entry = tk.Entry(
            row1, textvariable=self.btn_conf_var,
            font=("Microsoft YaHei", 9, "bold"), width=6, justify="center",
            relief="solid", borderwidth=1
        )
        self.btn_conf_entry.pack(side=tk.LEFT, padx=(4, 2))
        tk.Label(row1, text="(默认0.80)", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(side=tk.LEFT, padx=(0, 22))

        tk.Label(row1, text="播放冷却:", font=("Microsoft YaHei", 9), bg="white", fg="#444").pack(side=tk.LEFT)
        self.play_cooldown_var = tk.StringVar(value=str(DEFAULT_PLAY_COOLDOWN))
        self.play_cooldown_entry = tk.Entry(
            row1, textvariable=self.play_cooldown_var,
            font=("Microsoft YaHei", 9, "bold"), width=5, justify="center",
            relief="solid", borderwidth=1
        )
        self.play_cooldown_entry.pack(side=tk.LEFT, padx=(4, 2))
        tk.Label(row1, text="秒 (默认15)", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(side=tk.LEFT)

        # 第 2 行：自动播放开关、识别模式、音频录制开关与打开目录按钮
        row2 = tk.Frame(config_frame, bg="white")
        row2.pack(fill=tk.X, pady=(10, 2))

        self.auto_play_var = tk.BooleanVar(value=True)
        self.auto_play_cb = tk.Checkbutton(
            row2, text="自动播放", variable=self.auto_play_var,
            font=("Microsoft YaHei", 9, "bold"), bg="white", fg="#1a73e8",
            activebackground="white", selectcolor="white"
        )
        self.auto_play_cb.pack(side=tk.LEFT, padx=(0, 16))

        tk.Label(row2, text="播放模式:", font=("Microsoft YaHei", 9), bg="white", fg="#444").pack(side=tk.LEFT)
        self.play_mode_var = tk.StringVar(value="智能综合识别 (推荐)")
        self.play_mode_cb = ttk.Combobox(
            row2, textvariable=self.play_mode_var,
            values=["智能综合识别 (推荐)", "视频中心大播放键", "纯几何免模板", "模板匹配优先"],
            state="readonly", width=18,
            font=("Microsoft YaHei", 9)
        )
        self.play_mode_cb.pack(side=tk.LEFT, padx=(4, 22))

        # 音频录制独立开关（默认不开启）
        self.auto_record_var = tk.BooleanVar(value=False)
        self.auto_record_cb = tk.Checkbutton(
            row2, text="🎙️ 录制课程音频", variable=self.auto_record_var,
            font=("Microsoft YaHei", 9, "bold"), bg="white", fg="#d81b60",
            activebackground="white", selectcolor="white"
        )
        self.auto_record_cb.pack(side=tk.LEFT, padx=(0, 12))

        tk.Button(
            row2, text="📁 打开录音目录", command=self.open_recordings_dir,
            font=("Microsoft YaHei", 8), bg="#eceff1", fg="#37474f",
            relief="solid", borderwidth=1, padx=10, pady=2, cursor="hand2"
        ).pack(side=tk.LEFT)

        # 状态概览区域
        status_frame = tk.Frame(main_container, bg="white", padx=18, pady=12, relief="solid", borderwidth=1)
        status_frame.pack(fill=tk.X, pady=(0, 8))

        status_frame.columnconfigure(0, weight=1)
        status_frame.columnconfigure(1, weight=1)
        status_frame.columnconfigure(2, weight=1)
        status_frame.columnconfigure(3, weight=1)
        status_frame.columnconfigure(4, weight=1)

        # 状态 1
        card1 = tk.Frame(status_frame, bg="white")
        card1.grid(row=0, column=0, sticky="ew")
        tk.Label(card1, text="运行状态", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(anchor="w")
        self.status_label = tk.Label(
            card1, text="⏸ 已停止",
            font=("Microsoft YaHei", 11, "bold"), bg="white", fg="#888"
        )
        self.status_label.pack(anchor="w", pady=(2, 0))

        # 状态 2
        card2 = tk.Frame(status_frame, bg="white")
        card2.grid(row=0, column=1, sticky="ew")
        tk.Label(card2, text="切课点击", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(anchor="w")
        self.popup_count_label = tk.Label(
            card2, text="0 次",
            font=("Microsoft YaHei", 11, "bold"), bg="white", fg="#2e7d32"
        )
        self.popup_count_label.pack(anchor="w", pady=(2, 0))

        # 状态 3
        card3 = tk.Frame(status_frame, bg="white")
        card3.grid(row=0, column=2, sticky="ew")
        tk.Label(card3, text="播放点击", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(anchor="w")
        self.play_count_label = tk.Label(
            card3, text="0 次",
            font=("Microsoft YaHei", 11, "bold"), bg="white", fg="#1976d2"
        )
        self.play_count_label.pack(anchor="w", pady=(2, 0))

        # 状态 4: 录音状态
        card4 = tk.Frame(status_frame, bg="white")
        card4.grid(row=0, column=3, sticky="ew")
        tk.Label(card4, text="音频录制", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(anchor="w")
        self.record_status_label = tk.Label(
            card4, text="⏸ 未开启",
            font=("Microsoft YaHei", 10, "bold"), bg="white", fg="#888"
        )
        self.record_status_label.pack(anchor="w", pady=(2, 0))

        # 状态 5: 模板状态
        card5 = tk.Frame(status_frame, bg="white")
        card5.grid(row=0, column=4, sticky="ew")
        tk.Label(card5, text="模板库状态", font=("Microsoft YaHei", 8), bg="white", fg="#888").pack(anchor="w")
        self.play_tpl_label = tk.Label(
            card5, text="检查中...",
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
        self.start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        self.stop_btn = tk.Button(
            btn_frame, text="⏹ 停止监控", command=self.stop_monitor,
            font=("Microsoft YaHei", 10, "bold"),
            bg="#d32f2f", fg="white", relief="flat", padx=20, pady=8,
            cursor="hand2", state=tk.DISABLED, activebackground="#b71c1c"
        )
        self.stop_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        self.test_btn = tk.Button(
            btn_frame, text="🔍 测试识别当前屏幕", command=self.test_recognition,
            font=("Microsoft YaHei", 10),
            bg="#0288d1", fg="white", relief="flat", padx=16, pady=8,
            cursor="hand2", activebackground="#01579b"
        )
        self.test_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        # 截取模板下拉菜单按钮
        self.cap_next_btn = tk.Button(
            btn_frame, text="📷 截「下一个」", command=lambda: self.capture_template_interactive("next_button.png", "「下一个」按钮"),
            font=("Microsoft YaHei", 9),
            bg="#5c6bc0", fg="white", relief="flat", padx=10, pady=8,
            cursor="hand2", activebackground="#3949ab"
        )
        self.cap_next_btn.pack(side=tk.LEFT, padx=3)

        self.cap_popup_btn = tk.Button(
            btn_frame, text="📷 截「弹窗」", command=lambda: self.capture_template_interactive("task_complete.png", "「完成弹窗」"),
            font=("Microsoft YaHei", 9),
            bg="#7e57c2", fg="white", relief="flat", padx=10, pady=8,
            cursor="hand2", activebackground="#5e35b1"
        )
        self.cap_popup_btn.pack(side=tk.LEFT, padx=3)

        self.capture_btn = tk.Button(
            btn_frame, text="📷 截「播放」", command=lambda: self.capture_template_interactive("play_button.png", "「播放」按钮"),
            font=("Microsoft YaHei", 9),
            bg="#455a64", fg="white", relief="flat", padx=10, pady=8,
            cursor="hand2", activebackground="#263238"
        )
        self.capture_btn.pack(side=tk.LEFT, padx=(3, 0))

        # 日志输出区域
        log_frame = tk.Frame(main_container, bg="#f4f6f9")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        log_hdr = tk.Frame(log_frame, bg="#f4f6f9")
        log_hdr.pack(fill=tk.X)
        tk.Label(log_hdr, text="📋 实时运行日志", font=("Microsoft YaHei", 9, "bold"), bg="#f4f6f9", fg="#555").pack(side=tk.LEFT)
        tk.Button(
            log_hdr, text="清空日志", command=self.clear_log,
            font=("Microsoft YaHei", 8), bg="#e0e0e0", fg="#444", relief="flat", padx=10, pady=2
        ).pack(side=tk.RIGHT)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=15, font=("Consolas", 10),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white", relief="flat", borderwidth=1
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        self._update_play_tpl_status()

    def open_recordings_dir(self):
        """在文件资源管理器中打开录音目录"""
        rec_dir = get_recordings_dir()
        try:
            os.startfile(str(rec_dir))
        except Exception as e:
            self.log.error(f"打开录音文件夹失败: {e}")

    def _update_play_tpl_status(self):
        tpl_count = 0
        for name in ["task_complete.png", "next_button.png", "play_button.png"]:
            if os.path.exists(get_template_path(name)):
                tpl_count += 1
        self.play_tpl_label.config(text=f"✅ {tpl_count}/3 模板就绪", fg="#2e7d32")

    def _setup_logging(self):
        log_handler = logging.StreamHandler(RedirectText(self.log_text))
        log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        logging.basicConfig(level=logging.INFO, handlers=[log_handler], force=True)
        self.log = logging.getLogger(__name__)

    def _update_status(self, text, color):
        self.status_label.config(text=text, fg=color)

    def _update_record_status(self, text, color):
        self.record_status_label.config(text=text, fg=color)

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
        1. 模式选择分流 (智能综合 / 视频中心大键 / 纯几何免模板 / 模板匹配优先)
        2. 智能几何拓扑识别（寻找标准播放三角形与同心圆盘/圆角矩形底座）
        3. 边缘多尺度模板匹配（消除半透明与背景色干扰）
        返回: (center_x, center_y, confidence, method_desc) 或 None
        """
        if screen_bgr is None:
            screen_bgr = self.grab_screen()
        if screen_bgr is None:
            return None

        mode = self.play_mode_var.get()
        play_tpl_path = get_template_path("play_button.png")
        has_tpl = os.path.exists(play_tpl_path)
        tpl_bgr = cv2.imread(play_tpl_path) if has_tpl else None

        # 模式 1: 视频中心大播放键
        if "视频中心" in mode:
            return detect_play_button_geometry(screen_bgr, center_only=True)

        # 模式 2: 纯几何免模板
        if "纯几何" in mode:
            return detect_play_button_geometry(screen_bgr, center_only=False)

        # 模式 3: 模板匹配优先
        if "模板匹配" in mode and tpl_bgr is not None:
            res = match_template_edge_multiscale(screen_bgr, tpl_bgr)
            if res:
                return res
            return detect_play_button_geometry(screen_bgr, center_only=False)

        # 模式 0 (默认): 智能综合识别（推荐）
        # 1. 优先检测中心高置信度几何播放圆盘/圆角矩形按键
        geo_center = detect_play_button_geometry(screen_bgr, center_only=True)
        if geo_center and geo_center[2] >= 0.88:
            return geo_center

        # 2. 若存在模板，执行多尺度边缘匹配
        if tpl_bgr is not None:
            edge_res = match_template_edge_multiscale(screen_bgr, tpl_bgr)
            if edge_res and edge_res[2] >= 0.65:
                return edge_res

        # 3. 广域几何识别（包含控制栏与各类播放按键）
        geo_res = detect_play_button_geometry(screen_bgr, center_only=False)
        if geo_res:
            return geo_res

        # 4. 降级尝试普通边缘匹配
        if tpl_bgr is not None:
            return match_template_edge_multiscale(screen_bgr, tpl_bgr, threshold=0.55)

        return None

    def find_popup_and_next_button(self, screen_bgr, min_popup_conf=DEFAULT_POPUP_CONFIDENCE,
                                   min_btn_conf=DEFAULT_NEXT_CONFIDENCE):
        """
        全场景智能切课定位系统（双轨并行检测）：
        【轨 1: 弹窗模式】：先检测完成弹窗 (多尺度 0.75x~1.30x)，并在弹窗内寻找/计算「下一个」按钮
        【轨 2: 独立按钮模式】：当无弹窗但页面/播放器亮起「下一个」时，多尺度高精度锁定「下一个」按钮
        返回: (click_x, click_y, match_score, trigger_desc) 或 None
        """
        if screen_bgr is None:
            return None

        # 加载所有可用模板
        complete_tpl_path = get_template_path("task_complete.png")
        next_tpl_path = get_template_path("next_button.png")
        next_wide_path = get_template_path("next_button_wide.png")

        tc_tpl = cv2.imread(complete_tpl_path) if os.path.exists(complete_tpl_path) else None
        nb_tpl = cv2.imread(next_tpl_path) if os.path.exists(next_tpl_path) else None
        nb_wide = cv2.imread(next_wide_path) if os.path.exists(next_wide_path) else None

        # ========== 轨 1: 弹窗模式匹配 ==========
        if tc_tpl is not None:
            popup_match = find_best_template_match(screen_bgr, tc_tpl, min_confidence=min_popup_conf)
            if popup_match:
                pcx, pcy, pw, ph, pconf, pscale = popup_match
                popup_roi = (pcx - pw // 2, pcy - ph // 2, pw, ph)

                # 优先在弹窗 ROI 内寻找标准按钮
                if nb_tpl is not None:
                    btn_in_popup = find_best_template_match(screen_bgr, nb_tpl, min_confidence=0.72, roi=popup_roi)
                    if btn_in_popup:
                        bx, by, _, _, bconf, bscale = btn_in_popup
                        return (bx, by, bconf, f"完成弹窗内「下一个」按钮 (弹窗相似度:{pconf:.2f}, 按钮:{bconf:.2f})")

                # 次选在弹窗 ROI 内寻找宽按钮
                if nb_wide is not None:
                    btn_wide_in_popup = find_best_template_match(screen_bgr, nb_wide, min_confidence=0.72, roi=popup_roi)
                    if btn_wide_in_popup:
                        bx, by, _, _, bconf, bscale = btn_wide_in_popup
                        return (bx, by, bconf, f"完成弹窗内宽按钮 (弹窗相似度:{pconf:.2f}, 按钮:{bconf:.2f})")

                # 弹窗内几何相对偏移计算 (右下方 84% 宽, 82% 高)
                top_left_x = pcx - pw // 2
                top_left_y = pcy - ph // 2
                fallback_x = top_left_x + int(pw * 0.84)
                fallback_y = top_left_y + int(ph * 0.82)
                return (fallback_x, fallback_y, pconf, f"完成弹窗几何偏移定位 (弹窗相似度:{pconf:.2f}, 缩放:{pscale:.2f}x)")

        # ========== 轨 2: 独立「下一个」按钮全屏多尺度匹配 ==========
        # (即使没有弹窗，视频播放完毕时页面上亮起的「下一个」按钮也能被准确捕获)
        if nb_tpl is not None:
            nb_match = find_best_template_match(screen_bgr, nb_tpl, min_confidence=min_btn_conf)
            if nb_match:
                nx, ny, _, _, nconf, nscale = nb_match
                return (nx, ny, nconf, f"独立「下一个」按钮 (相似度:{nconf:.2f}, 缩放:{nscale:.2f}x)")

        if nb_wide is not None:
            nb_wide_match = find_best_template_match(screen_bgr, nb_wide, min_confidence=min_btn_conf)
            if nb_wide_match:
                nx, ny, _, _, nconf, nscale = nb_wide_match
                return (nx, ny, nconf, f"独立宽「下一个」按钮 (相似度:{nconf:.2f}, 缩放:{nscale:.2f}x)")

        return None

    def test_recognition(self):
        """单次测试识别当前屏幕（用于用户调优与诊断）"""
        self.log.info("=" * 45)
        self.log.info("🔍 开始全场景屏幕识别诊断...")
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

        try:
            b_conf = float(self.btn_conf_var.get().strip())
        except ValueError:
            b_conf = DEFAULT_NEXT_CONFIDENCE

        # 1. 弹窗模板多尺度诊断
        complete_tpl_path = get_template_path("task_complete.png")
        if os.path.exists(complete_tpl_path):
            tc_tpl = cv2.imread(complete_tpl_path)
            raw_popup = find_best_template_match(screen, tc_tpl, min_confidence=0.50)
            if raw_popup:
                r_pcx, r_pcy, r_pw, r_ph, r_pconf, r_pscale = raw_popup
                status_str = "✅ 匹配成功" if r_pconf >= p_conf else f"⚠️ 低于阈值 {p_conf}"
                self.log.info(f"📋 弹窗模板扫描: 坐标 ({r_pcx}, {r_pcy}) | 最高相似度: {r_pconf:.3f} (缩放 {r_pscale:.2f}x) -> {status_str}")
            else:
                self.log.info("📋 弹窗模板扫描: 未发现相似区域")
        else:
            self.log.warning("⚠️ 未找到 task_complete.png 弹窗模板")

        # 2. 「下一个」按钮多尺度诊断
        next_tpl_path = get_template_path("next_button.png")
        if os.path.exists(next_tpl_path):
            nb_tpl = cv2.imread(next_tpl_path)
            raw_btn = find_best_template_match(screen, nb_tpl, min_confidence=0.50)
            if raw_btn:
                r_bx, r_by, _, _, r_bconf, r_bscale = raw_btn
                status_str = "✅ 匹配成功" if r_bconf >= b_conf else f"⚠️ 低于阈值 {b_conf}"
                self.log.info(f"🔘 「下一个」按钮扫描: 坐标 ({r_bx}, {r_by}) | 最高相似度: {r_bconf:.3f} (缩放 {r_bscale:.2f}x) -> {status_str}")
            else:
                self.log.info("🔘 「下一个」按钮扫描: 未发现相似按钮")

        # 3. 综合切课策略诊断
        target = self.find_popup_and_next_button(screen, min_popup_conf=p_conf, min_btn_conf=b_conf)
        if target:
            tx, ty, score, desc = target
            self.log.info(f"🎯 【切课目标已锁定】: 点击坐标 ({tx}, {ty}) | 置信度: {score:.3f} | 策略: {desc}")
        else:
            self.log.info("ℹ️ 当前屏幕无需切课（未出现完成弹窗且未亮起独立「下一个」按钮）")

        # 4. 播放按钮诊断
        play_res = self.find_play_button(screen)
        if play_res:
            px, py, conf, method = play_res
            self.log.info(f"▶ 成功定位播放按钮: 坐标 ({px}, {py}) | 置信度: {conf:.2f} | 策略: {method}")
        else:
            self.log.info(f"ℹ️ 未检测到播放按钮 [模式: {self.play_mode_var.get()}]（画面可能正在播放中，或无播放图标）")

        # 5. 音频录制设备诊断
        if HAS_SOUNDCARD:
            try:
                spk = sc.default_speaker()
                self.log.info(f"🎙️ 系统音频内录组件就绪 (默认输出设备: {spk.name}) | 当前设置: {'已勾选开启' if self.auto_record_var.get() else '默认未开启'}")
            except Exception as e:
                self.log.warning(f"⚠️ 音频设备检测异常: {e}")

        self.log.info("🔍 诊断完成")
        self.log.info("=" * 45)

    def capture_template_interactive(self, filename, title_desc):
        """通用交互式模板截取工具"""
        self.log.info("=" * 45)
        self.log.info(f"📷 准备截取 {title_desc} 模板 ({filename})...")
        self.log.info(f"👉 请在 5 秒内将鼠标移到 {title_desc} 的【左上角】...")

        def do_capture():
            time.sleep(5)
            x1, y1 = pyautogui.position()
            self.log.info(f"📍 左上角坐标记录: ({x1}, {y1})")
            self.log.info(f"👉 请在 5 秒内将鼠标移到 {title_desc} 的【右下角】...")
            time.sleep(5)
            x2, y2 = pyautogui.position()
            self.log.info(f"📍 右下角坐标记录: ({x2}, {y2})")

            left, top = min(x1, x2), min(y1, y2)
            w, h = abs(x2 - x1), abs(y2 - y1)

            if w < 6 or h < 6:
                self.log.error("截取区域过小，请重新截取！")
                return

            pad = 2
            left = max(0, left - pad)
            top = max(0, top - pad)
            w += pad * 2
            h += pad * 2

            save_dir = get_template_save_dir()
            img = ImageGrab.grab(bbox=(left, top, left + w, top + h))
            save_path = save_dir / filename
            img.save(str(save_path))
            self.log.info(f"✅ {title_desc} 模板已成功保存: {save_path} (尺寸 {w}x{h})")
            self.root.after(0, self._update_play_tpl_status)

        threading.Thread(target=do_capture, daemon=True).start()

    def start_audio_recording(self, episode_num=None):
        """启动课程音频录制"""
        if not self.auto_record_var.get() or not HAS_SOUNDCARD:
            return

        if not self.audio_recorder.recording:
            ep = episode_num if episode_num is not None else (self.popup_click_count + 1)
            filepath = self.audio_recorder.start_recording(ep)
            if filepath:
                self.log.info(f"🎙️ [音频录制] 已开始录制第 {ep} 节课程音频 -> {filepath.name}")
                self.root.after(0, self._update_record_status, f"🔴 录音中(第{ep}节)", "#d81b60")

    def stop_audio_recording(self):
        """停止并保存当前课程音频录制"""
        if self.audio_recorder.recording:
            res = self.audio_recorder.stop_recording()
            if res:
                path, dur = res
                mins = int(dur // 60)
                secs = int(dur % 60)
                dur_str = f"{mins}分{secs}秒" if mins > 0 else f"{secs}秒"
                self.log.info(f"🎙️ [音频录制] 本节课程录音已保存: {path.name} (时长 {dur_str})")
            self.root.after(0, self._update_record_status, "⏸ 未开启" if not self.auto_record_var.get() else "⏸ 未录制", "#888")

    def click_play_button(self, x, y, method_desc):
        """安全点击播放按钮并进行防多点、状态确认及触发录音"""
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

            # 视频开始播放时触发音频录音
            self.start_audio_recording(self.popup_click_count + 1)
        else:
            self.play_click_attempts += 1
            self.last_play_coord = (x, y)
            if self.play_click_attempts >= 3:
                self.log.warning(f"⚠️ 同一位置已连续点击 {self.play_click_attempts} 次未消除，进入防刷保护冷却")

    def click_next(self, x, y, desc=""):
        """点击「下一个」按钮并停止当前小节录音"""
        self.stop_audio_recording()

        pyautogui.click(x, y)
        self.popup_click_count += 1
        self.last_popup_click_time = time.time()
        self.root.after(0, self._update_counts)
        self.log.info(f"✅ 点击「下一个」 ({x}, {y}) {f'[{desc}]' if desc else ''} (切课第 {self.popup_click_count} 次)")

    def monitor_loop(self, check_interval, play_cooldown, popup_confidence, btn_confidence):
        self.log.info("=" * 45)
        self.log.info("🚀 刷课监控已启动")
        self.log.info(f"检测间隔: {check_interval}s | 弹窗阈值: {popup_confidence} | 按钮阈值: {btn_confidence} | 播放冷却: {play_cooldown}s")
        rec_status_str = "已开启 (小节自动切分)" if self.auto_record_var.get() else "未开启 (默认关闭，可手动勾选开启)"
        self.log.info(f"自动播放: {'已开启' if self.auto_play_var.get() else '已禁用'} | 播放模式: {self.play_mode_var.get()} | 音频录制: {rec_status_str}")
        self.log.info("=" * 45)

        # 启动时如果视频已经在播放（未显示播放键），且开启了录音，立即启动第 1 节录音
        if self.auto_record_var.get() and not self.audio_recorder.recording:
            init_screen = self.grab_screen()
            init_play = self.find_play_button(init_screen)
            if not init_play:
                self.start_audio_recording(1)

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

                # ========== 2. 检测切课目标（全场景双轨多尺度匹配） ==========
                screen = self.grab_screen()
                target = self.find_popup_and_next_button(screen, min_popup_conf=popup_confidence,
                                                         min_btn_conf=btn_confidence)

                if target:
                    tx, ty, score, desc = target

                    if now - self.last_popup_click_time < MIN_POPUP_CLICK_INTERVAL:
                        pass
                    else:
                        self.log.info(f"🎯 捕获切课信号 ({desc})，准备点击 ({tx}, {ty})")

                        # 0.3 秒二次确认，保证稳定常驻
                        time.sleep(0.3)
                        verify_screen = self.grab_screen()
                        re_target = self.find_popup_and_next_button(verify_screen, min_popup_conf=popup_confidence,
                                                                    min_btn_conf=btn_confidence)

                        if re_target:
                            rtx, rty, rscore, rdesc = re_target
                            self.click_next(rtx, rty, rdesc)

                            # 点击切换后，等待页面加载并主动触发新一节视频播放与录音
                            time.sleep(3.5)
                            if self.auto_play_var.get():
                                screen_after_next = self.grab_screen()
                                next_play_pos = self.find_play_button(screen_after_next)
                                if next_play_pos:
                                    npx, npy, nconf, nmethod = next_play_pos
                                    self.click_play_button(npx, npy, f"切课后-{nmethod}")
                                else:
                                    # 如果新页面已自动开始播放，直接开启新一节录音
                                    self.start_audio_recording(self.popup_click_count + 1)
                        else:
                            self.log.info("ℹ️ 切课信号二次复检未通过（已自动过滤瞬态闪烁）")

                # 间隔休眠等待（支持快速响应停止事件）
                sleep_steps = max(1, int(check_interval * 2))
                for _ in range(sleep_steps):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.5)

            except Exception as e:
                self.log.error(f"监控循环异常: {e}")
                time.sleep(2)

        # 退出循环时，确保正在进行的录音安全保存
        self.stop_audio_recording()
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
            popup_conf = max(0.50, min(0.98, popup_conf))
        except ValueError:
            popup_conf = DEFAULT_POPUP_CONFIDENCE
        self.popup_conf_var.set(f"{popup_conf:.2f}")

        # 解析按钮置信度
        try:
            btn_conf = float(self.btn_conf_var.get().strip())
            btn_conf = max(0.50, min(0.98, btn_conf))
        except ValueError:
            btn_conf = DEFAULT_NEXT_CONFIDENCE
        self.btn_conf_var.set(f"{btn_conf:.2f}")

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
        self.cap_next_btn.config(state=tk.DISABLED, bg="#999")
        self.cap_popup_btn.config(state=tk.DISABLED, bg="#999")
        self.capture_btn.config(state=tk.DISABLED, bg="#999")
        self.interval_entry.config(state=tk.DISABLED, bg="#eee")
        self.popup_conf_entry.config(state=tk.DISABLED, bg="#eee")
        self.btn_conf_entry.config(state=tk.DISABLED, bg="#eee")
        self.play_cooldown_entry.config(state=tk.DISABLED, bg="#eee")
        self.play_mode_cb.config(state=tk.DISABLED)
        self.auto_play_cb.config(state=tk.DISABLED)
        self.auto_record_cb.config(state=tk.DISABLED)
        self._update_status("▶ 运行中...", "#2e7d32")

        self.monitor_thread = threading.Thread(
            target=self.monitor_loop,
            args=(interval, cooldown, popup_conf, btn_conf),
            daemon=True
        )
        self.monitor_thread.start()

    def stop_monitor(self):
        self.running = False
        self.stop_event.set()

        # 停止并保存正在进行的录音
        self.stop_audio_recording()

        self.start_btn.config(state=tk.NORMAL, bg="#2e7d32")
        self.stop_btn.config(state=tk.DISABLED, bg="#999")
        self.test_btn.config(state=tk.NORMAL, bg="#0288d1")
        self.cap_next_btn.config(state=tk.NORMAL, bg="#5c6bc0")
        self.cap_popup_btn.config(state=tk.NORMAL, bg="#7e57c2")
        self.capture_btn.config(state=tk.NORMAL, bg="#455a64")
        self.interval_entry.config(state=tk.NORMAL, bg="white")
        self.popup_conf_entry.config(state=tk.NORMAL, bg="white")
        self.btn_conf_entry.config(state=tk.NORMAL, bg="white")
        self.play_cooldown_entry.config(state=tk.NORMAL, bg="white")
        self.play_mode_cb.config(state="readonly")
        self.auto_play_cb.config(state=tk.NORMAL)
        self.auto_record_cb.config(state=tk.NORMAL)
        self._update_status("⏸ 已停止", "#888")
        self._update_record_status("⏸ 未开启" if not self.auto_record_var.get() else "⏸ 未录制", "#888")


if __name__ == "__main__":
    root = tk.Tk()
    app = CourseAutoApp(root)
    root.mainloop()