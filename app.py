import gradio as gr
import json
import asyncio
from pathlib import Path
# 导入所需的类型提示，确保 AsyncGenerator 导入
from typing import Dict, Any, List, Tuple, AsyncGenerator

# 假设 core.downloader 模块可用
from core.downloader import (
    get_work_info_async,
    process_download_job,
    load_config,
    read_log_sync,
    # ❗ 必须使用异步版本
    log_message,
    search_work_async,
    process_bulk_download_job
)

# --- Configuration & Helpers ---
CONFIG_FILE = Path("config.json")
# 存储当前作品文件索引和文件名的映射，用于进度跟踪
download_progress_map: Dict[str, Dict[int, str]] = {}


def load_current_config():
    """加载配置并处理目录显示"""
    current_config = load_config()
    return current_config


def save_config(config: dict):
    """同步保存配置并返回状态"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        current_config = load_config()
        return "✅ 配置保存成功！请重新启动 Web UI 使部分配置生效。", current_config["output_dir"]
    except Exception as e:
        return f"❌ 配置保存失败: {e}", config["output_dir"]


def update_config_ui(
        output_dir: str,
        hq_only: bool,
        file_types: List[str],
        max_concurrent_downloads: int,
        proxy: str,
        listen_host: str,
        listen_port: str
):
    """处理 UI 配置更新逻辑"""
    current_config = load_config()

    new_output_dir = str(Path(output_dir).resolve())

    try:
        # 使用配置中的默认端口 7683
        port_num = int(listen_port)
    except ValueError:
        port_num = current_config.get("listen_port", 7683)

    new_config = {
        **current_config,
        "output_dir": new_output_dir,
        "hq_audio_only": hq_only,
        "default_file_types": file_types,
        "max_concurrent_downloads": max_concurrent_downloads,
        "proxy": proxy.strip(),
        "listen_host": listen_host.strip(),
        "listen_port": port_num
    }
    return save_config(new_config)


# 异步函数：获取信息
async def handle_get_info(rj_id: str) -> Tuple[List[List[Any]], str, str]:
    """
    处理“获取信息”按钮点击事件，获取文件列表并转换为 Dataframe 格式。
    """
    if not rj_id:
        return [], "❌ 错误: RJ ID 不能为空。", "无法获取信息"

    rj_id = rj_id.upper().strip().replace("RJ", "")
    full_rj_id = f"RJ{rj_id}"

    try:
        # 调用核心下载器逻辑
        files_info_dicts, title_or_error = await get_work_info_async(full_rj_id)

        if files_info_dicts:
            global download_progress_map
            # 存储文件名映射用于进度跟踪
            download_progress_map[full_rj_id] = {item['index']: item['filename'] for item in files_info_dicts}

            # 转换 List[Dict] 为 Gradio Dataframe 需要的 List[List] 格式
            data_for_dataframe = [
                [
                    item['index'],
                    item['filename'],
                    item['type'],
                    item['size_formatted'],
                    item['folder_path']
                ] for item in files_info_dicts
            ]

            return data_for_dataframe, "✅ 成功获取文件列表。", title_or_error
        else:
            return [], f"❌ 获取信息失败: {title_or_error}", "无法获取信息"

    except Exception as e:
        # 修正：确保在异步上下文中使用 await 调用异步日志函数
        await log_message(f"Critical error in handle_get_info for {full_rj_id}: {e}")
        return [], f"❌ 严重错误: {e}", "无法获取信息"


def format_progress_data(rj_id: str, filename: str, downloaded: int, total: int) -> Tuple[str, str, float]:
    """格式化进度数据，供 Gradio Markdown 和 Progress 使用"""
    if rj_id not in download_progress_map:
        # 如果下载中途 rj_id 不见了，使用默认值
        index = 0
    else:
        index_map = {v: k for k, v in download_progress_map[rj_id].items()}
        index = index_map.get(filename, 0)

    status = "RUNNING"
    progress_percent = 0.0

    if total > 0:
        progress_percent = (downloaded / total)

    if downloaded == 0 and total == 0:
        status = "PENDING"
    elif progress_percent >= 0.999:
        status = "COMPLETED"
        progress_percent = 1.0

    # 转换为 MB/GB
    def bytes_to_human(b):
        if b < 1024 * 1024: return f"{b / 1024:.2f} KB"
        if b < 1024 * 1024 * 1024: return f"{b / (1024 * 1024):.2f} MB"
        return f"{b / (1024 * 1024 * 1024):.2f} GB"

    status_str = f"文件 {index}: {filename[:40]}... [{status}]"

    # 使用 Markdown 格式增强显示
    progress_str = (
        f"**进度:** {progress_percent * 100:.2f}% | "
        f"**大小:** {bytes_to_human(downloaded)} / {bytes_to_human(total)}"
    )

    return status_str, progress_str, progress_percent


# 异步生成器函数：处理单个 RJ ID 下载任务 (实现实时更新)
async def handle_download(
        rj_id: str,
        selected_indices_json: str,
        progress: gr.Progress,  # Gradio 自动注入
) -> AsyncGenerator[gr.update, None]:
    """处理单个 RJ ID 下载任务，通过 yield 实时更新进度 Textbox"""

    if not rj_id:
        yield gr.update(value="❌ 错误: RJ ID 不能为空。")
        return

    rj_id = rj_id.upper().strip().replace("RJ", "")
    full_rj_id = f"RJ{rj_id}"

    try:
        selected_indices = json.loads(selected_indices_json)

        if not selected_indices:
            yield gr.update(value="⚠️ 没有文件被选中。请先获取文件列表。")
            return

        # 初始化显示
        yield gr.update(value=f"正在启动下载任务 (RJ{full_rj_id})...")

        # 用于存储所有文件的进度信息，方便统一显示
        current_file_progress: Dict[str, Tuple[str, str, float]] = {}
        total_files = len(selected_indices)

        def progress_callback(rj_id_local: str, filename: str, downloaded: int, total: int):
            """同步进度回调，用于更新内部状态"""
            status_str, progress_str, progress_percent = format_progress_data(
                rj_id_local, filename, downloaded, total
            )

            # 更新内部状态
            current_file_progress[filename] = (status_str, progress_str, progress_percent)

            # 更新 Gradio 顶部进度条 (全局进度条)
            # ❗ 修正：增加 callable() 检查，防止 progress 对象被 Gradio 回收后，后台线程继续调用它
            if progress and callable(progress):
                # 使用当前文件的进度百分比，让进度条波动起来
                progress(progress_percent, desc=f"文件下载中: {filename[:25]}... ({progress_percent * 100:.1f}%)")

            # 下载器中的 log_message_sync 已被调用，这里无需再次调用，避免警告
            pass

        # 启动下载任务，并将回调函数传入
        process_task = asyncio.create_task(
            process_download_job(full_rj_id, selected_indices, progress_callback)
        )

        # 实时更新循环：每 0.5 秒更新一次 Textbox
        while not process_task.done():
            # 构建当前的实时进度信息
            progress_output_lines = [f"**--- 任务状态 (RJ{full_rj_id}) ---**"]

            # 遍历当前正在下载/已完成的文件
            completed_count = 0
            for filename, (status_str, progress_str, progress_percent) in current_file_progress.items():
                # 实时更新行：显示文件名和进度
                progress_output_lines.append(f"- **{status_str}**\n   - {progress_str}")
                if progress_percent >= 0.999:
                    completed_count += 1

            progress_output_lines.insert(
                1,
                f"**总进度:** 已完成 **{completed_count}** / **{total_files}** 个文件"
            )

            # 使用 yield 实时更新前端 Markdown
            yield gr.update(value="\n".join(progress_output_lines))

            await asyncio.sleep(0.5)  # 0.5 秒刷新一次

        # 任务完成后，获取结果
        try:
            success = await process_task
        except Exception as e:
            # 修正：确保在异步上下文中使用 await 调用异步日志函数
            await log_message(f"Fatal error during download task: {e}")
            success = False

        if success:
            final_message = f"✅ **下载任务完成！** (RJ{full_rj_id})。所有 {total_files} 个文件已下载到：{load_config()['output_dir']}/{full_rj_id}"
            # 最终更新全局进度条到 100%
            # 确保 progress 存在且可调用
            if progress and callable(progress):
                progress(1.0, desc=f"下载完成: RJ{full_rj_id}")
        else:
            final_message = f"❌ **下载任务失败或未完全完成。** 详情请查看日志。"

        # 最终输出给 Markdown
        yield gr.update(value=final_message)

    except json.JSONDecodeError:
        yield gr.update(value="❌ 错误: 无法解析选中的文件索引。")
    except Exception as e:
        # 修正：确保在异步上下文中使用 await 调用异步日志函数
        await log_message(f"Fatal error in handle_download for {rj_id}: {e}")
        yield gr.update(value=f"❌ 严重错误: {e}")


# 异步函数：处理通用批量下载任务 (批量下载不使用生成器，仅依赖全局进度条)
async def handle_bulk_download(rj_ids_json: str, progress: gr.Progress) -> str:
    """处理搜索结果列表的批量下载任务 (作品顺序下载)"""
    try:
        rj_ids = json.loads(rj_ids_json)
    except json.JSONDecodeError:
        return "❌ 错误：无法解析 RJ ID 列表。"

    if not rj_ids:
        return "❌ 错误：搜索结果中没有 RJ ID。请先进行搜索。"

    total_works = len(rj_ids)

    # Gradio Progress 回调函数
    def overall_progress_callback(current_work_index: int, total_works: int, status_message: str):
        """整体进度回调，更新 Gradio 进度条"""
        if total_works > 0:
            # 进度条显示总任务的完成度
            percent = (current_work_index / total_works) * 0.999
        else:
            percent = 0.0

        # 修正：增加 callable() 检查
        if progress and callable(progress):
            progress(percent, desc=f"批量下载进度: {status_message}")

    try:
        # 调用核心下载器逻辑
        success, final_message = await process_bulk_download_job(rj_ids, overall_progress_callback)

        # 修正：增加 callable() 检查
        if success and progress and callable(progress):
            progress(1.0, desc=f"批量下载进度: {final_message}")
            return f"✅ **批量下载任务完成！** {final_message}"
        else:
            # 确保即使失败也更新进度条
            if progress and callable(progress):
                progress(total_works / total_works * 0.999, desc=f"批量下载进度: {final_message}")
            return f"❌ **批量下载任务未完全成功：** {final_message}"

    except Exception as e:
        # 修正：确保在异步上下文中使用 await 调用异步日志函数
        await log_message(f"Fatal error in handle_bulk_download: {e}")
        return f"❌ 严重错误：{e}"


async def handle_search(keyword: str, page: str, size: str) -> Tuple[List[List[Any]], str]:
    """处理关键词搜索"""
    if not keyword:
        return [], "请输入关键词进行搜索。"

    try:
        page_num = int(page)
        size_num = int(size)
    except ValueError:
        return [], "页码和每页数量必须是数字。"

    # 修正：确保在异步上下文中使用 await 调用异步日志函数
    await log_message(f"Handling search for '{keyword}' on page {page_num}, size {size_num}")

    try:
        results_dicts, total_pages = await search_work_async(keyword, page_num, size_num)

        if not results_dicts:
            return [], f"❌ 未找到关键词 '{keyword}' 的相关作品。"

        data_for_dataframe = [
            [
                item['rj_id'],
                item['title'],
                item['author'],
                item['total_tracks']
            ] for item in results_dicts
        ]

        status_msg = f"✅ 搜索成功！找到 {len(results_dicts)} 个结果。总页数: {total_pages}。"
        return data_for_dataframe, status_msg

    except Exception as e:
        return [], f"❌ 搜索失败: {e}"


def extract_rj_id_from_selection_event(evt: gr.SelectData, search_data: List[List[Any]]) -> str:
    """提取 RJ ID"""
    if evt.index:
        row_index = evt.index[0]

        if 0 <= row_index < len(search_data):
            return search_data[row_index][0]
    return ""


def get_latest_log() -> str:
    """用于刷新日志的回调函数"""
    return read_log_sync(lines=200)


# --- Gradio UI Definition ---

def create_ui():
    current_config = load_current_config()
    default_proxy = current_config.get("proxy", "")
    default_host = current_config.get("listen_host", "127.0.0.1")
    # 使用配置中定义的端口 7683
    default_port = str(current_config.get("listen_port", 7683))
    max_concurrents = current_config.get("max_concurrent_downloads", 3)

    with gr.Blocks(title="ASMR Downloader WebUI", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# 🎧 ASMR Downloader Web UI")

        # --- 1. 配置区域 ---
        with gr.Accordion("⚙️ 配置 (Config)", open=False):
            with gr.Row():
                config_output_dir = gr.Textbox(
                    label="下载输出目录 (Output Directory)",
                    value=current_config["output_dir"],
                    placeholder="例如: C:/ASMR_Downloads",
                    scale=2
                )
                config_max_concurrent = gr.Slider(
                    label="单作品最大并发下载数",
                    minimum=1,
                    maximum=10,
                    step=1,
                    value=max_concurrents,
                    scale=1
                )

            config_proxy = gr.Textbox(
                label="下载代理 (Proxy)",
                value=default_proxy,
                placeholder="例如: http://127.0.0.1:1080 或留空 (不使用代理)",
            )

            with gr.Row():
                config_listen_host = gr.Textbox(
                    label="Web UI 监听地址 (Host)",
                    value=default_host,
                    placeholder="例如: 0.0.0.0 (公网访问) 或 127.0.0.1 (本地访问)",
                    scale=1
                )
                config_listen_port = gr.Textbox(
                    label="Web UI 监听端口 (Port)",
                    value=default_port,
                    placeholder="例如: 7683",
                    scale=1
                )

            config_hq_only = gr.Checkbox(
                label="只下载 HQ 音频 (FLAC/WAV/MP3)",
                value=current_config.get("hq_audio_only", False)
            )
            config_file_types = gr.CheckboxGroup(
                label="默认文件类型 (Default File Types)",
                choices=["audio", "image", "text"],
                value=current_config.get("default_file_types", ["audio", "image", "text"])
            )
            config_save_status = gr.Markdown("⚠️ 修改配置后需点击保存，并建议重启 Web UI。")
            config_save_btn = gr.Button("💾 保存配置并更新目录显示", variant="primary")

            config_save_btn.click(
                update_config_ui,
                inputs=[
                    config_output_dir,
                    config_hq_only,
                    config_file_types,
                    config_max_concurrent,
                    config_proxy,
                    config_listen_host,
                    config_listen_port
                ],
                outputs=[config_save_status, config_output_dir],
                queue=False
            )

        gr.Markdown("---")

        # --- 3. RJ ID 下载区域 ---
        with gr.Tab("💾 RJ ID 下载", elem_id="download_tab_button"):
            gr.Markdown("### 作品文件下载 (自动全选)")

            with gr.Row():
                rj_id_input = gr.Textbox(
                    label="RJ ID",
                    placeholder="请输入 RJ 号 (例如: RJ01396119)",
                    scale=3,
                    elem_id="rj_id_input"
                )
                get_info_btn = gr.Button("🔍 获取文件信息", variant="primary", scale=1)

            rj_title = gr.Textbox(label="作品标题", interactive=False, value="等待输入...", elem_id="rj_title")
            status_message = gr.Markdown("状态信息：准备就绪。", elem_id="status_message")

            selected_indices_state = gr.State(value="[]")

            file_list_table = gr.Dataframe(
                headers=["Index", "Filename", "Type", "Size", "Folder Path"],
                datatype=["number", "str", "str", "str", "str"],
                label="可下载文件列表 (点击获取信息后，所有文件自动被选中)",
                col_count=(5, "fixed"),
                interactive=False,
                type="array",
                elem_id="file_list_table"
            )

            # 获取信息按钮事件
            get_info_btn.click(
                handle_get_info,
                inputs=[rj_id_input],
                outputs=[file_list_table, status_message, rj_title]
            ).success(
                lambda data: json.dumps([item[0] for item in data]),
                inputs=[file_list_table],
                outputs=[selected_indices_state],
                queue=False
            )

            # 下载控制和进度
            download_btn = gr.Button("🚀 开始下载全部文件", variant="stop")

            # 实时进度 Markdown，使用 Markdown 格式
            download_progress = gr.Markdown(
                label="下载进度/最终状态 (实时进度显示)",
                value="等待下载任务启动..."
            )

            # 关键：使用生成器函数，通过 yield 实时更新 download_progress
            download_btn.click(
                handle_download,
                inputs=[rj_id_input, selected_indices_state],
                outputs=[download_progress]
            )

        # --- 2. 搜索区域 (集成批量下载) ---
        with gr.Tab("🔍 关键词搜索"):
            gr.Markdown("### 关键词搜索作品")
            with gr.Row():
                search_keyword = gr.Textbox(
                    label="关键词/标签",
                    placeholder="请输入关键词，例如：耳语/催眠",
                    scale=3
                )
                search_page = gr.Textbox(
                    label="页码",
                    value="1",
                    scale=1
                )
                search_size = gr.Textbox(
                    label="每页数量",
                    value="20",
                    scale=1
                )
                search_btn = gr.Button("🔎 搜索作品", variant="secondary", scale=1)

            search_status_message = gr.Markdown("状态：等待搜索...")

            all_rj_ids_state = gr.State(value="[]")

            search_result_table = gr.Dataframe(
                headers=["RJ ID", "作品标题", "作者", "音轨数"],
                datatype=["str", "str", "str", "number"],
                label="搜索结果 (点击一行可将 RJ ID 自动填充到下载区)",
                col_count=(4, "fixed"),
                interactive=False,
                type="array",
                elem_id="search_result_table"
            )

            with gr.Row():
                list_count_display = gr.Textbox(
                    label="当前列表作品数",
                    interactive=False,
                    value="0",
                    scale=1
                )
                bulk_download_btn = gr.Button(
                    "⬇️ 批量下载列表中所有作品 (按顺序)",
                    variant="primary",
                    scale=2
                )

            bulk_download_status = gr.Markdown("批量下载状态：未启动")

            # 搜索按钮事件：执行搜索 -> 填充表格 -> 提取所有 RJ ID -> 更新列表作品数
            search_btn.click(
                handle_search,
                inputs=[search_keyword, search_page, search_size],
                outputs=[search_result_table, search_status_message]
            ).success(
                lambda data: json.dumps([item[0] for item in data]),
                inputs=[search_result_table],
                outputs=[all_rj_ids_state],
                queue=False
            ).success(
                lambda rj_ids_json: str(len(json.loads(rj_ids_json))),
                inputs=[all_rj_ids_state],
                outputs=[list_count_display],
                queue=False
            )

            # 搜索结果点击事件 (联动到下载区)
            search_result_table.select(
                extract_rj_id_from_selection_event,
                inputs=[search_result_table],
                outputs=[rj_id_input],
                queue=False
            ).success(
                handle_get_info,
                inputs=[rj_id_input],
                outputs=[
                    file_list_table,
                    status_message,
                    rj_title
                ]
            ).success(
                lambda data: json.dumps([item[0] for item in data]),
                inputs=[file_list_table],
                outputs=[selected_indices_state],
                queue=False
            )

            # 批量下载按钮点击事件
            bulk_download_btn.click(
                handle_bulk_download,
                inputs=[all_rj_ids_state],
                outputs=[bulk_download_status]
            )

        # --- 4. 日志区域 ---
        with gr.Accordion("📝 日志 (Log)", open=True):
            log_output = gr.Textbox(
                label="下载日志 (download_log.txt - 倒序，需手动刷新)",
                lines=15,
                value=read_log_sync(),
                interactive=False
            )
            refresh_log_btn = gr.Button("🔄 刷新日志", variant="secondary")

            # 保持手动刷新按钮的连接
            refresh_log_btn.click(
                get_latest_log,
                inputs=[],
                outputs=[log_output],
                queue=False
            )

    return demo


if __name__ == "__main__":
    ui = create_ui()
    config = load_config()
    host = config.get("listen_host", "127.0.0.1")
    port = config.get("listen_port", 7683)

    try:
        port = int(port)
    except ValueError:
        print(f"⚠️ 警告: 配置中的端口号 '{config.get('listen_port')}' 无效，使用默认端口 7683。")
        port = 7683

    print(f"🚀 正在启动 Web UI，监听地址: {host}:{port}")

    # Gradio 的 launch() 调用会阻塞程序
    ui.launch(server_name=host, server_port=port, inbrowser=True, show_api=False)

    print("Web UI 服务器已正常关闭。")