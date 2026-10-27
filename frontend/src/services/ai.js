import { API_CONFIG } from '../config';

const AI_ENDPOINT = `${API_CONFIG.BASE_URL}/api/ai`;

/**
 * 对已有文件生成/重建 AI 描述（通过 md5）
 * 注1：文件 md5 由上传流程（images.js）计算并回传，此处不再重复计算
 * 注2：AI 密钥由服务端统一配置，前端不再携带
 */
export const describeFileByMd5 = async (md5, filename, type, user) => {
  const body = {
    user: user.username,
    token: user.token,
    md5: md5,
    filename: filename,
    type: type,
    force: true
  };

  const response = await fetch(`${AI_ENDPOINT}/describe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });

  const data = await response.json();
  if (data.code === 4) {
    const err = new Error('token expired');
    err.tokenExpired = true;
    throw err;
  }
  if (data.code !== 0) {
    throw new Error(data.msg || '生成描述失败');
  }
  return data;
};

// 注：语义搜索已并入 AI 助手对话（/api/agent/chat 的 search_files 工具），
// 独立的 /ai/search 端点与前端 aiSearch 已移除。
