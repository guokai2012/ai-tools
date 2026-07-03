import logging
import os
import queue
import shutil
import subprocess
import threading
import uuid
import json

from flask import Flask, request, jsonify, send_from_directory

# 尝试导入广告移除模块
try:
    from ad_remover import process_video_for_ad_removal

    AD_REMOVER_AVAILABLE = True
except ImportError:
    AD_REMOVER_AVAILABLE = False
    print("警告: 无法导入ad_remover模块，广告移除功能将不可用")

# ===== 初始化 Flask App =====
app = Flask(__name__)

# 启用日志输出（即使 debug=False）
if not app.debug:
    app.logger.setLevel(logging.INFO)
    # 如果没有 handler，则添加一个（防止重复）
    if not app.logger.handlers:
        import sys
        from logging import StreamHandler, Formatter

        handler = StreamHandler(sys.stdout)
        handler.setFormatter(Formatter(
            '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
        ))
        app.logger.addHandler(handler)

INPUT_DIR = '/videos/input'
OUTPUT_DIR = '/videos/output'
SAMPLE_DIR = '/videos/sample'

os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(SAMPLE_DIR, exist_ok=True)

SUPPORTED_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm', '.m4v'}


def is_video_file(filename):
    return any(filename.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS)


# ===== 全局任务队列 =====
task_queue = queue.Queue()
worker_running = True

# 进程内视频时长缓存：{(path, mtime, size): duration}
_duration_cache = {}


def get_video_duration(filepath):
    # 按 path+mtime+size 作 key 缓存，避免同一文件重复起 ffprobe
    try:
        st = os.stat(filepath)
        cache_key = (filepath, int(st.st_mtime), st.st_size)
    except OSError:
        cache_key = (filepath, 0, 0)

    cached = _duration_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        result = subprocess.run([
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'csv=p=0',
            filepath
        ], capture_output=True, text=True, timeout=30)

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            app.logger.error(f"FFprobe 非零退出码 ({result.returncode}) - 文件: {filepath}, stderr: {stderr}")
            return None

        if not stdout:
            app.logger.warning(f"FFprobe 未返回时长 - 文件: {filepath}")
            return None

        duration = float(stdout)
        if duration <= 0:
            app.logger.warning(f"无效视频时长 ({duration}) - 文件: {filepath}")
            return None

        _duration_cache[cache_key] = duration
        return duration

    except ValueError:
        app.logger.error(f"FFprobe 返回非法数值: '{stdout}' - 文件: {filepath}")
        return None
    except subprocess.TimeoutExpired:
        app.logger.error(f"FFprobe 超时 (30s) - 文件: {filepath}")
        return None
    except Exception as e:
        app.logger.exception(f"FFprobe 意外异常 - 文件: {filepath}")
        return None


def run_ffmpeg(cmd, cwd=None):
    """
    统一的 ffmpeg 执行包装：正常时静默，失败时把 stderr 记入日志并抛出异常。
    返回 CompletedProcess。
    """
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    if result.returncode != 0:
        stderr_text = result.stderr.decode('utf-8', errors='ignore').strip()
        app.logger.error(f"FFmpeg 失败 (返回码 {result.returncode}): {' '.join(cmd)}\n  stderr: {stderr_text}")
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return result


def trim_worker():
    """工作线程：处理裁剪任务"""
    while worker_running:
        try:
            task = task_queue.get(timeout=1)
            if task is None:
                break

            filename, mode, trim_param = task
            input_path = os.path.join(INPUT_DIR, filename)
            output_path = os.path.join(OUTPUT_DIR, filename)

            if not os.path.exists(input_path):
                app.logger.error(f"输入文件不存在: {input_path}")
                task_queue.task_done()
                continue

            success = False
            try:
                if mode == 'start':
                    app.logger.info(f"正在对 {filename} 裁剪开头 {trim_param} 秒")
                    # -ss 前置于 -i：流复制直接跳关键帧，无需先解码到目标位置
                    cmd = ['ffmpeg', '-ss', str(trim_param), '-i', input_path,
                           '-c', 'copy', '-avoid_negative_ts', 'make_zero', '-y', output_path]
                    run_ffmpeg(cmd)
                    success = True

                elif mode == 'end':
                    app.logger.info(f"正在对 {filename} 裁剪结尾 {trim_param} 秒")
                    total_duration = get_video_duration(input_path)
                    if total_duration is None or total_duration <= trim_param:
                        app.logger.warning(f"无法裁剪结尾: {filename} (总时长={total_duration}, 裁剪={trim_param})")
                    else:
                        keep_duration = max(0.0, total_duration - trim_param)
                        cmd = ['ffmpeg', '-i', input_path, '-t', str(keep_duration),
                               '-c', 'copy', '-avoid_negative_ts', 'make_zero', '-y', output_path]
                        run_ffmpeg(cmd)
                        success = True

                elif mode == 'extract':
                    app.logger.info(f"正在对 {filename} 提取 {trim_param} 秒")
                    # 提取模式：从start秒开始保留duration秒
                    start, duration = trim_param
                    cmd = ['ffmpeg', '-ss', str(start), '-i', input_path, '-t', str(duration),
                           '-c', 'copy', '-avoid_negative_ts', 'make_zero', '-y', output_path]
                    run_ffmpeg(cmd)
                    success = True

                elif mode == 'ad_remove':
                    app.logger.info(f"正在对 {filename} 移除广告")
                    # 广告移除模式
                    if not AD_REMOVER_AVAILABLE:
                        app.logger.error("广告移除功能不可用，缺少必要的模块")
                        success = False
                    else:
                        search_window_start = trim_param['search_window_start']
                        search_window_end = trim_param['search_window_end']
                        ad_duration = trim_param['ad_duration']
                        hash_threshold = trim_param.get('hash_threshold', 0.8)  # 获取图像哈希对比阈值参数
                        consecutive_frames = trim_param.get('consecutive_frames', 3)  # 获取连续对比帧数参数
                        frame_step = trim_param.get('frame_step', 0.5)  # 获取帧提取步长参数
                        time_adjust = trim_param.get('time_adjust', 0.0)  # 获取时间调整参数

                        sample_path = os.path.join(SAMPLE_DIR, 'ad_sample.png')
                        success = process_video_for_ad_removal(
                            video_path=input_path,
                            ad_sample_path=sample_path,
                            search_window=(search_window_start, search_window_end),
                            ad_duration=ad_duration,
                            hash_threshold=hash_threshold,  # 传递图像哈希对比阈值参数
                            consecutive_frames=consecutive_frames,  # 传递连续对比帧数参数
                            frame_step=frame_step,  # 传递帧提取步长参数
                            time_adjust=time_adjust,  # 传递时间调整参数
                            output_dir=OUTPUT_DIR  # 传递输出目录参数
                        )

                elif mode == 'sample_split':
                    app.logger.info(f"正在对 {filename} 样本图片分割")
                    # 样本图片分割模式
                    if not AD_REMOVER_AVAILABLE:
                        app.logger.error("样本图片分割功能不可用，缺少必要的模块")
                        success = False
                    else:
                        hash_threshold = trim_param.get('hash_threshold', 0.8)
                        consecutive_frames = trim_param.get('consecutive_frames', 3)
                        frame_step = trim_param.get('frame_step', 0.5)
                        time_adjust = trim_param.get('time_adjust', 0.0)

                        sample_path = os.path.join(SAMPLE_DIR, 'split_sample.png')
                        if not os.path.exists(sample_path):
                            app.logger.error(f"样本图片不存在: {sample_path}")
                            success = False
                        else:
                            # 获取视频总时长，作为搜索窗口
                            total_duration = get_video_duration(input_path)
                            if total_duration is None:
                                app.logger.error(f"无法获取视频时长: {filename}")
                                success = False
                            else:
                                # 查找所有匹配的时间点
                                from ad_remover import find_all_sample_matches, split_video_by_time_points
                                skip_interval = trim_param.get('skip_interval', 30.0)  # 添加跳过间隔参数
                                search_duration = trim_param.get('search_duration', 10.0)  # 添加搜索持续秒参数
                                split_time_points = find_all_sample_matches(
                                    video_path=input_path,
                                    sample_path=sample_path,
                                    search_window=(0, total_duration),  # 使用整个视频时长作为搜索窗口
                                    hash_threshold=hash_threshold,
                                    consecutive_frames=consecutive_frames,
                                    frame_step=frame_step,
                                    skip_interval=skip_interval,
                                    search_duration=search_duration
                                )
                                
                                if not split_time_points:
                                    app.logger.error(f"未找到匹配的样本图片时间点: {filename}")
                                    success = False
                                else:
                                    # 分割视频
                                    success = split_video_by_time_points(
                                        video_path=input_path,
                                        split_time_points=split_time_points,
                                        output_dir=OUTPUT_DIR,
                                        time_adjust=time_adjust
                                    )

                elif mode == 'middle':
                    # 处理多段剪切，使用无损方式（分段处理再合并）
                    total_duration = get_video_duration(input_path)
                    if total_duration is None:
                        app.logger.warning(f"无法获取视频时长: {filename}")
                    else:
                        # 计算保留时段
                        keep_segments = calculate_keep_segments(total_duration, trim_param)
                        if not keep_segments:
                            app.logger.warning(f"剪切段设置无效: {filename}")
                        else:
                            # 使用无损方式处理多段剪切
                            success = process_middle_trim_lossless(input_path, output_path, keep_segments)

                elif mode == 'watermark_remove':
                    app.logger.info(f"正在对 {filename} 去除水印")
                    # 水印去除模式 - 优化版：分段处理
                    temp_dir = None
                    try:
                        # 1. 获取视频信息
                        info = get_video_info(input_path)
                        app.logger.debug(f"视频信息: {info}")
                        # 2. 准备水印时间段，确保有序
                        watermark_segments = []
                        for watermark in trim_param:
                            watermark_segments.append((watermark['start'], watermark.get('end', None), watermark['x1'], watermark['y1'], watermark['w'], watermark['h']))

                        # 按开始时间排序
                        watermark_segments.sort()

                        # 3. 计算需要处理的所有时间段（去水印段和保留段）
                        all_segments = []
                        current_pos = 0.0

                        for start, end, x1, y1, w, h in watermark_segments:
                            # 添加去水印段之前的保留段（直接copy）
                            if current_pos < start:
                                all_segments.append({
                                    'type': 'copy',
                                    'start': current_pos,
                                    'end': start
                                })

                            # 添加去水印段（使用delogo）
                            all_segments.append({
                                'type': 'delogo',
                                'start': start,
                                'end': end,
                                'x1': x1,
                                'y1': y1,
                                'w': w,
                                'h': h
                            })
                            current_pos = end

                        # 添加最后一个去水印段之后的保留段
                        if current_pos is not None:
                            all_segments.append({
                                'type': 'copy',
                                'start': current_pos,
                                'end': None
                            })
                        app.logger.info(f"需要处理的所有时间段: {all_segments}")
                        # 4. 创建临时目录
                        temp_dir = os.path.join(os.path.dirname(output_path), f"temp_{uuid.uuid4().hex}")
                        os.makedirs(temp_dir, exist_ok=True)
                        temp_files = []

                        # 5. 处理每个片段（-ss 前置于 -i）
                        for i, segment in enumerate(all_segments):
                            temp_file = os.path.join(temp_dir, f"segment_{i:03d}.mp4")
                            cmd = ['ffmpeg', '-ss', str(segment['start']), '-i', input_path]

                            # 有end，则限制输出时长
                            if segment['end'] is not None:
                                cmd.extend(['-t', str(segment['end'] - segment['start'])])

                            if segment['type'] == 'copy':
                                # 直接流copy，不重新编码
                                cmd.extend(['-c', 'copy', '-avoid_negative_ts', 'make_zero', '-y', temp_file])
                            else:
                                # 使用delogo滤镜处理（移除硬编码 -tune animation 以兼容实拍内容）
                                x1 = segment['x1']
                                y1 = segment['y1']
                                w = segment['w']
                                h = segment['h']

                                cmd.extend([
                                    '-filter:v', f"delogo=x={x1}:y={y1}:w={w}:h={h}",
                                    '-c:v', info['encoder'],
                                    '-preset', 'fast',
                                    '-crf', str(info['crf']),
                                    '-g', str(int(info['fps'] * 2)),
                                    '-keyint_min', str(int(info['fps'])),
                                    '-sc_threshold', '0',
                                    '-profile:v', info['profile'],
                                    '-level:v', str(info['level']),
                                    '-pix_fmt', info['pix_fmt'],
                                    '-video_track_timescale', str(info['timescale']),
                                    '-copyts',
                                    '-avoid_negative_ts', 'make_zero',
                                    '-c:a', 'copy',
                                    '-y', temp_file
                                ])

                            app.logger.info(f"处理片段 {i+1}/{len(all_segments)}: {' '.join(cmd)}")
                            run_ffmpeg(cmd)
                            temp_files.append(temp_file)

                        # 6. 合并所有片段
                        if len(temp_files) == 1:
                            # 只有一个片段，直接重命名
                            os.replace(temp_files[0], output_path)
                        else:
                            # 使用concat协议合并多个片段（先尝试流复制）
                            concat_file = os.path.join(temp_dir, "concat_list.txt")
                            with open(concat_file, 'w') as f:
                                for temp_file in temp_files:
                                    f.write(f"file '{os.path.basename(temp_file)}'\n")

                            merge_cmd = [
                                'ffmpeg', '-f', 'concat',
                                '-safe', '0',
                                '-i', concat_file,
                                '-c', 'copy',
                                '-y', output_path
                            ]

                            app.logger.info(f"合并片段: {' '.join(merge_cmd)}")
                            merge_result = subprocess.run(merge_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=temp_dir)
                            if merge_result.returncode != 0:
                                # 流复制 concat 失败（重编码段与原始流编码参数不一致），降级为重编码 concat
                                app.logger.warning(
                                    f"流复制合并失败 (返回码 {merge_result.returncode})，尝试降级为重编码合并: "
                                    f"{merge_result.stderr.decode('utf-8', errors='ignore')}")
                                merge_cmd = [
                                    'ffmpeg', '-f', 'concat',
                                    '-safe', '0',
                                    '-i', concat_file,
                                    '-c:v', 'libx264',
                                    '-crf', '23',
                                    '-preset', 'fast',
                                    '-c:a', 'aac',
                                    '-y', output_path
                                ]
                                app.logger.info(f"重编码合并片段: {' '.join(merge_cmd)}")
                                run_ffmpeg(merge_cmd, cwd=temp_dir)

                        success = True

                    except subprocess.CalledProcessError as e:
                        app.logger.error(f"FFmpeg 失败 - 文件: {filename}, 返回码: {e.returncode}")
                        success = False
                    except Exception as e:
                        app.logger.exception(f"处理 {filename} 时发生异常")
                        success = False
                    finally:
                        # 统一清理临时目录
                        if temp_dir is not None:
                            shutil.rmtree(temp_dir, ignore_errors=True)

            except subprocess.CalledProcessError as e:
                app.logger.error(f"FFmpeg 失败 - 文件: {filename}, 返回码: {e.returncode}")
            except Exception as e:
                app.logger.exception(f"处理 {filename} 时发生异常")

            if success:
                app.logger.info(f"✅ 成功生成: {output_path}")
            else:
                # 清理可能的残缺文件
                # 注意：ad_remove 输出 {base}_clean{ext}、sample_split 输出 {base}_part*{ext}，
                # 它们不使用原文件名 output_path，需一并清理其衍生残留。
                stale_files = [output_path]
                base, ext = os.path.splitext(output_path)
                if mode == 'ad_remove':
                    stale_files.append(f"{base}_clean{ext}")
                elif mode == 'sample_split':
                    # 清理可能的分片残留（part1, part2, ...）
                    if os.path.isdir(OUTPUT_DIR):
                        for fn in os.listdir(OUTPUT_DIR):
                            if fn.startswith(os.path.basename(base) + '_part') and fn.endswith(ext):
                                stale_files.append(os.path.join(OUTPUT_DIR, fn))

                for stale in stale_files:
                    if os.path.exists(stale):
                        try:
                            os.remove(stale)
                            app.logger.info(f"已清理无效输出文件: {stale}")
                        except Exception as e:
                            app.logger.error(f"无法删除无效文件 {stale}: {e}")

            task_queue.task_done()

        except queue.Empty:
            continue
        except Exception as e:
            app.logger.exception("工作线程崩溃")

def get_video_info(path):
    """获取视频关键信息"""
    cmd = [
        'ffprobe', '-v', 'quiet',
        '-select_streams', 'v:0',
        '-show_entries',
        'stream=codec_name,profile,level,pix_fmt,r_frame_rate,time_base',
        '-of', 'json', path
    ]
    app.logger.debug(f"执行ffprobe命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr}")

    info = json.loads(result.stdout)['streams'][0]
    app.logger.debug(f"ffprobe 输出: {info}")
    # 解析帧率（可能为 "30/1" 或 "2997/100"）
    fps_str = info['r_frame_rate']
    if '/' in fps_str:
        num, den = map(int, fps_str.split('/'))
        fps = num / den
    else:
        fps = float(fps_str)

    # CRF 默认值：H.264 用 23，H.265 用 28（原值 34 画质损失过大）
    if info['codec_name'] == 'h264':
        encoder = 'libx264'
        crf = 23
    elif info['codec_name'] == 'hevc':
        encoder = 'libx265'
        crf = 28
    else:
        encoder = 'libx264'  # fallback
        crf = 23

    # level 规范化：ffprobe 可能输出整数（如 40/31/50）或带小数（如 4.0），
    # 统一转换为 "x.y" 字符串，避免传给编码器时无效。
    raw_level = info.get('level', 40)
    try:
        lvl = float(raw_level)
    except (TypeError, ValueError):
        lvl = 40.0
    if lvl > 10:  # 整数形式（如 40 表示 4.0）
        lvl = lvl / 10.0
    level_str = f"{lvl:.1f}"

    return {
        'encoder': encoder,
        'fps': fps,
        'crf': crf,
        'profile': info.get('profile', 'main').lower(),
        'level': level_str,
        'pix_fmt': info.get('pix_fmt', 'yuv420p'),
        'timescale': int(info.get('time_base', '1/1000').split('/')[1]),
    }

def calculate_keep_segments(total_duration, cut_segments):
    """
    根据总时长和需要剪切的段，计算需要保留的段
    :param total_duration: 视频总时长
    :param cut_segments: 需要剪切的段列表 [(start, duration), ...]
    :return: 保留的段列表 [(start, duration), ...]
    """
    # 按起始时间排序
    sorted_cuts = sorted(cut_segments, key=lambda x: x[0])

    # 合并重叠的剪切段
    merged_cuts = []
    for start, duration in sorted_cuts:
        end = start + duration
        # 忽略超出视频范围的部分
        if start >= total_duration:
            continue
        if end > total_duration:
            end = total_duration

        if not merged_cuts:
            merged_cuts.append((start, end - start))
        else:
            last_start, last_duration = merged_cuts[-1]
            last_end = last_start + last_duration

            # 如果当前段与上一段重叠或相邻，则合并
            if start <= last_end:
                new_end = max(last_end, end)
                merged_cuts[-1] = (last_start, new_end - last_start)
            else:
                merged_cuts.append((start, end - start))

    # 从剪切段计算保留段
    keep_segments = []
    current_pos = 0

    for cut_start, cut_duration in merged_cuts:
        cut_end = cut_start + cut_duration

        # 添加剪切段之前的保留段
        if current_pos < cut_start:
            keep_segments.append((current_pos, cut_start - current_pos))

        current_pos = cut_end

    # 添加最后一段保留段（如果有的话）
    if current_pos < total_duration:
        keep_segments.append((current_pos, total_duration - current_pos))

    return keep_segments


def process_middle_trim_lossless(input_path, output_path, keep_segments):
    """
    使用无损方式处理多段剪切
    :param input_path: 输入文件路径
    :param output_path: 输出文件路径
    :param keep_segments: 保留的段列表 [(start, duration), ...]
    :return: 是否成功
    """
    # 获取输出文件的扩展名
    _, output_ext = os.path.splitext(output_path)

    # 创建唯一的临时目录，避免文件名冲突
    temp_dir_path = os.path.join(os.path.dirname(output_path), f"temp_{uuid.uuid4().hex}")
    os.makedirs(temp_dir_path, exist_ok=True)
    temp_files = []

    try:
        # 第一步：提取各个保留段（使用流复制，-ss 前置以直接跳关键帧）
        for i, (start, duration) in enumerate(keep_segments):
            temp_file = os.path.join(temp_dir_path, f"part_{i}{output_ext}")
            temp_files.append(temp_file)

            cmd = [
                'ffmpeg',
                '-ss', str(start),
                '-i', input_path,
                '-t', str(duration),
                '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                '-y', temp_file
            ]

            app.logger.info(f"提取段 {i}: {' '.join(cmd)}")
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode != 0:
                app.logger.error(
                    f"FFmpeg 提取段失败，返回码: {result.returncode}, stderr: {result.stderr.decode('utf-8', errors='ignore')}")
                raise subprocess.CalledProcessError(result.returncode, cmd)

        # 第二步：创建concat文件
        concat_file = os.path.join(temp_dir_path, "concat_list.txt")
        with open(concat_file, 'w') as f:
            for temp_file in temp_files:
                f.write(f"file '{os.path.basename(temp_file)}'\n")

        # 第三步：合并所有段（先尝试流复制 concat）
        cmd = [
            'ffmpeg',
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_file,
            '-c', 'copy',
            '-y', output_path
        ]

        app.logger.info(f"合并段: {' '.join(cmd)}")
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=temp_dir_path)
        if result.returncode != 0:
            # 流复制 concat 失败（通常是各段编码参数不一致），降级为重编码 concat
            app.logger.warning(
                f"流复制合并失败 (返回码 {result.returncode})，尝试降级为重编码合并: "
                f"{result.stderr.decode('utf-8', errors='ignore')}")
            cmd = [
                'ffmpeg',
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file,
                '-c:v', 'libx264',
                '-crf', '23',
                '-preset', 'fast',
                '-c:a', 'aac',
                '-y', output_path
            ]
            app.logger.info(f"重编码合并段: {' '.join(cmd)}")
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=temp_dir_path)
            if result.returncode != 0:
                app.logger.error(
                    f"FFmpeg 重编码合并段失败，返回码: {result.returncode}, stderr: {result.stderr.decode('utf-8', errors='ignore')}")
                raise subprocess.CalledProcessError(result.returncode, cmd)

        # 清理临时目录
        shutil.rmtree(temp_dir_path, ignore_errors=True)

        return True

    except subprocess.CalledProcessError as e:
        app.logger.error(f"FFmpeg 失败，返回码: {e.returncode}")
        shutil.rmtree(temp_dir_path, ignore_errors=True)
        return False
    except Exception as e:
        app.logger.exception("处理过程中发生异常")
        shutil.rmtree(temp_dir_path, ignore_errors=True)
        return False


# ===== 启动工作线程 =====
def start_worker():
    t = threading.Thread(target=trim_worker, daemon=True)
    t.start()
    app.logger.info("✂️ 裁剪工作线程已启动")


# ===== API 路由 =====
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/videos')
def list_input_videos():
    try:
        files = [f for f in os.listdir(INPUT_DIR) if is_video_file(f)]
        return jsonify(sorted(files))
    except Exception as e:
        app.logger.error(f"列出输入视频失败: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/samples')
def list_sample_videos():
    """列出样本视频文件"""
    try:
        return jsonify([])
    except Exception as e:
        app.logger.error(f"列出样本文件失败: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/outputs')
def list_output_videos():
    try:
        files = [f for f in os.listdir(OUTPUT_DIR) if is_video_file(f)]
        return jsonify(sorted(files))
    except Exception as e:
        app.logger.error(f"列出输出视频失败: {e}")
        return jsonify({'error': str(e)}), 500


def validate_files_exist(filenames):
    """验证所有选择的文件是否存在"""
    for fn in filenames:
        file_path = os.path.join(INPUT_DIR, fn)
        if not os.path.exists(file_path) or not is_video_file(fn):
            return jsonify({'error': f'文件不存在或不是视频文件: {fn}'}), 400
    return None


def validate_start_end_params(data):
    """校验start和end模式的参数并组装数据"""
    duration = float(data.get('duration', 15))
    if duration <= 0:
        return jsonify({'error': '裁剪时长必须大于0'}), 400
    return duration


def validate_middle_params(data):
    """校验middle模式的参数并组装数据"""
    segments = data.get('segments')
    if not segments or not isinstance(segments, list):
        return jsonify({'error': '中间剪切模式需要提供有效的剪切段'}), 400

    # 验证每个段的格式
    for segment in segments:
        if not isinstance(segment, list) or len(segment) != 2:
            return jsonify({'error': '剪切段格式错误'}), 400

        start, duration = segment
        if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
            return jsonify({'error': '剪切段数值类型错误'}), 400

        if start < 0 or duration <= 0:
            return jsonify({'error': '起始时间必须>=0，剪切时长必须>0'}), 400
    return segments


def validate_extract_params(data):
    """校验extract模式的参数并组装数据"""
    start = float(data.get('start', 0))
    duration = float(data.get('duration', 15))
    if start < 0:
        return jsonify({'error': '起始时间必须大于等于0'}), 400
    if duration <= 0:
        return jsonify({'error': '保留时长必须大于0'}), 400
    return (start, duration)


def validate_sample_split_params(data):
    """校验sample_split模式的参数并组装数据"""
    hash_threshold = data.get('hash_threshold', 0.8)
    consecutive_frames = data.get('consecutive_frames', 3)
    frame_step = data.get('frame_step', 0.5)
    time_adjust = data.get('time_adjust', 0.0)
    skip_interval = data.get('skip_interval', 30.0)
    search_duration = data.get('search_duration', 10.0)

    # 验证参数
    if hash_threshold < 0.1 or hash_threshold > 1.0:
        return jsonify({'error': '图像哈希对比阈值必须在0.1~1.0之间'}), 400
    if consecutive_frames <= 0:
        return jsonify({'error': '连续对比帧数必须大于0'}), 400
    if frame_step <= 0:
        return jsonify({'error': '帧提取步长必须大于0'}), 400
    if skip_interval <= 0:
        return jsonify({'error': '跳过间隔必须大于0'}), 400
    if search_duration <= 0:
        return jsonify({'error': '搜索持续秒必须大于0'}), 400
    
    return {
        'hash_threshold': hash_threshold,
        'consecutive_frames': consecutive_frames,
        'frame_step': frame_step,
        'time_adjust': time_adjust,
        'skip_interval': skip_interval,
        'search_duration': search_duration
    }


def validate_ad_remove_params(data):
    """校验ad_remove模式的参数并组装数据"""
    search_window_start = data.get('search_window_start', 0.0)
    search_window_end = data.get('search_window_end', 60.0)
    ad_duration = data.get('ad_duration', 20.0)
    hash_threshold = data.get('hash_threshold', 0.8)
    consecutive_frames = data.get('consecutive_frames', 3)
    frame_step = data.get('frame_step', 0.5)
    time_adjust = data.get('time_adjust', 0.0)

    # 验证参数
    if search_window_start < 0 or search_window_end <= search_window_start:
        return jsonify({'error': '搜索窗口参数无效'}), 400
    if ad_duration <= 0:
        return jsonify({'error': '广告持续时间必须大于0'}), 400
    if hash_threshold < 0.1 or hash_threshold > 1.0:
        return jsonify({'error': '图像哈希对比阈值必须在0.1~1.0之间'}), 400
    if consecutive_frames <= 0:
        return jsonify({'error': '连续对比帧数必须大于0'}), 400
    if frame_step <= 0:
        return jsonify({'error': '帧提取步长必须大于0'}), 400
    # time_adjust 可以是正数或负数，不需要特殊验证
    
    return {
        'search_window_start': search_window_start,
        'search_window_end': search_window_end,
        'ad_duration': ad_duration,
        'hash_threshold': hash_threshold,
        'consecutive_frames': consecutive_frames,
        'frame_step': frame_step,
        'time_adjust': time_adjust
    }


def validate_watermark_params(data):
    """校验watermark_remove模式的参数并组装数据"""
    watermark_params = data.get('watermark_params', [])
    if not watermark_params or not isinstance(watermark_params, list):
        return jsonify({'error': '水印去除模式需要提供有效的水印参数'}), 400

    # 验证每个水印参数的格式
    for i, param in enumerate(watermark_params):
        start = param.get('start', 0.0)
        end = param.get('end')
        x1 = param.get('x1', 0)
        y1 = param.get('y1', 0)
        w = param.get('w', 0)
        h = param.get('h', 0)

        if not isinstance(start, (int, float)):
            return jsonify({'error': '水印开始时间参数类型错误'}), 400
        if end is not None and not isinstance(end, (int, float)):
            return jsonify({'error': '水印结束时间参数类型错误'}), 400
        if not isinstance(x1, (int, float)) or not isinstance(y1, (int, float)):
            return jsonify({'error': '水印坐标参数类型错误'}), 400
        if not isinstance(w, (int, float)) or not isinstance(h, (int, float)):
            return jsonify({'error': '水印宽度高度参数类型错误'}), 400

        if start < 0:
            return jsonify({'error': '水印开始时间必须>=0'}), 400
        # 只有当不是最后一条或有结束时间时，才验证结束时间大于开始时间
        if (i != len(watermark_params) - 1 or end is not None) and (end is None or end <= start):
            return jsonify({'error': '水印结束时间必须大于开始时间'}), 400
        if w <= 0 or h <= 0:
            return jsonify({'error': '水印宽度和高度必须大于0'}), 400
    
    return watermark_params


@app.route('/api/trim', methods=['POST'])
def trim_batch():
    data = request.get_json()
    filenames = data.get('filenames', [])
    mode = data.get('mode', 'start')

    # 1. 统一必要的参数校验
    if not filenames:
        return jsonify({'error': '未选择文件'}), 400

    valid_modes = ['start', 'end', 'middle', 'extract', 'ad_remove', 'sample_split', 'watermark_remove']
    if mode not in valid_modes:
        return jsonify({'error': '模式错误'}), 400

    # 校验所有选择的文件是否存在
    file_error = validate_files_exist(filenames)
    if file_error:
        return file_error

    # 2. 根据mode模式，调用对应的参数校验和组装方法
    mode_validators = {
        'start': validate_start_end_params,
        'end': validate_start_end_params,
        'middle': validate_middle_params,
        'extract': validate_extract_params,
        'ad_remove': validate_ad_remove_params,
        'sample_split': validate_sample_split_params,
        'watermark_remove': validate_watermark_params
    }

    validator = mode_validators.get(mode)
    if not validator:
        return jsonify({'error': '模式错误'}), 400

    # 调用参数校验和组装方法
    params = validator(data)
    if isinstance(params, tuple):
        # 校验失败，返回错误响应
        return params

    # 3. 对组装好的数据，循环put到task_queue，并校验是否存在重复任务
    added_count = 0
    for fn in filenames:
        # 检查任务是否已经在队列中（仅检查文件名）
        task_exists = False
        task_items = list(task_queue.queue)
        for item in task_items:
            if item[0] == fn:
                task_exists = True
                break

        if not task_exists:
            # 记录日志
            app.logger.info(f"加入队列: {fn}, 模式: {mode}, 参数: {params}")
            task_queue.put((fn, mode, params))
            added_count += 1

    # 记录日志
    app.logger.info(f"📥 收到裁剪任务 - 文件数: {len(filenames)}, 模式: {mode}, 新增任务: {added_count}")
    if mode in ['start', 'end']:
        app.logger.info(f"时长: {params}s")
    elif mode == 'middle':
        app.logger.info(f"剪切段: {params}")
    elif mode == 'extract':
        app.logger.info(f"起始时间: {params[0]}, 保留时长: {params[1]}")
    elif mode == 'ad_remove':
        app.logger.info(
            f"广告移除参数: 搜索窗口=[{params['search_window_start']}, {params['search_window_end']}], 广告时长={params['ad_duration']}, 哈希阈值={params['hash_threshold']}, 连续帧数={params['consecutive_frames']}, 帧步长={params['frame_step']}, 时间调整={params['time_adjust']}")
    elif mode == 'sample_split':
        app.logger.info(
            f"样本分割参数: 哈希阈值={params['hash_threshold']}, 连续帧数={params['consecutive_frames']}, 帧步长={params['frame_step']}, 跳过间隔={params['skip_interval']}s, 时间调整={params['time_adjust']}")

    return jsonify({'message': f'已加入队列 ({added_count} 个新任务)'})

# ===== 启动应用 =====
if __name__ == '__main__':
    start_worker()
    app.run(host='0.0.0.0', port=8080, debug=True, use_reloader=False)
