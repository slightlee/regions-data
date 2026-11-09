"""
行政区划数据清洗脚本
处理代码格式问题：
- 省/地/县级：统一为6位代码
- 乡/镇/街道级：统一为9位代码

文件规则：
- 对 data/raw/ 中的源文件只读，不改名、不移动、不覆盖
- 明确排除断点文件 regions_*_tmp.json
- 清洗结果以同名文件写入 data/processed/，形成 raw/processed 1:1 批次映射
"""

import json
import os
from datetime import datetime


def normalize_code(code, level):
    """
    标准化行政区划代码
    
    Args:
        code: 原始代码
        level: 行政级别 (1=省, 2=地, 3=县, 4=乡)
    
    Returns:
        标准化后的代码
    """
    if not code:
        return None
    
    code_str = str(code)

    # 官方未发布代码时可能返回非数字占位符（如台湾省“资料暂缺”），原样保留，不补零造值
    if not code_str.isdigit():
        return code_str
    
    # 省/地/县级：6位代码
    if level in [1, 2, 3]:
        if len(code_str) <= 6:
            # 短代码，补齐到6位
            return code_str.ljust(6, '0')
        elif len(code_str) > 6:
            # 长代码，截取前6位
            return code_str[:6]
    
    # 乡/镇/街道级：9位代码
    elif level == 4:
        if len(code_str) <= 9:
            # 补齐到9位
            return code_str.ljust(9, '0')
        elif len(code_str) > 9:
            # 截取前9位
            return code_str[:9]
    
    return code_str


def normalize_parent_code(parent_code, parent_level):
    """
    标准化父级代码
    
    Args:
        parent_code: 父级代码
        parent_level: 父级级别
    
    Returns:
        标准化后的父级代码
    """
    if not parent_code:
        return None
    
    parent_code_str = str(parent_code)

    # 非数字占位符原样保留，不进行码位补齐
    if not parent_code_str.isdigit():
        return parent_code_str
    
    # 父级是省/地/县级：6位代码
    if parent_level in [1, 2, 3]:
        if len(parent_code_str) <= 6:
            return parent_code_str.ljust(6, '0')
        else:
            return parent_code_str[:6]
    
    # 父级是乡级（理论上不存在）
    elif parent_level == 4:
        if len(parent_code_str) <= 9:
            return parent_code_str.ljust(9, '0')
        else:
            return parent_code_str[:9]
    
    return parent_code_str


def clean_regions_data(input_file, output_file):
    """
    清洗行政区划数据
    
    Args:
        input_file: 输入文件路径
        output_file: 输出文件路径
    """
    print("=" * 80)
    print("开始清洗行政区划数据")
    print("=" * 80)
    
    # 读取原始数据
    print(f"\n读取文件: {input_file}")
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    meta = data.get("meta", {})
    regions = data.get("data", [])
    
    print(f"原始记录数: {len(regions):,}")
    
    # 清洗数据
    print("\n开始清洗数据...")
    cleaned_regions = []
    stats = {
        "total": 0,
        "level_1": 0,
        "level_2": 0,
        "level_3": 0,
        "level_4": 0,
        "code_changed": 0,
        "parent_code_changed": 0
    }
    
    for region in regions:
        level = region.get("level")
        original_code = region.get("code")
        original_parent_code = region.get("parent_code")
        
        # 标准化代码
        normalized_code = normalize_code(original_code, level)
        
        # 标准化父级代码
        parent_level = level - 1 if level > 1 else None
        normalized_parent_code = normalize_parent_code(original_parent_code, parent_level) if original_parent_code else None
        
        # 统计变化
        if normalized_code != original_code:
            stats["code_changed"] += 1
        
        if normalized_parent_code != original_parent_code:
            stats["parent_code_changed"] += 1
        
        # 创建清洗后的记录
        cleaned_region = {
            "code": normalized_code,
            "name": region.get("name"),
            "level": level,
            "type": region.get("type"),
            "parent_code": normalized_parent_code,
            "year": region.get("year"),
            "name_path": region.get("name_path", "")
        }
        
        cleaned_regions.append(cleaned_region)
        
        # 统计
        stats["total"] += 1
        if level == 1:
            stats["level_1"] += 1
        elif level == 2:
            stats["level_2"] += 1
        elif level == 3:
            stats["level_3"] += 1
        elif level == 4:
            stats["level_4"] += 1
    
    # 数量守恒断言：清洗只做确定性变换，不允许增删记录
    if len(cleaned_regions) != len(regions):
        raise SystemExit(f"❌ 数量守恒失败: 输入 {len(regions):,} != 输出 {len(cleaned_regions):,}")

    # 保存清洗后的数据
    print(f"\n保存清洗后的数据到: {output_file}")
    
    output_data = {
        "meta": {
            **meta,
            "cleaned_time": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "cleaned": True,
            "original_file": os.path.basename(input_file)
        },
        "data": cleaned_regions
    }
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    # 输出统计信息
    print("\n" + "=" * 80)
    print("清洗完成！")
    print("=" * 80)
    print(f"\n总记录数: {stats['total']:,}")
    print(f"  省级: {stats['level_1']:,} 个")
    print(f"  地级: {stats['level_2']:,} 个")
    print(f"  县级: {stats['level_3']:,} 个")
    print(f"  乡级: {stats['level_4']:,} 个")
    print(f"\n代码变化:")
    print(f"  code 修改: {stats['code_changed']:,} 条")
    print(f"  parent_code 修改: {stats['parent_code_changed']:,} 条")
    
    # 显示示例
    print("\n" + "=" * 80)
    print("清洗示例（前5条）:")
    print("=" * 80)
    for i, region in enumerate(cleaned_regions[:5], 1):
        print(f"\n{i}. {region['name']}")
        print(f"   code: {region['code']} (level={region['level']})")
        print(f"   parent_code: {region['parent_code']}")
    
    print("\n" + "=" * 80)
    print("清洗示例（乡级，前5条）:")
    print("=" * 80)
    level_4_regions = [r for r in cleaned_regions if r['level'] == 4]
    for i, region in enumerate(level_4_regions[:5], 1):
        print(f"\n{i}. {region['name']}")
        print(f"   code: {region['code']} (9位)")
        print(f"   parent_code: {region['parent_code']} (6位)")


def find_latest_final_file(directory):
    """查找最新的已完成采集文件，排除断点续传的 _tmp.json。"""
    files = [
        name for name in os.listdir(directory)
        if name.startswith("regions_")
        and name.endswith(".json")
        and not name.endswith("_tmp.json")
    ]
    return sorted(files)[-1] if files else None


def main():
    """主函数"""
    # 自动寻找最新的原始数据文件
    raw_dir = "data/raw"
    processed_dir = "data/processed"
    os.makedirs(processed_dir, exist_ok=True)
    
    # 获取最新的 raw 文件
    if not os.path.exists(raw_dir):
        print(f"❌ 错误: 原始数据目录不存在 - {raw_dir}")
        return
        
    latest_raw = find_latest_final_file(raw_dir)
    if not latest_raw:
        print(f"❌ 错误: 找不到已完成的原始数据文件 in {raw_dir}")
        return

    input_file = os.path.join(raw_dir, latest_raw)
    
    # 输出文件名沿用输入文件名，实现 1:1 映射
    output_file = os.path.join(processed_dir, latest_raw)
    
    print(f"🚀 开始清洗最新原始数据: {input_file}")
    clean_regions_data(input_file, output_file)
    
    print(f"\n✅ 清洗完成！输出文件: {output_file}")


if __name__ == "__main__":
    main()
