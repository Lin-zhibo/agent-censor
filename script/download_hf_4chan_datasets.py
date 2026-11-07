from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

SCRIPT_VERSION = "hf-4chan-downloader/1.0.0"

DATASET_REGISTRY = {
    "epguy-conversational": {
        "hf_id": "EpGuy/4chan-archive-conversational",
        "description": "4chan 对话格式数据集 (input-output pairs)",
        "config": None,
        "split": "train",
        "columns": ["input", "output"],
        "size_hint": "10K–100K 条",
    },
    "vmfunc-pol": {
        "hf_id": "vmfunc/4chan-pol-extensive",
        "description": "4chan /pol/ 板结构化数据集 (丰富特征)",
        "config": None,
        "split": "train",
        "columns": [
            "id", "thread_id", "board", "timestamp", "title", "text",
            "text_length", "filename", "file_ext", "file_size",
            "image_width", "image_height", "is_op", "mentions",
            "mention_count", "replies", "images", "unique_ips",
            "content_hash", "archived", "semantic_url",
            "hour_of_day", "day_of_week", "is_weekend",
            "post_count", "total_images", "avg_text_length",
            "std_text_length", "total_mentions",
        ],
        "size_hint": "~1.24 亿条帖子 / 12,000+ 线程",
    },
}


@dataclass
class DownloadConfig:
    dataset_key: str
    output_dir: Path = Path("data/4chan")
    state_path: Path = Path("data/4chan/download_state.json")
    streaming: bool = True
    max_rows: int = 0
    batch_size: int = 10_000
    retries: int = 3
    backoff_base_sec: float = 5.0
    timeout_sec: float = 120.0
    resume: bool = True
    verify_checksum: bool = True


@dataclass(frozen=True)
class DownloadStats:
    dataset_key: str
    hf_id: str
    total_rows: int
    written_rows: int
    output_path: Path
    checksum: str = ""
    elapsed_sec: float = 0.0
    error: str = ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 Hugging Face Hub 下载 4chan 数据集。"
    )
    parser.add_argument(
        "dataset",
        nargs="?",
        choices=list(DATASET_REGISTRY) + ["all"],
        default="all",
        help="要下载的数据集（默认 all 下载全部）。",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/4chan"),
        help="输出目录。",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("data/4chan/download_state.json"),
        help="断点续传状态文件路径。",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="最多下载 N 行（0=不限制）。",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10_000,
        help="每批次写入的行数。",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="网络失败重试次数。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="网络超时秒数。",
    )
    parser.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        default=True,
        help="忽略已有文件，从头下载。",
    )
    parser.add_argument(
        "--no-streaming",
        dest="streaming",
        action="store_false",
        default=True,
        help="关闭流式模式（整表加载到内存）。",
    )
    parser.add_argument(
        "--no-verify",
        dest="verify_checksum",
        action="store_false",
        default=True,
        help="跳过 SHA256 校验。",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def build_config(args: argparse.Namespace) -> DownloadConfig:
    if args.batch_size < 1:
        raise ValueError("--batch-size 必须为正整数")
    if args.max_rows < 0:
        raise ValueError("--max-rows 不能为负")
    return DownloadConfig(
        dataset_key=args.dataset,
        output_dir=args.out_dir,
        state_path=args.state,
        streaming=args.streaming,
        max_rows=args.max_rows,
        batch_size=args.batch_size,
        retries=args.retries,
        timeout_sec=args.timeout,
        resume=args.resume,
        verify_checksum=args.verify_checksum,
    )


def _try_import_datasets() -> bool:
    try:
        import datasets  # noqa: F401
        return True
    except ImportError:
        return False


def _install_guide() -> str:
    return (
        "HuggingFace datasets 库未安装。请运行:\n"
        "  pip install datasets pyarrow\n"
        "或:\n"
        "  uv pip install datasets pyarrow"
    )


def download_dataset(
    config: DownloadConfig,
    registry_entry: dict,
    now: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> DownloadStats:
    """下载单个 HuggingFace 数据集到本地 Parquet 文件。"""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from datasets import load_dataset

    now = now or (lambda: datetime.now(timezone.utc))
    hf_id: str = registry_entry["hf_id"]
    split: str = registry_entry.get("split", "train")
    description: str = registry_entry.get("description", hf_id)

    logging.info("开始下载: %s (%s)", hf_id, description)
    t0 = time.monotonic()

    dataset_dir = config.output_dir / config.dataset_key
    dataset_dir.mkdir(parents=True, exist_ok=True)

    output_path = dataset_dir / "data.parquet"
    checksum_path = dataset_dir / "checksum.sha256"
    meta_path = dataset_dir / "metadata.json"

    existing_rows = 0
    if config.resume and output_path.exists():
        try:
            existing_table = pq.read_table(str(output_path))
            existing_rows = existing_table.num_rows
            logging.info("已存在 %d 行，从第 %d 行继续。", existing_rows, existing_rows)
        except Exception as exc:
            logging.warning("无法读取已有文件，重新下载: %s", exc)

    if config.max_rows and config.max_rows <= existing_rows:
        logging.info("--max-rows=%d 已达到 (%d 行已存在)，跳过。", config.max_rows, existing_rows)
        elapsed = time.monotonic() - t0
        checksum = sha256_file(output_path) if output_path.exists() else ""
        return DownloadStats(
            dataset_key=config.dataset_key,
            hf_id=hf_id,
            total_rows=existing_rows,
            written_rows=existing_rows,
            output_path=output_path,
            checksum=checksum,
            elapsed_sec=elapsed,
        )

    dataset = None
    last_error = ""
    for attempt in range(config.retries + 1):
        try:
            logging.info("加载数据集 (streaming=%s, 尝试 %d/%d) ...",
                         config.streaming, attempt + 1, config.retries + 1)
            dataset = load_dataset(
                hf_id,
                split=split,
                streaming=config.streaming,
                trust_remote_code=True,
            )
            break
        except Exception as exc:
            last_error = str(exc)
            logging.warning("加载失败: %s", exc)
            if attempt < config.retries:
                delay = config.backoff_base_sec * (2 ** attempt)
                logging.info("等待 %.1f 秒后重试...", delay)
                sleeper(delay)

    if dataset is None:
        elapsed = time.monotonic() - t0
        return DownloadStats(
            dataset_key=config.dataset_key, hf_id=hf_id,
            total_rows=0, written_rows=0, output_path=output_path,
            elapsed_sec=elapsed,
            error=f"加载失败 (重试 {config.retries} 次): {last_error}",
        )

    rows_buffer: list[dict] = []
    written_total = existing_rows

    iterable = dataset
    if existing_rows > 0:
        iterable = dataset.skip(existing_rows)

    try:
        for row in iterable:
            rows_buffer.append(dict(row))

            if len(rows_buffer) >= config.batch_size:
                table = pa.Table.from_pylist(rows_buffer)
                _write_or_append(output_path, table, append=(written_total > 0))
                written_total += len(rows_buffer)
                logging.info("已写入 %d 行", written_total)
                rows_buffer.clear()

            if config.max_rows and written_total >= config.max_rows:
                logging.info("已达到 --max-rows=%d，停止。", config.max_rows)
                break
    except Exception as exc:
        logging.error("下载中断: %s", exc)
        elapsed = time.monotonic() - t0
        return DownloadStats(
            dataset_key=config.dataset_key, hf_id=hf_id,
            total_rows=written_total + len(rows_buffer),
            written_rows=written_total, output_path=output_path,
            elapsed_sec=elapsed, error=str(exc),
        )

    if rows_buffer:
        table = pa.Table.from_pylist(rows_buffer)
        _write_or_append(output_path, table, append=(written_total > 0))
        written_total += len(rows_buffer)
        logging.info("写入最后一批 %d 行 (累计 %d 行)", len(rows_buffer), written_total)

    elapsed = time.monotonic() - t0

    checksum = ""
    if config.verify_checksum and output_path.exists():
        checksum = sha256_file(output_path)
        checksum_path.write_text(f"{checksum}  data.parquet\n", encoding="utf-8")
        logging.info("SHA256: %s", checksum)

    features = getattr(dataset, "features", None)
    actual_columns = list(features.keys()) if features is not None else []
    metadata = {
        "script_version": SCRIPT_VERSION,
        "hf_id": hf_id,
        "dataset_key": config.dataset_key,
        "description": description,
        "split": split,
        "downloaded_at": now().isoformat(),
        "total_rows": written_total,
        "checksum_sha256": checksum,
        "columns": actual_columns,
        "streaming": config.streaming,
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    return DownloadStats(
        dataset_key=config.dataset_key, hf_id=hf_id,
        total_rows=written_total, written_rows=written_total,
        output_path=output_path, checksum=checksum, elapsed_sec=elapsed,
    )


def _write_or_append(path: Path, table: "pa.Table", append: bool) -> None:  # noqa: F821
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not append or not path.exists():
        pq.write_table(table, str(path))
        return

    existing = pq.read_table(str(path))
    existing_cols = set(existing.column_names)
    new_cols = set(table.column_names)
    for col in new_cols - existing_cols:
        ctype = table.schema.field(col).type
        existing = existing.append_column(
            pa.field(col, ctype), pa.nulls(existing.num_rows, type=ctype)
        )
    for col in existing_cols - new_cols:
        ctype = existing.schema.field(col).type
        table = table.append_column(
            pa.field(col, ctype), pa.nulls(table.num_rows, type=ctype)
        )
    existing = existing.select(table.column_names)
    combined = pa.concat_tables([existing, table])
    pq.write_table(combined, str(path))


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logging.warning("状态文件损坏 %s: %s", state_path, exc)
        return {}


def save_state(state_path: Path, state: dict, now: Callable[[], datetime]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state["script_version"] = SCRIPT_VERSION
    state["updated_at"] = now().isoformat()
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not _try_import_datasets():
        logging.error(_install_guide())
        return 1

    try:
        config = build_config(args)
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    keys: list[str]
    if config.dataset_key == "all":
        keys = list(DATASET_REGISTRY)
    else:
        keys = [config.dataset_key]

    state: dict = {} if not config.resume else load_state(config.state_path)
    now = lambda: datetime.now(timezone.utc)
    all_ok = True

    for key in keys:
        entry = DATASET_REGISTRY[key]
        sub_config = DownloadConfig(
            dataset_key=key,
            output_dir=config.output_dir,
            state_path=config.state_path,
            streaming=config.streaming,
            max_rows=config.max_rows,
            batch_size=config.batch_size,
            retries=config.retries,
            timeout_sec=config.timeout_sec,
            resume=config.resume,
            verify_checksum=config.verify_checksum,
        )

        stats = download_dataset(sub_config, entry, now=now)
        state[f"download_{key}"] = {
            "hf_id": stats.hf_id,
            "total_rows": stats.total_rows,
            "written_rows": stats.written_rows,
            "checksum": stats.checksum,
            "elapsed_sec": stats.elapsed_sec,
            "error": stats.error,
            "output": str(stats.output_path),
        }

        if stats.error:
            logging.error("[%s] 下载失败: %s", key, stats.error)
            all_ok = False
        else:
            logging.info(
                "[%s] 下载完成: %d 行 → %s (%.1f 秒)",
                key, stats.written_rows, stats.output_path, stats.elapsed_sec,
            )

    save_state(config.state_path, state, now)

    summary = {
        k.replace("download_", ""): {
            "rows": v["written_rows"], "error": v.get("error") or None
        }
        for k, v in state.items() if k.startswith("download_")
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
