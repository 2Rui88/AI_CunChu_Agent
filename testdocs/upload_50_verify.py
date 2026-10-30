# -*- coding: utf-8 -*-
"""将 out50/ 的 50 份文档上传到 zcode_doc_test 并触发 AI 描述/切片/向量化，
随后经只读接口全量校验，与真实后端 chunker 的本地复算结果逐字节比对。

校验项（每份文档）:
  1) 上传 code==0（或 5=已存在）；describe code==0
  2) /vector/docs: chunk_count==embedded_count、stub_count==0、type/归属正确
  3) /vector/chunks: 切块数、每块 description/char_len/context_label 与
     本地真实 chunker（stub app.config 后加载 backend/app/chunker.py）输出逐字节一致
  4) 每块 faiss_id 非空、has_embedding=True、status==1（含 FAISS 回写）
  5) 三个唯一校验标记确实出现在切片文本中（内容归属正确）
  6) /ai/rebuild 全量重建后 count == total_vectors（索引与 DB 对账）

支持断点续传：进度落 out50/upload_state.json，重跑时跳过已完成 describe 的文件。
用法:
  PowerShell: $env:ZCODE_DOC_TEST_PASSWORD = '...'
  python upload_50_verify.py upload   # 只上传+处理（可反复跑，幂等）
  python upload_50_verify.py verify   # 只校验
  python upload_50_verify.py all      # 全流程（默认）
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

sys.path.insert(0, str(Path(__file__).parent))
from gen_50_docs import load_chunker  # noqa: E402

BASE = "https://127.0.0.1/api"
USER = "zcode_doc_test"
NICK = "批量文档处理测试员"
PWD = os.environ.get("ZCODE_DOC_TEST_PASSWORD", "")
if not PWD:
    raise RuntimeError("请先设置环境变量 ZCODE_DOC_TEST_PASSWORD")
DOCS_DIR = Path(__file__).parent / "out50"
STATE_FILE = DOCS_DIR / "upload_state.json"

chunker = load_chunker()
s = requests.Session()
s.verify = False


def load_manifest():
    manifest = json.loads((DOCS_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 50
    for m in manifest:
        # manifest 的 meta 里 JSON 化后 tuple/list 等价，chunker 只读取 dict/list
        assert (Path(DOCS_DIR) / m["file"]).exists(), m["file"]
    return manifest


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def login():
    r = s.post(f"{BASE}/reg", json={"username": USER, "nick_name": NICK,
                                    "password": PWD}, timeout=15).json()
    print("注册:", r)
    d = s.post(f"{BASE}/login", json={"user": USER, "pwd": PWD}, timeout=15).json()
    assert d["code"] == 0, f"登录失败: {d}"
    print("登录 OK")
    return d["token"]


# ═══════════════════════════════════════════════════════════
#  阶段一：上传 + describe（文档处理）
# ═══════════════════════════════════════════════════════════

def do_upload_and_describe(manifest, token, state):
    t0 = time.time()
    done = failed = 0
    for i, m in enumerate(manifest, 1):
        rec = state.get(m["md5"], {})
        if rec.get("describe") == 0:
            done += 1
            continue

        path = DOCS_DIR / m["file"]
        data = path.read_bytes()
        local_md5 = hashlib.md5(data).hexdigest()
        assert local_md5 == m["md5"], f"{m['file']} 内容与 manifest 不符"

        up = s.post(
            f"{BASE}/upload",
            data={"user": USER, "token": token, "md5": m["md5"]},
            files={"file": (m["file"], data)},
            timeout=120,
        ).json()
        if up.get("code") not in (0, 5):
            print(f"[{i:02d}/50] {m['file']} 上传失败: {up}")
            rec["upload"] = up.get("code")
            rec["describe"] = None
            state[m["md5"]] = rec
            save_state(state)
            failed += 1
            continue

        desc = None
        for attempt in (1, 2, 3):
            try:
                r = s.post(
                    f"{BASE}/ai/describe",
                    json={"user": USER, "token": token, "md5": m["md5"],
                          "filename": m["file"], "type": m["type"]},
                    timeout=(15, 600),
                ).json()
                desc = r
                if r.get("code") == 0:
                    break
            except requests.RequestException as exc:
                desc = {"code": -1, "msg": f"network: {exc}"}
            print(f"    describe 第{attempt}次失败: {desc}，5s 后重试")
            time.sleep(5)

        rec["upload"] = up.get("code")
        rec["describe"] = desc.get("code") if desc else None
        rec["describe_msg"] = (desc or {}).get("msg", "")
        state[m["md5"]] = rec
        save_state(state)

        status = "OK" if rec["describe"] == 0 else "FAIL"
        if rec["describe"] == 0:
            done += 1
        else:
            failed += 1
        print(f"[{i:02d}/50] {m['file']} upload={up.get('code')} "
              f"describe={rec['describe']} {rec['describe_msg']} [{status}] "
              f"累计 {time.time()-t0:.0f}s")

    print(f"\n上传+处理完成: 成功 {done} 失败 {failed} 耗时 {time.time()-t0:.0f}s")
    return failed == 0


# ═══════════════════════════════════════════════════════════
#  阶段二：全量校验
# ═══════════════════════════════════════════════════════════

def do_verify(manifest, token):
    docs = s.post(f"{BASE}/vector/docs",
                  json={"user": USER, "token": token, "start": 0, "count": 500},
                  timeout=30).json()
    assert docs["code"] == 0, docs
    by_md5 = {d["md5"]: d for d in docs["docs"]}
    print(f"vector/docs: 共 {docs['total']} 份已向量化文档，总向量数 {docs['total_vectors']}")

    all_ok = True
    fail_list = []
    fmt_stats = {}
    total_server_chunks = 0

    for m in manifest:
        name, md5, fmt = m["file"], m["md5"], m["type"]
        row = by_md5.get(md5)
        if not row:
            print(f"[FAIL] {name}: 未出现在 vector/docs")
            all_ok = False
            fail_list.append((name, "未入库"))
            continue

        det = s.post(f"{BASE}/vector/chunks",
                     json={"user": USER, "token": token, "md5": md5},
                     timeout=30).json()
        assert det["code"] == 0, det
        chunks = det["chunks"]
        local = chunker.chunk_text(fmt, m["desc"], metadata=m.get("meta"))
        joined = "".join(c["description"] for c in chunks)

        checks = {
            "type一致": row["type"] == fmt,
            "切块数一致": len(chunks) == len(local),
            "embedded==chunks": row["embedded_count"] == row["chunk_count"] == len(chunks),
            "stub==0": row["stub_count"] == 0,
            "faiss_id全回写": all(c["faiss_id"] is not None and c["faiss_id"] >= 0
                                  for c in chunks),
            "embedding全有": all(c["has_embedding"] for c in chunks),
            "status全1": all(c["status"] == 1 for c in chunks),
            "chunk_index连续": [c["chunk_index"] for c in chunks] == list(range(len(chunks))),
        }
        # 逐块内容比对（description / char_len / context_label）
        text_diff = []
        for ci, (srv, loc) in enumerate(zip(chunks, local)):
            if srv["description"] != loc.text:
                text_diff.append((ci, "description", len(srv["description"]), len(loc.text)))
            if srv["char_len"] != len(loc.text):
                text_diff.append((ci, "char_len", srv["char_len"], len(loc.text)))
            if srv["context_label"] != loc.context_label:
                text_diff.append((ci, "label", srv["context_label"][:20], loc.context_label[:20]))
        checks["逐块内容一致"] = not text_diff
        for kw in m["keywords"]:
            checks[f"标记[{kw}]"] = kw in joined

        bad = [k for k, v in checks.items() if not v]
        status = "PASS" if not bad else "FAIL:" + ",".join(bad)
        if bad:
            all_ok = False
            fail_list.append((name, ",".join(bad)))
            if text_diff:
                print(f"    {name} 首个内容差异: {text_diff[0]}")

        total_server_chunks += len(chunks)
        st = fmt_stats.setdefault(fmt, [0, 0])  # 文档数, 切块数
        st[0] += 1
        st[1] += len(chunks)
        print(f"[{status}] {name} 切块={len(chunks)} "
              f"(本地复算={len(local)}) desc={len(m['desc'])}字符")

    # ── FAISS 全量重建对账 ──
    print("\n触发 /ai/rebuild 全量重建对账 ...")
    rb = s.post(f"{BASE}/ai/rebuild",
                json={"user": USER, "token": token}, timeout=300).json()
    rebuild_ok = rb.get("code") == 0 and rb.get("count") == docs["total_vectors"]
    print(f"rebuild: {rb} | vector/docs total_vectors={docs['total_vectors']} "
          f"-> {'一致' if rebuild_ok else '不一致'}")
    if not rebuild_ok:
        all_ok = False

    print("\n═══ 按格式汇总 ═══")
    for fmt in sorted(fmt_stats):
        d_cnt, c_cnt = fmt_stats[fmt]
        print(f"  {fmt:5s}: {d_cnt} 份 / {c_cnt} 块")
    print(f"  合计  : 50 份 / {total_server_chunks} 块")
    if fail_list:
        print("\n失败清单:")
        for name, why in fail_list:
            print(f"  - {name}: {why}")
    print("\n总体结论:", "全部通过 ✓" if all_ok else "存在失败项 ✗")
    return all_ok


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    manifest = load_manifest()
    state = load_state()
    token = login()

    ok = True
    if mode in ("upload", "all"):
        ok = do_upload_and_describe(manifest, token, state) and ok
    if mode in ("verify", "all"):
        ok = do_verify(manifest, token) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
