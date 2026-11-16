"""按时间、数量和容量整理日记配图。"""
from pathlib import Path
from datetime import timedelta


class ImageRetention:
    def __init__(self, store, config, data_dir):
        self.store, self.config = store, config
        self.directory = Path(data_dir) / "diary_images"

    def collect(self, now):
        cfg = self.config.retention
        records = [(category, row) for category in ("diary", "diary_publication")
                   for row in self.store.all(category)]
        refs = {Path(row["image_file"]).name: (category, row) for category, row in records if row.get("image_file")}
        busy = {name for name, (_, row) in refs.items()
                if row.get("image_status") == "生成中" or row.get("status") in {"生成中", "发布中", "待核对"}}
        generating = any(row.get("image_status") == "生成中" for _, row in records)
        files = sorted(self.directory.glob("*"), key=lambda path: path.stat().st_mtime)
        files = [path for path in files if path.is_file()]
        sizes = {path: path.stat().st_size for path in files}
        cutoff = (now - timedelta(days=cfg.image_days)).timestamp()
        removed = {path for path in files if path.name not in busy and
                   ((path.name not in refs and not generating) or path.stat().st_mtime < cutoff)}
        count = len(files) - len(removed)
        size = sum(sizes[path] for path in files if path not in removed)
        for path in files:
            if count <= cfg.image_count and size <= cfg.image_max_mb * 1024 * 1024:
                break
            if path not in removed and path.name not in busy and (path.name in refs or not generating):
                removed.add(path)
                count -= 1
                size -= sizes[path]
        summary = dict(category="diary_images", label="日记与空间配图", total=len(files),
                       days=cfg.image_days, limit=cfg.image_count, protected=len(busy), remove=len(removed),
                       policy=f"{cfg.image_days} 天 / {cfg.image_max_mb} MiB，当前 {sum(sizes.values()) / 1024 / 1024:.1f} MiB")
        return removed, refs, summary

    def clean(self, now):
        removed, refs, summary = self.collect(now)
        with self.store.db:
            for path in removed:
                path.unlink()
                if path.name in refs:
                    category, row = refs[path.name]
                    for key in ("image_file", "image_mime", "image_content", "image_error"):
                        row.pop(key, None)
                    row["image_status"] = "配图已按保留设置清理"
                    self.store.put(category, row)
        return summary
