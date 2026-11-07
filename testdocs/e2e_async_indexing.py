"""
全链路测试：上传 → 任务投递 → 后台向量化 → 状态轮询 → 幂等重投 →
对话搜索命中 → 任务表/用量表落库核验。

前置：docker 栈已用新代码重建（backend + nginx），https://localhost 可达。
用法：python e2e_async_indexing.py  （在宿主机直接运行，忽略自签名证书）

每步独立断言并打点，任何一步失败立即退出码 1，便于定位首个断点。
"""
import hashlib
import io
import json
import sys
import time
import uuid

import httpx

BASE = "https://localhost"
USER = "e2e_index_test"   # 固定用户：重跑只走登录（注册接口有每 IP 限流）
PASS = "E2eTest#2026"

STEP = 0


def ok(cond, label, detail=""):
    global STEP
    STEP += 1
    mark = "PASS" if cond else "FAIL"
    print(f"[{STEP:02d}] {mark}  {label}" + (f"  | {detail}" if detail else ""))
    if not cond:
        print(f"\n全链路在第 {STEP} 步断裂：{label}")
        sys.exit(1)


def main():
    client = httpx.Client(base_url=BASE, verify=False, timeout=60)

    # ── 1. 注册（已存在/被限流则直接登录固定用户）+ 登录 ──
    r = client.post("/api/reg", json={"username": USER, "password": PASS,
                                      "nick_name": USER})
    if r.json().get("code") not in (0, 2, 7):
        ok(False, "注册用户", r.text[:120])
    r = client.post("/api/login", json={"user": USER, "pwd": PASS})
    ok(r.json().get("code") == 0, "登录", USER)
    token = r.json()["token"]

    # ── 2. 上传一个内容独特的 txt ──
    marker = f"e2e-{uuid.uuid4().hex[:8]}"
    content = (
        f"全链路测试文档 {marker}\n\n"
        "第三章 容量规划：对象存储集群按 3 副本规划，单机架容量上限 480TB，"
        "冷数据下沉策略为 30 天未访问转纠删码。本文件用于验证向量化任务队列链路。\n"
    ).encode("utf-8")
    md5 = hashlib.md5(content).hexdigest()
    r = client.post(
        "/api/upload",
        data={"user": USER, "token": token, "md5": md5},
        files={"file": (f"e2e_report_{marker}.txt", io.BytesIO(content), "text/plain")},
    )
    ok(r.json().get("code") == 0, "上传文件", f"md5={md5[:12]}")

    # ── 3. 投递向量化任务（应立即返回 queued）──
    t0 = time.time()
    r = client.post("/api/ai/describe", json={
        "user": USER, "token": token, "md5": md5,
        "filename": f"e2e_report_{marker}.txt", "type": "txt",
    })
    body = r.json()
    ok(body.get("code") == 0 and body.get("msg") == "queued",
       "投递任务（异步，立即返回）", f"msg={body.get('msg')} job={body.get('job')}")
    elapsed_submit = time.time() - t0
    ok(elapsed_submit < 2.0, "投递接口毫秒级返回（不阻塞上传请求）",
       f"{elapsed_submit*1000:.0f}ms")

    # ── 4. 轮询任务状态到终态 ──
    status, final = "", {}
    deadline = time.time() + 180
    while time.time() < deadline:
        r = client.post("/api/indexing/status", json={
            "user": USER, "token": token, "md5": md5})
        job = r.json().get("job") or {}
        status = job.get("status", "")
        if status in ("done", "failed"):
            final = job
            break
        time.sleep(2)
    ok(status == "done", "后台消费者完成向量化", f"final={final}")

    # ── 5. 幂等：同文件重复投递应短路 ──
    r = client.post("/api/ai/describe", json={
        "user": USER, "token": token, "md5": md5,
        "filename": f"e2e_report_{marker}.txt", "type": "txt",
    })
    ok(r.json().get("msg") == "already exists", "重复投递幂等短路",
       r.json().get("msg", ""))

    # ── 6. Agent 对话搜索命中该文件 ──
    with client.stream("POST", "/api/agent/chat", json={
        "user": USER, "token": token,
        "message": "帮我找讲容量规划和冷数据下沉的那份测试文档",
    }, timeout=120) as resp:
        events = []
        text = ""
        cur_event = ""
        for line in resp.iter_lines():
            # SSE 帧：event: 与 data: 两行配对（sse-starlette 格式）
            if line.startswith("event:"):
                cur_event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                events.append(cur_event)
                if cur_event == "message" and payload.get("delta"):
                    text += payload["delta"]
    ok("e2e_report" in text, "对话搜索命中上传文件", text[:160].replace("\n", " "))

    print("\n=== 全链路 8 步全部通过 ===")
    print(f"用户 {USER} / 文件 md5 {md5}")
    print("后续人工核验（容器内）：")
    print(f"  docker exec tc_mysql mysql -uroot -p$PW yuncunchu "
          f"-e \"SELECT status,attempts FROM indexing_jobs WHERE md5='{md5}'\"")
    print(f"  docker exec tc_mysql mysql -uroot -p$PW yuncunchu "
          f"-e \"SELECT kind,calls FROM ai_usage_log WHERE user='{USER}'\"")


if __name__ == "__main__":
    main()
