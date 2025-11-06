import re
import os
import random
import aiohttp
import asyncio
import aiofiles
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Tuple, Callable
from math import ceil

# --- 全局常量和配置 ---

BASE_URLS = [
    "https://api.asmr.one",
    "https://api.asmr-100.com",
    "https://api.asmr-200.com",
    "https://api.asmr-300.com"
]
RJ_RE = re.compile(r"(?:RJ)?(?P<id>\d+)", re.IGNORECASE)
CONFIG_FILE = Path("config.json")
LOG_FILE = Path("download_log.txt")
API_INDEX = 0  # 当前使用的 API 索引


# --- 辅助函数：日志、配置和格式化 ---

async def log_message(message: str):
    """异步写入日志"""
    try:
        async with aiofiles.open(LOG_FILE, "a", encoding="utf-8") as f:
            await f.write(f"[{datetime.now().isoformat()}] {message}\n")
    except Exception as e:
        # 兜底防止日志写入失败
        print(f"Error writing to log: {e}")


def read_log_sync(lines: int = 200) -> str:
    """同步读取日志文件（倒序）"""
    try:
        if not LOG_FILE.exists():
            return "日志文件不存在。"
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            all_lines = f.readlines()

        # 返回最新的 n 行
        return "".join(reversed(all_lines[-lines:]))
    except Exception as e:
        return f"读取日志失败: {e}"


def load_config() -> Dict[str, Any]:
    """同步加载配置，如果文件不存在则返回默认配置"""
    default_config = {
        "output_dir": str(Path("Downloads").resolve()),
        "hq_audio_only": False,
        "default_file_types": ["audio", "image", "text"],
        "max_concurrent_downloads": 3,
        "proxy": "",
        "listen_host": "127.0.0.1",
        "listen_port": 7683
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                # 使用 strip() 去除 BOM，防止解析错误
                config = json.loads(f.read().strip())
                return {**default_config, **config}
        except Exception as e:
            print(f"Error loading config file: {e}. Using default config.")
            return default_config
    return default_config


def format_size(size_bytes: int) -> str:
    """将字节数格式化为可读的字符串"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


# --- API 访问逻辑 ---

def rotate_api():
    """切换到下一个API端点"""
    global API_INDEX
    API_INDEX = (API_INDEX + 1) % len(BASE_URLS)
    print(f"[ASMR API] 切换到 API: {BASE_URLS[API_INDEX]}")


def get_current_api():
    """获取当前API端点"""
    return BASE_URLS[API_INDEX]


async def fetch_with_retry(session: aiohttp.ClientSession, url_path: str, params=None, max_retries=4):
    """带重试机制的API请求"""
    api_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Origin": "https://asmr.one",
        "Referer": "https://asmr.one/",
        "Accept": "application/json"
    }

    for _ in range(max_retries):
        current_api = get_current_api()
        url = f"{current_api}{url_path}"
        try:
            # 获取代理配置
            config = load_config()
            proxy = config.get("proxy", None)

            async with session.get(url, params=params, timeout=10, proxy=proxy) as response:
                if response.status == 200:
                    return await response.json()

                await log_message(f"API {current_api} returned status {response.status} for {url_path}")
                rotate_api()
        except Exception as e:
            await log_message(f"API {current_api} request failed for {url_path}: {type(e).__name__}: {str(e)}")
            rotate_api()
            await asyncio.sleep(1)  # 短暂等待后重试

    await log_message(f"All API attempts failed for {url_path}.")
    return None


# --- 核心逻辑：文件信息解析 (V2 - 保留文件夹结构) ---

def recursively_transform_data_v2(
        data: List[Dict[str, Any]],
        all_files: List[Dict[str, Any]],
        current_folder_path: List[str],
        index_start: int,
        config: Dict[str, Any]
) -> int:
    """
    【修复：保留文件结构】递归遍历 API JSON 结构，收集所有文件信息并保留文件夹路径。
    返回下一个可用的文件索引。
    """
    current_index = index_start
    hq_only = config.get("hq_audio_only", False)
    allowed_types = set(config.get("default_file_types", ["audio"]))

    for item in data:
        item_type = item.get("type")
        item_title = item.get("title")

        if item_type == "folder":
            new_path = current_folder_path + [item_title]
            if "children" in item and item["children"] is not None:
                # 递归调用，并更新索引
                current_index = recursively_transform_data_v2(
                    item["children"], all_files, new_path, current_index, config
                )

        # 仅处理允许的类型
        elif item_type in allowed_types:

            # 判断是否是 HQ 音频 (常见的 HQ 格式)
            is_hq = item_title.lower().endswith((".flac", ".wav", ".mp3"))
            # 兼容：如果 size 很大，也可能是 HQ
            is_hq_by_size = item.get("size", 0) > (50 * 1024 * 1024)  # 假设大于 50MB 可能是 HQ

            # 如果配置为只下载 HQ 且当前是音频但不是 HQ，则跳过
            if hq_only and item_type == "audio" and not (is_hq or is_hq_by_size):
                continue

            # 构建当前文件的完整文件夹路径（相对于 RJ ID 目录）
            # 注意：这里使用 '/' 作为分隔符，保存时会转换为系统路径
            folder_path_str = "/".join(current_folder_path)

            file_info = {
                "index": current_index,
                "filename": item_title,
                "url": item.get("mediaDownloadUrl"),
                "type": item_type,
                "size": item.get("size", 0),
                "size_formatted": format_size(item.get("size", 0)),
                "folder_path": folder_path_str,  # 保留文件夹路径
            }
            all_files.append(file_info)
            current_index += 1

    return current_index


async def get_work_info_async(rj_id: str) -> Tuple[List[Dict[str, Any]], str]:
    """
    获取作品文件列表，并返回作品标题。
    返回 (文件信息列表, 作品标题或错误信息)
    """
    await log_message(f"Fetching info for {rj_id}...")

    try:
        async with aiohttp.ClientSession() as session:
            # 1. 获取作品信息（用于获取标题）
            work_info_path = f"/api/workInfo/{rj_id.replace('RJ', '')}"
            work_data = await fetch_with_retry(session, work_info_path)

            if work_data is None or "title" not in work_data:
                await log_message(f"Failed to get work info for {rj_id}.")
                return [], "作品信息获取失败或资源不存在。"

            work_title = work_data.get("title", f"Work_{rj_id}")

            # 2. 获取音轨信息（用于获取文件结构）
            tracks_path = f"/api/tracks/{rj_id.replace('RJ', '')}?v=2"
            tracks_data = await fetch_with_retry(session, tracks_path)

            if tracks_data is None:
                await log_message(f"Failed to get tracks for {rj_id}.")
                return [], f"{work_title} (文件列表获取失败)"

            # 3. 解析文件信息
            all_files: List[Dict[str, Any]] = []
            config = load_config()

            # 使用 V2 转换函数，保留文件结构
            recursively_transform_data_v2(tracks_data, all_files, [], 1, config)

            if not all_files:
                return [], f"{work_title} (未找到符合条件的文件)"

            # 返回适合 Gradio 的 List[List] 格式（但这里返回 Dict 列表，由 app.py 转换）
            return all_files, work_title

    except Exception as e:
        await log_message(f"Critical error in get_work_info_async for {rj_id}: {e}")
        return [], f"系统错误: {e}"


# --- 核心逻辑：文件下载 ---

async def download_worker(
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        file_info: Dict[str, Any],
        base_dir: Path,
        progress_callback: Callable[[str, str, int, int], None],
        rj_id: str
) -> bool:
    """处理单个文件的下载，支持断点续传"""
    file_url = file_info.get('url')
    file_name = file_info['filename']
    expected_size = file_info.get('size', 0)

    # ❗ 关键修复：使用 folder_path 构建完整的保存路径
    folder_path_str = file_info.get("folder_path", "")

    # 清理路径中的非法字符（Windows/Linux 兼容）
    def sanitize_filename(name):
        # 移除或替换 Windows 非法字符 \ / : * ? " < > |
        # 此外，移除导致 Gradio UI 问题的特殊字符
        name = re.sub(r'[\\/:*?"<>|]', ' ', name).strip()
        # 移除路径中的非法字符
        name = name.replace("..", "_").replace(".", " ")
        return name

    safe_folder_path = Path(sanitize_filename(folder_path_str).replace("/", os.sep))
    safe_file_name = sanitize_filename(file_name)

    # full_path 现在是: base_dir / safe_folder_path / safe_file_name
    full_path = base_dir / safe_folder_path / safe_file_name

    mode = 'wb'
    headers_range = {}
    downloaded_size = 0

    full_path.parent.mkdir(parents=True, exist_ok=True)

    if full_path.exists():
        downloaded_size = full_path.stat().st_size
        if expected_size > 0 and downloaded_size == expected_size:
            await log_message(f"File already exists (skipping): {file_name}")
            progress_callback(rj_id, file_name, expected_size, expected_size)
            return True
        elif downloaded_size < expected_size:
            mode = 'ab'
            headers_range['Range'] = f'bytes={downloaded_size}-'
            await log_message(f"Resuming download: {file_name}, from {format_size(downloaded_size)}")
        else:
            # 文件大小异常，重新下载（或 expected_size=0 但已下载）
            full_path.unlink(missing_ok=True)
            downloaded_size = 0

    current_downloaded = downloaded_size

    async with semaphore:
        try:
            config = load_config()
            proxy = config.get("proxy", None)

            download_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
                "Referer": "https://asmr.one/"
            }
            if headers_range:
                download_headers.update(headers_range)

            async with session.get(file_url, headers=download_headers, proxy=proxy) as response:
                response.raise_for_status()

                # 实际总大小 = 响应内容长度 + 已下载部分
                content_length = int(response.headers.get('content-length', 0))
                total_size = content_length + downloaded_size

                # 如果 API 提供的 expected_size 更大且 total_size 为 0，则使用 expected_size
                if total_size == 0 and expected_size > 0:
                    total_size = expected_size

                await log_message(f"Starting download: {file_name} (Total size {format_size(total_size)})")

                async with aiofiles.open(full_path, mode) as f:
                    chunk_size = 8192
                    update_counter = 0
                    async for chunk in response.content.iter_chunked(chunk_size):
                        await f.write(chunk)
                        current_downloaded += len(chunk)
                        update_counter += 1

                        # 每 50 次迭代更新一次进度条，减少 UI 压力
                        if update_counter % 50 == 0:
                            progress_callback(rj_id, file_name, current_downloaded, total_size)

                # 最终更新进度条至 100%
                progress_callback(rj_id, file_name, total_size, total_size)

            await log_message(f"Download successful: {file_name}")
            return True

        except aiohttp.ClientResponseError as e:
            await log_message(f"Download failed (HTTP {e.status}): {file_name}")
            return False
        except Exception as e:
            await log_message(f"Download failed (Unknown error): {file_name}, {e}")
            return False


async def process_download_job(
        rj_id: str,
        selected_indices: List[int],
        progress_callback: Callable[[str, str, int, int], None]
) -> bool:
    """
    主下载逻辑，执行选中的下载任务。
    """
    await log_message(f"Processing download job for {rj_id}, indices: {selected_indices}")

    # 1. 重新获取完整的作品信息（确保最新的文件列表和 HQ 过滤）
    files_info_dicts, _ = await get_work_info_async(rj_id)

    # 2. 过滤出用户选择的文件
    selected_files = [f for f in files_info_dicts if f['index'] in selected_indices]

    if not selected_files:
        await log_message(f"No valid files selected for {rj_id}.")
        return False

    config = load_config()
    max_concurrent_downloads = config.get("max_concurrent_downloads", 3)

    # 3. 设置下载目录：配置的输出目录 / RJ ID 文件夹
    base_dir = Path(config["output_dir"]) / rj_id.upper()
    base_dir.mkdir(parents=True, exist_ok=True)

    await log_message(f"Starting {len(selected_files)} downloads into {base_dir.as_posix()}")

    # 4. 初始化 ClientSession，设置并发限制
    async with aiohttp.ClientSession() as session:
        semaphore = asyncio.Semaphore(max_concurrent_downloads)

        download_tasks = [
            download_worker(session, semaphore, f, base_dir, progress_callback, rj_id)
            for f in selected_files
        ]

        results = await asyncio.gather(*download_tasks)
        success_count = sum(results)

        await log_message(f"Download summary for {rj_id}: {success_count}/{len(selected_files)} succeeded.")

        return success_count == len(selected_files)


# --- 核心逻辑：批量下载作品 (新功能：顺序下载作品) ---

async def process_bulk_download_job(
        rj_ids: List[str],
        overall_progress_callback: Callable[[int, int, str], None]
) -> Tuple[bool, str]:
    """
    主逻辑：批量下载指定 RJ ID 列表中的作品。
    作品按顺序下载，每个作品内的文件并发下载。
    overall_progress_callback: (current_work_index, total_works, status_message)
    返回 (是否全部成功, 状态信息)
    """
    total_works = len(rj_ids)

    if not rj_ids:
        return False, "RJ ID 列表为空。"

    await log_message(f"Starting bulk download for {total_works} works: {rj_ids[:5]}...")

    # 初始状态更新
    overall_progress_callback(0, total_works, f"找到 {total_works} 个作品。开始按顺序处理...")

    success_count = 0

    # 2. 循环下载每个作品 (顺序执行)
    for i, rj_id in enumerate(rj_ids):
        current_index = i + 1

        # 1. 获取作品文件列表
        files_info_dicts, work_title = await get_work_info_async(rj_id)

        if not files_info_dicts:
            await log_message(f"Skipping {rj_id} ({work_title}): No files found or failed to retrieve.")
            overall_progress_callback(current_index, total_works,
                                      f"[{current_index}/{total_works}] 跳过 {rj_id} ({work_title})：未找到文件。")
            continue

        # 2. 自动选择所有文件的索引
        selected_indices = [f['index'] for f in files_info_dicts]

        overall_progress_callback(current_index, total_works,
                                  f"[{current_index}/{total_works}] 正在下载 {rj_id} ({work_title})...")

        # 3. 下载作品
        def work_progress_callback(rj_id_local: str, filename: str, downloaded: int, total: int):
            # 将单个作品的下载进度信息传递给整体进度回调
            def format_size_local(b):
                if b < 1024 * 1024: return f"{b / 1024:.2f} KB"
                if b < 1024 * 1024 * 1024: return f"{b / (1024 * 1024):.2f} MB"
                return f"{b / (1024 * 1024 * 1024):.2f} GB"

            progress_msg = (
                f"[{current_index}/{total_works}] {rj_id_local} - {work_title} | 文件 {filename[:20]}... | "
                f"{format_size_local(downloaded)}/{format_size_local(total)}"
            )
            overall_progress_callback(current_index, total_works, progress_msg)

        try:
            # process_download_job 在循环中顺序调用，实现了作品间的顺序下载
            success = await process_download_job(rj_id, selected_indices, work_progress_callback)

            if success:
                success_count += 1
                overall_progress_callback(current_index, total_works,
                                          f"[{current_index}/{total_works}] ✅ {rj_id} ({work_title}) 下载成功！")
            else:
                overall_progress_callback(current_index, total_works,
                                          f"[{current_index}/{total_works}] ❌ {rj_id} ({work_title}) 下载失败。")

        except Exception as e:
            await log_message(f"Error during bulk download of {rj_id}: {e}")
            overall_progress_callback(current_index, total_works,
                                      f"[{current_index}/{total_works}] ❌ {rj_id} ({work_title}) 发生错误。")

        await asyncio.sleep(1)  # 每个作品下载完成后稍作等待

    final_message = f"批量下载完成。成功下载 {success_count} / {total_works} 个作品。"
    await log_message(final_message)

    return success_count == total_works, final_message


# --- 新功能：关键词搜索 (保持不变) ---

async def search_work_async(keyword: str, page: int = 1, size: int = 20) -> Tuple[List[Dict[str, Any]], int]:
    """
    根据关键词搜索作品。
    返回 (作品信息列表, 总页数)
    """
    await log_message(f"Searching for '{keyword}' on page {page}, size {size}")

    try:
        nsfw = True

        keyword_encoded = keyword.strip().replace("/", "%20")

        async with aiohttp.ClientSession() as session:
            r = await fetch_with_retry(
                session,
                f"/api/search/{keyword_encoded}",
                params={
                    "order": "dl_count",
                    "sort": "desc",
                    "page": page,
                    "size": size,
                    "subtitle": 0,
                    "includeTranslationWorks": "true"
                }
            )

            if r is None or "works" not in r:
                return [], 0

            works = r["works"]
            # 使用 size 来计算总页数
            total_pages = ceil(r["pagination"]["totalCount"] / size) if r["pagination"][
                                                                            "totalCount"] and size > 0 else 0

            search_results = []
            for work in works:
                ids = str(work["id"])
                full_rj_id = f"RJ{ids}" if not ids.startswith("RJ") else ids

                search_results.append({
                    "rj_id": full_rj_id,
                    "title": work["title"],
                    "author": work["name"],
                    "total_tracks": work.get("tracksCount", 0)
                })

            return search_results, total_pages

    except Exception as e:
        await log_message(f"Critical error in search_work_async for '{keyword}': {e}")
        return [], 0