import { API_CONFIG } from '../config';

const AI_ENDPOINT = `${API_CONFIG.BASE_URL}/api/ai`;
const API_KEY_STORAGE_PREFIX = 'dashscope_api_key_';

/**
 * 从浏览器本地读取 API Key
 */
export const fetchApiKey = async (user) => {
  if (!user || !user.username) return '';
  return localStorage.getItem(API_KEY_STORAGE_PREFIX + user.username) || '';
};

/**
 * 保存 API Key 到浏览器本地
 */
export const saveApiKey = async (key, user) => {
  if (!user || !user.username) {
    throw new Error('用户信息无效');
  }
  const storageKey = API_KEY_STORAGE_PREFIX + user.username;
  if (key) {
    localStorage.setItem(storageKey, key);
  } else {
    localStorage.removeItem(storageKey);
  }
  return { code: 0, msg: 'ok' };
};

/**
 * 对已有文件重新生成 AI 描述（通过 md5）
 * 注：文件 md5 由上传流程（images.js）计算并回传，此处不再重复计算
 */
export const describeFileByMd5 = async (md5, filename, type, user, apiKey, skipRebuild = false) => {
  const body = {
    user: user.username,
    token: user.token,
    md5: md5,
    filename: filename,
    type: type,
    force: true
  };
  if (skipRebuild) body.skip_rebuild = true;
  if (apiKey) body.api_key = apiKey;

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

/**
 * AI 语义搜索
 */
export const aiSearch = async (query, user, apiKey) => {
  const body = {
    user: user.username,
    token: user.token,
    query: query
  };
  if (apiKey) body.api_key = apiKey;

  const response = await fetch(`${AI_ENDPOINT}/search`, {
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
    throw new Error(data.msg || '搜索失败');
  }
  return data;
};

/**
 * 重建 FAISS 索引
 */
export const rebuildIndex = async (user) => {
  const response = await fetch(`${AI_ENDPOINT}/rebuild`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user: user.username,
      token: user.token
    })
  });

  const data = await response.json();
  if (data.code !== 0) {
    throw new Error(data.msg || '重建索引失败');
  }
  return data;
};
