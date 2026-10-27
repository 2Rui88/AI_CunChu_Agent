# -*- coding: utf-8 -*-
"""上传 8 份 docx 到文件管理,触发 AI 描述/切片,并经只读接口全量校验。

校验项:
  1) 上传 code==0(或 5=已存在)
  2) describe code==0
  3) /vector/docs: chunk_count>=2, embedded_count==chunk_count, stub_count==0
  4) /vector/chunks: 每块 faiss_id 非空、has_embedding=True;
     sum(char_len) == 本地提取字符数 + len("文件名：…\n文件内容：") 前缀(零丢失零截断)
  5) 每份文档的独有关键词确实出现在切片文本中(内容归属正确)
"""
import hashlib
import io
import re
import sys
import zipfile
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

BASE = "https://127.0.0.1/api"
USER = "zcode_doc_test"
NICK = "切片验证员"
PWD = "Zc#Test2026"
DOCS_DIR = Path(__file__).parent / "out"

# 每份文档的独有关键词(用于内容归属抽查)
KEYWORDS = {
    "云存储技术演进与对象存储体系实践": ["纠删码", "扁平命名空间", "生命周期"],
    "向量数据库与语义检索核心原理": ["分层可导航小世界图", "余弦相似度", "乘积量化"],
    "检索增强生成RAG架构设计与调优": ["倒数排序融合", "交叉编码", "查询改写"],
    "数据加密与个人信息保护技术体系": ["信封加密", "消息认证码", "差分隐私"],
    "分布式系统一致性理论与共识算法": ["随机超时", "多数派", "快照"],
    "大语言模型技术演进与行业落地": ["投机解码", "键值缓存", "指令微调"],
    "软件工程质量内建与自动化测试体系": ["测试金字塔", "变异测试", "契约测试"],
    "容器化技术栈与云原生运维实践": ["命名空间与控制组", "多阶段构建", "服务网格"],
}

s = requests.Session()
s.verify = False


def local_extract(path: Path) -> str:
    """与修复后后端 _parse_docx 一致的本地提取"""
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as zf:
        xml_text = zf.read("word/document.xml").decode("utf-8")
    paras = []
    for p_match in re.finditer(r"<w:p[ >].*?</w:p>", xml_text, re.DOTALL):
        parts = re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p_match.group())
        if parts:
            paras.append("".join(parts))
    return "\n".join(paras).strip()


def main():
    # ── 注册(已存在则忽略) + 登录 ──
    r = s.post(f"{BASE}/reg", json={"username": USER, "nick_name": NICK, "password": PWD}, timeout=15).json()
    print("注册:", r)
    d = s.post(f"{BASE}/login", json={"user": USER, "pwd": PWD}, timeout=15).json()
    assert d["code"] == 0, f"登录失败: {d}"
    token = d["token"]
    print("登录 OK")

    files = sorted(DOCS_DIR.glob("*.docx"))
    assert len(files) == 8, f"应有8份文档,实际 {len(files)}"

    uploaded = []
    for f in files:
        data = f.read_bytes()
        md5 = hashlib.md5(data).hexdigest()
        up = s.post(
            f"{BASE}/upload",
            data={"user": USER, "token": token, "md5": md5},
            files={"file": (f.name, data)},
            timeout=60,
        ).json()
        desc = s.post(
            f"{BASE}/ai/describe",
            json={"user": USER, "token": token, "md5": md5, "filename": f.name, "type": "docx"},
            timeout=300,
        ).json()
        print(f"上传={up} 描述={desc}  {f.name}")
        assert up.get("code") in (0, 5), f"{f.name} 上传失败: {up}"
        assert desc.get("code") == 0, f"{f.name} 描述失败: {desc}"
        uploaded.append((f.name, md5))

    # ── /vector/docs 聚合校验 ──
    docs = s.post(f"{BASE}/vector/docs", json={"user": USER, "token": token, "start": 0, "count": 50}, timeout=30).json()
    assert docs["code"] == 0, docs
    by_md5 = {d["md5"]: d for d in docs["docs"]}
    print(f"\nvector/docs 共 {docs['total']} 份已向量化文档, 总向量数 {docs['total_vectors']}")

    all_ok = True
    for name, md5 in uploaded:
        stem = name.rsplit(".", 1)[0]
        row = by_md5.get(md5)
        if not row:
            print(f"[FAIL] {name}: 未出现在 vector/docs")
            all_ok = False
            continue
        # ── /vector/chunks 明细校验 ──
        det = s.post(f"{BASE}/vector/chunks", json={"user": USER, "token": token, "md5": md5}, timeout=30).json()
        assert det["code"] == 0, det
        chunks = det["chunks"]
        extracted = local_extract(DOCS_DIR / name)
        expect_sum = len(extracted) + len("文件名：") + len(name) + len("\n文件内容：")

        checks = {
            "chunk>=2": len(chunks) >= 2,
            "embedded==chunks": row["embedded_count"] == row["chunk_count"] == len(chunks),
            "stub==0": row["stub_count"] == 0,
            "faiss_id全回写": all(c["faiss_id"] is not None for c in chunks),
            "embedding全有": all(c["has_embedding"] for c in chunks),
            "status全1": all(c["status"] == 1 for c in chunks),
            "字符零丢失": sum(c["char_len"] for c in chunks) == expect_sum,
            "标签非空": all(c["context_label"] for c in chunks),
        }
        joined = "".join(c["description"] for c in chunks)
        for kw in KEYWORDS[stem]:
            checks[f"关键词[{kw}]"] = kw in joined
        bad = [k for k, v in checks.items() if not v]
        status = "PASS" if not bad else "FAIL:" + ",".join(bad)
        if bad:
            all_ok = False
        lens = [c["char_len"] for c in chunks]
        print(f"[{status}] {name} 切块数={len(chunks)} 各块字符={lens} 预期总字符={expect_sum}")

    print("\n总体结论:", "全部通过" if all_ok else "存在失败项")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
