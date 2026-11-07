import { API_CONFIG } from '../config';

const AI_ENDPOINT = `${API_CONFIG.BASE_URL}/api/ai`;
const INDEXING_ENDPOINT = `${API_CONFIG.BASE_URL}/api/indexing`;

/**
 * 投递一个「AI 描述 + 向量化」任务（异步执行：后端只写任务行立即返回，
 * 重活由后台消费者处理，上传高峰不再阻塞请求）。
 * - 默认 force=false：幂等（已有索引/在途任务直接复用），上传后自动触发用
 * - force=true：手动重建（清旧切片全量重做），"建立内容索引"按钮用
 * 注：文件 md5 由上传流程计算并回传；AI 密钥由服务端统一配置，前端不携带
 */
export const describeFileByMd5 = async (md5, filename, type, user, { force = false } = {}) => {
  const body = {
    user: user.username,
    token: user.token,
    md5: md5,
    filename: filename,
    type: type,
    force: force,
  };

  const response = await fetch(`${AI_ENDPOINT}/describe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  const data = await response.json();
  if (data.code === 4) {
    const err = new Error('token expired');
    err.tokenExpired = true;
    throw err;
  }
  if (data.code !== 0) {
    throw new Error(data.msg || '任务投递失败');
  }
  return data;
};

/**
 * 查询向量化任务状态（pending 排队中 / running 处理中 / done / failed）
 */
export const getIndexingStatus = async (md5, user) => {
  const response = await fetch(`${INDEXING_ENDPOINT}/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user: user.username, token: user.token, md5 }),
  });
  const data = await response.json();
  if (data.code === 4) {
    const err = new Error('token expired');
    err.tokenExpired = true;
    throw err;
  }
  // code=1（无任务记录）视为已结束，交由调用方按 done 处理
  return data.code === 0 ? data.job : { status: 'done', error: '' };
};

/**
 * 轮询等待任务终态（手动重建场景的进度反馈）。
 * 返回最终 job；超时（默认 15 分钟）不视为失败——任务仍在后台执行。
 */
export const waitIndexingDone = async (md5, user, { intervalMs = 2000, timeoutMs = 15 * 60 * 1000 } = {}) => {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const job = await getIndexingStatus(md5, user);
    if (job.status === 'done' || job.status === 'failed') return job;
    if (Date.now() > deadline) {
      const err = new Error('索引仍在后台建立中，稍后可在此页面查看结果');
      err.stillRunning = true;
      throw err;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
};

// 注：语义搜索已并入 AI 助手对话（/api/agent/chat 的 search_files 工具），
// 独立的 /ai/search 端点与前端 aiSearch 已移除。
