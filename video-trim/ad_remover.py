#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import subprocess
import logging
import shutil
import uuid
from typing import Tuple, Optional, List, Iterator
from PIL import Image
import imagehash

# 配置日志
logger = logging.getLogger(__name__)

# 创建控制台处理器并设置级别
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# 创建格式化器并将其添加到处理器
formatter = logging.Formatter('[%(asctime)s] %(levelname)s in %(module)s: %(message)s')
console_handler.setFormatter(formatter)

# 将处理器添加到日志记录器
logger.addHandler(console_handler)

# 设置日志级别为DEBUG以显示详细信息
logger.setLevel(logging.DEBUG)

# phash 默认输出 64 位哈希，归一化常数
_HASH_BITS = 64

# 进程内视频时长缓存：{(path, mtime, size): duration}
_duration_cache: dict = {}


def get_video_duration(video_path: str, timeout: int = 30) -> Optional[float]:
    """获取视频时长（秒），带进程内缓存（按 path+mtime+size 作 key）。"""
    try:
        st = os.stat(video_path)
        cache_key = (video_path, int(st.st_mtime), st.st_size)
    except OSError:
        cache_key = (video_path, 0, 0)

    cached = _duration_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'csv=p=0',
            video_path
        ], capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.error(f"FFprobe 非零退出码 ({result.returncode}) - 文件: {video_path}, "
                         f"stderr: {result.stderr.strip()}")
            return None
        duration = float(result.stdout.strip())
        if duration <= 0:
            logger.warning(f"无效视频时长 ({duration}) - 文件: {video_path}")
            return None
        _duration_cache[cache_key] = duration
        return duration
    except (ValueError, subprocess.TimeoutExpired) as e:
        logger.error(f"FFprobe 解析失败 - 文件: {video_path}: {e}")
        return None
    except Exception:
        logger.exception(f"FFprobe 意外异常 - 文件: {video_path}")
        return None


def iter_video_frames(video_path: str, start: float, end: float,
                      frame_step: float) -> Iterator[Tuple[float, Image.Image]]:
    """
    通过 rawvideo 管道按固定步长提取帧，逐帧 yield (时间点, PIL灰度图)。
    用管道直出 rawvideo，避免逐帧落盘 PNG 带来的编解码与磁盘 I/O 开销。

    :param video_path: 视频文件路径
    :param start: 起始时间（秒）
    :param end: 结束时间（秒）
    :param frame_step: 帧提取步长（秒）
    """
    # 统一缩放为固定尺寸做哈希，提升稳定性并固定每帧字节数
    HASH_SIZE = 8  # phash 默认 8x8
    SCALE_W = HASH_SIZE * 4
    SCALE_H = HASH_SIZE * 4
    # 只提取 [start, end] 区间内的帧：
    #   -ss 放在 -i 之前作为输入选项，做快速跳转（直接定位，不解码到目标位置）；
    #   -t 放在 -i 之后作为输出选项，表示从 -ss 起点开始只输出 duration 秒，
    #      语义在所有 ffmpeg 版本一致，避免读到文件末尾 EOF。
    duration = max(0.0, end - start)

    cmd = [
        'ffmpeg',
        '-ss', str(start),
        '-i', video_path,
        '-t', str(duration),
        '-vf', f'fps=1/{frame_step},scale={SCALE_W}:{SCALE_H}',
        '-f', 'image2pipe',
        '-vcodec', 'rawvideo',
        '-pix_fmt', 'gray',
        '-',
    ]
    logger.info(f"提取帧(rawvideo管道): start={start}s, end={end}s, 时长={duration}s, step={frame_step}s")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        logger.error(f"FFmpeg提取帧失败，返回码: {proc.returncode}, "
                     f"stderr: {proc.stderr.decode('utf-8', errors='ignore')}")
        return

    frame_bytes = SCALE_W * SCALE_H  # 灰度单通道
    data = proc.stdout
    n_frames = len(data) // frame_bytes
    for i in range(n_frames):
        offset = i * frame_bytes
        raw = data[offset:offset + frame_bytes]
        img = Image.frombytes('L', (SCALE_W, SCALE_H), raw)
        yield (start + i * frame_step, img)


def extract_frame_from_video(video_path: str, time_point: float, output_path: str) -> bool:
    """
    从视频中提取特定时间点的帧
    
    :param video_path: 视频文件路径
    :param time_point: 时间点（秒）
    :param output_path: 输出图像文件路径
    :return: 是否成功
    """
    logger.info(f"正在从视频 {video_path} 提取时间点 {time_point}s 的帧到 {output_path}")
    
    cmd = [
        'ffmpeg',
        '-i', video_path,
        '-ss', str(time_point),
        '-vframes', '1',
        '-y',
        output_path
    ]
    
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"成功提取帧到 {output_path}")
        return True
    except FileNotFoundError as e:
        logger.error(f"FFmpeg未找到，请确保已安装并添加到系统PATH中: {e}")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"提取帧失败: {e}")
        return False


def calculate_image_hash(image_path: str) -> Optional[imagehash.ImageHash]:
    """
    计算图像的感知哈希值
    
    :param image_path: 图像文件路径
    :return: 图像哈希值或None
    """
    logger.info(f"正在计算图像 {image_path} 的哈希值")
    
    try:
        with Image.open(image_path) as img:
            # 转换为灰度图以提高一致性
            img = img.convert('L')
            hash_value = imagehash.phash(img)
            logger.info(f"成功计算图像哈希值: {hash_value}")
            return hash_value
    except Exception as e:
        logger.error(f"计算图像哈希值失败: {e}")
        return None


def find_ad_start_time_with_hash(video_path: str, ad_sample_path: str,
                                 search_window: Tuple[float, float],
                                 hash_threshold: float = 0.8,
                                 consecutive_frames: int = 3,
                                 frame_step: float = 0.5) -> Optional[float]:
    """
    在视频中使用图像哈希查找广告开始时间

    :param video_path: 视频文件路径
    :param ad_sample_path: 广告样本图像文件路径
    :param search_window: 搜索时间窗口 (start, end)
    :param hash_threshold: 图像哈希对比阈值 (0.0-1.0)
    :param consecutive_frames: 连续匹配帧数
    :param frame_step: 帧提取步长（秒）
    :return: 广告开始时间或None
    """
    logger.info(f"开始在视频中查找广告开始时间: 视频路径={video_path}, 样本路径={ad_sample_path}")
    logger.info(f"搜索参数: 搜索窗口={search_window}, 阈值={hash_threshold}, 连续帧数={consecutive_frames}, 帧步长={frame_step}")

    # 计算广告样本图像的哈希值（仅一次）
    ad_hash = calculate_image_hash(ad_sample_path)
    if ad_hash is None:
        logger.error("无法计算广告样本图像的哈希值")
        return None

    window_start, window_end = search_window
    matched_frames = 0
    first_match_time = None
    checked = 0

    # 通过管道按步长逐帧提取并比较，避免逐帧落盘 PNG
    for frame_time, frame_img in iter_video_frames(video_path, window_start, window_end, frame_step):
        checked += 1
        frame_hash = imagehash.phash(frame_img)
        similarity = 1.0 - (ad_hash - frame_hash) / _HASH_BITS
        logger.debug(f"时间 {frame_time:.1f}s, 相似度: {similarity:.3f}, 阈值: {hash_threshold}")

        if similarity >= hash_threshold:
            if matched_frames == 0:
                first_match_time = frame_time
            matched_frames += 1
            if matched_frames >= consecutive_frames:
                logger.info(f"找到广告开始时间: {first_match_time:.1f}s (已检查 {checked} 帧)")
                return first_match_time
        else:
            matched_frames = 0
            first_match_time = None

    logger.warning(f"在搜索窗口内未找到符合条件的广告开始时间 (已检查 {checked} 帧)")
    return None


def find_all_sample_matches(video_path: str, sample_path: str,
                            search_window: Tuple[float, float],
                            hash_threshold: float = 0.8,
                            consecutive_frames: int = 3,
                            frame_step: float = 0.5,
                            skip_interval: float = 30.0,
                            search_duration: float = 10.0) -> List[float]:
    """
    在视频中使用图像哈希查找所有匹配样本图片的时间点

    分段提取帧 + 滑动搜索窗口策略：
      - 每段用 iter_video_frames 提取 [start, end] 区间的帧并逐帧哈希比对；
      - 命中样本后，下一段从命中点 T 开始，搜索到 T + skip_interval + search_duration；
      - 未命中时，下一段从当前段终点开始，推进到 终点 + skip_interval + search_duration；
      - 每段均受 window_end 截断。

    :param video_path: 视频文件路径
    :param sample_path: 样本图像文件路径
    :param search_window: 搜索时间窗口 (start, end)
    :param hash_threshold: 图像哈希对比阈值 (0.0-1.0)
    :param consecutive_frames: 连续匹配帧数
    :param frame_step: 帧提取步长（秒）
    :param skip_interval: 跳过间隔（秒），命中/未命中后推进的时间
    :param search_duration: 每次搜索的持续时间（秒），来自前端参数，默认 10.0
    :return: 所有匹配的时间点列表
    """
    logger.info(f"开始在视频中查找所有匹配样本的时间点: 视频路径={video_path}, 样本路径={sample_path}")
    logger.info(f"搜索参数: 搜索窗口={search_window}, 阈值={hash_threshold}, 连续帧数={consecutive_frames}, "
                f"帧步长={frame_step}, 跳过间隔={skip_interval}s, 搜索持续={search_duration}s")

    # 计算样本图像的哈希值（仅一次）
    sample_hash = calculate_image_hash(sample_path)
    if sample_hash is None:
        logger.error("无法计算样本图像的哈希值")
        return []

    window_start, window_end = search_window
    matched_times: List[float] = []

    # 初始段：[window_start, window_start + search_duration]
    seg_start = window_start
    seg_end = min(window_start + search_duration, window_end)

    while seg_start < window_end:
        # 当前段结束不超过搜索窗口上限
        seg_end = min(seg_end, window_end)
        logger.info(f"搜索段: [{seg_start:.1f}s, {seg_end:.1f}s]")

        matched_frames = 0
        first_match_time: Optional[float] = None
        seg_checked = 0

        # 提取当前段帧并逐帧比对
        for frame_time, frame_img in iter_video_frames(video_path, seg_start, seg_end, frame_step):
            seg_checked += 1
            frame_hash = imagehash.phash(frame_img)
            similarity = 1.0 - (sample_hash - frame_hash) / _HASH_BITS
            logger.debug(f"详细检查 - 时间 {frame_time:.1f}s, 相似度: {similarity:.3f}, 阈值: {hash_threshold}")

            if similarity >= hash_threshold:
                if matched_frames == 0:
                    first_match_time = frame_time
                matched_frames += 1
                if matched_frames >= consecutive_frames:
                    logger.info(f"找到匹配样本时间点: {first_match_time:.1f}s (本段检查 {seg_checked} 帧)")
                    matched_times.append(first_match_time)
                    # 命中：跳过 skip_interval 后再搜索 search_duration
                    seg_start = first_match_time + skip_interval
                    seg_end = first_match_time + skip_interval + search_duration
                    break
            else:
                matched_frames = 0
                first_match_time = None
        else:
            # 本段未命中：从当前段起点跳过 skip_interval 后再搜索 search_duration
            logger.info(f"本段 [{seg_start:.1f}s, {seg_end:.1f}s] 未找到匹配 (检查 {seg_checked} 帧)")
            seg_start = seg_start + skip_interval
            seg_end = seg_start + search_duration

    logger.info(f"在搜索窗口内找到 {len(matched_times)} 个匹配时间点")
    return matched_times


def split_video_by_time_points(video_path: str, split_time_points: List[float], output_dir: str, time_adjust: float = 0.0) -> bool:
    """
    根据时间点列表将视频分割成多个片段

    :param video_path: 视频文件路径
    :param split_time_points: 分割时间点列表（按升序排序）
    :param output_dir: 输出目录
    :param time_adjust: 时间调整值（秒），支持正负调整
    :return: 是否成功
    """
    logger.info(f"开始分割视频: 视频路径={video_path}, 分割点={split_time_points}, 输出目录={output_dir}")

    # 边界自防御：没有分割点直接返回
    if not split_time_points:
        logger.warning("分割时间点列表为空，跳过分割")
        return False

    # 添加时间调整
    adjusted_split_points = [point + time_adjust for point in split_time_points]
    logger.info(f"调整后的分割点: {adjusted_split_points}")

    # 复用带缓存的 get_video_duration
    total_duration = get_video_duration(video_path)
    if total_duration is None:
        logger.error(f"无法获取视频时长: {video_path}")
        return False
    logger.info(f"视频总时长: {total_duration}s")

    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)

    # 准备分割区间
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    ext = os.path.splitext(video_path)[1]

    # 添加0作为开始，总时长作为结束
    segments = [(0.0, adjusted_split_points[0])]
    for i in range(len(adjusted_split_points) - 1):
        segments.append((adjusted_split_points[i], adjusted_split_points[i+1] - adjusted_split_points[i]))
    segments.append((adjusted_split_points[-1], total_duration - adjusted_split_points[-1]))

    logger.info(f"分割段: {segments}")

    try:
        # 分割视频（各段 -ss 前置于 -i，流复制）
        for i, (start, duration) in enumerate(segments):
            output_path = os.path.join(output_dir, f"{base_name}_part{i+1}{ext}")
            logger.info(f"分割段 {i+1}: 从 {start:.1f}s 开始，时长 {duration:.1f}s，输出到 {output_path}")

            cmd = [
                'ffmpeg',
                '-ss', str(start),
                '-i', video_path,
                '-t', str(duration),
                '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                '-y',
                output_path
            ]

            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info(f"成功生成分割段: {output_path}")

        logger.info(f"视频分割完成，共生成 {len(segments)} 个片段")
        return True

    except Exception as e:
        logger.error(f"分割视频失败: {e}")
        return False


def remove_ad_from_video(video_path: str, ad_start_time: float, ad_duration: float, output_path: str, time_adjust: float = 0.0) -> bool:
    """
    从视频中移除广告段

    :param video_path: 视频文件路径
    :param ad_start_time: 广告开始时间
    :param ad_duration: 广告持续时间
    :param output_path: 输出视频路径
    :param time_adjust: 时间调整值（秒），支持正负调整
    :return: 是否成功
    """
    logger.info(f"开始从视频中移除广告段: 视频路径={video_path}, 输出路径={output_path}")

    # 应用时间调整
    adjusted_start_time = ad_start_time + time_adjust
    if adjusted_start_time < 0:
        adjusted_start_time = 0  # 确保不会出现负数时间

    logger.info(f"广告开始时间: {ad_start_time}s, 调整值: {time_adjust}s, 最终时间: {adjusted_start_time}s")

    # 创建唯一的临时目录，避免文件名冲突
    temp_dir = os.path.join(os.path.dirname(video_path), f'temp_{uuid.uuid4().hex}')
    os.makedirs(temp_dir, exist_ok=True)
    logger.info(f"创建临时目录: {temp_dir}")

    try:
        # 获取视频文件扩展名
        _, ext = os.path.splitext(video_path)

        # 提取广告前段（-ss 前置对流复制无意义，前段从 0 开始直接 -t）
        pre_ad_temp = os.path.join(temp_dir, f'pre_ad{ext}')
        cmd_pre = [
            'ffmpeg',
            '-i', video_path,
            '-t', str(adjusted_start_time),
            '-c', 'copy',
            '-avoid_negative_ts', 'make_zero',
            '-y',
            pre_ad_temp
        ]

        logger.info(f"正在提取广告前段到 {pre_ad_temp}")
        subprocess.run(cmd_pre, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 提取广告后段（-ss 前置于 -i，流复制跳转更快）
        post_ad_temp = os.path.join(temp_dir, f'post_ad{ext}')
        cmd_post = [
            'ffmpeg',
            '-ss', str(adjusted_start_time + ad_duration),
            '-i', video_path,
            '-c', 'copy',
            '-avoid_negative_ts', 'make_zero',
            '-y',
            post_ad_temp
        ]

        logger.info(f"正在提取广告后段到 {post_ad_temp}")
        subprocess.run(cmd_post, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 创建concat文件
        concat_file = os.path.join(temp_dir, 'concat_list.txt')
        with open(concat_file, 'w') as f:
            f.write(f"file '{os.path.basename(pre_ad_temp)}'\n")
            f.write(f"file '{os.path.basename(post_ad_temp)}'\n")

        logger.info(f"已创建concat文件: {concat_file}")

        # 合并两段视频
        cmd_concat = [
            'ffmpeg',
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_file,
            '-c', 'copy',
            '-y',
            output_path
        ]

        logger.info(f"正在合并视频段到最终输出 {output_path}")
        subprocess.run(cmd_concat, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=temp_dir)
        logger.info(f"广告移除完成，输出文件: {output_path}")
        return True

    except FileNotFoundError as e:
        logger.error(f"FFmpeg未找到，请确保已安装并添加到系统PATH中: {e}")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"移除广告失败: {e}")
        return False
    finally:
        # 清理临时目录（统一 rmtree）
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info(f"已清理临时目录: {temp_dir}")


def process_video_for_ad_removal(video_path: str, ad_sample_path: str,
                                 search_window: Tuple[float, float] = (0.0, 60.0),
                                 ad_duration: float = 20.0,
                                 hash_threshold: float = 0.8,
                                 consecutive_frames: int = 3,
                                 frame_step: float = 0.5,
                                 time_adjust: float = 0.0,
                                 output_dir: str = '/videos/output') -> bool:
    """
    处理视频以移除广告（基于图像哈希匹配）
    
    :param video_path: 视频文件路径
    :param ad_sample_path: 广告样本图像文件路径
    :param search_window: 搜索时间窗口 (start, end)
    :param ad_duration: 广告持续时间
    :param hash_threshold: 图像哈希对比阈值 (0.1-1.0)
    :param consecutive_frames: 连续对比帧数
    :param frame_step: 帧提取步长（秒）
    :param time_adjust: 时间调整值（秒），支持正负调整
    :param output_dir: 输出目录路径
    :return: 是否成功
    """
    logger.info(f"开始处理视频以移除广告: 视频路径={video_path}, 样本路径={ad_sample_path}")
    logger.info(f"处理参数: 搜索窗口={search_window}, 广告持续时间={ad_duration}s, "
                f"哈希阈值={hash_threshold}, 连续帧数={consecutive_frames}, "
                f"帧步长={frame_step}s, 时间调整={time_adjust}s, 输出目录={output_dir}")
    
    # 查找广告开始时间
    ad_start_time = find_ad_start_time_with_hash(
        video_path, ad_sample_path, search_window, hash_threshold, consecutive_frames, frame_step)
    
    if ad_start_time is None:
        logger.error("未能找到广告开始时间")
        return False
    
    logger.info(f"检测到广告开始时间: {ad_start_time}")
    
    # 生成输出文件名 - 直接保存到输出目录
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    ext = os.path.splitext(video_path)[1]
    output_path = os.path.join(
        output_dir,  # 直接保存到输出目录
        f"{base_name}_clean{ext}"
    )
    
    logger.info(f"输出文件路径: {output_path}")
    
    # 移除广告并生成清理后的视频
    success = remove_ad_from_video(video_path, ad_start_time, ad_duration, output_path, time_adjust)
    
    if success:
        logger.info(f"广告移除成功，输出文件: {output_path}")
    else:
        logger.error("广告移除失败")
    
    return success


if __name__ == "__main__":
    pass