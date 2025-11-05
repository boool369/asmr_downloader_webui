import asyncio
import sys
from typing import Dict, Any, List
from core.downloader import load_config, get_work_info_async, process_download_job
from pathlib import Path

# --- 请替换为您想要测试的实际 RJ ID ---
# ❗️ 警告：请将此处的 RJ 号替换为您想要测试的实际 RJ 号！
TEST_RJ_ID = "RJ01396119"  # <<<<<<<< 请修改此处为实际的 RJ ID


def print_progress(rj_id: str, filename: str, downloaded: int, total: int):
    """一个简单的同步进度回调函数，用于控制台输出"""
    if total > 0:
        percent = (downloaded / total) * 100
        total_mb = total / (1024 * 1024)
        downloaded_mb = downloaded / (1024 * 1024)
        # 实时打印进度，使用 \r 回到行首
        sys.stdout.write(
            f"\r[Download] {rj_id}: {filename[:30]}... "
            f"({downloaded_mb:.2f} MB / {total_mb:.2f} MB) {percent:.2f}%"
        )
        sys.stdout.flush()
    elif downloaded > 0 and total == 0:
        # 下载完成，换行
        print(f"\n[Finished] {rj_id}: {filename}")
    else:
        print(f"[Starting] {rj_id}: {filename}")


async def run_test():
    print(f"--- 🚀 核心下载功能测试启动：{TEST_RJ_ID} ---")

    # 1. 加载配置 (用于确定下载路径)
    config = load_config()
    output_dir = Path(config["output_dir"])
    print(f"下载目录设置为: {output_dir}")

    # 2. 获取文件信息
    print("正在获取文件信息...")
    files_info_dicts, title_or_error = await get_work_info_async(TEST_RJ_ID)

    if not files_info_dicts:
        print(f"❌ 失败: 无法获取 RJ ID {TEST_RJ_ID} 的文件信息。错误: {title_or_error}")
        return

    print(f"✅ 成功获取信息。作品标题: {title_or_error}")
    print(f"共找到 {len(files_info_dicts)} 个文件。")

    # 3. 选择所有文件
    selected_indices = [item['index'] for item in files_info_dicts]
    print(f"将尝试下载所有 {len(selected_indices)} 个文件。索引: {selected_indices}")

    # 4. 执行下载任务
    print("--- 📥 开始下载任务 ---")
    success = await process_download_job(
        TEST_RJ_ID,
        selected_indices,
        print_progress
    )

    if success:
        print(f"\n--- ✅ 下载任务 {TEST_RJ_ID} 成功完成！文件已保存到 {output_dir} ---")
    else:
        print(f"\n--- ❌ 下载任务 {TEST_RJ_ID} 失败或未完全完成。详情请查看 download_log.txt ---")


if __name__ == "__main__":
    if TEST_RJ_ID == "RJ01234567":
        print("⚠️ 警告：请将 test_download.py 中的 TEST_RJ_ID 替换为您想测试的实际 RJ 号码！")
        sys.exit(1)

    try:
        # 使用 asyncio.run 运行主异步函数
        asyncio.run(run_test())
    except KeyboardInterrupt:
        print("\n程序被用户中断。")
    except Exception as e:
        print(f"\n发生未预期错误: {e}")