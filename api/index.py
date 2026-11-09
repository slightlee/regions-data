"""
行政区划 API 服务
基于新的 regions 单表结构
"""

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

import sqlite3
import os

from contextlib import contextmanager

app = FastAPI(
    title="行政区划 API",
    description="行政区划数据查询接口",
    version="2.0.0"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
DB_PATH = os.path.join(PARENT_DIR, "data", "regions.db")


@contextmanager
def get_db_connection():
    """数据库连接上下文管理器"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 返回字典格式
    try:
        yield conn
    finally:
        conn.close()


def rows_to_list(rows):
    """将 sqlite3.Row 转换为字典列表"""
    return [dict(row) for row in rows]


# 层级 → 标签（level 为源数据字段；类型缺失时按层级重述，不做推断）
LEVEL_LABELS = {1: "省级", 2: "地级", 3: "县级", 4: "乡级"}


# ========== 级联查询接口 ==========

@app.get("/api/provinces")
def get_provinces():
    """
    获取所有省级行政区划
    返回：省、直辖市、自治区、特别行政区
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT code, name, type, type_code 
            FROM regions 
            WHERE depth = 1 AND is_deleted = 0 
            ORDER BY sort_order, code
        """)
        return rows_to_list(cursor.fetchall())


@app.get("/api/children/{parent_code}")
def get_children(parent_code: str):
    """
    通用接口：获取指定区划的下级
    自动适配直辖市（跳过地级）和普通省份
    返回额外信息：hasChildren, childrenTypeName
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT code, name, level, depth, type, type_code 
            FROM regions 
            WHERE parent_code = ? AND is_deleted = 0 
            ORDER BY sort_order, code
        """, (parent_code,))
        rows = cursor.fetchall()
        
        if not rows:
            return {
                "items": [],
                "hasChildren": False,
                "childrenTypeName": None,
                "count": 0
            }
        
        items = rows_to_list(rows)
        
        # 标签提示：优先用数据自带的类型；乡级接口未提供类型，按 level 重述
        first_type = items[0].get("type") or LEVEL_LABELS.get(items[0].get("level"), "")
        
        # 检查子项是否还有下级
        first_code = items[0].get("code")
        has_grandchildren = False
        if first_code:
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM regions 
                WHERE parent_code = ? AND is_deleted = 0
            """, (first_code,))
            has_grandchildren = cursor.fetchone()["cnt"] > 0
        
        return {
            "items": items,
            "hasChildren": True,
            "childrenTypeName": first_type,
            "hasGrandchildren": has_grandchildren,
            "count": len(items)
        }


# ========== 统计接口 ==========

@app.get("/api/stats")
def get_stats():
    """
    获取数据统计信息
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 总数
        cursor.execute("SELECT COUNT(*) as total FROM regions WHERE is_deleted = 0")
        total = cursor.fetchone()["total"]
        
        # 按 level 统计
        cursor.execute("""
            SELECT level, COUNT(*) as count 
            FROM regions 
            WHERE is_deleted = 0 
            GROUP BY level 
            ORDER BY level
        """)
        by_level = rows_to_list(cursor.fetchall())
        
        return {
            "total": total,
            "by_level": by_level
        }


# ========== 模板路由 ==========

templates_directory = os.path.join(PARENT_DIR, "templates")
templates = Jinja2Templates(directory=templates_directory)


@app.get("/")
async def read_index(request: Request):
    """渲染首页"""
    return templates.TemplateResponse("index.html", {"request": request})