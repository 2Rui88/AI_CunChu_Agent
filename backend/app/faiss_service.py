"""
FAISS 向量索引封装 —— 每用户独立索引，支持增量添加和余弦相似度搜索

索引类型: IndexFlatIP（内积搜索，配合 L2 归一化等效余弦相似度）
索引路径: /data/faiss/users/{md5(username)}.index.bin
锁路径:    /tmp/faiss_locks/{md5(username)}.lock
脏标记:    /tmp/faiss_locks/{md5(username)}.dirty
二进制兼容 C 版本 FAISS 1.7.2 写入的索引文件
"""
import hashlib
import os
import errno
import fcntl
from pathlib import Path
import numpy as np
import faiss
from app.config import settings

LOCK_DIR = "/tmp/faiss_locks"


def _user_hash(username: str) -> str:
    """对用户名取 MD5，作为索引文件名的一部分"""
    return hashlib.md5(username.encode()).hexdigest()


def _index_path(username: str) -> str:
    """返回用户 FAISS 索引文件的完整路径"""
    return str(
        Path(settings.faiss_user_index_dir) / f"{_user_hash(username)}.index.bin"
    )


def load_index(username: str, dim: int | None = None) -> faiss.IndexFlatIP:
    """
    加载用户 FAISS 索引。如果索引文件存在则从磁盘读取，否则创建新的空索引。
    如果磁盘索引的维度与配置不符，则删除旧索引并创建新索引。
    """
    dimension = dim or settings.embedding_dimension
    path = _index_path(username)

    if Path(path).exists():
        idx = faiss.read_index(path)
        if idx.d == dimension:
            return idx
        # 维度不匹配（模型切换导致），删除旧索引重建
        Path(path).unlink()
        return faiss.IndexFlatIP(dimension)

    return faiss.IndexFlatIP(dimension)


def save_index(username: str, index: faiss.IndexFlatIP):
    """将索引持久化到磁盘"""
    Path(settings.faiss_user_index_dir).mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, _index_path(username))


def add_vector(username: str, vec: np.ndarray) -> int:
    """
    向用户索引中追加一个向量，返回分配的 faiss_id（添加前的 ntotal）。
    向量添加前先做 L2 归一化，添加后自动保存索引到磁盘。
    """
    vec = l2_normalize(vec)
    idx = load_index(username)
    faiss_id = int(idx.ntotal)
    idx.add(vec.reshape(1, -1).astype(np.float32))
    save_index(username, idx)
    return faiss_id


def search(
    username: str,
    query_vec: np.ndarray,
    top_k: int = 10,
) -> list[tuple[int, float]]:
    """
    向量相似度搜索。
    返回 [(faiss_id, score), ...]，score 为余弦相似度（范围 [-1, 1]）。

    查询向量在搜索前会做 L2 归一化（修改传入的 ndarray）。
    结果按相似度降序排列。
    """
    idx = load_index(username)
    if idx.ntotal == 0:
        return []

    q = l2_normalize(query_vec).reshape(1, -1).astype(np.float32)
    k = min(top_k, int(idx.ntotal))

    scores, ids = idx.search(q, k)
    results = []
    for i in range(k):
        if ids[0][i] >= 0:
            results.append((int(ids[0][i]), float(scores[0][i])))

    return results


def rebuild_from_db(username: str, vectors: list[np.ndarray]):
    """
    从数据库中的向量列表全量重建用户索引（用于 rebuild 场景）。
    清空现有索引，批量添加后保存。
    """
    idx = faiss.IndexFlatIP(settings.embedding_dimension)
    for vec in vectors:
        v = l2_normalize(vec.copy()).reshape(1, -1).astype(np.float32)
        idx.add(v)
    save_index(username, idx)


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    """
    L2 归一化（原地修改 + 返回引用）。
    归一化后，IndexFlatIP 的内积 = 余弦相似度，范围 [-1, 1]。
    空向量或零范数向量不做归一化，直接返回。
    """
    if vec.size == 0:
        return vec
    norm = np.linalg.norm(vec)
    if norm > 1e-10:
        vec /= norm
    return vec


def get_ntotal(username: str) -> int:
    """获取用户索引中的向量总数"""
    idx = load_index(username)
    return int(idx.ntotal)


# ── 脏标记与并发锁 ──

def _lock_path(username: str) -> str:
    """返回用户锁文件路径"""
    return os.path.join(LOCK_DIR, f"{_user_hash(username)}.lock")


def _dirty_path(username: str) -> str:
    """返回用户脏标记文件路径"""
    return os.path.join(LOCK_DIR, f"{_user_hash(username)}.dirty")


def mark_dirty(username: str):
    """标记用户 FAISS 索引需要重建（文件删除后调用）"""
    os.makedirs(LOCK_DIR, exist_ok=True)
    path = _dirty_path(username)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("1")


def is_dirty(username: str) -> bool:
    """检查用户 FAISS 索引是否需要重建"""
    return os.path.exists(_dirty_path(username))


def clear_dirty(username: str):
    """清理脏标记（重建成功后调用）"""
    path = _dirty_path(username)
    if os.path.exists(path):
        os.remove(path)


def acquire_lock(username: str) -> int:
    """
    获取文件锁（排他锁），返回文件描述符。
    用于防止并发 search/rebuild 同时操作同一用户的索引。
    返回 -1 表示加锁失败。
    """
    os.makedirs(LOCK_DIR, exist_ok=True)
    path = _lock_path(username)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except OSError:
        return -1


def release_lock(fd: int):
    """释放文件锁并关闭文件"""
    if fd >= 0:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
