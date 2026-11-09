"""
行政区划数据获取脚本（v2：树接口 + 前缀搜索）
数据来源：国家地名信息库 (https://dmfw.mca.gov.cn)

接口用法（2026-11 实测验证）：
1. GET  {LIST_URL}?code=0&trimCode=true&maxLevel=3
   一次返回 全国→省→地→县 完整嵌套树（1~3 级含 type，短码 2/4/6 位）。
   直辖市、省直辖县级等跳级结构由接口按真实父子关系返回，无需特殊处理。
2. POST {CODE_LIST_URL}  form: parentCode=&placeCode=<代码前缀>&type=&pageNum=&pageSize=
   按行政区划代码前缀检索全国数据：分页、按 code 排序、total 可靠。
   注意：placeCode 模式下 type 必须留空，否则前缀不生效。

全国 1~4 级约 4.2 万条数据只需 ~46 次请求（1 次树 + ~45 页分页），
相比旧版逐父级采集（约 3200 次）大幅减少，串行 + 自适应限速即可。

接口地址可用环境变量覆盖（默认值写在下方常量中）：
    DMFW_GET_LIST_URL        树接口完整地址（getList）
    DMFW_GET_CODE_LIST_URL   列表接口完整地址（getCodeList）

用法（任意目录均可运行，产物默认写入 <仓库根>/data/raw/）：
    python scripts/fetch_regions.py                  # 全量采集（自动断点续传）
    python scripts/fetch_regions.py --fresh          # 忽略缓存重新采集
    python scripts/fetch_regions.py --prefixes 1,8   # 仅采集指定前缀（调试用）

断点续传：树与每个分页结果即时缓存于 data/raw/cache/，
中断或失败后重跑只会补拉缺失的页；版本变化风险由采集后对账兜底。
"""

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Windows 控制台 GBK 兜底，避免打印中文/emoji 崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 项目根目录（scripts/ 的上一级），不依赖运行时 CWD
BASE_DIR = Path(__file__).resolve().parent.parent

# 接口地址与官方页面（XzqhVersionPublish.html）的同源调用保持一致。
# 另存在 /9095/ 网关版（接口文档所载），实测与同源版返回字节级相同；
# getCodeList 仅存在于同源路径，故统一使用同源路径。
LIST_URL = "https://dmfw.mca.gov.cn/xzqh/getList"
CODE_LIST_URL = "https://dmfw.mca.gov.cn/xzqh/getCodeList"

PAGE_SIZE = 1000
TIMEOUT = 30
MIN_INTERVAL = 0.6        # 请求最小间隔（秒），触发限流后自适应上调
NORMAL_RETRIES = 3        # 普通错误重试次数
RATE_LIMIT_RETRIES = 6    # 限流重试预算（独立于普通错误，穷尽即记录失败）
MAX_PAGES_PER_PREFIX = 200  # 单前缀分页保护上限
PLACEHOLDER_CODES = {"资料暂缺"}  # 官方未发布代码的区划（如台湾省），原样保留并告警

# 软参考值仅用于对账提示，不作为失败判据（年度数据会自然变化）
REFERENCE_COUNTS = {1: 34, 2: 333, 3: 2845, 4: 38723}
LEVEL_NAMES = {1: "省级", 2: "地级", 3: "县级", 4: "乡级"}


class FetchError(Exception):
    """单次请求在重试穷尽后仍失败"""


class Throttle:
    """串行自适应限速器：连续成功缓慢提速，触发限流全局退避。"""

    def __init__(self, min_interval):
        self.base = max(min_interval, 0.05)
        self.interval = self.base
        self._last = 0.0

    def wait(self):
        now = time.monotonic()
        gap = self._last + self.interval - now
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def on_success(self):
        self.interval = max(self.base, self.interval * 0.9)

    def on_rate_limit(self, attempt):
        self.interval = min(max(self.interval * 1.5, self.interval + 0.5), 8.0)
        return min(10 * attempt, 60) + random.uniform(0, 3)


class RegionsFetcher:
    """行政区划数据获取器（树接口 + 前缀搜索）"""

    def __init__(self, list_url, code_list_url, output_dir=None,
                 page_size=PAGE_SIZE, min_interval=MIN_INTERVAL, use_cache=True,
                 only_prefixes=None):
        self.list_url = list_url
        self.code_list_url = code_list_url
        self.output_dir = Path(output_dir) if output_dir else BASE_DIR / "data" / "raw"
        self.cache_dir = self.output_dir / "cache"
        self.pages_dir = self.cache_dir / "pages"
        self.page_size = page_size
        self.use_cache = use_cache
        self.only_prefixes = only_prefixes

        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.request_count = 0
        self.failed_requests = []   # 每条失败都留痕，绝不静默
        self.warnings = []
        self.missing_pages = []     # 采集失败的分页，用于 is_partial 判定

        self.throttle = Throttle(min_interval)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://dmfw.mca.gov.cn",
            "Referer": "https://dmfw.mca.gov.cn/XzqhVersionPublish.html",
        })

    # ---------- 基础请求 ----------

    def _record_failure(self, scope, error):
        self.failed_requests.append({
            "scope": scope,
            "error": str(error),
            "time": datetime.now().isoformat(timespec="seconds"),
        })
        print(f"    ❌ 失败: {scope} -> {str(error)[:120]}")

    def _request_json(self, scope, method="GET", url=None, params=None, data=None):
        """带自适应限速、分类重试、失败留痕的请求。穷尽后抛 FetchError。"""
        url = url or (self.list_url if method == "GET" else self.code_list_url)
        normal_attempts = 0
        rl_attempts = 0

        while True:
            self.request_count += 1
            self.throttle.wait()
            try:
                if method == "GET":
                    resp = self.session.get(url, params=params, timeout=TIMEOUT)
                else:
                    resp = self.session.post(url, data=data, timeout=TIMEOUT)
            except requests.RequestException as e:
                normal_attempts += 1
                if normal_attempts > NORMAL_RETRIES:
                    self._record_failure(scope, f"网络异常: {e}")
                    raise FetchError(scope)
                wait = 3 * normal_attempts + random.uniform(0, 2)
                print(f"    ⚠️  {scope} 网络异常({type(e).__name__})，{wait:.0f}s 后重试")
                time.sleep(wait)
                continue

            # HTTP 层限流
            if resp.status_code in (429, 503):
                rl_attempts += 1
                if rl_attempts > RATE_LIMIT_RETRIES:
                    self._record_failure(scope, f"限流重试穷尽 (HTTP {resp.status_code})")
                    raise FetchError(scope)
                backoff = self.throttle.on_rate_limit(rl_attempts)
                print(f"    ⚠️  {scope} 触发限流(HTTP {resp.status_code})，等待 {backoff:.0f}s "
                      f"({rl_attempts}/{RATE_LIMIT_RETRIES})")
                time.sleep(backoff)
                continue

            try:
                result = resp.json()
            except ValueError:
                normal_attempts += 1
                if normal_attempts > NORMAL_RETRIES:
                    self._record_failure(scope, f"响应非 JSON (HTTP {resp.status_code})")
                    raise FetchError(scope)
                print(f"    ⚠️  {scope} 响应非 JSON，重试")
                time.sleep(3 * normal_attempts)
                continue

            # 业务层限流（status=2 + “频繁”文案）
            if result.get("status") == 2 and "频繁" in str(result.get("message") or ""):
                rl_attempts += 1
                if rl_attempts > RATE_LIMIT_RETRIES:
                    self._record_failure(scope, "限流重试穷尽 (status=2)")
                    raise FetchError(scope)
                backoff = self.throttle.on_rate_limit(rl_attempts)
                print(f"    ⚠️  {scope} 触发限流(频繁)，等待 {backoff:.0f}s "
                      f"({rl_attempts}/{RATE_LIMIT_RETRIES})")
                time.sleep(backoff)
                continue

            if resp.status_code != 200 or result.get("status") != 200:
                normal_attempts += 1
                if normal_attempts > NORMAL_RETRIES:
                    self._record_failure(scope, f"接口返回异常: HTTP={resp.status_code}, "
                                                f"status={result.get('status')}, "
                                                f"message={result.get('message')}")
                    raise FetchError(scope)
                print(f"    ⚠️  {scope} 接口异常，重试")
                time.sleep(3 * normal_attempts)
                continue

            self.throttle.on_success()
            return result

    # ---------- 缓存 ----------

    def _load_cache(self, path):
        if not self.use_cache or not path.exists():
            return None
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and "payload" in obj:
                return obj
        except (json.JSONDecodeError, OSError):
            pass
        print(f"    ⚠️  缓存损坏，重新获取: {path.name}")
        return None

    def _save_cache(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "payload": payload,
        }, ensure_ascii=False), encoding="utf-8")

    # ---------- 第 1 步：树接口（1~3 级） ----------

    def fetch_tree(self):
        path = self.cache_dir / "tree.json"
        cached = self._load_cache(path)
        if cached:
            print(f"✅ 使用树接口缓存（采集于 {cached['fetched_at']}，--fresh 可强制刷新）")
            return cached["payload"]

        print("[1/2] 获取省/地/县树（getList?code=0&maxLevel=3）...")
        result = self._request_json(
            "getList 树",
            method="GET",
            params={"code": "0", "trimCode": "true", "maxLevel": 3},
        )
        self._save_cache(path, result)
        children = (result.get("data") or {}).get("children") or []
        print(f"    ✅ 树获取成功：{len(children)} 个省级单位")
        return result

    # ---------- 第 2 步：前缀搜索（乡级 + 校验冗余） ----------

    def _fetch_page(self, prefix, page):
        path = self.pages_dir / f"{prefix}_p{page}.json"
        cached = self._load_cache(path)
        if cached:
            return cached["payload"]

        scope = f"getCodeList prefix={prefix} page={page}"
        result = self._request_json(
            scope,
            method="POST",
            data={
                "parentCode": "",
                "placeCode": prefix,
                "title": "",
                "tableName": "",
                "type": "",
                "pageNum": str(page),
                "pageSize": str(self.page_size),
            },
        )
        self._save_cache(path, result)
        return result

    def fetch_prefix(self, prefix):
        """采集单个前缀的全部分页，返回记录列表（空 total 视为无数据）。"""
        first = self._fetch_page(prefix, 1)
        total = int(first.get("total") or 0)
        if total == 0:
            data = first.get("data") or []
            if data:
                return data
            print(f"    前缀 {prefix}: 无数据（total=0）")
            return []

        pages = min(math.ceil(total / self.page_size), MAX_PAGES_PER_PREFIX)
        records = list(first.get("data") or [])
        print(f"    前缀 {prefix}: total={total:,}, 共 {pages} 页")

        for page in range(2, pages + 1):
            result = self._fetch_page(prefix, page)
            page_data = result.get("data") or []
            if not page_data:
                self.missing_pages.append(f"{prefix}#p{page}")
                self._record_failure(f"getCodeList prefix={prefix} page={page}",
                                     "分页返回空数据")
                continue
            records.extend(page_data)

        # 对账：实取条数必须等于 total
        if len(records) != total:
            self._record_failure(f"getCodeList prefix={prefix}",
                                 f"对账失败: 实取 {len(records)} != total {total}")
        return records

    # ---------- 汇总与校验 ----------

    def assemble(self, tree_result, prefix_records):
        """合并树（1~3 级）与前缀搜索（乡级），做结构校验。"""
        records = []
        # 乡级合法父级：县级(6位)或地级(4位补零，如东莞等不设县的地级市直辖乡镇)
        parent_candidates = set()
        seen_codes = set()
        self.warnings = []

        # 树 → 1~3 级记录（含 type，parent 为树内父级短码）
        def walk(node, parent_code, path_names):
            code = str(node.get("code") or "")
            name = node.get("name")
            level = node.get("level")
            if code in PLACEHOLDER_CODES:
                self.warnings.append(f"区划 '{name}' 官方代码为 '{code}'，原样保留")
            name_path = "全国/" + "/".join([*path_names, name]) if name else ""
            records.append({
                "code": code,
                "name": name,
                "level": level,
                "type": node.get("type"),
                "parent_code": parent_code,
                "year": None,
                "name_path": name_path,
            })
            seen_codes.add(code)
            if level in (2, 3):
                parent_candidates.add(code.ljust(6, "0"))
            for child in node.get("children") or []:
                walk(child, code, [*path_names, name])

        root = tree_result.get("data") or {}
        for province in root.get("children") or []:
            walk(province, None, [])

        # 前缀搜索 → 乡级记录（level=4）
        towns = []
        unresolved_parents = []
        for r in prefix_records:
            if r.get("level") != 4:
                continue
            code = str(r.get("code") or "")
            if not code:
                continue
            if code in seen_codes:
                self.warnings.append(f"乡级记录与已有数据重复: {code} {r.get('name')}")
                continue
            seen_codes.add(code)
            parent9 = str(r.get("parentCode") or "")[:9] or None
            towns.append({
                "code": code,
                "name": r.get("name"),
                "level": 4,
                "type": r.get("type"),
                "parent_code": parent9,
                "year": r.get("year"),
                "name_path": "全国/" + (r.get("namePath") or ""),
            })
            if not parent9 or parent9[:6] not in parent_candidates:
                unresolved_parents.append(f"{code} {r.get('name')} -> {r.get('parentCode')}")

        records.extend(towns)

        # ---- 校验报告 ----
        level_counts = {}
        for rec in records:
            level_counts[rec["level"]] = level_counts.get(rec["level"], 0) + 1

        print("\n" + "=" * 70)
        print("数据对账")
        print("-" * 70)
        for lv in (1, 2, 3, 4):
            count = level_counts.get(lv, 0)
            ref = REFERENCE_COUNTS.get(lv)
            delta = "" if ref is None else f"（软参考 {ref:,}，差 {count - ref:+d}）"
            print(f"  {LEVEL_NAMES[lv]}: {count:,} {delta}")

        # 树的省份是否都能被数字前缀覆盖（代码非数字的区划无法前缀检索）
        provinces = root.get("children") or []
        numeric = [p for p in provinces if str(p.get("code") or "").isdigit()]
        non_numeric = [p for p in provinces if not str(p.get("code") or "").isdigit()]
        derived = sorted({str(p["code"])[0] for p in numeric})
        print(f"  前缀覆盖: {' '.join(derived)}（由 {len(numeric)} 个省级代码推导）")
        for p in non_numeric:
            print(f"  ⚠️  无法前缀检索（代码非数字）: {p.get('name')} code={p.get('code')}")

        if unresolved_parents:
            self.warnings.append(f"{len(unresolved_parents)} 条乡级记录父级无法在县级中解析")
            print(f"  ⚠️  父级未解析 {len(unresolved_parents)} 条，样例: "
                  f"{unresolved_parents[:3]}")
        else:
            print(f"  ✅ {len(towns):,} 条乡级记录全部解析到父级单位（县级/地级直辖）")

        print("=" * 70)
        return records

    # ---------- 保存 ----------

    def save(self, records, extra_meta=None):
        records = sorted(records, key=lambda r: (r.get("level") or 0, str(r.get("code") or "")))
        is_partial = bool(self.missing_pages or self.failed_requests)
        meta = {
            "table_name": None,  # v2 无需表名，接口按最新版本返回
            "total": len(records),
            "request_count": self.request_count,
            "failed_count": len(self.failed_requests),
            "crawl_time": self.timestamp,
            "method": "tree_plus_prefix_v2",
            "is_partial": is_partial,
        }
        if extra_meta:
            meta.update(extra_meta)
        if self.warnings:
            meta["warnings"] = self.warnings

        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / f"regions_{self.timestamp}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "data": records}, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 数据已保存: {out_path}（{len(records):,} 条，is_partial={is_partial}）")

        if self.failed_requests:
            fail_path = self.output_dir / f"failed_requests_{self.timestamp}.json"
            with open(fail_path, "w", encoding="utf-8") as f:
                json.dump(self.failed_requests, f, ensure_ascii=False, indent=2)
            print(f"⚠️  失败明细已保存: {fail_path}")
        return out_path

    # ---------- 主流程 ----------

    def run(self):
        print("=" * 70)
        print("行政区划数据获取（v2：树接口 + 前缀搜索）")
        print("=" * 70)

        tree_result = self.fetch_tree()
        if tree_result is None:
            return 1

        root = tree_result.get("data") or {}
        provinces = root.get("children") or []
        numeric = [p for p in provinces if str(p.get("code") or "").isdigit()]
        prefixes = sorted({str(p["code"])[0] for p in numeric})
        if self.only_prefixes:
            prefixes = [p for p in prefixes if p in self.only_prefixes]
        print(f"\n[2/2] 前缀搜索采集乡级（前缀: {' '.join(prefixes)}，"
              f"限速 {self.throttle.interval:.1f}s/请求）...")

        collected = []
        try:
            for i, prefix in enumerate(prefixes, 1):
                print(f"  [{i}/{len(prefixes)}] 前缀 {prefix}")
                try:
                    recs = self.fetch_prefix(prefix)
                    collected.extend(recs)
                    print(f"    ✅ 完成（累计 {len(collected):,} 条）")
                except FetchError:
                    self.missing_pages.append(f"prefix={prefix}")
                    continue
        except KeyboardInterrupt:
            print("\n⚠️  用户中断，保存已采集部分（重跑可断点续传）...")
            records = self.assemble(tree_result, collected)
            self.save(records, extra_meta={"note": "interrupted"})
            return 130

        records = self.assemble(tree_result, collected)
        extra = {"prefixes": prefixes}
        if self.only_prefixes:
            extra["note"] = f"仅采集指定前缀: {','.join(sorted(self.only_prefixes))}，数据不完整"
        self.save(records, extra_meta=extra)

        print(f"\n请求总数: {self.request_count}，失败: {len(self.failed_requests)}")
        if self.failed_requests or self.missing_pages:
            print("❌ 存在失败项，数据不完整。重跑本命令可断点续传补齐。")
            return 1
        print("🎉 采集完成。下一步：")
        print("    python scripts/verify_data.py")
        print("    python scripts/clean_regions_data.py")
        print("    python scripts/import_regions.py")
        return 0


def main():
    parser = argparse.ArgumentParser(description="行政区划数据获取（树接口 + 前缀搜索）")
    parser.add_argument("--output-dir", default=None,
                        help="输出目录（默认 <仓库根>/data/raw）")
    parser.add_argument("--fresh", action="store_true", help="忽略缓存重新采集")
    parser.add_argument("--prefixes", default=None,
                        help="仅采集指定前缀，逗号分隔（调试用），如 1,8")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
    parser.add_argument("--min-interval", type=float, default=MIN_INTERVAL,
                        help="请求最小间隔秒数（默认 0.6，触发限流自动上调）")
    args = parser.parse_args()

    list_url = os.environ.get("DMFW_GET_LIST_URL", LIST_URL)
    code_list_url = os.environ.get("DMFW_GET_CODE_LIST_URL", CODE_LIST_URL)

    only = set(args.prefixes.split(",")) if args.prefixes else None
    fetcher = RegionsFetcher(
        list_url=list_url,
        code_list_url=code_list_url,
        output_dir=args.output_dir,
        page_size=args.page_size,
        min_interval=args.min_interval,
        use_cache=not args.fresh,
        only_prefixes=only,
    )
    return fetcher.run()


if __name__ == "__main__":
    sys.exit(main())
