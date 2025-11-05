import gradio as gr
import json
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Tuple

# 假设 core.downloader 模块可用
from core.downloader import (
    get_work_info_async,
    process_download_job,
    load_config,
    read_log_sync,
    log_message,
    search_work_async,
    # 导入批量下载函数
    process_bulk_download_job
)

# --- Configuration & Helpers ---
CONFIG_FILE = Path("config.json")
# 存储当前作品文件索引和文件名的映射，用于进度跟踪
download_progress_map: Dict[str, Dict[int, str]] = {}


def load_current_config():
    """加载配置并处理目录显示"""
    current_config = load_config()
    # 确保保存配置时返回正确的 output_dir，而不是加载时的
    return current_config


def save_config(config: dict):
    """同步保存配置并返回状态"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        # 重新加载以确保返回的是最新的配置值
        current_config = load_config()
        # 返回配置状态和 output_dir（因为它是界面上唯一需要即时更新的配置文本框）
        return "✅ 配置保存成功！请重新启动 Web UI 使部分配置生效。", current_config["output_dir"]
    except Exception as e:
        return f"❌ 配置保存失败: {e}", config["output_dir"]


def update_config_ui(
        output_dir: str,
        hq_only: bool,
        file_types: List[str],
        max_concurrent_downloads: int,
        proxy: str,  # ❗ 新增：代理
        listen_host: str,  # ❗ 新增：监听地址
        listen_port: str  # ❗ 新增：监听端口 (UI 传入的是字符串)
):
    """处理 UI 配置更新逻辑"""
    current_config = load_config()

    new_output_dir = str(Path(output_dir).resolve())

    # 尝试将端口转换为整数，如果失败则保持原值或默认值
    try:
        port_num = int(listen_port)
    except ValueError:
        port_num = current_config.get("listen_port", 7860)

    new_config = {
        **current_config,
        "output_dir": new_output_dir,
        "hq_audio_only": hq_only,
        "default_file_types": file_types,
        "max_concurrent_downloads": max_concurrent_downloads,
        "proxy": proxy.strip(),  # ❗ 新增：保存代理
        "listen_host": listen_host.strip(),  # ❗ 新增：保存监听地址
        "listen_port": port_num  # ❗ 新增：保存监听端口
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
        files_info_dicts, title_or_error = await get_work_info_async(full_rj_id)

        if files_info_dicts:
            global download_progress_map
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
        return [], f"❌ 严重错误: {e}", "无法获取信息"


def format_progress_data(rj_id: str, filename: str, downloaded: int, total: int) -> Tuple[str, str, float]:
    """格式化进度数据，供 Gradio Markdown 和 Progress 使用"""
    if rj_id not in download_progress_map:
        return f"RJ ID {rj_id} 错误", "", 0.0

    status = "RUNNING"
    progress_percent = 0.0

    if total > 0:
        progress_percent = (downloaded / total)

    if downloaded == 0 and total == 0:
        status = "PENDING"
    elif progress_percent >= 0.999:
        status = "COMPLETED"
        progress_percent = 1.0

    index_map = {v: k for k, v in download_progress_map[rj_id].items()}
    index = index_map.get(filename, 0)

    status_str = f"[{rj_id}] 文件 {index} - {filename[:40]}..."

    # 转换为 MB/GB
    def bytes_to_human(b):
        if b < 1024 * 1024: return f"{b / 1024:.2f} KB"
        if b < 1024 * 1024 * 1024: return f"{b / (1024 * 1024):.2f} MB"
        return f"{b / (1024 * 1024 * 1024):.2f} GB"

    progress_str = (
        f"**{status}** | "
        f"{bytes_to_human(downloaded)} / {bytes_to_human(total)} | "
        f"{progress_percent * 100:.2f}%"
    )

    return status_str, progress_str, progress_percent


# 异步函数：处理单个 RJ ID 下载任务
async def handle_download(
        rj_id: str,
        selected_indices_json: str,
        progress: gr.Progress  # Gradio 自动注入
) -> str:
    """处理单个 RJ ID 下载任务"""
    if not rj_id:
        return "❌ 错误: RJ ID 不能为空。"

    rj_id = rj_id.upper().strip().replace("RJ", "")
    full_rj_id = f"RJ{rj_id}"

    try:
        selected_indices = json.loads(selected_indices_json)

        if not selected_indices:
            return "⚠️ 没有文件被选中。请先获取文件列表。"

        def progress_callback(rj_id_local: str, filename: str, downloaded: int, total: int):
            """同步进度回调"""
            status_str, progress_str, progress_percent = format_progress_data(
                rj_id_local, filename, downloaded, total
            )

            if progress:
                progress(progress_percent, desc=f"{status_str} | {progress_str}")

        success = await process_download_job(full_rj_id, selected_indices, progress_callback)

        if success:
            return f"✅ **下载任务完成！** (RJ{rj_id})。请查看目录：{load_config()['output_dir']}/{full_rj_id}"
        else:
            return f"❌ **下载任务失败或未完全完成。** 详情请查看日志。"

    except json.JSONDecodeError:
        return "❌ 错误: 无法解析选中的文件索引。"
    except Exception as e:
        await log_message(f"Fatal error in handle_download for {rj_id}: {e}")
        return f"❌ 严重错误: {e}"


# 异步函数：处理通用批量下载任务
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
        # 进度条显示的是作品的完成度
        if total_works > 0:
            # 乘以 0.999 是为了防止 Gradio 进度条在主任务完成前跳到 1.0
            percent = (current_work_index / total_works) * 0.999
        else:
            percent = 0.0

        if progress:
            # 使用 status_message 作为描述
            progress(percent, desc=f"批量下载进度: {status_message}")

    try:
        # 调用核心下载逻辑 (downloader.py)
        success, final_message = await process_bulk_download_job(rj_ids, overall_progress_callback)

        # 最终更新进度条到 100%
        if success:
            progress(1.0, desc=f"批量下载进度: {final_message}")
        else:
            # 如果未完全成功，也更新到最新的进度
            progress(total_works / total_works * 0.999, desc=f"批量下载进度: {final_message}")

        if success:
            return f"✅ **批量下载任务完成！** {final_message}"
        else:
            return f"❌ **批量下载任务未完全成功：** {final_message}"

    except Exception as e:
        await log_message(f"Fatal error in handle_bulk_download: {e}")
        return f"❌ 严重错误：{e}"


# ❗ 修改：handle_search 接收 size 参数
async def handle_search(keyword: str, page: str, size: str) -> Tuple[List[List[Any]], str]:
    """处理关键词搜索"""
    if not keyword:
        return [], "请输入关键词进行搜索。"

    try:
        page_num = int(page)
        size_num = int(size)  # ❗ 新增：转换 size 为数字
    except ValueError:
        return [], "页码和每页数量必须是数字。"

    await log_message(f"Handling search for '{keyword}', page {page_num}, size {size_num}")  # 记录 size

    try:
        # ❗ 修改：传入 size_num
        results_dicts, total_pages = await search_work_async(keyword, page_num, size_num)

        if not results_dicts:
            return [], f"❌ 未找到关键词 '{keyword}' 的相关作品。"

        # 转换 List[Dict] 为 Gradio Dataframe 需要的 List[List] 格式
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


# ------------------------------------------------------------------
# 辅助函数：从搜索结果中提取 RJ ID
# ------------------------------------------------------------------
def extract_rj_id_from_selection_event(evt: gr.SelectData, search_data: List[List[Any]]) -> str:
    """
    接收标准的 SelectData 事件对象和表格数据，提取 RJ ID。
    """
    if evt.index:
        row_index = evt.index[0]

        if 0 <= row_index < len(search_data):
            return search_data[row_index][0]  # RJ ID 是第一列（索引 0）
    return ""


# --- Gradio UI Definition ---

def create_ui():
    current_config = load_current_config()
    # ❗ 获取新的配置默认值
    default_proxy = current_config.get("proxy", "")
    default_host = current_config.get("listen_host", "127.0.0.1")
    default_port = str(current_config.get("listen_port", 7860))

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
                    value=current_config.get("max_concurrent_downloads", 3),
                    scale=1
                )

            # ❗ 新增：代理配置
            config_proxy = gr.Textbox(
                label="下载代理 (Proxy)",
                value=default_proxy,
                placeholder="例如: http://127.0.0.1:1080 或留空 (不使用代理)",
            )

            with gr.Row():
                # ❗ 新增：Web UI 地址配置
                config_listen_host = gr.Textbox(
                    label="Web UI 监听地址 (Host)",
                    value=default_host,
                    placeholder="例如: 0.0.0.0 (公网访问) 或 127.0.0.1 (本地访问)",
                    scale=1
                )
                # ❗ 新增：Web UI 端口配置
                config_listen_port = gr.Textbox(
                    label="Web UI 监听端口 (Port)",
                    value=default_port,
                    placeholder="例如: 7860",
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
                # ❗ 修改：添加新的配置输入
                inputs=[
                    config_output_dir,
                    config_hq_only,
                    config_file_types,
                    config_max_concurrent,
                    config_proxy,  # 新增
                    config_listen_host,  # 新增
                    config_listen_port  # 新增
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

            # 状态变量：用于存储所有文件的 Index (实现自动全选)
            selected_indices_state = gr.State(value="[]")

            file_list_table = gr.Dataframe(
                headers=["Index", "Filename", "Type", "Size", "Folder Path"],
                datatype=["number", "str", "str", "str", "str"],
                label="可下载文件列表 (点击获取信息后，所有文件自动被选中)",
                col_count=(5, "fixed"),
                interactive=False,  # 禁用交互，避免用户手动选择
                type="array",
                elem_id="file_list_table"
            )

            # 获取信息按钮事件
            get_info_btn.click(
                handle_get_info,
                inputs=[rj_id_input],
                outputs=[file_list_table, status_message, rj_title]
            ).success(
                # 核心逻辑：获取信息成功后，自动将表格中的所有 Index 写入 state 变量
                lambda data: json.dumps([item[0] for item in data]),
                inputs=[file_list_table],
                outputs=[selected_indices_state],
                queue=False
            )

            # 下载控制和进度
            download_btn = gr.Button("🚀 开始下载全部文件", variant="stop")
            download_progress = gr.Markdown("等待下载任务启动...")

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
                # ❗ 新增：每页数量输入框
                search_size = gr.Textbox(
                    label="每页数量",
                    value="20",
                    scale=1
                )
                search_btn = gr.Button("🔎 搜索作品", variant="secondary", scale=1)

            search_status_message = gr.Markdown("状态：等待搜索...")

            # 状态变量，存储搜索结果中的所有 RJ ID 列表
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

            # 批量下载区域
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
                # ❗ 修改：传入 search_size
                inputs=[search_keyword, search_page, search_size],
                outputs=[search_result_table, search_status_message]
            ).success(
                # 1. 提取所有 RJ ID (第一列) 并存储到状态变量
                lambda data: json.dumps([item[0] for item in data]),
                inputs=[search_result_table],
                outputs=[all_rj_ids_state],
                queue=False
            ).success(
                # 2. 更新列表作品数显示
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
                # 自动触发获取信息
                handle_get_info,
                inputs=[rj_id_input],
                outputs=[
                    file_list_table,
                    status_message,
                    rj_title
                ]
            ).success(
                # 自动全选
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
                label="下载日志 (download_log.txt - 倒序)",
                lines=15,
                interactive=False,
                value=read_log_sync()
            )
            refresh_log_btn = gr.Button("🔄 刷新日志")

            refresh_log_btn.click(
                lambda: read_log_sync(lines=200),
                inputs=[],
                outputs=[log_output],
                queue=False
            )

    return demo


if __name__ == "__main__":
    ui = create_ui()
    # ❗ 修改：从配置中获取 host 和 port
    config = load_config()
    host = config.get("listen_host", "127.0.0.1")
    port = config.get("listen_port", 7860)

    # 确保 port 是整数
    try:
        port = int(port)
    except ValueError:
        print(f"⚠️ 警告: 配置中的端口号 '{config.get('listen_port')}' 无效，使用默认端口 7860。")
        port = 7860

    print(f"🚀 正在启动 Web UI，监听地址: {host}:{port}")
    ui.launch(server_name=host, server_port=port, inbrowser=True, show_api=False)