# -*- coding: utf-8 -*-
"""P3 体积审计：静态 PE 导入表分析，算出 dist 里哪些文件没有任何引用者。

用法：.venv\\Scripts\\python.exe tools\\bundle_audit.py
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "dye_tool"


def pe_imports(path: Path) -> set[str]:
    """返回 PE 文件的导入 DLL 名集合（小写）。非 PE 返回空集。"""
    try:
        b = path.read_bytes()
    except OSError:
        return set()
    if len(b) < 0x40 or b[:2] != b"MZ":
        return set()
    e_lfanew = struct.unpack_from("<I", b, 0x3C)[0]
    if e_lfanew + 24 > len(b) or b[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return set()
    coff = e_lfanew + 4
    n_sections = struct.unpack_from("<H", b, coff + 2)[0]
    size_opt = struct.unpack_from("<H", b, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", b, opt)[0]
    dd_off = opt + (0x70 if magic == 0x20B else 0x60)   # PE32+ vs PE32
    if dd_off + 16 > len(b):
        return set()
    imp_rva, imp_size = struct.unpack_from("<II", b, dd_off + 8)
    if imp_rva == 0:
        return set()
    # 节表 → RVA 转文件偏移
    secs = []
    so = opt + size_opt
    for i in range(n_sections):
        s = so + 40 * i
        if s + 40 > len(b):
            break
        vsize, vaddr, rsize, raw = struct.unpack_from("<IIII", b, s + 8)
        secs.append((vaddr, max(vsize, rsize), raw))

    def rva2off(rva: int) -> int | None:
        for vaddr, vsize, raw in secs:
            if vaddr <= rva < vaddr + vsize:
                return raw + (rva - vaddr)
        return None

    out: set[str] = set()
    off = rva2off(imp_rva)
    if off is None:
        return set()
    while off + 20 <= len(b):
        desc = b[off:off + 20]
        if desc == b"\0" * 20:
            break
        name_rva = struct.unpack_from("<I", desc, 12)[0]
        if name_rva:
            no = rva2off(name_rva)
            if no is not None:
                end = b.find(b"\0", no)
                out.add(b[no:end].decode("ascii", "replace").lower())
        off += 20
    return out


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not DIST.is_dir():
        print(f"没有 dist: {DIST}")
        return
    t0 = time.perf_counter()
    files = [p for p in DIST.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(f"dist: {len(files)} 文件, {total/1048576:.1f} MB\n")

    # 1) 建立"被导入名 → 谁导入"的映射
    imported_by: dict[str, list[Path]] = {}
    pe_count = 0
    for p in files:
        deps = pe_imports(p)
        if deps:
            pe_count += 1
        for d in deps:
            imported_by.setdefault(d, []).append(p)
        # python 扩展/pyd 还会通过 LoadLibrary 动态加载，另有文件名引用
    print(f"PE 文件数: {pe_count}（其余为数据/文本）")

    # 2) 找出 dist 内"没有任何 dist 内文件导入它"的大型二进制
    by_name: dict[str, Path] = {}
    for p in files:
        by_name.setdefault(p.name.lower(), p)
    orphans = []
    for name, path in by_name.items():
        if not name.endswith((".dll", ".pyd")):
            continue
        users = [q for q in imported_by.get(name, []) if q != path]
        if users:
            continue
        # 再查"是否有任何文件在字节里提到这个名字"（动态加载兜底）
        hits = 0
        for q in files:
            if q == path or q.suffix.lower() not in (".exe", ".pyd", ".dll", ".py", ".pyc"):
                continue
            try:
                if q.stat().st_size > 60 * 1048576:
                    continue
                if name.encode("ascii", "ignore") in q.read_bytes():
                    hits += 1
                    if hits >= 2:
                        break
            except OSError:
                continue
        if hits == 0:
            orphans.append((path.stat().st_size, path))
    orphans.sort(reverse=True)
    print(f"\n=== 无任何静态导入者、且无字节级名字引用（疑似可删）: {len(orphans)} 个 ===")
    for sz, p in orphans[:30]:
        print(f"  {sz/1048576:7.2f} MB  {p.relative_to(DIST)}")

    # 3) 分类汇总，给出可裁剪清单
    print("\n=== 分类体积 ===")
    groups = {
        "PySide6 翻译": lambda p: "PySide6/translations" in p.as_posix(),
        "matplotlib mpl-data": lambda p: "matplotlib/mpl-data" in p.as_posix(),
        "matplotlib sample_data": lambda p: "mpl-data/sample_data" in p.as_posix(),
        "matplotlib 字体 ttf": lambda p: "mpl-data/fonts" in p.as_posix(),
        "numpy tests": lambda p: "/tests/" in p.as_posix() and "numpy" in p.as_posix(),
        "dist-info": lambda p: ".dist-info" in p.as_posix(),
        "msvc 运行库": lambda p: p.name.lower().startswith(("msvcp140", "vcruntime", "concrt")),
    }
    for label, pred in groups.items():
        sel = [p for p in files if pred(p)]
        if sel:
            print(f"  {label:26s} {sum(p.stat().st_size for p in sel)/1048576:7.2f} MB  ({len(sel)} 文件)")

    print(f"\n用时 {time.perf_counter()-t0:.1f}s")


if __name__ == "__main__":
    main()
else:                                                   # pragma: no cover
    raise SystemExit("bundle_audit 只能直接运行")
