"""
行政区划采集数据验收脚本（fetch 之后、clean 之前运行）

验收对象：data/raw/ 下的采集产物 regions_*.json
依据：国家地名信息库两个独立接口的返回
  - 树接口 getList       → raw 中 1~3 级记录
  - 前缀搜索 getCodeList → data/raw/cache/pages/ 下的分页响应

检查项：
  1. 四级齐全 + 数量与 meta.total 对账（官方页面声明发布省/地/县/乡四级）
  2. 原始码位形态：省级 2 位、地级 4 位、县级 6 位、乡级 9 位，纯数字；
     "资料暂缺"（官方未发布代码的区划，如台湾省）列入白名单
  3. 引用完整性：每条乡级记录的父级码必须能解析到县级/地级单位
  4. 两接口交叉比对：1~3 级 code+name 逐条比对，输出差异清单
     - 搜索接口存在树没有的记录/名称不一致 → 验收失败（数据集可能漏采）
     - 树比搜索多 → 信息项（通常为源数据新增、搜索索引未同步），不阻断

用法：
  python scripts/verify_data.py [raw_json 路径]
  不带参数时自动选取 data/raw/ 下最新的已完成 regions_*.json（排除 _tmp.json）

验收不通过时退出码 1，不要对未验收数据执行清洗。
"""

import json
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"
CACHE_PAGES = RAW_DIR / "cache" / "pages"

PLACEHOLDER_CODES = {"资料暂缺"}  # 官方未发布代码的区划（如台湾省）
EXPECT_LEN = {1: 2, 2: 4, 3: 6, 4: 9}  # 各级原始码位（树码 2/4/6，搜索码 9）


def pad6(code):
    code = str(code)
    return code.ljust(6, "0") if len(code) <= 6 else code[:6]


def load_raw(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return doc.get("meta", {}), doc.get("data") or []


def load_search_pages():
    """前缀搜索的分页缓存（与 raw 同批采集）"""
    records = []
    if not CACHE_PAGES.exists():
        return records, False
    for fp in sorted(CACHE_PAGES.glob("*_p*.json")):
        obj = json.loads(fp.read_text(encoding="utf-8"))
        records.extend(obj.get("payload", {}).get("data") or [])
    return records, True


def main():
    if len(sys.argv) > 1:
        raw_path = Path(sys.argv[1])
    else:
        candidates = [c for c in sorted(RAW_DIR.glob("regions_*.json")) if "tmp" not in c.name]
        if not candidates:
            print("❌ 未找到采集产物（data/raw/regions_*.json），请先运行 fetch_regions.py")
            return 1
        raw_path = candidates[-1]

    meta, data = load_raw(raw_path)
    print("=" * 70)
    print(f"采集数据验收: {raw_path.name}")
    print(f"  采集批次: {meta.get('crawl_time')} | method: {meta.get('method')} | 声明总数: {meta.get('total'):,}")
    print("=" * 70)

    failures = []
    warnings = []

    # ---- 检查 1：四级齐全 + 数量对账 ----
    by_level = Counter(r.get("level") for r in data)
    print("\n[1] 四级齐全与数量对账")
    print("    各级数量: " + ", ".join(f"L{lv}={by_level.get(lv, 0):,}" for lv in (1, 2, 3, 4)))
    if sum(by_level.values()) != len(data):
        failures.append("存在 level 为空/异常的记录")
    if meta.get("total") not in (None, len(data)):
        failures.append(f"meta.total={meta.get('total')} 与实际 {len(data):,} 不符")
    missing = [lv for lv in (1, 2, 3, 4) if by_level.get(lv, 0) == 0]
    if missing:
        failures.append(f"缺少级别 {missing}（官方页面声明发布省/地/县/乡四级）")
    if not failures:
        print("    ✅ 通过")

    # ---- 检查 2：原始码位形态 ----
    print("\n[2] 原始码位形态（省级2位/地级4位/县级6位/乡级9位，纯数字）")
    bad_codes = []
    for r in data:
        code = str(r.get("code") or "")
        if code in PLACEHOLDER_CODES:
            warnings.append(f"官方未发布代码: {r.get('name')}（code={code}，如实保留）")
            continue
        lv = r.get("level")
        if lv in EXPECT_LEN and (len(code) != EXPECT_LEN[lv] or not code.isdigit()):
            bad_codes.append((code, r.get("name"), lv))
    if bad_codes:
        failures.append(f"码位形态异常 {len(bad_codes)} 条，样例: {bad_codes[:5]}")
    else:
        print(f"    ✅ {len(data):,} 条全部符合（白名单除外）")

    # ---- 检查 3：引用完整性 ----
    print("\n[3] 引用完整性（乡级父级可解析到县级/地级）")
    parent_candidates = {pad6(r["code"]) for r in data if r.get("level") in (2, 3)}
    unresolved = []
    for r in data:
        if r.get("level") != 4:
            continue
        p = str(r.get("parent_code") or "")
        if not p or p[:6] not in parent_candidates:
            unresolved.append((r.get("code"), r.get("name"), p))
    if unresolved:
        failures.append(f"乡级父级无法解析 {len(unresolved)} 条，样例: {unresolved[:5]}")
    else:
        print(f"    ✅ {by_level.get(4, 0):,} 条乡级全部解析")

    # ---- 检查 4：两接口交叉比对 ----
    print("\n[4] 两接口交叉比对（树 getList vs 前缀搜索 getCodeList，1~3 级）")
    search_records, has_cache = load_search_pages()
    if not has_cache:
        warnings.append("未找到分页缓存（cache/pages/），跳过交叉比对；请勿删除缓存后单独验收")
        print("    ⚠️  未找到分页缓存，跳过")
    else:
        def key_of(r):
            code = str(r.get("code") or "")
            return None if code in PLACEHOLDER_CODES else pad6(code)

        def brief(r):
            """code + 名称 + 行政归属路径，便于直接定位排查"""
            code = str(r.get("code") or "")
            path = str(r.get("name_path") or r.get("namePath") or "")
            if path.startswith("全国/"):
                path = path[3:]
            return f"{code} {r.get('name')}（{path}）" if path else f"{code} {r.get('name')}"

        tree = {1: {}, 2: {}, 3: {}}
        for r in data:
            if r.get("level") in (1, 2, 3):
                k = key_of(r)
                if k:
                    tree[r["level"]].setdefault(k, r)

        search = {1: {}, 2: {}, 3: {}}
        for r in search_records:
            if r.get("level") in (1, 2, 3):
                k = key_of(r)
                if k:
                    search[r["level"]].setdefault(k, r)

        for lv in (1, 2, 3):
            only_tree = set(tree[lv]) - set(search[lv])
            only_search = set(search[lv]) - set(tree[lv])
            name_diff = {c for c in set(tree[lv]) & set(search[lv])
                         if tree[lv][c].get("name") != search[lv][c].get("name")}
            status = "✅ 一致" if not (only_tree or only_search or name_diff) else "⚠️  有差异"
            print(f"    level {lv}: 树 {len(tree[lv]):,} / 搜索 {len(search[lv]):,} -> {status}")
            for c in sorted(only_tree)[:6]:
                print(f"       仅树: {brief(tree[lv][c])}")
            for c in sorted(only_search)[:6]:
                print(f"       仅搜索: {brief(search[lv][c])}")
            for c in sorted(name_diff)[:6]:
                print(f"       名称不同: {c} 树记为 {tree[lv][c].get('name')}"
                      f"（{str(tree[lv][c].get('name_path') or '').removeprefix('全国/')}）"
                      f" vs 搜索记为 {search[lv][c].get('name')}")
            # 搜索接口有而树没有 → 1~3 级以树为准合并，会漏采 → 验收失败
            if only_search or name_diff:
                failures.append(
                    f"level {lv}: 搜索接口存在树没有的记录/名称不一致"
                    f"（仅搜索 {len(only_search)}、名称差异 {len(name_diff)}），需人工核查")
            # 树比搜索多 → 我方数据为超集，通常为源新增、搜索索引未同步 → 信息项
            if only_tree:
                warnings.append(
                    f"level {lv}: 树比搜索多 {len(only_tree)} 条（通常为源数据新增、搜索索引未同步）: "
                    + "; ".join(brief(tree[lv][c]) for c in sorted(only_tree)[:3]))

    # ---- 汇总 ----
    print("\n" + "=" * 70)
    if warnings:
        print(f"信息项 {len(warnings)} 条（不阻断）:")
        for w in warnings:
            print(f"  ⚠️  {w}")
    if failures:
        print(f"❌ 验收未通过，{len(failures)} 项:")
        for f in failures:
            print(f"  ❌ {f}")
        print("\n请排查后重新采集，不要对未验收数据执行清洗。")
        return 1
    print("✅ 验收通过，可以进行清洗（python scripts/clean_regions_data.py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
