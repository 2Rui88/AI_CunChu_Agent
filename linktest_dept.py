"""
部门权限隔离 · 真实链路测试（对运行中的 nginx(https://localhost) 发请求）

前置：docker compose 已启动、migration_dept.sql 已执行。
账户规划：
  部门2 研发部：dev_boss(supervisor) / dev_alice(member) / dev_bob(member)
  部门3 市场部：mkt_carol(member)

场景：
  1  注册 4 账户 + SQL 建部门/分配角色
  2  登录返回 role/department（主管与成员）
  3  alice 上传文件（user_file_list 落部门）
  4  alice 分享 → 广场可见（含 size 字段 = 修复验证）
  5  同部门 bob 广场可见 / 跨部门 carol 不可见（隔离）
  6  carol 转存外部门共享 → 拒绝；bob 转存成功
  7  bob 删 alice 文件 → code=6 拒绝（无权）
  8  carol 冒充调用 /api/dept/files → code=6（非主管）
  9  boss /api/dept/files 可见 alice 文件（含昵称）
  10 boss 删 alice 文件 → code=0；alice 列表与广场同步消失
  11 广场/排行 Redis key 部门化（FILE_PUBLIC_ZSET:2）
  12 主管跨部门删除 → code=6
"""
import json
import ssl
import subprocess
import sys
import urllib.request
import urllib.error
import uuid

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台 GBK 兜底

BASE = "https://localhost"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def call(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"http": e.code}


def upload_file(user, token, content: bytes, filename: str):
    """multipart 直传 /api/upload（手拼 body，无第三方依赖）"""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in [("user", user), ("token", token), ("md5", "")]:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(BASE + "/api/upload", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, context=CTX, timeout=60) as resp:
        return json.loads(resp.read())


def mysql(sql):
    """SQL 走 UTF-8 文件重定向 + SET NAMES（防 mysql 客户端 latin1 解释中文）"""
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write("SET NAMES utf8mb4;\n" + sql)
        path = f.name
    try:
        with open(path, "rb") as f:
            subprocess.run(
                ["docker", "exec", "-i", "tc_mysql", "sh", "-c",
                 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" yuncunchu'],
                stdin=f, capture_output=True, check=True)
    finally:
        os.unlink(path)


# ═══ 1. 注册 + 建部门 + 分配角色 ═══
# 幂等：已注册过（code=2/6）视为通过；先清掉本机 IP 的注册限流键（测试机自用）
subprocess.run(["docker", "exec", "tc_redis", "sh", "-c",
                "redis-cli --scan --pattern 'rl:reg:*' | xargs -r redis-cli del"], capture_output=True)
for u, n in [("dev_boss", "研发主管B"), ("dev_alice", "研发爱丽A"), ("dev_bob", "研发小博B"), ("mkt_carol", "市场卡罗C")]:
    r = call("/api/reg", {"username": u, "nick_name": n, "password": "Test123456"})
    check(f"1. 注册 {u}", r.get("code") in (0, 2, 6), str(r))

mysql("INSERT INTO departments (id, name) VALUES (2, '研发部'), (3, '市场部') ON DUPLICATE KEY UPDATE name=VALUES(name);")
mysql("UPDATE user_info SET department_id=2 WHERE user_name IN ('dev_boss','dev_alice','dev_bob');")
mysql("UPDATE user_info SET department_id=3 WHERE user_name='mkt_carol';")
mysql("UPDATE user_info SET role='supervisor' WHERE user_name='dev_boss';")

# ═══ 2. 登录返回角色/部门 ═══
tk = {}
for u in ["dev_boss", "dev_alice", "dev_bob", "mkt_carol"]:
    r = call("/api/login", {"user": u, "pwd": "Test123456"})
    tk[u] = r.get("token", "")
    check(f"2. {u} 登录", r.get("code") == 0 and tk[u], str(r))

r_boss = call("/api/login", {"user": "dev_boss", "pwd": "Test123456"})
tk["dev_boss"] = r_boss.get("token", tk["dev_boss"])  # 二次登录会刷新 token，同步持有最新值
check("2. 主管登录返回 role=supervisor + 部门名=研发部",
      r_boss.get("role") == "supervisor" and r_boss.get("department") == "研发部", str(r_boss))
r_carol = call("/api/login", {"user": "mkt_carol", "pwd": "Test123456"})
tk["mkt_carol"] = r_carol.get("token", tk["mkt_carol"])
check("2. 成员登录返回 role=member + 部门名=市场部",
      r_carol.get("role") == "member" and r_carol.get("department") == "市场部", str(r_carol))


def auth(u):
    return {"user": u, "token": tk[u]}


# ═══ 3. alice 上传 ═══
FILENAME = f"合同草稿A_{uuid.uuid4().hex[:6]}.txt"  # 每轮唯一文件名，避免跨轮重复行干扰断言
content = f"研发部合同草稿 {uuid.uuid4().hex}".encode() * 20
r = upload_file("dev_alice", tk["dev_alice"], content, FILENAME)
check("3. alice 上传文件", r.get("code") == 0, str(r))
r = call("/api/myfiles", auth("dev_alice") | {"cmd": "normal", "start": 0, "count": 50})
mf = [f for f in r.get("files", []) if f["file_name"] == FILENAME]
check("3. alice 文件列表含新文件", len(mf) == 1, str(r)[:200])
FILE = {"md5": mf[0]["md5"], "filename": FILENAME} if mf else None

# ═══ 4. alice 分享；广场含 size（修复验证） ═══
r = call("/api/dealfile?cmd=share", auth("dev_alice") | {"md5": FILE["md5"], "filename": FILE["filename"]})
check("4. alice 分享文件", r.get("code") in (0, 3), str(r))  # 3=重复执行时已分享
r = call("/api/sharefiles?cmd=normal", auth("dev_bob") | {"cmd": "normal"})
shared_bob = [f for f in r.get("files", []) if f["md5"] == FILE["md5"]]
check("4. 广场条目含 size/type（大小显示修复）",
      bool(shared_bob) and shared_bob[0].get("size", 0) > 0 and shared_bob[0].get("type") == "txt",
      str(shared_bob)[:200])

# ═══ 5. 部门隔离：bob 可见 / carol 不可见 ═══
check("5. 同部门 bob 在广场看到 alice 的文件", bool(shared_bob))
r = call("/api/sharefiles?cmd=normal", auth("mkt_carol") | {"cmd": "normal"})
carol_sees = [f for f in r.get("files", []) if f["md5"] == FILE["md5"]]
check("5. 跨部门 carol 看不到该文件（隔离）", r.get("code") == 0 and not carol_sees, str(r)[:200])
r = call("/api/sharefiles?cmd=normal", {"cmd": "normal"})
check("5. 未登录访问广场被拒（原先是匿名可看全站）", r.get("code") == 4, str(r))

# ═══ 6. 转存：carol 拒绝 / bob 成功 ═══
r = call("/api/dealsharefile?cmd=save", auth("mkt_carol") | FILE)
check("6. carol 转存外部门共享被拒", r.get("code") == 1, str(r))
r = call("/api/dealsharefile?cmd=save", auth("dev_bob") | FILE)
check("6. bob 转存同部门共享成功", r.get("code") == 0, str(r))

# ═══ 7. bob 删 alice 文件 → 拒绝 ═══
r = call("/api/dealfile?cmd=del", auth("dev_bob") | FILE | {"owner": "dev_alice"})
check("7. 成员 bob 删 alice 文件被拒(code=6)", r.get("code") == 6, str(r))

# ═══ 8. carol 访问部门文件接口 → 拒绝 ═══
r = call("/api/dept/files", auth("mkt_carol"))
check("8. 非主管调用 /api/dept/files 被拒(code=6)", r.get("code") == 6, str(r))

# ═══ 9. boss 部门文件视图 ═══
r = call("/api/dept/files", auth("dev_boss"))
boss_files = [f for f in r.get("files", []) if f["md5"] == FILE["md5"]]
check("9. 主管看到 alice 的文件（含昵称）",
      bool(boss_files) and boss_files[0].get("nick_name") == "研发爱丽A", str(r)[:300])

# ═══ 10. boss 删 alice 文件 ═══
r = call("/api/dealfile?cmd=del", auth("dev_boss") | FILE | {"owner": "dev_alice"})
check("10. 主管删成员文件成功", r.get("code") == 0, str(r))
r = call("/api/myfiles", auth("dev_alice") | {"cmd": "normal", "start": 0, "count": 50})
check("10. alice 列表中文件已消失",
      not [f for f in r.get("files", []) if f["md5"] == FILE["md5"]], str(r)[:200])
r = call("/api/sharefiles?cmd=normal", auth("dev_bob") | {"cmd": "normal"})
check("10. 广场中该共享已消失",
      not [f for f in r.get("files", []) if f["md5"] == FILE["md5"]], str(r)[:200])

# ═══ 11. Redis 部门化 key ═══
out = subprocess.run(["docker", "exec", "tc_redis", "redis-cli", "--scan", "--pattern", "FILE_PUBLIC*"],
                     capture_output=True, text=True).stdout
check("11. Redis 出现部门化 ZSET key（FILE_PUBLIC_ZSET:2 等）", "FILE_PUBLIC_ZSET:2" in out, out.strip()[:200])

# ═══ 12. 主管跨部门删除 → 拒绝 ═══
FILENAME2 = f"市场素材C_{uuid.uuid4().hex[:6]}.txt"
content2 = f"市场部素材 {uuid.uuid4().hex}".encode() * 10
r = upload_file("mkt_carol", tk["mkt_carol"], content2, FILENAME2)
check("12. carol 上传文件", r.get("code") == 0, str(r))
r = call("/api/myfiles", auth("mkt_carol") | {"cmd": "normal", "start": 0, "count": 50})
mf2 = [f for f in r.get("files", []) if f["file_name"] == FILENAME2]
FILE2 = {"md5": mf2[0]["md5"], "filename": FILENAME2} if mf2 else None
r = call("/api/dealfile?cmd=del", auth("dev_boss") | FILE2 | {"owner": "mkt_carol"})
check("12. 研发主管删市场部成员文件被拒(code=6)", r.get("code") == 6, str(r))

print(f"\n===== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 =====")
if FAIL:
    print("失败项：", *FAIL, sep="\n  - ")
    raise SystemExit(1)
