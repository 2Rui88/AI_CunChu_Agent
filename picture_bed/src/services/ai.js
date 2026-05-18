import { API_CONFIG } from '../config';
import SparkMD5 from 'spark-md5';

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

// 计算文件 MD5（与 images.js 中一致）
const calculateMD5 = (file) => {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const spark = new SparkMD5.ArrayBuffer();
      spark.append(e.target.result);
      resolve(spark.end());
    };
    reader.onerror = reject;
    reader.readAsArrayBuffer(file);
  });
};

/**
 * 上传后异步调用 AI 生成文件描述 + 向量
 * 失败不影响上传流程
 */
export const describeFile = async (file, user, apiKey) => {
  try {
    const md5 = await calculateMD5(file);
    const ext = file.name.split('.').pop() || '';

    const body = {
      user: user.username,
      token: user.token,
      md5: md5,
      filename: file.name,
      type: ext.toLowerCase()
    };
    const key = apiKey || localStorage.getItem('dashscope_api_key_' + user.username) || '';
    if (key) body.api_key = key;

    const response = await fetch(`${AI_ENDPOINT}?cmd=describe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    const data = await response.json();
    if (data.code === 0) {
      console.log('AI describe success:', file.name);
    } else {
      console.warn('AI describe failed:', data.msg);
    }
    return data;
  } catch (error) {
    console.warn('AI describe error (non-blocking):', error);
    return null;
  }
};

/**
 * 对已有文件重新生成 AI 描述（通过 md5）
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

  const response = await fetch(`${AI_ENDPOINT}?cmd=describe`, {
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

  const response = await fetch(`${AI_ENDPOINT}?cmd=search`, {
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
  const response = await fetch(`${AI_ENDPOINT}?cmd=rebuild`, {
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
