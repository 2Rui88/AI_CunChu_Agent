"""
部门权限隔离单元测试

场景清单：
  1-3   can_manage_file 判权矩阵：所有者/同部门主管/跨部门主管/普通成员
  4     _del_file 成员删他人文件 → code=6 拒绝（判权前置，无任何写操作）
  5     _del_file 主管删同部门成员文件 → code=0，连带动作作用在所有者维度
  6     _del_file 主管删跨部门成员文件 → code=6 拒绝
  7     dealfile 未登录 → code=4
  8     sharefiles 未登录 → code=4（共享广场补鉴权）
  9     sharefiles 普通列表只查本部门（SQL 含 department_id 过滤）
  10    _save_shared 转存外部门共享 → code=1 拒绝
  11    dept_files 非主管 → code=6
  12    share_stats 部门化 key 与哨兵行命名
  13    bump_public_count +1 缺行则建 / -1 缺行不建
"""
import pytest

from app.dependencies import Actor, can_manage_file
from app.models import UserFileCount
from app.share_stats import (
    public_count_user, plaza_zset_key, plaza_hash_key, bump_public_count,
)
from app.routers import share as share_router
from app.routers import dept as dept_router


MD5 = "a" * 32
FILENAME = "report.pdf"


def _actor(name="alice", dept=1, role="member"):
    return Actor(username=name, department_id=dept, role=role)


# ── 测试辅助：伪 DB session（按调用顺序消费预设结果） ──

class _FakeResult:
    def __init__(self, scalar=None, scalars_all=None, rowcount=0, first_tuple=None):
        self._scalar = scalar
        self.rowcount = rowcount
        self._scalars_all = scalars_all or []
        self._first_tuple = first_tuple

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._scalars_all

    def first(self):
        return self._first_tuple


class _FakeDB:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.added = []
        self.committed = 0
        self.rollbacked = 0

    async def execute(self, sql, params=None):
        try:  # 字面量编译：让 WHERE 里的绑定参数值直接出现在 SQL 文本中，便于断言
            sql_text = str(sql.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            sql_text = str(sql)
        self.calls.append(sql_text)
        if self.results:
            item = self.results.pop(0)
            return item(sql_text) if callable(item) else item
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rollbacked += 1

    async def delete(self, obj):
        pass


class _UFL:
    """伪 UserFileList 行"""
    def __init__(self, user="alice", dept=1, shared=0, md5=MD5, file_name=FILENAME):
        self.user = user
        self.department_id = dept
        self.shared_status = shared
        self.md5 = md5
        self.file_name = file_name


class _Ctx:
    """伪 async with Session() as db 的上下文"""
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


# ═══════════════════════════════════════════════════════════
#  1-3. can_manage_file 判权矩阵
# ═══════════════════════════════════════════════════════════

@pytest.mark.parametrize("actor,owner,dept,expected", [
    # 1. 所有者本人（成员/主管）始终可操作
    (_actor("alice", 1, "member"), "alice", 1, True),
    (_actor("alice", 1, "supervisor"), "alice", 1, True),
    # 2. 同部门主管可操作任意成员文件；普通成员不可
    (_actor("boss", 1, "supervisor"), "alice", 1, True),
    (_actor("bob", 1, "member"), "alice", 1, False),
    # 3. 跨部门一律拒绝（主管权力边界 = 本部门）
    (_actor("boss", 2, "supervisor"), "alice", 1, False),
    (_actor("boss", 2, "member"), "alice", 1, False),
])
def test_can_manage_file_matrix(actor, owner, dept, expected):
    assert can_manage_file(actor, owner, dept) is expected


# ═══════════════════════════════════════════════════════════
#  4-6. _del_file 判权与所有者维度连带
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_del_file_member_denied_on_others(monkeypatch):
    """4. 成员删他人文件：判权前置拒绝，无删除/提交"""
    ufl = _UFL(user="alice", dept=1, shared=0)
    db = _FakeDB(results=[_FakeResult(scalar=ufl)])
    monkeypatch.setattr(share_router, "Session", lambda: _Ctx(db))
    monkeypatch.setattr(share_router, "mark_dirty", lambda u: None)

    resp = await share_router._del_file(_actor("bob", 1, "member"), "alice", MD5, FILENAME)
    assert resp == {"code": 6, "msg": "无权操作他人的文件"}
    assert db.committed == 0 and db.rollbacked == 0  # 判权失败直接返回，不动数据


@pytest.mark.asyncio
async def test_del_file_supervisor_cascades_on_owner(monkeypatch):
    """5. 主管删同部门成员文件：成功，AI 记录/计数/FAISS 重建全部按所有者查"""
    ufl = _UFL(user="alice", dept=1, shared=0)
    results = [
        _FakeResult(scalar=ufl),            # 查 owner 的 user_file_list
        _FakeResult(scalar=None),           # owner 的 user_file_count（无行则不减）
        _FakeResult(scalar=None),           # file_info 引用（无行则不处理）
    ]
    db = _FakeDB(results=results)
    monkeypatch.setattr(share_router, "Session", lambda: _Ctx(db))
    dirty = []
    monkeypatch.setattr(share_router, "mark_dirty", lambda u: dirty.append(u))

    resp = await share_router._del_file(_actor("boss", 1, "supervisor"), "alice", MD5, FILENAME)
    assert resp == {"code": 0}
    assert db.committed == 1
    # SQL 序列里所有按用户过滤的条件都以 owner=alice 出现（判权通过后操作所有者维度）
    joined = " | ".join(db.calls)
    assert "alice" in joined and "boss" not in joined
    assert dirty == ["alice"]  # FAISS 重建标记在所有者名下


@pytest.mark.asyncio
async def test_del_file_supervisor_cross_dept_denied(monkeypatch):
    """6. 主管删跨部门成员文件：拒绝"""
    ufl = _UFL(user="alice", dept=1, shared=0)
    db = _FakeDB(results=[_FakeResult(scalar=ufl)])
    monkeypatch.setattr(share_router, "Session", lambda: _Ctx(db))
    monkeypatch.setattr(share_router, "mark_dirty", lambda u: None)

    resp = await share_router._del_file(_actor("boss", 2, "supervisor"), "alice", MD5, FILENAME)
    assert resp == {"code": 6, "msg": "无权操作他人的文件"}


# ═══════════════════════════════════════════════════════════
#  7-8. 鉴权兜底
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dealfile_requires_login(monkeypatch):
    """7. dealfile 未登录 → code=4"""
    async def none_actor(user, token):
        return None
    monkeypatch.setattr(share_router, "get_actor", none_actor)
    resp = await share_router.dealfile(_Req({"cmd": "del"}), {"user": "x", "token": "bad"})
    assert resp == {"code": 4}


@pytest.mark.asyncio
async def test_sharefiles_requires_login(monkeypatch):
    """8. 共享广场未登录 → code=4（原先不鉴权，任何人不登录可看全站共享）"""
    async def none_actor(user, token):
        return None
    monkeypatch.setattr(share_router, "get_actor", none_actor)
    resp = await share_router.sharefiles(_Req({}), {"user": "x", "token": "bad"})
    assert resp == {"code": 4}


class _Req:
    """伪 Request：query_params 透传 body 的 cmd（与生产同优先级）"""
    def __init__(self, query):
        self.query_params = query


# ═══════════════════════════════════════════════════════════
#  9. 共享广场只查本部门
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sharefiles_filters_by_department(monkeypatch, caplog):
    """9. cmd=normal 列表 SQL 带 department_id 过滤，且返回 size（修复大小不显示）"""
    share_row = type("R", (), {
        "user": "alice", "md5": MD5, "file_name": FILENAME, "pv": 3,
        "department_id": 1, "create_time": "2026-01-01 00:00:00",
    })()
    fi = type("F", (), {"url": "/files/b/o", "size": 2048, "type": "pdf"})()

    def route(sql_text):
        if "share_file_list" in sql_text:
            return _FakeResult(scalars_all=[share_row])
        if "file_info" in sql_text:
            return _FakeResult(scalar=fi)
        return _FakeResult()

    db = _FakeDB(results=[lambda s: route(s), lambda s: route(s)])
    monkeypatch.setattr(share_router, "Session", lambda: _Ctx(db))

    async def fake_actor(u, t):
        return _actor("bob", 1, "member")
    monkeypatch.setattr(share_router, "get_actor", fake_actor)
    monkeypatch.setattr(share_router, "presign_from_db_url", lambda url: f"P({url})")

    resp = await share_router.sharefiles(_Req({}), {"user": "bob", "token": "t", "cmd": "normal"})
    assert resp["code"] == 0
    assert "department_id" in db.calls[0]      # 部门过滤在 SQL 里，不靠内存过滤
    assert resp["files"][0]["size"] == 2048    # 大小字段已返回（修复）
    assert resp["files"][0]["type"] == "pdf"


# ═══════════════════════════════════════════════════════════
#  10. 转存限本部门
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_save_shared_rejects_other_department(monkeypatch):
    """10. 转存时本部门共享查不到（外部门/私有）→ code=1"""
    db = _FakeDB(results=[_FakeResult(scalar=None)])  # 本部门共享查询无结果
    monkeypatch.setattr(share_router, "Session", lambda: _Ctx(db))
    resp = await share_router._save_shared(_actor("bob", 1, "member"), MD5, FILENAME)
    assert resp["code"] == 1
    assert "department_id" in db.calls[0]


# ═══════════════════════════════════════════════════════════
#  11. 部门文件接口仅主管
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dept_files_requires_supervisor(monkeypatch):
    """11. 普通成员访问 /api/dept/files → code=6，不触达任何查询"""
    db = _FakeDB()
    monkeypatch.setattr(dept_router, "Session", lambda: _Ctx(db))

    async def fake_actor(u, t):
        return _actor("bob", 1, "member")
    monkeypatch.setattr(dept_router, "get_actor", fake_actor)
    resp = await dept_router.dept_files({"user": "bob", "token": "t"})
    assert resp == {"code": 6, "msg": "仅部门主管可访问"}
    assert db.calls == []


# ═══════════════════════════════════════════════════════════
#  12-13. 部门化基础设施
# ═══════════════════════════════════════════════════════════

def test_share_stats_departmentalized_keys():
    """12. 哨兵行/ZSET/HASH key 全部带部门维度（不再是全局 key）"""
    assert public_count_user(3) == "FILE_PUBLIC_COUNT:3"
    assert plaza_zset_key(3) == "FILE_PUBLIC_ZSET:3"
    assert plaza_hash_key(3) == "FILE_NAME_HASH:3"
    assert public_count_user(1) != public_count_user(2)


@pytest.mark.asyncio
async def test_bump_public_count_semantics():
    """13. +1 缺行则建（写入哨兵行带部门）；-1 缺行不建"""
    db = _FakeDB(results=[_FakeResult(scalar=None)])
    await bump_public_count(db, 2, 1)
    assert len(db.added) == 1
    assert isinstance(db.added[0], UserFileCount)
    assert db.added[0].user == "FILE_PUBLIC_COUNT:2"
    assert db.added[0].count == 1

    db2 = _FakeDB(results=[_FakeResult(scalar=None)])
    await bump_public_count(db2, 2, -1)
    assert db2.added == []  # 缺行不减也不建，防哨兵变负数
